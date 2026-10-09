"""IR → `.pptx`: the emitter.

The shape of this module is the shape of the job. `emit` opens the customer's master, empties it of
slides, and for each IR adds one slide on that IR's own layout and walks the elements in paint
order. `shapes.py` turns geometry into native objects, `text.py` turns measured lines into text that
still reflows if you edit it, `template.py` owns the master's placeholders and the promise that the
template comes out untouched. Charts go to `emit/charts.py` (WP3a).

Two whole-package concerns live here because nothing smaller can own them:

* **Paint order.** Elements are emitted back to front; a placeholder that gets filled is moved to
  its element's place in the shape tree (python-pptx clones placeholders first, which would
  otherwise put the title behind the background). Groups are created when their first member
  appears, and a chart's `overlay` elements are emitted straight after it whatever their `z`.
* **Determinism.** `finalize` rewrites the saved package: every zip entry stamped 1980-01-01,
  `dcterms:created/modified` fixed in `docProps/core.xml` *and* in every embedded chart workbook
  (XlsxWriter stamps the current time), PNG metadata chunks stripped from images that are ours, and
  the master's own bytes restored for `ppt/slideMasters/**`, `ppt/slideLayouts/**` and
  `ppt/theme/**` — python-pptx re-serialises those parts, so "we changed nothing" has to be made
  true at the byte level, not just meant.
"""
from __future__ import annotations

import os
import re
import zipfile
from collections.abc import Iterable
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from typing import Any

from pptx import Presentation

from app.engine.emit import shapes as shape_emitter
from app.engine.emit import template as template_module
from app.engine.emit import text as text_engine
from app.engine.emit.charts import add_chart
from app.engine.emit.template import EmitContext
from app.engine.ir import IR, Element
from app.engine.manifest import Layout, Manifest, ManifestError
from app.engine.reports import EmitOptions, EmitReport

_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_P14_NS = "http://schemas.microsoft.com/office/powerpoint/2010/main"

#: The zip epoch. Every entry gets this timestamp so two exports of the same IRs are byte-identical.
FIXED_ZIP_DATE: tuple[int, int, int, int, int, int] = (1980, 1, 1, 0, 0, 0)
FIXED_TIMESTAMP = "1980-01-01T00:00:00Z"

#: PNG chunks that carry a timestamp, a comment or an EXIF block — everything else is picture.
_PNG_METADATA_CHUNKS = {b"tEXt", b"zTXt", b"iTXt", b"tIME", b"eXIf"}


def open_master(master_path: Path) -> Any:
    """Open the customer's master. Nothing here may alter masters, layouts or themes."""
    return Presentation(str(master_path))


def strip_slides(prs: Any) -> int:
    """Delete every slide, and the bookkeeping that still points at them.

    Sections and custom shows reference slides; leaving a stale reference behind is a PowerPoint
    repair prompt, which silently drops shapes and makes every downstream count wrong.
    """
    slide_id_list = prs.slides._sldIdLst
    removed = 0
    for entry in list(slide_id_list):
        rid = entry.get(f"{{{_R_NS}}}id")
        prs.part.drop_rel(rid)
        slide_id_list.remove(entry)
        removed += 1

    root = prs.part._element
    for ext in root.findall(f".//{{{_P_NS}}}ext"):
        if ext.find(f"{{{_P14_NS}}}sectionLst") is not None:
            ext.getparent().remove(ext)
    for custom_shows in root.findall(f"{{{_P_NS}}}custShowLst"):
        root.remove(custom_shows)
    return removed


def resolve_layout(prs: Any, manifest: Manifest, layout: Layout) -> Any:
    """The python-pptx layout for a manifest layout — by position, verified by part name.

    `(masterIndex, layoutIndex)` are positions in the id lists, which is python-pptx's own order, so
    the lookup is direct. The assertion is the valuable part: five client layouts are called
    "Title only", so if the manifest and the file ever drift, this raises instead of quietly
    putting content on the dark cover.
    """
    try:
        master = prs.slide_masters[layout.masterIndex]
        resolved = master.slide_layouts[layout.layoutIndex]
    except IndexError as error:
        raise ManifestError(
            f"{layout.id}: the master has no layout at (master {layout.masterIndex}, "
            f"index {layout.layoutIndex})"
        ) from error

    actual_part = str(resolved.part.partname)
    if layout.partName and actual_part != layout.partName:
        raise ManifestError(
            f"{layout.id}: manifest says {layout.partName} but (master {layout.masterIndex}, "
            f"index {layout.layoutIndex}) is {actual_part} — the manifest does not match this master"
        )
    if not layout.partName and resolved.name != layout.name:
        raise ManifestError(
            f"{layout.id}: legacy manifest names it {layout.name!r} but the master calls it "
            f"{resolved.name!r} at that position"
        )
    return resolved


