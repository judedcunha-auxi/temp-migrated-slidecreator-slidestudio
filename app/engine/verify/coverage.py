"""What became native, whether the charts are real charts, and whether the template survived.

`coverage(irs, emit_report, renderer_warnings, baseline_warnings)` is the interface in master brief
§8 and still works exactly as written. Two checks it is asked to make need the file as well as the
IRs — the template comparison and the structural chart check — so `pptx=` and `master=` are
**optional keyword arguments**: given them, the report carries `template` and `charts`; without
them those stay empty and everything else is unchanged.

The renderer-warning subtraction is the reason `baseline_warnings` exists at all:

    ours = warnings(export) − warnings(baseline deck: the master with one empty slide per used layout)

An 18 MB brand master warns about its own fonts and its own OLE objects on every render. Reporting
those as "our" defects would make the acceptance row unreachable and, worse, hide a real one in the
noise. `baseline_deck` below builds that empty-slide deck; the suite renders it once per run.
"""
from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# re-serialises bytes defusedxml parsed (bandit B405)
from xml.etree.ElementTree import canonicalize  # nosec B405

from app.config import engine as config
from app.engine import chart_model
from app.engine.emit.charts import expected_series, expected_workbook
from app.engine.ir import IR, Box, Element
from app.engine.manifest import Manifest
from app.engine.reports import CoverageReport, EmitReport
from app.engine.verify.text_deck import text_report

_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

#: Package prefixes the export must not touch (master brief §9, "Template").
TEMPLATE_PREFIXES: tuple[str, ...] = ("ppt/slideMasters/", "ppt/slideLayouts/", "ppt/theme/")

#: How far the emitted plot rect may sit from the design's, in px (master brief §9, "Charts").
PLOT_RECT_TOLERANCE_PX: float = 4.0

#: Values must match the spec to this tolerance (master brief §9).
VALUE_TOLERANCE: float = 1e-6


# ------------------------------------------------------------------------------------- counting


def _counts_of(ir: IR) -> dict[str, Any]:
    """Per-slide native counts, with charts split by origin — the shape of the coverage table."""
    counts = {kind: 0 for kind in ("shape", "text", "image", "table", "chart", "raster")}
    for element in ir.elements:
        counts[element.kind] = counts.get(element.kind, 0) + 1
    charts = [e for e in ir.elements if e.kind == "chart"]
    return {
        "slide": ir.slide.id,
        **counts,
        "native": sum(1 for e in ir.elements if e.is_native),
        "chartsAuthored": sum(1 for e in charts if e.origin == "authored"),
        "chartsRecognised": sum(1 for e in charts if e.origin == "recognised"),
        "groups": len(ir.groups),
    }


def _unsupported(irs: Sequence[IR], emit_report: EmitReport | None) -> list[str]:
    """Every construct this export could not do natively, deduplicated and sorted."""
    out: set[str] = set()
    for ir in irs:
        for element in ir.elements:
            if element.kind == "raster" and element.reason:
                out.add(element.reason)
    if emit_report:
        out.update(emit_report.renderer_gaps)
        out.update(entry.get("reason", "") for entry in emit_report.rasters if entry.get("reason"))
    return sorted(w for w in out if w)


# ------------------------------------------------------------------------------ template checks


def _template_parts(archive: zipfile.ZipFile) -> set[str]:
    """The master/layout/theme parts plus everything they reference (media, OLE, tags)."""
    names = {n for n in archive.namelist() if n.startswith(TEMPLATE_PREFIXES)}
    referenced: set[str] = set()
    for name in list(names):
        if not name.endswith(".rels"):
            continue
        for _, _, target, mode in _relationships(archive, name):
            if mode == "External":
                continue
            resolved = _resolve(name, target)
            if resolved in archive.namelist():
                referenced.add(resolved)
    return names | {n for n in referenced if not n.startswith(TEMPLATE_PREFIXES)}


