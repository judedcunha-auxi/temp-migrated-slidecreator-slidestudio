"""The renderer port: PptxRender, behind one `Renderer` protocol.

PptxRender is an **external** .NET HTTP service (decision D3b); this service only calls it. Every
renderer call in the engine goes through a `Renderer` object the caller passes in, never through a
URL read here, so a renderer change lands in one file and a test can hand in a fake.

Implementations:

* `PptxRenderClient`: the HTTP client. Built from the engine settings with `from_settings()`
  (`None` when `SLIDE_ENGINE_RENDERER_URL` is unset: no renderer is a fact the caller reports, not
  something to render around).
* `BlankLayoutsRenderer`: offline `render_layouts` (one white PNG per layout, named and joined
  exactly as the service's are). For code that needs *a* background per layout, not a picture of it.
* Test doubles live in `tests/fakes/renderer.py`.

What the deployed PptxRender serves (checked against its source, 2026-10): `POST /render-layouts`
only, reading the form fields `width`, `masters`, `placeholderText` and `noPlaceholderText`. It does
**not** accept `names=true` and returns no `index.json`, and `/render`, `/verify` and `/info` do not
exist. So:

* `render_layouts` sends `width` alone and joins the returned PNGs to the package **by filename**
  (`layout-NN-<slug>.png`, NN = PptxRender's 1-based enumeration index). That is the real path.
  When a build does return an `index.json`, every entry is cross-checked against the package too.
* `render` and `verify` (the visual gate's needs) are sent only when `SLIDE_ENGINE_RENDERER_ENDPOINTS`
  lists them (a local PptxRender build); otherwise they raise `RendererError` saying so.

## The layout-ordering problem, and how this module solves it

PptxRender builds its layout list as `foreach master in sldMasterIdLst: AddRange(
master.SlideLayoutParts)`, and `SlideLayoutParts` yields the layout parts in the **order the
relationships appear in that master's `.rels` part**. python-pptx walks `<p:sldLayoutIdLst>`
instead. On real client masters those two orders disagree (a master's rels can run rId3, rId2,
rId1), and several layouts can share one name, so neither position nor name identifies a layout.

So `layout_identities` reconstructs the renderer's own enumeration from the package (masters in
`sldMasterIdLst` order, each master's layouts in `.rels` document order), the filename join checks
the count and every index, and the result is keyed by `partName`, the one identifier both sides agree
on. A mismatch raises instead of returning a plausible-looking wrong mapping. An `index.json` in the
same shape is written beside the PNGs for the importer.

Ported from Slide Studio `engine/renderer.py` (migration plan §4.2). Dropped: `info` (no such
endpoint), the module-global stand-in backend (callers pass a `Renderer`), and the CLI advice.
"""

from __future__ import annotations

import base64
import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx2 as httpx
from defusedxml import ElementTree

from app.config import engine as config
from app.engine.reports import Box, GateReport, GateSlide


class RendererError(RuntimeError):
    """The renderer failed, is not configured, or returned something this client cannot trust."""


@runtime_checkable
class Renderer(Protocol):
    """What the engine needs from a renderer. Paths in, files out; nothing global."""

    def render_layouts(self, pptx: Path, width: int, out_dir: Path) -> dict[str, Path]:
        """Render every layout; return `{partName: png}` and write `out_dir/index.json`."""
        ...

    def render(self, pptx: Path, width: int, out_dir: Path) -> list[Path]:
        """Render every slide to `out_dir/slide-NN.png`; write `out_dir/warnings.json`."""
        ...

    def verify(self, pptx: Path, references: list[Path], out_dir: Path) -> GateReport:
        """Score the deck against reference PNGs with the visible-defect gate."""
        ...


#: OPC relationship namespace, and the layout relationship type.
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_LAYOUT_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
_MASTER_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster"
_PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DRAWING_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


