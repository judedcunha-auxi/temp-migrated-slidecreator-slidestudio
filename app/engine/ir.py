"""The intermediate representation: the only contract between extract → classify → emit → verify.

Spec: `docs/engine/02-IR-SCHEMA.md`. Owned by WP0a and **frozen** after WP0a's freeze checklist —
a change needs orchestrator approval, a `version` bump and a migration of `fixtures/torture/ir/*.json`.
Packages that need a field the schema lacks use `extras` with a namespaced key (`x-wp2-…`).

Shape of the model, and why:

* One `Element` dataclass covers every `kind` rather than a subclass per kind. The schema is a JSON
  contract shared by five packages and hand-written fixtures; a flat record round-trips exactly,
  needs no dispatch table in `from_json`, and lets `validate()` own the "which fields does this kind
  require" question in one readable place.
* Sub-structures the schema spells out as JSON objects (geometry, fill, stroke, paragraphs, cells,
  chart spec) stay plain dicts. `02-IR-SCHEMA.md` is their spec and `validate()` is their enforcement;
  wrapping them in dataclasses would fork the contract into two places that can disagree.
* Coordinates are canvas px, floats, absolute, origin top-left. `to_json` rounds every float to 3
  decimals so two runs of the same input produce byte-identical JSON (master brief §9, determinism).
"""
from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

from app.engine import charts_spec

VERSION = 1

Kind = Literal["shape", "text", "image", "table", "chart", "raster"]
KINDS: tuple[str, ...] = ("shape", "text", "image", "table", "chart", "raster")

GEOMETRY_TYPES: tuple[str, ...] = (
    "rect", "roundRect", "ellipse", "line", "polyline", "custom", "blockArc", "pie", "chord",
)
PATH_COMMANDS: dict[str, int] = {"M": 2, "L": 2, "C": 6, "Q": 4, "Z": 0}
ALIGNMENTS: tuple[str, ...] = ("left", "center", "right", "justify")
ANCHORS: tuple[str, ...] = ("top", "middle", "bottom")
DIAGNOSTIC_LEVELS: tuple[str, ...] = ("error", "warn", "info")
FILL_TYPES: tuple[str, ...] = ("none", "solid", "gradient")


class IRValidationError(ValueError):
    """Raised by `validate(strict=True)` / `validate_or_raise()` with every problem found."""


# ------------------------------------------------------------------------------------ primitives


def _r3(value: Any) -> Any:
    """Round floats to 3 decimals, recursively — determinism, and no 0.30000000000000004 noise."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        rounded = round(value, 3)
        # -0.0 and 0.0 serialise differently; normalise so two runs agree.
        return 0.0 if rounded == 0 else rounded
    if isinstance(value, dict):
        return {k: _r3(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_r3(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class Box:
    """An axis-aligned rectangle in canvas px, origin top-left, before rotation."""

    x: float
    y: float
    w: float
    h: float

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def area(self) -> float:
        return self.w * self.h

    def intersect(self, other: Box) -> Box:
        """The overlap, or a zero-size box at the clamped origin when they do not overlap."""
        x = max(self.x, other.x)
        y = max(self.y, other.y)
        return Box(x, y, max(0.0, min(self.x2, other.x2) - x), max(0.0, min(self.y2, other.y2) - y))

    def overlap_fraction(self, other: Box) -> float:
        """Share of *this* box covered by `other` — the ≥ 60 % test in placeholder mapping."""
        return 0.0 if self.area <= 0 else self.intersect(other).area / self.area

    def union(self, other: Box) -> Box:
        x = min(self.x, other.x)
        y = min(self.y, other.y)
        return Box(x, y, max(self.x2, other.x2) - x, max(self.y2, other.y2) - y)

    def to_json(self) -> dict[str, float]:
        # Always floats: a box built from ints must serialise identically to one built from floats,
        # or a JSON round-trip changes the bytes and determinism dies on a technicality.
        return cast(dict[str, float], _r3({"x": float(self.x), "y": float(self.y), "w": float(self.w), "h": float(self.h)}))

    @staticmethod
    def from_json(data: dict[str, Any] | None) -> Box | None:
        if data is None:
            return None
        return Box(float(data["x"]), float(data["y"]), float(data["w"]), float(data["h"]))


@dataclass(frozen=True, slots=True)
class Canvas:
    """The slide's pixel canvas: physical size in inches × 96 (master brief §5)."""

    w: int
    h: int
    pxPerIn: int = 96

    def to_json(self) -> dict[str, int]:
        return {"w": self.w, "h": self.h, "pxPerIn": self.pxPerIn}

    @staticmethod
    def from_json(data: dict[str, Any]) -> Canvas:
        return Canvas(int(data["w"]), int(data["h"]), int(data.get("pxPerIn", 96)))

    @property
    def box(self) -> Box:
        return Box(0.0, 0.0, float(self.w), float(self.h))