# ------------------------------------------------------------------------------------------ emit


def emit(
    irs: list[IR],
    manifest: Manifest,
    master_path: Path,
    out_path: Path,
    options: EmitOptions | None = None,
) -> EmitReport:
    """Write `out_path` from these IRs on this master, and report what became of every element."""
    options = options or EmitOptions()
    report = EmitReport()
    master_path = Path(master_path)
    prs = open_master(master_path)
    strip_slides(prs)

    for index, ir in enumerate(irs, start=1):
        layout = manifest.layout(ir.slide.layoutId) if ir.slide.layoutId else manifest.layouts[0]
        slide = prs.slides.add_slide(resolve_layout(prs, manifest, layout))
        context = EmitContext(
            manifest=manifest,
            layout=layout,
            slide_id=ir.slide.id,
            slide_index=index,
            canvas=ir.canvas,
            options=options,
            report=report,
            theme_fonts=dict((manifest.theme_of(layout) or {}).get("fonts") or {}),
        )
        _emit_slide(slide, ir, context)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))
    finalize(out_path, master_path=master_path, determinism=options.finalize)

    for problem in template_module.verify_template(out_path, master_path):
        report.warnings.append(f"template: {problem}")
    return report


def _emit_slide(slide: Any, ir: IR, ctx: EmitContext) -> None:
    """One IR onto one slide: placeholders first-class, groups nested, paint order preserved."""
    groups = {group.id: group for group in ir.groups}
    containers: dict[str | None, Any] = {None: slide.shapes}
    used_placeholders: list[Any] = []

    for element in _emission_order(ir):
        container = _container_for(element.group, groups, containers, slide, ctx)
        try:
            _emit_element(slide, container, element, ctx, used_placeholders)
        except Exception as error:                        # one bad element must not lose the deck
            ctx.warn(element.id, f"{element.kind} failed to emit: {type(error).__name__}: {error}")

    removed = template_module.prune_placeholders(slide, used_placeholders)
    if removed:
        ctx.report.warnings.append(
            f"{ctx.slide_id}: removed {len(removed)} unused placeholder(s): {', '.join(sorted(removed))}"
        )
    empty = _prune_empty_groups(slide)
    if empty:
        ctx.report.warnings.append(
            f"{ctx.slide_id}: removed {len(empty)} empty group(s): {', '.join(empty)}"
        )
    template_module.set_notes(slide, ir.slide.notes)


def _prune_empty_groups(slide: Any) -> list[str]:
    """Delete groups that ended up with no children, innermost first.

    A group is created when its first member is emitted, so one can be left empty after the fact —
    a chart, for instance, cannot be emitted inside a group and is moved onto the slide, and if that
    chart was the group's only member the `grpSp` remains with nothing in it. An empty group is
    invisible but real: it shows up in the selection pane, it counts as a shape, and the fit
    predictor rightly reports a 0×0 shape. Nothing downstream wants it.
    """
    removed: list[str] = []

    def sweep(container: Any) -> None:
        for shape in list(container):
            if shape.shape_type is None or not str(shape.shape_type).startswith("GROUP"):
                continue
            sweep(shape.shapes)                       # innermost first, so a group of empty groups goes too
            if len(shape.shapes) == 0:
                removed.append(shape.name)
                shape._element.getparent().remove(shape._element)

    sweep(slide.shapes)
    return removed


def _emission_order(ir: IR) -> list[Element]:
    """Paint order, with each chart's `overlay` elements pulled to just after the chart."""
    elements = ir.paint_sorted()
    owner: dict[str, str] = {}
    for element in elements:
        if element.kind == "chart" and element.overlay and element.id:
            for overlay_id in element.overlay:
                owner[str(overlay_id)] = element.id

    deferred: dict[str, list[Element]] = {}
    for element in elements:
        if element.id in owner:
            deferred.setdefault(owner[element.id], []).append(element)

    ordered: list[Element] = []
    for element in elements:
        if element.id in owner:
            continue
        ordered.append(element)
        if element.kind == "chart" and element.id:
            ordered.extend(deferred.get(element.id, []))
    return ordered


def _container_for(group_id: str | None, groups: dict[str, Any], containers: dict[str | None, Any],
                   slide: Any, ctx: EmitContext) -> Any:
    """The shape collection an element belongs in, creating the group (and its parents) on demand."""
    if group_id in containers:
        return containers[group_id]
    if not ctx.options.group_svg or group_id not in groups:
        containers[group_id] = slide.shapes
        return slide.shapes
    group = groups[group_id]
    parent = _container_for(group.parent, groups, containers, slide, ctx)
    shape = parent.add_group_shape()
    if ctx.options.name_shapes:
        shape.name = str(group.name or group_id)
    ctx.count("group")
    containers[group_id] = shape.shapes
    return shape.shapes