@dataclass(frozen=True, slots=True)
class LayoutIdentity:
    """One layout as both renderers see it: the renderer's flat index, and the manifest's identity."""

    renderIndex: int      # 1-based, the order PptxRender enumerates in (and names files by)
    masterIndex: int      # 0-based position in <p:sldMasterIdLst>
    layoutIndex: int      # 0-based position in that master's <p:sldLayoutIdLst>
    name: str
    partName: str

    def to_json(self) -> dict[str, Any]:
        return {
            "renderIndex": self.renderIndex,
            "masterIndex": self.masterIndex,
            "layoutIndex": self.layoutIndex,
            "name": self.name,
            "partName": self.partName,
        }


# ------------------------------------------------------------------------------- package reading


def _rels_path(part_name: str) -> str:
    """`/ppt/slideMasters/slideMaster1.xml` -> `ppt/slideMasters/_rels/slideMaster1.xml.rels`."""
    trimmed = part_name.lstrip("/")
    folder, _, name = trimmed.rpartition("/")
    return f"{folder}/_rels/{name}.rels"


def _resolve(part_name: str, target: str) -> str:
    """Resolve a relationship target against the part that declares it, to an absolute part name."""
    if target.startswith("/"):
        return target
    base = part_name.lstrip("/").rpartition("/")[0].split("/")
    for segment in target.split("/"):
        if segment == "..":
            base = base[:-1]
        elif segment not in (".", ""):
            base.append(segment)
    return "/" + "/".join(base)


def _rel_targets(archive: zipfile.ZipFile, part_name: str, rel_type: str) -> list[str]:
    """Targets of one relationship type, **in the order they appear in the `.rels` part**.

    That order is what the Open XML SDK hands back from `GetPartsOfType<T>()`, and therefore what
    PptxRender enumerates: not the numeric rId order, and not the id-list order.
    """
    try:
        data = archive.read(_rels_path(part_name))
    except KeyError:
        return []
    root = ElementTree.fromstring(data)
    return [
        _resolve(part_name, rel.get("Target", ""))
        for rel in root.findall(f"{{{_REL_NS}}}Relationship")
        if rel.get("Type") == rel_type and rel.get("TargetMode") != "External"
    ]


def _id_list_order(archive: zipfile.ZipFile, part_name: str, tag: str, id_tag: str) -> list[str]:
    """Targets in `<p:sldMasterIdLst>` / `<p:sldLayoutIdLst>` order: python-pptx's order."""
    root = ElementTree.fromstring(archive.read(part_name.lstrip("/")))
    id_list = root.find(f"{{{_PRESENTATION_NS}}}{tag}")
    if id_list is None:
        return []
    rels_root = ElementTree.fromstring(archive.read(_rels_path(part_name)))
    by_id = {
        rel.get("Id"): _resolve(part_name, rel.get("Target", ""))
        for rel in rels_root.findall(f"{{{_REL_NS}}}Relationship")
    }
    targets: list[str] = []
    for entry in id_list.findall(f"{{{_PRESENTATION_NS}}}{id_tag}"):
        rid = entry.get(f"{{{_DRAWING_REL_NS}}}id")
        if rid in by_id:
            targets.append(by_id[rid])
    return targets


def _layout_name(archive: zipfile.ZipFile, part_name: str) -> str:
    """`<p:cSld name>` of a layout part: the name both renderers show."""
    root = ElementTree.fromstring(archive.read(part_name.lstrip("/")))
    common = root.find(f"{{{_PRESENTATION_NS}}}cSld")
    if common is not None and common.get("name"):
        return str(common.get("name", ""))
    layout_type = root.get("type")
    return str(layout_type) if layout_type else Path(part_name).stem