@dataclass(slots=True)
class Slide:
    """Which slide this IR is, and which layout it sits on."""

    id: str
    title: str | None = None
    layoutId: str | None = None
    notes: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "layoutId": self.layoutId, "notes": self.notes}

    @staticmethod
    def from_json(data: dict[str, Any]) -> Slide:
        return Slide(
            id=data["id"],
            title=data.get("title"),
            layoutId=data.get("layoutId"),
            notes=data.get("notes"),
        )


@dataclass(slots=True)
class Fonts:
    """What the measurement forced, what the page actually used, and what got substituted."""

    forced: dict[str, str] = field(default_factory=dict)          # {"major": "Arial", "minor": "Arial"}
    used: list[str] = field(default_factory=list)
    substituted: list[dict[str, str]] = field(default_factory=list)  # [{"wanted": …, "got": …}]

    def to_json(self) -> dict[str, Any]:
        return {"forced": dict(self.forced), "used": list(self.used), "substituted": list(self.substituted)}

    @staticmethod
    def from_json(data: dict[str, Any] | None) -> Fonts:
        data = data or {}
        return Fonts(
            forced=dict(data.get("forced") or {}),
            used=list(data.get("used") or []),
            substituted=[dict(s) for s in data.get("substituted") or []],
        )


@dataclass(slots=True)
class Group:
    """A PowerPoint group. Membership is by id — the element list stays flat and in paint order."""

    id: str
    parent: str | None = None
    name: str | None = None
    box: Box | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent": self.parent,
            "name": self.name,
            "box": self.box.to_json() if self.box else None,
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> Group:
        return Group(
            id=data["id"],
            parent=data.get("parent"),
            name=data.get("name"),
            box=Box.from_json(data.get("box")),
        )


@dataclass(slots=True)
class Diagnostic:
    """Something the engine could not do natively, or did differently than the HTML asked."""

    level: Literal["error", "warn", "info"]
    source: str
    message: str
    elementId: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "source": self.source,
            "message": self.message,
            "elementId": self.elementId,
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> Diagnostic:
        return Diagnostic(
            level=data["level"],
            source=data.get("source", ""),
            message=data.get("message", ""),
            elementId=data.get("elementId"),
        )


# --------------------------------------------------------------------------------------- element