def _relationships(archive: zipfile.ZipFile, rels_name: str) -> list[tuple[str, str, str, str]]:
    from defusedxml.ElementTree import fromstring

    try:
        root = fromstring(archive.read(rels_name))
    except Exception:  # noqa: BLE001 — an unreadable rels part compares as empty, not as a crash
        return []
    return [
        (r.get("Id", ""), r.get("Type", ""), r.get("Target", ""), r.get("TargetMode", ""))
        for r in root.findall(f"{{{_REL_NS}}}Relationship")
    ]


def _resolve(rels_name: str, target: str) -> str:
    """`ppt/slideLayouts/_rels/slideLayout1.xml.rels` + `../media/x.png` → `ppt/media/x.png`."""
    if target.startswith("/"):
        return target.lstrip("/")
    base = rels_name.rsplit("/_rels/", 1)[0].split("/")
    for segment in target.split("/"):
        if segment == "..":
            base = base[:-1]
        elif segment not in (".", ""):
            base.append(segment)
    return "/".join(base)


def _same_xml(a: bytes, b: bytes) -> bool:
    """Canonical XML equality: python-pptx re-serialises every part it parses, so raw bytes lie."""
    try:
        return (canonicalize(xml_data=a.decode("utf-8"), strip_text=True)
                == canonicalize(xml_data=b.decode("utf-8"), strip_text=True))
    except Exception:  # noqa: BLE001 — not XML after all; fall back to bytes
        return a == b


def template_checks(
    pptx: Path, master: Path | None, irs: Sequence[IR], manifest: Manifest | None = None
) -> dict[str, Any]:
    """Did the export leave the customer's template alone, and are its placeholders sane?

    Byte equality is reported but is *not* the verdict: python-pptx re-serialises every XML part it
    opens, so a deck that changed nothing still differs byte for byte in every layout. The verdict
    is canonical-XML equality for XML parts, relationship-set equality for `.rels` (order differs
    after a round trip, and PptxRender's layout enumeration follows that order — `engine.renderer`
    verifies it separately), and raw bytes for media.
    """
    result: dict[str, Any] = {"ok": True, "checked": False}
    pptx = Path(pptx)
    if master is None or not Path(master).exists() or not pptx.exists():
        result["skipped"] = "no master to compare against" if master is None else "missing file"
        return result

    result["checked"] = True
    changed: list[str] = []
    missing: list[str] = []
    byte_identical = 0
    reordered: list[str] = []

    with zipfile.ZipFile(master) as original, zipfile.ZipFile(pptx) as produced:
        produced_names = set(produced.namelist())
        names = sorted(_template_parts(original))
        for name in names:
            if name not in produced_names:
                missing.append(name)
                continue
            before, after = original.read(name), produced.read(name)
            if before == after:
                byte_identical += 1
                continue
            if name.endswith(".rels"):
                a, b = _relationships(original, name), _relationships(produced, name)
                if set(a) == set(b):
                    if a != b:
                        reordered.append(name)
                    continue
                changed.append(name)
            elif name.endswith(".xml"):
                if not _same_xml(before, after):
                    changed.append(name)
            else:
                changed.append(name)

    result.update({
        "parts": len(names),
        "byteIdentical": byte_identical,
        "changed": changed,
        "missing": missing,
        "relsReordered": reordered,
    })
    result.update(_placeholder_checks(pptx, irs))
    result["ok"] = not changed and not missing and not result.get("placeholderProblems")
    return result