def layout_identities(pptx: Path) -> list[LayoutIdentity]:
    """Every layout, in **PptxRender's** enumeration order, carrying the manifest's identity too.

    Pure package reading (no renderer, no python-pptx), so the importer and the gate can both use it
    without paying for a render.
    """
    identities: list[LayoutIdentity] = []
    with zipfile.ZipFile(pptx) as archive:
        presentation = "/ppt/presentation.xml"
        masters = _id_list_order(archive, presentation, "sldMasterIdLst", "sldMasterId")
        render_index = 0
        for master_index, master in enumerate(masters):
            by_id_list = _id_list_order(archive, master, "sldLayoutIdLst", "sldLayoutId")
            position = {part: index for index, part in enumerate(by_id_list)}
            for layout in _rel_targets(archive, master, _LAYOUT_REL):
                render_index += 1
                identities.append(
                    LayoutIdentity(
                        renderIndex=render_index,
                        masterIndex=master_index,
                        # A layout reachable by relationship but absent from the id list cannot be
                        # placed; -1 makes that visible instead of silently colliding on 0.
                        layoutIndex=position.get(layout, -1),
                        name=_layout_name(archive, layout),
                        partName=layout,
                    )
                )
    return identities


def slide_size_emu(pptx: Path) -> tuple[int, int]:
    """`<p:sldSz cx cy>` straight from the package: the exact slide size, in EMU."""
    with zipfile.ZipFile(pptx) as archive:
        root = ElementTree.fromstring(archive.read("ppt/presentation.xml"))
    size = root.find(f"{{{_PRESENTATION_NS}}}sldSz")
    if size is None:
        raise RendererError(f"{Path(pptx).name} has no <p:sldSz>")
    return int(size.get("cx", 0)), int(size.get("cy", 0))


# ------------------------------------------------------------------------------- joining layouts

#: `layout-07-title-slide.png` -> render index 7 (1-based, PptxRender's enumeration order).
_LAYOUT_FILE_RE = re.compile(r"^layout-(?P<index>\d+)(?:-.*)?\.png$", re.IGNORECASE)


def _slug(name: str) -> str:
    """A layout name slugged the way PptxRender slugs it for the filename."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "layout"


def _warnings_block(payloads: dict[str, Any], deck: Path, slides: int) -> dict[str, Any]:
    """The renderer's warnings as `warnings.json`, in one stable shape."""
    warnings = dict(
        payloads.get("warnings.json")
        or {"warnings": [], "hidden": 0, "truncated": False, "fontsSubstituted": []}
    )
    warnings["source"] = "pptxrender"
    warnings["deck"] = Path(deck).name
    warnings["slides"] = slides
    return warnings


def map_layouts_by_filename(
    pptx: Path,
    produced: list[Path],
    identities: list[LayoutIdentity],
    out_dir: Path,
    payloads: dict[str, Any] | None = None,
) -> dict[str, Path]:
    """Join layout PNGs to `layout_identities` by the filename's render index (the real path).

    The deployed PptxRender returns PNGs only, named `layout-NN-<slug>.png`. The count must equal the
    package's layout relationships and every index must be present, or this raises. The mapping is
    keyed by `partName`, and an `index.json` is written so the rest of import is the same either way.
    """
    payloads = payloads or {}
    if len(produced) != len(identities):
        raise RendererError(
            f"{Path(pptx).name}: the renderer returned {len(produced)} layout PNGs but the package "
            f"declares {len(identities)} layout relationships; cannot join them safely."
        )
    by_index: dict[int, Path] = {}
    for png in produced:
        match = _LAYOUT_FILE_RE.match(png.name)
        if not match:
            raise RendererError(f"unexpected layout filename from the renderer: {png.name}")
        by_index[int(match.group("index"))] = png

    mapping: dict[str, Path] = {}
    entries: list[dict[str, Any]] = []
    for identity in identities:
        found = by_index.get(identity.renderIndex)
        if found is None:
            raise RendererError(f"{Path(pptx).name}: no layout PNG for render index {identity.renderIndex}")
        mapping[identity.partName] = found
        entries.append({"file": found.name, **identity.to_json()})

    (out_dir / "index.json").write_text(json.dumps(entries, indent=2), encoding="utf-8")
    warnings = _warnings_block(payloads, pptx, len(mapping))
    (out_dir / "warnings.json").write_text(json.dumps(warnings, indent=2), encoding="utf-8")
    return mapping