@dataclass(slots=True)
class Element:
    """One thing on the slide, in canvas px, in paint order.

    `id` and `z` are assigned at assembly by the HTML extractor (`assign_ids_and_z`); elements that
    come back from `expand_svg` carry `id=None, z=None` until they are spliced in.
    """

    kind: Kind
    box: Box
    id: str | None = None
    z: int | None = None
    rotation: float = 0.0
    opacity: float = 1.0
    clip: Box | None = None
    name: str | None = None
    group: str | None = None
    source: dict[str, Any] = field(default_factory=dict)
    placeholder: dict[str, Any] | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    # kind == "shape"
    geometry: dict[str, Any] | None = None
    fill: dict[str, Any] | None = None
    stroke: dict[str, Any] | None = None
    shadow: dict[str, Any] | None = None
    flipH: bool = False
    flipV: bool = False

    # kind == "text"
    paragraphs: list[dict[str, Any]] | None = None
    anchor: str | None = None
    writingMode: str | None = None
    wrap: bool = True
    transformCase: str | None = None

    # kind == "image"
    src: str | None = None            # also kind == "raster"
    fit: str | None = None
    crop: dict[str, float] | None = None
    radius: float = 0.0
    circle: bool = False

    # kind == "table"
    rows: int | None = None
    cols: int | None = None
    colWidthsPx: list[float] | None = None
    rowHeightsPx: list[float] | None = None
    cells: list[dict[str, Any]] | None = None

    # kind == "chart"
    spec: dict[str, Any] | None = None
    style: dict[str, Any] | None = None
    origin: str | None = None         # "authored" | "recognised"
    confidence: float | None = None
    plotRect: Box | None = None
    overlay: list[str] | None = None

    # kind == "raster"
    reason: str | None = None

    # -- serialisation ---------------------------------------------------------------------------
    # Only the fields the kind owns are written, so a shape's JSON carries no `paragraphs: null`
    # (see `_KIND_FIELDS` below the class).
    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "box": self.box.to_json(),
            "z": self.z,
        }
        if self.rotation:
            data["rotation"] = _r3(float(self.rotation))
        if self.opacity != 1.0:
            data["opacity"] = _r3(float(self.opacity))
        if self.clip is not None:
            data["clip"] = self.clip.to_json()
        if self.name is not None:
            data["name"] = self.name
        if self.group is not None:
            data["group"] = self.group
        if self.source:
            data["source"] = _r3(self.source)
        if self.placeholder is not None:
            data["placeholder"] = self.placeholder
        for name in _KIND_FIELDS[self.kind]:
            value = getattr(self, name)
            if isinstance(value, Box):
                value = value.to_json()
            elif name in _FLOAT_FIELDS and value is not None:
                value = float(value)
            data[name] = _r3(value)
        if self.extras:
            data["extras"] = _r3(self.extras)
        return data

    @staticmethod
    def from_json(data: dict[str, Any]) -> Element:
        kind = data["kind"]
        if kind not in KINDS:
            raise IRValidationError(f"unknown element kind {kind!r}")
        element = Element(
            kind=kind,
            box=Box.from_json(data["box"]),  # type: ignore[arg-type]
            id=data.get("id"),
            z=data.get("z"),
            rotation=float(data.get("rotation") or 0.0),
            opacity=float(data.get("opacity", 1.0)),
            clip=Box.from_json(data.get("clip")),
            name=data.get("name"),
            group=data.get("group"),
            source=dict(data.get("source") or {}),
            placeholder=data.get("placeholder"),
            extras=dict(data.get("extras") or {}),
        )
        for name in _KIND_FIELDS[kind]:
            if name not in data:
                continue
            value = data[name]
            if name == "plotRect":
                value = Box.from_json(value)
            setattr(element, name, value)
        return element

    # -- convenience -------------------------------------------------------------------------------
    def replace(self, **changes: Any) -> Element:
        """A copy with fields changed — classifiers rewrite elements instead of mutating them."""
        return replace(self, **changes)

    @property
    def is_native(self) -> bool:
        """True for everything the emitter turns into a real PowerPoint object (not a picture)."""
        return self.kind != "raster"


#: Kind-specific scalars that are conceptually floats, coerced on the way out for the same reason
#: `Box.to_json` coerces: an int and a float must not produce different bytes.
_FLOAT_FIELDS: frozenset[str] = frozenset({"radius", "confidence"})

