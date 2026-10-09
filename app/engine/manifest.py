"""The master manifest: what the importer produces and everything downstream reads.

Schema v2. The producer is `app/engine/importer.py`. The pre-v2 "legacy" conversion
(`from_legacy`, for Slide Studio projects imported by Claude) was dropped in the port (decision D14:
Slide Studio projects are not migrated); a manifest without `slideWidthEmu` is now an error.

The one rule worth repeating here, because getting it wrong puts the dark cover under slide 2:

    Layout identity is `(masterIndex, layoutIndex)` in id-list order **plus `partName`**, and every
    join between renderer output and this manifest goes through `partName`.

PptxRender enumerates layouts in *relationship* order, python-pptx in `sldLayoutIdLst` order; on real
client masters whole masters come out reversed, and several layouts can share one name. Position
and name are both ambiguous; the part name is not.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import engine as config
from app.engine.ir import Box

VERSION = 2

#: OOXML placeholder types (`<p:ph type>`); `obj` is the body-like default python-pptx reports.
PLACEHOLDER_TYPES: tuple[str, ...] = (
    "title", "body", "subTitle", "ctrTitle", "pic", "chart", "tbl", "dt", "ftr", "sldNum", "obj",
    "clipArt", "dgm", "media", "sldImg", "hdr",
)

#: Which layout placeholder types a `data-placeholder` value may map onto (WP4's classifier).
PLACEHOLDER_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("title", "ctrTitle"),
    "subtitle": ("subTitle",),
    "body": ("body", "obj"),
}


class ManifestError(ValueError):
    """The manifest is unusable — a missing layout, a broken join, a schema violation."""


@dataclass(slots=True)
class Placeholder:
    """One placeholder zone on a layout, in canvas px and in EMU."""

    type: str
    idx: int | None = None
    name: str | None = None
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    emu: dict[str, int] | None = None
    style: dict[str, Any] | None = None

    @property
    def box(self) -> Box:
        return Box(self.x, self.y, self.w, self.h)

    def matches(self, wanted: str) -> bool:
        """True when this placeholder can hold text the author marked `data-placeholder=<wanted>`."""
        return self.type in PLACEHOLDER_ALIASES.get(wanted, (wanted,))

    def to_json(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "idx": self.idx,
            "name": self.name,
            "x": self.x, "y": self.y, "w": self.w, "h": self.h,
            "emu": self.emu,
            "style": self.style,
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> Placeholder:
        return Placeholder(
            type=data.get("type") or "obj",
            idx=data.get("idx"),
            name=data.get("name"),
            x=float(data.get("x") or 0.0),
            y=float(data.get("y") or 0.0),
            w=float(data.get("w") or 0.0),
            h=float(data.get("h") or 0.0),
            emu=data.get("emu"),
            style=data.get("style"),
        )


@dataclass(slots=True)
class Layout:
    """One slide layout: its identity, its background render and its placeholder zones."""

    id: str
    name: str
    masterIndex: int = 0
    layoutIndex: int = 0
    partName: str | None = None
    background: str | None = None
    usage: str = ""
    placeholders: list[Placeholder] = field(default_factory=list)

    def placeholder(self, wanted: str) -> Placeholder | None:
        """The first placeholder a `data-placeholder=<wanted>` element may target."""
        return next((p for p in self.placeholders if p.matches(wanted)), None)

    def placeholders_of(self, wanted: str) -> list[Placeholder]:
        return [p for p in self.placeholders if p.matches(wanted)]

    def masked_boxes(self, types: Iterable[str] = config.GATE_MASK_PLACEHOLDERS) -> list[Box]:
        """Boxes the pixel gate paints out on both images (slide number, date, footer)."""
        wanted = set(types)
        return [p.box for p in self.placeholders if p.type in wanted]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "masterIndex": self.masterIndex,
            "layoutIndex": self.layoutIndex,
            "partName": self.partName,
            "background": self.background,
            "usage": self.usage,
            "placeholders": [p.to_json() for p in self.placeholders],
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> Layout:
        return Layout(
            id=data["id"],
            name=data.get("name") or data["id"],
            masterIndex=int(data.get("masterIndex") or 0),
            layoutIndex=int(data.get("layoutIndex") or 0),
            partName=data.get("partName"),
            background=data.get("background"),
            usage=data.get("usage") or "",
            placeholders=[Placeholder.from_json(p) for p in data.get("placeholders") or []],
        )


@dataclass(slots=True)
class Master:
    """One slide master: its position, its part and its own theme (a real client master has eight, with different schemes)."""

    index: int
    name: str
    partName: str | None = None
    theme: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"index": self.index, "name": self.name, "partName": self.partName, "theme": self.theme}

    @staticmethod
    def from_json(data: dict[str, Any]) -> Master:
        return Master(
            index=int(data.get("index") or 0),
            name=data.get("name") or "",
            partName=data.get("partName"),
            theme=dict(data.get("theme") or {}),
        )


@dataclass(slots=True)
class Manifest:
    """Everything the engine knows about a master deck."""

    slideWidthEmu: int
    slideHeightEmu: int
    canvas: dict[str, int]
    masters: list[Master] = field(default_factory=list)
    theme: dict[str, Any] = field(default_factory=dict)
    textStyles: dict[str, Any] = field(default_factory=dict)
    bullets: list[dict[str, Any]] = field(default_factory=list)
    layouts: list[Layout] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)
    designNotes: str = ""
    importer: str = "legacy"
    sourceHash: str | None = None
    version: int = VERSION

    # -- accessors --------------------------------------------------------------------------------
    def layout(self, layout_id: str) -> Layout:
        """The layout with this id — raises rather than returning None, because callers cannot go on."""
        found = next((lay for lay in self.layouts if lay.id == layout_id), None)
        if found is None:
            known = ", ".join(lay.id for lay in self.layouts) or "(none)"
            raise ManifestError(f"no layout {layout_id!r} in this manifest; known ids: {known}")
        return found

    def layout_by_part(self, part_name: str) -> Layout:
        """The layout at this OOXML part name — the only join that is safe across renderers."""
        found = next((lay for lay in self.layouts if lay.partName == part_name), None)
        if found is None:
            raise ManifestError(
                f"no layout with partName {part_name!r}; this manifest "
                f"{'has no part names at all (legacy import)' if self.has_part_names is False else 'knows ' + str(len(self.layouts)) + ' layouts'}"
            )
        return found

    def layout_at(self, master_index: int, layout_index: int) -> Layout:
        found = next(
            (lay for lay in self.layouts if lay.masterIndex == master_index and lay.layoutIndex == layout_index),
            None,
        )
        if found is None:
            raise ManifestError(f"no layout at master {master_index}, index {layout_index}")
        return found

    def master(self, index: int) -> Master:
        found = next((m for m in self.masters if m.index == index), None)
        if found is None:
            raise ManifestError(f"no master at index {index}")
        return found

    def theme_of(self, layout: Layout) -> dict[str, Any]:
        """The theme that governs this layout — per-master, falling back to the deck theme."""
        try:
            return self.master(layout.masterIndex).theme or self.theme
        except ManifestError:
            return self.theme

    @property
    def has_part_names(self) -> bool:
        return any(lay.partName for lay in self.layouts)

    @property
    def canvas_w(self) -> int:
        return int(self.canvas["w"])

    @property
    def canvas_h(self) -> int:
        return int(self.canvas["h"])

    @property
    def fonts(self) -> dict[str, str]:
        """`{"major": …, "minor": …}` — what measurement forces (master brief §10.2)."""
        return dict((self.theme or {}).get("fonts") or {})

    # -- serialisation ------------------------------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "importer": self.importer,
            "sourceHash": self.sourceHash,
            "slideWidthEmu": self.slideWidthEmu,
            "slideHeightEmu": self.slideHeightEmu,
            "canvas": dict(self.canvas),
            "masters": [m.to_json() for m in self.masters],
            "theme": self.theme,
            "textStyles": self.textStyles,
            "bullets": self.bullets,
            "layouts": [lay.to_json() for lay in self.layouts],
            "assets": list(self.assets),
            "designNotes": self.designNotes,
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> Manifest:
        canvas = dict(data.get("canvas") or {})
        canvas.setdefault("pxPerIn", config.PX_PER_IN)
        return Manifest(
            slideWidthEmu=int(data["slideWidthEmu"]),
            slideHeightEmu=int(data["slideHeightEmu"]),
            canvas=canvas,
            masters=[Master.from_json(m) for m in data.get("masters") or []],
            theme=dict(data.get("theme") or {}),
            textStyles=dict(data.get("textStyles") or {}),
            bullets=list(data.get("bullets") or []),
            layouts=[Layout.from_json(lay) for lay in data.get("layouts") or []],
            assets=list(data.get("assets") or []),
            designNotes=data.get("designNotes") or "",
            importer=data.get("importer") or "legacy",
            sourceHash=data.get("sourceHash"),
            version=int(data.get("version") or VERSION),
        )

    def dumps(self) -> str:
        return json.dumps(self.to_json(), indent=2, ensure_ascii=False) + "\n"

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.dumps(), encoding="utf-8")
        return path

    @staticmethod
    def load(path: Path) -> Manifest:
        """Load a v2 `manifest.json`."""
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if "slideWidthEmu" not in data:
            raise ManifestError(f"{path.name} is not a v2 manifest (no slideWidthEmu)")
        return Manifest.from_json(data)

    # -- validation ----------------------------------------------------------------------------------
    def validate(self, *, project_dir: Path | None = None, strict: bool = False) -> list[str]:
        """Every problem with this manifest (empty list = usable). `project_dir` enables file checks."""
        problems: list[str] = []
        if self.slideWidthEmu <= 0 or self.slideHeightEmu <= 0:
            problems.append("slide size must be positive")
        px_per_in = int(self.canvas.get("pxPerIn") or config.PX_PER_IN)
        expected_w = round(self.slideWidthEmu / config.EMU_PER_IN * px_per_in)
        expected_h = round(self.slideHeightEmu / config.EMU_PER_IN * px_per_in)
        if abs(self.canvas_w - expected_w) > 1 or abs(self.canvas_h - expected_h) > 1:
            problems.append(
                f"canvas {self.canvas_w}×{self.canvas_h} does not match the slide size "
                f"({expected_w}×{expected_h} at {px_per_in} px/in)"
            )
        if not self.layouts:
            problems.append("manifest has no layouts")

        ids = [lay.id for lay in self.layouts]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            problems.append(f"duplicate layout ids: {duplicates}")

        parts = [lay.partName for lay in self.layouts if lay.partName]
        duplicate_parts = sorted({p for p in parts if parts.count(p) > 1})
        if duplicate_parts:
            problems.append(f"duplicate layout partNames: {duplicate_parts}")

        positions = [(lay.masterIndex, lay.layoutIndex) for lay in self.layouts]
        duplicate_positions = sorted({p for p in positions if positions.count(p) > 1})
        if duplicate_positions:
            problems.append(f"duplicate (masterIndex, layoutIndex): {duplicate_positions}")

        for layout in self.layouts:
            for placeholder in layout.placeholders:
                if placeholder.type not in PLACEHOLDER_TYPES:
                    problems.append(f"{layout.id}: unknown placeholder type {placeholder.type!r}")
                if placeholder.w < 0 or placeholder.h < 0:
                    problems.append(f"{layout.id}: placeholder {placeholder.type} has a negative extent")
            if project_dir is not None and layout.background:
                background = Path(project_dir) / "layouts" / layout.background
                if not background.exists():
                    problems.append(f"{layout.id}: background {background} is missing")

        if self.importer == "deterministic" and not self.has_part_names:
            problems.append("a deterministic import must record partName on every layout")

        if strict and problems:
            raise ManifestError("; ".join(problems))
        return problems


def sha256_of(path: Path) -> str:
    """`sourceHash` — the identity of the master file a manifest was imported from."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