def _emit_element(slide: Any, container: Any, element: Element, ctx: EmitContext,
                  used_placeholders: list[Any]) -> None:
    kind = element.kind
    if kind == "shape":
        if shape_emitter.add_shape(container, element, ctx) is not None:
            ctx.count("shape")
    elif kind == "text":
        _emit_text(slide, container, element, ctx, used_placeholders)
    elif kind == "image":
        if shape_emitter.add_image(container, element, ctx) is not None:
            ctx.count("image")
    elif kind == "raster":
        if shape_emitter.add_image(container, element, ctx) is not None:
            ctx.count("raster")
            ctx.report.rasters.append({"id": element.id, "reason": element.reason})
    elif kind == "table":
        if container is not slide.shapes:
            ctx.warn(element.id, "a table cannot live inside a group — emitted on the slide")
        if shape_emitter.add_table(slide, element, ctx) is not None:
            ctx.count("table")
    elif kind == "chart":
        if container is not slide.shapes:
            ctx.warn(element.id, "a chart cannot live inside a group — emitted on the slide")
        if _emit_chart(slide, element, ctx) is not None:
            ctx.count("chart")
    else:
        ctx.warn(element.id, f"unknown element kind {kind!r} — nothing emitted")


def _emit_text(slide: Any, container: Any, element: Element, ctx: EmitContext,
               used_placeholders: list[Any]) -> None:
    """Text into the layout's own placeholder when the classifier mapped one, else a free text box."""
    target = None
    if element.placeholder:
        target, problem = template_module.find_placeholder(slide, element.placeholder)
        if problem:
            ctx.warn(element.id, f"placeholder {element.placeholder}: {problem} — emitted as a text box")
        elif container is not slide.shapes:
            ctx.warn(element.id, "a placeholder cannot live inside a group — emitted on the slide")
            target = None

    if target is not None:
        template_module.prepare_placeholder_frame(target, element.anchor)
        shape = text_engine.add_text(slide.shapes, element, ctx, shape=target)
        if shape is None:
            return
        used_placeholders.append(target)
        # python-pptx clones placeholders before anything else, so without this the title would sit
        # behind every shape the design draws under it.
        tree = target._element.getparent()
        tree.remove(target._element)
        tree.append(target._element)
        shape_emitter.name_shape(target, element, ctx)
        ctx.count("placeholder")
        ctx.report.placeholders_used.append({
            "slide": ctx.slide_index,
            "type": template_module.placeholder_type(target),
            "idx": template_module.placeholder_idx(target),
        })
        return

    shape = text_engine.add_text(container, element, ctx)
    if shape is not None:
        shape_emitter.name_shape(shape, element, ctx)
        ctx.count("text")


def _emit_chart(slide: Any, element: Element, ctx: EmitContext) -> Any | None:
    """A native chart part with its own workbook. The design's plot rect drives `c:manualLayout`.

    `engine.emit.charts` is StageFlow's module and takes a *slide*, which is also the only place a
    chart can go: `GraphicFrame`s are not allowed inside a `grpSp`.

    A spec the chart model cannot draw (an unknown or Path-A-unsupported type, no series) or a
    chart whose styling failed comes back as a labelled placeholder rectangle rather than a chart
    (WP-C §4.1). It is counted as a `shape`, and its `report.charts` entry says `failed` with the
    `error`, which `coverage()` copies into the charts row. Only a real chart part returns here.
    """
    spec = deepcopy(element.spec or {})
    if not spec:
        ctx.warn(element.id, "chart without a spec — nothing emitted")
        return None
    box = element.box
    if element.plotRect is not None and box.w > 0 and box.h > 0 and isinstance(spec, dict):
        options = spec.setdefault("options", {})
        if isinstance(options, dict):
            options.setdefault("plotArea", {
                "x": round((element.plotRect.x - box.x) / box.w, 4),
                "y": round((element.plotRect.y - box.y) / box.h, 4),
                "w": round(element.plotRect.w / box.w, 4),
                "h": round(element.plotRect.h / box.h, 4),
            })
    options = spec.get("options") if isinstance(spec, dict) else None
    plot_area = options.get("plotArea") if isinstance(options, dict) else None
    source = element.source or {}
    label = element.name or source.get("svg") or source.get("path") or element.id
    diagnostics: list[dict[str, str]] = []
    style = dict(element.style or {})
    style["diagnostics"] = diagnostics
    # The frame is named by `add_chart` (made XML-safe there: C1 review #13), not by `name_shape`,
    # whose setter would raise on a control character in a `data-name`.
    style["name"] = str(label)[:255] if (label and ctx.options.name_shapes) else None
    shape = add_chart(
        slide,
        spec,
        (template_module.inches(box.x), template_module.inches(box.y),
         template_module.inches(box.w), template_module.inches(box.h)),
        style,
    )
    entry: dict[str, Any] = {"id": element.id, "slide": ctx.slide_id, "origin": element.origin,
                             "type": spec.get("type") if isinstance(spec, dict) else None}
    for diagnostic in diagnostics:
        if diagnostic["level"] in ("warn", "error"):
            ctx.warn(element.id, f"chart: {diagnostic['message']}")
    notes = [d["message"] for d in diagnostics if d["level"] == "info"]
    if notes:
        entry["notes"] = notes
    if not getattr(shape, "has_chart", False):
        errors = [d["message"] for d in diagnostics if d["level"] == "error"]
        entry.update(failed=True, error=errors[0] if errors else "chart not built")
        ctx.report.charts.append(entry)
        ctx.count("shape")
        return None
    # `plotArea` is reported as well as passed on: turning it into `c:manualLayout` is the chart
    # module's half of the job, and the report is where the two halves can be checked.
    entry["plotArea"] = plot_area
    ctx.report.charts.append(entry)
    return shape