def check_layout_index(pptx: Path, index: list[dict[str, Any]], identities: list[LayoutIdentity]) -> None:
    """Cross-check a renderer-supplied `index.json` against the package; raise on any difference.

    Optional: only builds that return an index are checked. Two implementations of one rule (the
    renderer's C# and `layout_identities`), so comparing them catches the failure that matters, a
    background joined to the wrong layout.
    """
    if len(index) != len(identities):
        raise RendererError(
            f"{Path(pptx).name}: the renderer's index has {len(index)} entries but the package declares "
            f"{len(identities)} layout relationships; do not join these by position."
        )
    ordered = sorted(index, key=lambda e: int(e.get("renderIndex", 0)))
    for identity, entry in zip(identities, ordered, strict=True):
        theirs = (entry.get("renderIndex"), entry.get("masterIndex"), entry.get("layoutIndex"),
                  entry.get("name"), entry.get("partName"))
        ours = (identity.renderIndex, identity.masterIndex, identity.layoutIndex, identity.name,
                identity.partName)
        if theirs != ours:
            raise RendererError(
                f"{Path(pptx).name}: layout identity mismatch at render index {identity.renderIndex}: "
                f"the renderer says {theirs} and the package says {ours}. PptxRender's layout order is "
                f"no longer 'masters in sldMasterIdLst order, layouts in .rels order'; fix "
                f"layout_identities before trusting any layout background."
            )


def _unpack(content: bytes, out_dir: Path) -> tuple[list[Path], dict[str, Any]]:
    """Write the PNGs of a returned zip into `out_dir`; parse its JSON members.

    Entry names are flattened to their basename: the service writes flat names, and a member that
    tried to escape `out_dir` through `../` would be a nasty way to learn otherwise.
    """
    pngs: list[Path] = []
    payloads: dict[str, Any] = {}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for name in sorted(archive.namelist()):
            member = Path(name.replace("\\", "/")).name
            if not member:
                continue
            if member.lower().endswith(".png"):
                target = out_dir / member
                target.write_bytes(archive.read(name))
                pngs.append(target)
            elif member.lower().endswith(".json"):
                payloads[member] = json.loads(archive.read(name).decode("utf-8"))
    return pngs, payloads


def _get(data: dict[str, Any], key: str, *, default: Any) -> Any:
    """Case-insensitive lookup: .NET serialises records PascalCase and its wrapper camelCase."""
    if key in data:
        return data[key]
    lowered = key.lower()
    for candidate, value in data.items():
        if candidate.lower() == lowered:
            return value
    return default


def _check_deck(pptx: Path, out_dir: Path) -> tuple[Path, Path]:
    pptx, out_dir = Path(pptx), Path(out_dir)
    if not pptx.exists():
        raise RendererError(f"deck not found: {pptx.name}")
    out_dir.mkdir(parents=True, exist_ok=True)
    return pptx, out_dir


# ------------------------------------------------------------------------------------ HTTP client