#: Which extra fields each kind serialises. Module-level so `Element.to_json` stays a lookup.
_KIND_FIELDS: dict[str, tuple[str, ...]] = {
    "shape": ("geometry", "fill", "stroke", "shadow", "flipH", "flipV"),
    "text": ("paragraphs", "anchor", "writingMode", "wrap", "transformCase"),
    "image": ("src", "fit", "crop", "radius", "circle"),
    "table": ("rows", "cols", "colWidthsPx", "rowHeightsPx", "cells"),
    "chart": ("spec", "style", "origin", "confidence", "plotRect", "overlay"),
    "raster": ("src", "reason"),
}


# --------------------------------------------------------------- internal (never serialised) types


@dataclass(slots=True)
class SvgPlaceholder:
    """The HTML extractor's marker for an `<svg>` root awaiting `expand_svg`.

    Internal to the extractor: it never appears in a saved IR (WP1 splices the expansion in its place).
    """

    svg_id: str
    box: Box
    source: dict[str, Any] = field(default_factory=dict)
    group: str | None = None
    clip: Box | None = None


@dataclass(slots=True)
class SvgExpansion:
    """What `expand_svg` returns for one `<svg>` root.

    Elements arrive with `id=None, z=None` and local group ids prefixed `svg#<n>-`; WP1 splices them
    at the placeholder's paint position and renumbers the whole list with `assign_ids_and_z`.
    """

    elements: list[Element] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    assets_written: list[Path] = field(default_factory=list)


# --------------------------------------------------------------------------------------------- IR


@dataclass(slots=True)
class IR:
    """One slide, fully measured."""

    canvas: Canvas
    slide: Slide
    elements: list[Element] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    fonts: Fonts = field(default_factory=Fonts)
    version: int = VERSION

    # -- serialisation ---------------------------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        """The JSON-able dict, floats rounded to 3 decimals (determinism)."""
        return {
            "version": self.version,
            "canvas": self.canvas.to_json(),
            "slide": self.slide.to_json(),
            "fonts": self.fonts.to_json(),
            "groups": [g.to_json() for g in self.groups],
            "elements": [e.to_json() for e in self.elements],
            "diagnostics": [d.to_json() for d in self.diagnostics],
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> IR:
        version = int(data.get("version", VERSION))
        if version != VERSION:
            raise IRValidationError(f"IR version {version} but this engine speaks {VERSION}")
        return IR(
            canvas=Canvas.from_json(data["canvas"]),
            slide=Slide.from_json(data["slide"]),
            elements=[Element.from_json(e) for e in data.get("elements") or []],
            groups=[Group.from_json(g) for g in data.get("groups") or []],
            diagnostics=[Diagnostic.from_json(d) for d in data.get("diagnostics") or []],
            fonts=Fonts.from_json(data.get("fonts")),
            version=version,
        )

    def dumps(self) -> str:
        """Canonical JSON text: 2-space indent, UTF-8, stable key order. Two runs must match byte for byte."""
        return json.dumps(self.to_json(), indent=2, ensure_ascii=False) + "\n"

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.dumps(), encoding="utf-8")
        return path

    @staticmethod
    def load(path: Path) -> IR:
        return IR.from_json(json.loads(Path(path).read_text(encoding="utf-8")))

    # -- queries ---------------------------------------------------------------------------------
    def paint_sorted(self) -> list[Element]:
        """Elements back → front. Unnumbered elements keep their list position (stable sort)."""
        return sorted(self.elements, key=lambda e: (e.z is None, e.z if e.z is not None else 0))

    def by_id(self, element_id: str) -> Element | None:
        return next((e for e in self.elements if e.id == element_id), None)

    def of_kind(self, kind: str) -> list[Element]:
        return [e for e in self.elements if e.kind == kind]

    def counts(self) -> dict[str, int]:
        """`{"text": 12, "shape": 9, …}` — the shape of `expect.json`'s `kinds` block."""
        return {kind: sum(1 for e in self.elements if e.kind == kind) for kind in KINDS if any(
            e.kind == kind for e in self.elements
        )}

    def group_of(self, element: Element) -> Group | None:
        return next((g for g in self.groups if g.id == element.group), None)

    def add_diagnostic(self, level: str, source: str, message: str, element_id: str | None = None) -> Diagnostic:
        diagnostic = Diagnostic(level=level, source=source, message=message, elementId=element_id)  # type: ignore[arg-type]
        self.diagnostics.append(diagnostic)
        return diagnostic

    # -- validation ------------------------------------------------------------------------------
    def validate(self, *, check_files: bool = True, strict: bool = False) -> list[str]:
        """Every problem in this IR, as readable strings (empty list = valid).

        `check_files=False` skips the on-disk existence checks so a schema-only check can run where
        the assets are not mounted. `strict=True` raises `IRValidationError` instead of returning.
        """
        problems = _validate_ir(self, check_files=check_files)
        if strict and problems:
            raise IRValidationError("; ".join(problems))
        return problems

    def validate_or_raise(self, *, check_files: bool = True) -> None:
        self.validate(check_files=check_files, strict=True)