# ---------------------------------------------------------------------------------- determinism


def finalize(pptx_path: Path, *, master_path: Path | None = None, determinism: bool = True) -> Path:
    """Rewrite the saved package so it is reproducible and the template is untouched.

    Both jobs are one pass over the zip because both are "write these bytes, with this timestamp":

    * the master's own bytes go back for every template part (python-pptx re-serialises them),
    * `dcterms:created/modified` is fixed in `docProps/core.xml` and in every embedded workbook,
    * PNG metadata is stripped from images that are ours (never from the master's own media),
    * every entry is stamped 1980-01-01 and written in a fixed order.
    """
    path = Path(pptx_path)
    restore = template_module.master_template_bytes(Path(master_path)) if master_path else {}

    with zipfile.ZipFile(path) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]

    rewritten: list[tuple[str, bytes]] = []
    for name, data in entries:
        if name in restore:
            data = restore[name]
        elif determinism:
            if name == "docProps/core.xml":
                data = _fix_timestamps(data)
            elif name.startswith("ppt/embeddings/") and name.endswith(".xlsx"):
                data = _fix_embedded_workbook(data)
            elif name.lower().endswith(".png") and name not in restore:
                data = strip_png_metadata(data)
        rewritten.append((name, data))

    temporary = path.with_suffix(path.suffix + ".tmp")
    _write_zip(temporary, rewritten, fixed_dates=determinism)
    os.replace(temporary, path)
    return path


def _write_zip(path: Path, entries: Iterable[tuple[str, bytes]], *, fixed_dates: bool) -> None:
    """`[Content_Types].xml` first, then every part by name — an order that cannot drift."""
    ordered = sorted(entries, key=lambda item: (item[0] != "[Content_Types].xml", item[0]))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in ordered:
            info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_DATE if fixed_dates else (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0o600 << 16
            archive.writestr(info, data)


def _fix_timestamps(core_xml: bytes) -> bytes:
    """`dcterms:created` / `dcterms:modified` pinned — otherwise every save differs by its clock."""
    for tag in (b"dcterms:created", b"dcterms:modified"):
        core_xml = re.sub(
            rb"(<" + tag + rb"[^>]*>)[^<]*(</" + tag + rb">)",
            rb"\g<1>" + FIXED_TIMESTAMP.encode() + rb"\g<2>",
            core_xml,
        )
    return core_xml


def _fix_embedded_workbook(xlsx: bytes) -> bytes:
    """The chart's own workbook is a zip too, and XlsxWriter stamps it with the current time."""
    with zipfile.ZipFile(BytesIO(xlsx)) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries, key=lambda item: (item[0] != "[Content_Types].xml", item[0])):
            if name == "docProps/core.xml":
                data = _fix_timestamps(data)
            info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            archive.writestr(info, data)
    return buffer.getvalue()


def strip_png_metadata(data: bytes) -> bytes:
    """Drop `tEXt`/`zTXt`/`iTXt`/`tIME`/`eXIf` chunks. Pixels, gamma and transparency are kept."""
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        return data
    out = bytearray(signature)
    offset = len(signature)
    while offset + 8 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        chunk_type = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            return data                                   # truncated file: leave it exactly as it is
        if chunk_type not in _PNG_METADATA_CHUNKS:
            out += data[offset:end]
        offset = end
    return bytes(out)


def emit_summary(report: EmitReport) -> dict[str, Any]:
    """A compact dict for the CLI and the app's progress stream."""
    return {
        "shapes": dict(report.shapes_by_kind),
        "rasters": len(report.rasters),
        "charts": len(report.charts),
        "warnings": len(report.warnings),
    }