def _placeholder_checks(pptx: Path, irs: Sequence[IR]) -> dict[str, Any]:
    """A title placeholder holds text iff the IR mapped one, and no placeholder is left empty."""
    from pptx import Presentation

    problems: list[str] = []
    empty: list[str] = []
    titles: list[dict[str, Any]] = []
    presentation = Presentation(str(pptx))
    for index, slide in enumerate(presentation.slides, start=1):
        ir = irs[index - 1] if index - 1 < len(irs) else None
        mapped = bool(ir) and any(
            (element.placeholder or {}).get("type") in ("title", "ctrTitle")
            for element in ir.elements  # type: ignore[union-attr]
        )
        holds = False
        for shape in slide.shapes:
            if not shape.is_placeholder:
                continue
            kind = str(shape.placeholder_format.type or "")
            text = shape.text_frame.text.strip() if shape.has_text_frame else ""
            if "TITLE" in kind.upper():
                holds = holds or bool(text)
            if not text and shape.has_text_frame:
                empty.append(f"slide {index}: {shape.name!r} ({kind}) has an empty text frame")
        titles.append({"slide": index, "irMapped": mapped, "deckHolds": holds})
        if ir is not None and mapped != holds:
            problems.append(
                f"slide {index}: the IR {'mapped' if mapped else 'did not map'} a title placeholder "
                f"but the deck's title placeholder {'holds' if holds else 'holds no'} text"
            )
    return {
        "titles": titles,
        "emptyPlaceholders": empty,
        "placeholderProblems": problems + empty,
    }


# --------------------------------------------------------------------------------- chart checks


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cache_values(series_element: Any, tag: str) -> list[float | None]:
    """The `c:numCache` points of one series, in index order — what the renderer actually draws."""
    holder = series_element.find(f"{{{_C}}}{tag}")
    if holder is None:
        return []
    cache = holder.find(f"{{{_C}}}numRef/{{{_C}}}numCache")
    if cache is None:
        cache = holder.find(f"{{{_C}}}numLit")
    if cache is None:
        return []
    points: dict[int, float | None] = {}
    for point in cache.findall(f"{{{_C}}}pt"):
        points[int(point.get("idx", "0"))] = _number(point.findtext(f"{{{_C}}}v"))
    total = cache.find(f"{{{_C}}}ptCount")
    length = int(total.get("val", "0")) if total is not None else (max(points) + 1 if points else 0)
    return [points.get(i) for i in range(length)]


def _cache_categories(series_element: Any) -> list[str]:
    holder = series_element.find(f"{{{_C}}}cat")
    if holder is None:
        return []
    for path in (f"{{{_C}}}strRef/{{{_C}}}strCache", f"{{{_C}}}numRef/{{{_C}}}numCache",
                 f"{{{_C}}}strLit", f"{{{_C}}}numLit"):
        cache = holder.find(path)
        if cache is None:
            continue
        points: dict[int, str] = {}
        for point in cache.findall(f"{{{_C}}}pt"):
            points[int(point.get("idx", "0"))] = point.findtext(f"{{{_C}}}v") or ""
        total = cache.find(f"{{{_C}}}ptCount")
        length = int(total.get("val", "0")) if total is not None else (max(points) + 1 if points else 0)
        return [points.get(i, "") for i in range(length)]
    return []


def _manual_plot_rect(chart_space: Any, frame: Box) -> Box | None:
    """`c:plotArea/c:layout/c:manualLayout` in canvas px, or None when the layout is automatic."""
    manual = chart_space.find(
        f"{{{_C}}}chart/{{{_C}}}plotArea/{{{_C}}}layout/{{{_C}}}manualLayout"
    )
    if manual is None:
        return None
    values: dict[str, float] = {}
    for key, tag in (("x", "x"), ("y", "y"), ("w", "w"), ("h", "h")):
        node = manual.find(f"{{{_C}}}{tag}")
        if node is None or node.get("val") is None:
            return None
        values[key] = float(node.get("val"))
    return Box(
        frame.x + values["x"] * frame.w,
        frame.y + values["y"] * frame.h,
        values["w"] * frame.w,
        values["h"] * frame.h,
    )