#: How a `.pptx` is labelled in the multipart upload.
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class PptxRenderClient:
    """The PptxRender HTTP client. One instance per configured service; safe to share.

    `transport` is for tests (an `httpx2.MockTransport`); production leaves it unset. Authentication
    is not sent yet: PptxRender has none today (plan C4, §7.4). When it gains one, the header goes
    here and nowhere else.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 300.0,
        endpoints: frozenset[str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise RendererError("the PptxRender URL must be an http(s) URL")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.endpoints = endpoints if endpoints is not None else frozenset({"render-layouts"})
        self._transport = transport

    @classmethod
    def from_settings(cls, settings: config.EngineSettings | None = None) -> PptxRenderClient | None:
        """The configured client, or `None` when `SLIDE_ENGINE_RENDERER_URL` is unset."""
        s = settings or config.engine_settings
        if not s.renderer_url:
            return None
        return cls(s.renderer_url, timeout_s=s.renderer_timeout_s, endpoints=s.endpoints)

    def supports(self, endpoint: str) -> bool:
        return endpoint in self.endpoints

    def _post(
        self,
        endpoint: str,
        *,
        deck: Path,
        data: dict[str, str] | None = None,
        extra_files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> httpx.Response:
        """POST a deck (plus anything else); raise `RendererError` saying why not."""
        if not self.supports(endpoint):
            raise RendererError(
                f"this PptxRender does not serve /{endpoint} (SLIDE_ENGINE_RENDERER_ENDPOINTS lists "
                f"{sorted(self.endpoints)}). The deployed service serves render-layouts only."
            )
        try:
            with Path(deck).open("rb") as handle:
                files: dict[str, Any] = {"file": (Path(deck).name, handle, _PPTX_MIME)}
                if extra_files:
                    files.update(extra_files)
                with httpx.Client(timeout=self.timeout_s, transport=self._transport) as client:
                    response = client.post(f"{self.base_url}/{endpoint}", data=data or {}, files=files)
        except httpx.HTTPError as error:
            raise RendererError(f"PptxRender did not answer /{endpoint} ({type(error).__name__})") from error
        if response.status_code >= 400:
            raise RendererError(f"PptxRender /{endpoint} failed ({response.status_code}): {_detail(response)}")
        return response

    def render_layouts(self, pptx: Path, width: int, out_dir: Path) -> dict[str, Path]:
        """Render every layout; return `{partName: png}` and write `out_dir/index.json`.

        Only `width` is sent: of the fields the deployed service reads, it is the one this engine needs.
        """
        pptx, out_dir = _check_deck(pptx, out_dir)
        response = self._post("render-layouts", deck=pptx, data={"width": str(int(width))})
        produced, payloads = _unpack(response.content, out_dir)
        produced = [png for png in produced if png.name.lower().startswith("layout-")]
        identities = layout_identities(pptx)
        index = payloads.get("index.json")
        if isinstance(index, list):
            check_layout_index(pptx, index, identities)
        return map_layouts_by_filename(pptx, produced, identities, out_dir, payloads)

    def render(self, pptx: Path, width: int, out_dir: Path) -> list[Path]:
        """Render every slide (needs a PptxRender build that serves `/render`)."""
        pptx, out_dir = _check_deck(pptx, out_dir)
        response = self._post("render", deck=pptx, data={"width": str(int(width))})
        produced, payloads = _unpack(response.content, out_dir)
        pngs = [png for png in produced if png.name.startswith("slide-")]
        if not pngs:
            raise RendererError(f"PptxRender returned no slides for {pptx.name}")
        warnings = _warnings_block(payloads, pptx, len(pngs))
        (out_dir / "warnings.json").write_text(json.dumps(warnings, indent=2), encoding="utf-8")
        return pngs

    def verify(self, pptx: Path, references: list[Path], out_dir: Path) -> GateReport:
        """The visible-defect gate (needs a PptxRender build that serves `/verify`).

        The thresholds travel with the request. No width is sent: the references choose the
        comparison scale, so give it canvas-sized images. `slidesChecked` is asserted rather than
        trusted: a slide whose render and reference differ in size is never a clean slide.
        """
        pptx, out_dir = _check_deck(pptx, out_dir)
        if not references:
            raise RendererError("verify needs at least one reference PNG")
        missing = [Path(r).name for r in references if not Path(r).exists()]
        if missing:
            raise RendererError(f"reference PNGs are missing: {missing}")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for number, reference in enumerate(references, start=1):
                archive.write(reference, f"slide-{number:02d}.png")
        response = self._post(
            "verify",
            deck=pptx,
            data={
                "blur": str(config.GATE_BLUR),
                "delta": str(config.GATE_DELTA_E),
                "minArea": str(config.GATE_MIN_AREA),
                "slack": str(config.GATE_SLACK),
            },
            extra_files={"reference": ("references.zip", buffer.getvalue(), "application/zip")},
        )
        return _gate_report(response.json(), pptx, len(references), out_dir)


def _detail(response: httpx.Response) -> str:
    """The service's own words for a failure (it answers errors as problem JSON), trimmed."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(payload, dict):
        return str(payload.get("detail") or payload.get("title") or "")[:300]
    return str(payload)[:300]