# --------------------------------------------------------------------------------------- assembly


def assign_ids_and_z(elements: Iterable[Element], *, start: int = 1) -> list[Element]:
    """Number a paint-ordered list: `id` = "e1"… and `z` = 0,1,2… in list order.

    WP1 calls this after splicing every `SvgExpansion` into the element list, so SVG-derived
    elements (which arrive with `id=None, z=None`) get identities consistent with the rest.
    Mutates in place and returns the list, because callers hold references to these elements.
    """
    numbered = list(elements)
    for index, element in enumerate(numbered):
        element.id = f"e{start + index}"
        element.z = index
    return numbered


# ------------------------------------------------------------------------------------- validation


def _finite(*values: Any) -> bool:
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)


def _is_hex6(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value)


def _is_alpha(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0.0 <= float(value) <= 1.0


def _check_box(box: Box | None, where: str, problems: list[str]) -> None:
    if box is None:
        problems.append(f"{where}: box is missing")
        return
    if not _finite(box.x, box.y, box.w, box.h):
        problems.append(f"{where}: box has a non-finite coordinate ({box})")
    elif box.w < 0 or box.h < 0:
        problems.append(f"{where}: box has negative extent ({box})")


def _check_fill(fill: Any, where: str, problems: list[str]) -> None:
    if fill is None:
        return
    if not isinstance(fill, dict) or "type" not in fill:
        problems.append(f"{where}: fill must be an object with a type")
        return
    kind = fill["type"]
    if kind not in FILL_TYPES:
        problems.append(f"{where}: unknown fill type {kind!r}")
        return
    if kind == "solid":
        if not _is_hex6(fill.get("color")):
            problems.append(f"{where}: solid fill colour must be 6 hex digits, got {fill.get('color')!r}")
        if "alpha" in fill and not _is_alpha(fill["alpha"]):
            problems.append(f"{where}: fill alpha out of range: {fill['alpha']!r}")
    if kind == "gradient":
        stops = fill.get("stops")
        if not isinstance(stops, list) or len(stops) < 2:
            problems.append(f"{where}: gradient needs at least 2 stops")
        else:
            for i, stop in enumerate(stops):
                if not _is_hex6(stop.get("color")):
                    problems.append(f"{where}: gradient stop {i} colour must be 6 hex digits")
                if not _finite(stop.get("pos")):
                    problems.append(f"{where}: gradient stop {i} needs a numeric pos")


def _check_stroke(stroke: Any, where: str, problems: list[str]) -> None:
    if stroke is None:
        return
    if not isinstance(stroke, dict):
        problems.append(f"{where}: stroke must be an object or null")
        return
    if not _is_hex6(stroke.get("color")):
        problems.append(f"{where}: stroke colour must be 6 hex digits, got {stroke.get('color')!r}")
    if not _finite(stroke.get("width")):
        problems.append(f"{where}: stroke width must be a number")
    if "alpha" in stroke and not _is_alpha(stroke["alpha"]):
        problems.append(f"{where}: stroke alpha out of range: {stroke['alpha']!r}")
    dash = stroke.get("dash")
    if isinstance(dash, list) and (not dash or not _finite(*dash)):
        problems.append(f"{where}: stroke dash must be a non-empty list of numbers")


def _check_geometry(element: Element, where: str, problems: list[str]) -> None:
    geometry = element.geometry
    if not isinstance(geometry, dict) or "type" not in geometry:
        problems.append(f"{where}: shape needs a geometry object with a type")
        return
    gtype = geometry["type"]
    if gtype not in GEOMETRY_TYPES:
        problems.append(f"{where}: unknown geometry type {gtype!r}")
        return
    if gtype == "custom":
        path = geometry.get("path")
        if not isinstance(path, list) or not path:
            problems.append(f"{where}: custom geometry needs a non-empty path")
            return
        if not isinstance(path[0], (list, tuple)) or path[0][0] != "M":
            problems.append(f"{where}: custom path must start with M")
        for index, segment in enumerate(path):
            if not isinstance(segment, (list, tuple)) or not segment:
                problems.append(f"{where}: path segment {index} is not a command list")
                continue
            command = segment[0]
            if command not in PATH_COMMANDS:
                problems.append(f"{where}: path segment {index} uses {command!r}; only M L C Q Z are allowed")
            elif len(segment) - 1 != PATH_COMMANDS[command]:
                problems.append(
                    f"{where}: path segment {index} ({command}) takes {PATH_COMMANDS[command]} numbers, "
                    f"got {len(segment) - 1}"
                )
            elif not _finite(*segment[1:]):
                problems.append(f"{where}: path segment {index} has a non-finite coordinate")
        rule = geometry.get("fillRule")
        if rule is not None and rule not in ("nonzero", "evenodd"):
            problems.append(f"{where}: fillRule must be nonzero or evenodd, got {rule!r}")
    if gtype in ("line", "polyline"):
        points = geometry.get("points")
        if not isinstance(points, list) or len(points) < 2:
            problems.append(f"{where}: {gtype} needs at least 2 points")
        elif any(not isinstance(p, (list, tuple)) or len(p) != 2 or not _finite(*p) for p in points):
            problems.append(f"{where}: {gtype} points must be [x, y] pairs of numbers")
    if gtype in ("blockArc", "pie", "chord"):
        arc = geometry.get("arc")
        if not isinstance(arc, dict):
            problems.append(f"{where}: {gtype} needs an arc object")
        else:
            missing = [k for k in ("cx", "cy", "rOuter", "startDeg", "endDeg") if not _finite(arc.get(k))]
            if missing:
                problems.append(f"{where}: arc is missing numeric {', '.join(missing)}")
            elif arc.get("rOuter", 0) <= 0:
                problems.append(f"{where}: arc rOuter must be positive")
            elif gtype == "blockArc" and not _finite(arc.get("rInner")):
                problems.append(f"{where}: blockArc needs a numeric rInner")
    if gtype == "roundRect":
        radius = geometry.get("radius")
        if radius is not None and (
            not isinstance(radius, dict) or not _finite(*[radius.get(k, 0) for k in ("tl", "tr", "br", "bl")])
        ):
            problems.append(f"{where}: roundRect radius must be {{tl,tr,br,bl}} numbers")


def _check_paragraphs(element: Element, where: str, problems: list[str]) -> None:
    paragraphs = element.paragraphs
    if not isinstance(paragraphs, list) or not paragraphs:
        problems.append(f"{where}: text needs a non-empty paragraphs list")
        return
    for pi, paragraph in enumerate(paragraphs):
        at = f"{where} p{pi}"
        if not isinstance(paragraph, dict):
            problems.append(f"{at}: paragraph must be an object")
            continue
        align = paragraph.get("align")
        if align is not None and align not in ALIGNMENTS:
            problems.append(f"{at}: unknown align {align!r}")
        lines = paragraph.get("lines")
        if not isinstance(lines, list) or not lines:
            problems.append(f"{at}: paragraph needs a non-empty lines list")
            continue
        for li, line in enumerate(lines):
            line_at = f"{at} l{li}"
            if not isinstance(line, dict):
                problems.append(f"{line_at}: line must be an object")
                continue
            if line.get("box") is not None:
                _check_box(Box.from_json(line["box"]), line_at, problems)
            runs = line.get("runs")
            if not isinstance(runs, list) or not runs:
                problems.append(f"{line_at}: line needs a non-empty runs list")
                continue
            for ri, run in enumerate(runs):
                run_at = f"{line_at} r{ri}"
                if not isinstance(run, dict):
                    problems.append(f"{run_at}: run must be an object")
                    continue
                if not isinstance(run.get("text"), str):
                    problems.append(f"{run_at}: run needs text")
                if not _finite(run.get("sizePx")):
                    problems.append(f"{run_at}: run needs a numeric sizePx")
                if not _is_hex6(run.get("color")):
                    problems.append(f"{run_at}: run colour must be 6 hex digits, got {run.get('color')!r}")
                if "alpha" in run and not _is_alpha(run["alpha"]):
                    problems.append(f"{run_at}: run alpha out of range")
    if element.anchor is not None and element.anchor not in ANCHORS:
        problems.append(f"{where}: unknown anchor {element.anchor!r}")


def _check_table(element: Element, where: str, problems: list[str]) -> None:
    if not isinstance(element.rows, int) or not isinstance(element.cols, int):
        problems.append(f"{where}: table needs integer rows and cols")
        return
    if element.rows <= 0 or element.cols <= 0:
        problems.append(f"{where}: table needs positive rows and cols")
    widths, heights = element.colWidthsPx, element.rowHeightsPx
    if not isinstance(widths, list) or len(widths) != element.cols or not _finite(*widths):
        problems.append(f"{where}: colWidthsPx must hold {element.cols} numbers")
    if not isinstance(heights, list) or len(heights) != element.rows or not _finite(*heights):
        problems.append(f"{where}: rowHeightsPx must hold {element.rows} numbers")
    if not isinstance(element.cells, list) or not element.cells:
        problems.append(f"{where}: table needs cells")
        return
    for index, cell in enumerate(element.cells):
        at = f"{where} cell{index}"
        if not isinstance(cell, dict):
            problems.append(f"{at}: cell must be an object")
            continue
        r, c = cell.get("r"), cell.get("c")
        if not isinstance(r, int) or not isinstance(c, int) or not (0 <= r < element.rows) or not (0 <= c < element.cols):
            problems.append(f"{at}: cell (r={r}, c={c}) is outside the {element.rows}×{element.cols} grid")


def _check_chart(element: Element, where: str, problems: list[str]) -> None:
    if not isinstance(element.spec, dict):
        problems.append(f"{where}: chart needs a spec object")
    else:
        problems.extend(f"{where}: {problem}" for problem in charts_spec.validate(element.spec))
    if element.origin not in ("authored", "recognised"):
        problems.append(f"{where}: chart origin must be authored or recognised, got {element.origin!r}")
    if element.confidence is not None and not (0.0 <= float(element.confidence) <= 1.0):
        problems.append(f"{where}: chart confidence out of range: {element.confidence!r}")
    if element.plotRect is not None:
        _check_box(element.plotRect, f"{where} plotRect", problems)


def _validate_ir(ir: IR, *, check_files: bool) -> list[str]:
    problems: list[str] = []

    if ir.canvas.w <= 0 or ir.canvas.h <= 0:
        problems.append(f"canvas must be positive, got {ir.canvas.w}×{ir.canvas.h}")
    if not ir.slide.id:
        problems.append("slide.id is required")

    # groups form a tree
    group_ids = [g.id for g in ir.groups]
    duplicate_groups = {g for g in group_ids if group_ids.count(g) > 1}
    if duplicate_groups:
        problems.append(f"duplicate group ids: {sorted(duplicate_groups)}")
    known_groups = set(group_ids)
    for group in ir.groups:
        if group.parent is not None and group.parent not in known_groups:
            problems.append(f"group {group.id}: parent {group.parent!r} does not exist")
    for group in ir.groups:  # no cycles
        seen, cursor = {group.id}, group.parent
        while cursor is not None:
            if cursor in seen:
                problems.append(f"group {group.id}: parent chain is a cycle")
                break
            seen.add(cursor)
            cursor = next((g.parent for g in ir.groups if g.id == cursor), None)

    seen_ids: set[str] = set()
    previous_z: int | None = None
    diagnostic_element_ids = {d.elementId for d in ir.diagnostics if d.elementId}

    for index, element in enumerate(ir.elements):
        where = f"element[{index}]{'/' + element.id if element.id else ''}"
        if element.kind not in KINDS:
            problems.append(f"{where}: unknown kind {element.kind!r}")
            continue
        if element.id is None:
            problems.append(f"{where}: id is required in a saved IR (assign_ids_and_z was not run)")
        elif element.id in seen_ids:
            problems.append(f"{where}: duplicate id {element.id!r}")
        else:
            seen_ids.add(element.id)
        if element.z is None:
            problems.append(f"{where}: z is required in a saved IR")
        elif previous_z is not None and element.z <= previous_z:
            problems.append(f"{where}: z must strictly increase (got {element.z} after {previous_z})")
        if element.z is not None:
            previous_z = element.z

        _check_box(element.box, where, problems)
        if not _is_alpha(element.opacity):
            problems.append(f"{where}: opacity out of range: {element.opacity!r}")
        if not _finite(element.rotation):
            problems.append(f"{where}: rotation must be a number")
        if element.group is not None and element.group not in known_groups:
            problems.append(f"{where}: group {element.group!r} does not exist")
        if element.clip is not None:
            _check_box(element.clip, f"{where} clip", problems)
            canvas = ir.canvas
            if (
                element.clip.x < -1 or element.clip.y < -1
                or element.clip.x2 > canvas.w + 1 or element.clip.y2 > canvas.h + 1
            ):
                problems.append(f"{where}: clip {element.clip} lies outside the canvas")

        if element.kind == "shape":
            _check_geometry(element, where, problems)
            _check_fill(element.fill, where, problems)
            _check_stroke(element.stroke, where, problems)
        elif element.kind == "text":
            _check_paragraphs(element, where, problems)
        elif element.kind == "table":
            _check_table(element, where, problems)
        elif element.kind == "chart":
            _check_chart(element, where, problems)
        elif element.kind in ("image", "raster"):
            if not element.src:
                problems.append(f"{where}: {element.kind} needs a src")
            elif check_files and not Path(element.src).exists():
                problems.append(f"{where}: {element.kind} src does not exist: {element.src}")
            if element.kind == "image" and element.fit is not None and element.fit not in ("contain", "cover", "fill"):
                problems.append(f"{where}: unknown image fit {element.fit!r}")
            if element.kind == "raster":
                if not element.reason:
                    problems.append(f"{where}: raster needs a reason")
                if element.id and element.id not in diagnostic_element_ids:
                    problems.append(f"{where}: raster has no matching diagnostic")

    for index, diagnostic in enumerate(ir.diagnostics):
        if diagnostic.level not in DIAGNOSTIC_LEVELS:
            problems.append(f"diagnostics[{index}]: unknown level {diagnostic.level!r}")

    return problems