def _workbook_columns(chart_part: Any) -> list[list[Any]] | None:
    """The embedded workbook as columns, or None when there is none (or openpyxl is absent)."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        return None
    try:
        blob = chart_part.chart_workbook.xlsx_part.blob
    except Exception:  # noqa: BLE001 — a chart without an embedded workbook is itself a finding
        return None
    try:
        workbook = load_workbook(io.BytesIO(blob), data_only=True)
        sheet = workbook.worksheets[0]
        rows = list(sheet.iter_rows(values_only=True))
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return []
    width = max(len(row) for row in rows)
    return [[row[i] if i < len(row) else None for row in rows] for i in range(width)]


def chart_checks(pptx: Path, irs: Sequence[IR]) -> list[dict[str, Any]]:
    """Every IR chart, verified structurally against the chart part the emitter produced.

    Pixels cannot judge a chart — the reference draws it with `chart-preview.js` and the candidate
    with PptxRender, so the gate masks the frame (master brief §9). This is what replaces it:
    the series the deck will draw must be the series the design asked for, and the plot rectangle
    from `c:manualLayout` must land within 4 px of the design's.
    """
    from pptx import Presentation

    results: list[dict[str, Any]] = []
    pptx = Path(pptx)
    if not pptx.exists():
        return results
    presentation = Presentation(str(pptx))
    slides = list(presentation.slides)

    for index, ir in enumerate(irs):
        charts = [e for e in ir.elements if e.kind == "chart"]
        if not charts:
            continue
        if index >= len(slides):
            for element in charts:
                results.append({"slide": ir.slide.id, "element": element.id, "found": False,
                                "ok": False, "problems": [f"no slide {index + 1} in the deck"]})
            continue
        frames = [shape for shape in slides[index].shapes if getattr(shape, "has_chart", False)]
        used: set[int] = set()
        for element in charts:
            results.append(_check_one_chart(ir, element, frames, used))
    return results


def _check_one_chart(ir: IR, element: Element, frames: list[Any], used: set[int]) -> dict[str, Any]:
    box = element.box
    best, best_score = -1, 0.0
    for position, shape in enumerate(frames):
        if position in used:
            continue
        frame = Box(shape.left / config.EMU_PER_PX, shape.top / config.EMU_PER_PX,
                    (shape.width or 0) / config.EMU_PER_PX, (shape.height or 0) / config.EMU_PER_PX)
        overlap = box.intersect(frame).area
        score = overlap / box.area if box.area else 0.0
        if score > best_score:
            best, best_score = position, score

    entry: dict[str, Any] = {
        "slide": ir.slide.id,
        "element": element.id,
        "origin": element.origin,
        "type": (element.spec or {}).get("type"),
        "found": False,
        "ok": False,
        "problems": [],
    }
    if best < 0 or best_score < 0.4:
        entry["problems"].append("no chart part on this slide overlaps the IR chart's box")
        return entry

    used.add(best)
    shape = frames[best]
    entry["found"] = True
    entry["shape"] = shape.name
    frame = Box(shape.left / config.EMU_PER_PX, shape.top / config.EMU_PER_PX,
                (shape.width or 0) / config.EMU_PER_PX, (shape.height or 0) / config.EMU_PER_PX)

    chart = shape.chart
    chart_space = chart._chartSpace
    spec = element.spec or {}
    series_elements = chart_space.findall(f".//{{{_C}}}ser")
    # What the chart *should* hold is not always what the spec lists: a waterfall is emitted as an
    # invisible base series plus the visible steps, and its connectors as a literal series of the
    # chart (WP-C §9), so "the chart equals the spec" has to be asked of the emitter's own expansion.
    # `expected_series` is identity for every other type.
    wanted_series = expected_series(spec) if isinstance(spec, dict) else []
    wanted_series = wanted_series or (spec.get("series") if isinstance(spec, dict) else None) or []
    if len(series_elements) != len(wanted_series):
        entry["problems"].append(
            f"the chart part has {len(series_elements)} series, the spec has {len(wanted_series)}"
        )

    expected_categories = list(expected_workbook(spec).get("categories") or [])
    for position, series_element in enumerate(series_elements):
        if position >= len(wanted_series):
            break
        wanted = wanted_series[position]
        values = _cache_values(series_element, "val") or _cache_values(series_element, "yVal")
        expected_values = list(wanted.get("values") or wanted.get("y") or [])
        if len(values) != len(expected_values):
            entry["problems"].append(
                f"series {position}: the chart holds {len(values)} points, the spec has "
                f"{len(expected_values)}"
            )
        for point, (got, want) in enumerate(zip(values, expected_values, strict=False)):
            if want is None and got is None:
                continue
            if want is None or got is None or abs(got - float(want)) > VALUE_TOLERANCE:
                entry["problems"].append(
                    f"series {position} point {point}: the chart holds {got!r}, the spec says {want!r}"
                )
        if expected_categories:
            categories = _cache_categories(series_element)
            if categories and categories != expected_categories:
                entry["problems"].append(
                    f"series {position}: categories {categories} are not the spec's {expected_categories}"
                )

    columns = _workbook_columns(chart.part)
    if columns is None:
        entry["problems"].append("the chart part carries no readable embedded workbook")
    else:
        entry["workbookColumns"] = len(columns)

    plot_rect = _manual_plot_rect(chart_space, frame)
    if element.plotRect is None:
        _check_unmeasured_layout(entry, element, spec, plot_rect)
    else:
        if plot_rect is None:
            entry["problems"].append(
                "the design has a plot rect but the chart has no c:manualLayout (PowerPoint will "
                "place the plot area itself)"
            )
        else:
            entry["plotRectPx"] = plot_rect.to_json()
            entry["designPlotRectPx"] = element.plotRect.to_json()
            deltas = {
                "x": abs(plot_rect.x - element.plotRect.x), "y": abs(plot_rect.y - element.plotRect.y),
                "w": abs(plot_rect.w - element.plotRect.w), "h": abs(plot_rect.h - element.plotRect.h),
            }
            worst = max(deltas.values())
            entry["plotRectDeltaPx"] = round(worst, 2)
            if worst > PLOT_RECT_TOLERANCE_PX:
                entry["problems"].append(
                    f"the plot rect is {worst:.1f}px from the design's (tolerance "
                    f"{PLOT_RECT_TOLERANCE_PX:.0f}px): {deltas}"
                )
    entry["ok"] = not entry["problems"]
    return entry


def _check_unmeasured_layout(entry: dict[str, Any], element: Element, spec: Any,
                             plot_rect: Box | None) -> None:
    """A chart with no measured plot rect: pinned charts carry the model's rect, the rest none.

    `c:manualLayout` is written only for a pinned chart (WP-C §3.6) — the author gave `plotArea`, or
    the chart carries an overlay (waterfall connectors or labels, `referenceLines`), in which case
    the rect is `chart_model.layout`'s default at the element's own frame and text size. Every
    other chart is laid out by PowerPoint, so a manual layout on it is a guess that should not be
    there ("no guessed layout", master brief §9 Charts).
    """
    if plot_rect is not None:
        entry["plotRectPx"] = plot_rect.to_json()
    box = element.box
    model = chart_model.normalise(spec)
    size_px = (element.style or {}).get("sizePx") or chart_model.DEFAULT_SIZE_PX
    geometry = chart_model.layout(model, box.w, box.h, size_px)
    if not geometry["pinned"]:
        if plot_rect is not None:
            entry["problems"].append("unpinned chart carries a manualLayout (PowerPoint should lay it out)")
        return
    area = geometry["plotArea"]
    expected = Box(box.x + area["x"] * box.w, box.y + area["y"] * box.h, area["w"] * box.w, area["h"] * box.h)
    entry["designPlotRectPx"] = expected.to_json()
    if plot_rect is None:
        entry["problems"].append("the chart is pinned (an author plotArea or an overlay) but carries no "
                                 "c:manualLayout, so its overlay cannot be placed")
        return
    deltas = {
        "x": abs(plot_rect.x - expected.x), "y": abs(plot_rect.y - expected.y),
        "w": abs(plot_rect.w - expected.w), "h": abs(plot_rect.h - expected.h),
    }
    worst = max(deltas.values())
    entry["plotRectDeltaPx"] = round(worst, 2)
    if worst > PLOT_RECT_TOLERANCE_PX:
        entry["problems"].append(
            f"the pinned plot rect is {worst:.1f}px from the chart model's (tolerance "
            f"{PLOT_RECT_TOLERANCE_PX:.0f}px): {deltas}"
        )


def _merge_failed_charts(charts: list[dict[str, Any]], emit_report: EmitReport | None) -> None:
    """A chart the emitter could not build is named in its charts-row entry, with the cause.

    `_emit_chart` leaves a placeholder and a `{"failed": True, "error": …}` entry in
    `emit_report.charts`; the structural check alone would only say that no chart part overlaps
    the box. Matched by (slide, element id), by id alone for an entry that names no slide.
    """
    if emit_report is None:
        return
    failed: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for chart_record in emit_report.charts or []:
        if chart_record.get("failed"):
            failed[(chart_record.get("slide"), chart_record.get("id"))] = chart_record
    if not failed:
        return
    for entry in charts:
        record = failed.get((entry.get("slide"), entry.get("element"))) or failed.get((None, entry.get("element")))
        if record is None:
            continue
        error = record.get("error") or "chart not built"
        entry["failed"] = True
        entry["error"] = error
        entry["problems"].insert(0, f"chart not built: {error}")
        entry["ok"] = False


# ------------------------------------------------------------------------------- baseline deck


def baseline_deck(master: Path, layout_ids: Sequence[str], manifest: Manifest, out_path: Path) -> Path:
    """The master with one empty slide per used layout — the renderer warnings that are *not* ours.

    Rendering this deck tells us what the master says about itself: missing fonts, OLE objects the
    renderer cannot draw, a layout picture in an unsupported format. Subtracting it from the
    export's warnings leaves exactly the warnings our own shapes caused.
    """
    from app.engine.emit.pptx import open_master, resolve_layout, strip_slides

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    presentation = open_master(Path(master))
    strip_slides(presentation)
    for layout_id in layout_ids:
        layout = manifest.layout(layout_id)
        presentation.slides.add_slide(resolve_layout(presentation, manifest, layout))
    presentation.save(str(out_path))
    return out_path


# ------------------------------------------------------------------------------------- coverage


def coverage(
    irs: list[IR],
    emit_report: EmitReport | None,
    renderer_warnings: list[str],
    baseline_warnings: list[str],
    *,
    pptx: Path | None = None,
    master: Path | None = None,
    manifest: Manifest | None = None,
) -> CoverageReport:
    """What this export made native, and what it did not.

    The four positional arguments are master brief §8's interface. `pptx`/`master`/`manifest` are
    optional: with them the report also carries the structural chart checks, the text row
    (`text_deck.text_report`, judged against `irs`) and the template comparison, which need the
    produced file and the original master.
    """
    report = CoverageReport(
        per_slide=[_counts_of(ir) for ir in irs],
        rasters=[
            {"slide": ir.slide.id, "id": element.id, "reason": element.reason,
             "box": element.box.to_json()}
            for ir in irs
            for element in ir.elements
            if element.kind == "raster"
        ],
        diagnostics=[
            {"slide": ir.slide.id, **diagnostic.to_json()}
            for ir in irs
            for diagnostic in ir.diagnostics
        ],
        renderer_warnings_ours=sorted(set(renderer_warnings) - set(baseline_warnings)),
        unsupported=_unsupported(irs, emit_report),
    )
    if pptx is not None:
        report.charts = chart_checks(Path(pptx), irs)
        report.text = text_report(Path(pptx), irs)
        _merge_failed_charts(report.charts, emit_report)
        report.template = template_checks(Path(pptx), master, irs, manifest)
    return report