def _gate_report(data: dict[str, Any], pptx: Path, expected: int, out_dir: Path) -> GateReport:
    slides_checked = int(_get(data, "slidesChecked", default=0))
    if slides_checked != expected:
        raise RendererError(
            f"verify checked {slides_checked} slides but {expected} references were given; references "
            f"must be exactly canvas-sized."
        )
    annotate_dir = out_dir / "annotated" / pptx.stem
    slides: list[GateSlide] = []
    for entry in _get(data, "slides", default=[]) or []:
        number = int(_get(entry, "index", default=0))
        diff_png: Path | None = None
        if _get(entry, "annotatedPng", default=None):
            annotate_dir.mkdir(parents=True, exist_ok=True)
            diff_png = annotate_dir / f"slide-{number:02d}.png"
            diff_png.write_bytes(base64.b64decode(_get(entry, "annotatedPng", default="")))
        slides.append(GateSlide(
            index=number,
            defect_area=int(_get(entry, "defectArea", default=0)),
            clean=bool(_get(entry, "clean", default=True)),
            components=[
                Box(float(c["x"]), float(c["y"]), float(c["w"]), float(c["h"]))
                for c in _get(entry, "components", default=[]) or []
            ],
            diff_png=diff_png,
        ))
    trimmed = dict(data)
    trimmed["slides"] = [
        {key: value for key, value in entry.items() if key != "annotatedPng"}
        for entry in _get(data, "slides", default=[]) or []
    ]
    (out_dir / "verify.json").write_text(json.dumps(trimmed, indent=2), encoding="utf-8")
    return GateReport(
        slides=sorted(slides, key=lambda slide: slide.index),
        slides_checked=slides_checked,
        total_area=int(_get(data, "total", default=0)),
        settings={"blur": config.GATE_BLUR, "deltaE": config.GATE_DELTA_E,
                  "minArea": config.GATE_MIN_AREA, "slack": config.GATE_SLACK},
        source="pptxrender",
        warnings=list(_get(data, "warnings", default=[]) or []),
    )


# ------------------------------------------------------------------------------------- offline


class BlankLayoutsRenderer:
    """An offline `render_layouts`: one plain white PNG per layout, named and indexed exactly as the
    service names them, and joined to the package by the same code (`map_layouts_by_filename`).

    For code that needs *a* background per layout (the importer's manifest, tests), not a picture of
    it. `render` and `verify` judge pixels, so they need the real service and raise here.
    """

    def render_layouts(self, pptx: Path, width: int, out_dir: Path) -> dict[str, Path]:
        from PIL import Image

        pptx, out_dir = _check_deck(pptx, out_dir)
        cx, cy = slide_size_emu(pptx)
        height = max(1, round(width * cy / cx)) if cx else width
        identities = layout_identities(pptx)
        produced: list[Path] = []
        for identity in identities:
            png = out_dir / f"layout-{identity.renderIndex:02d}-{_slug(identity.name)}.png"
            Image.new("RGB", (width, height), "white").save(png)
            produced.append(png)
        return map_layouts_by_filename(pptx, produced, identities, out_dir, {})

    def render(self, pptx: Path, width: int, out_dir: Path) -> list[Path]:
        raise RendererError("BlankLayoutsRenderer cannot render slides; configure PptxRender")

    def verify(self, pptx: Path, references: list[Path], out_dir: Path) -> GateReport:
        raise RendererError("BlankLayoutsRenderer cannot run the visual gate; configure PptxRender")
