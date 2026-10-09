"""Static lint on slide HTML — the rules of `03-AUTHORING-CONTRACT.md` §Lint levels.

No browser, no network, no export: html5lib parses the document, tinycss2 scans the declarations in
`<style>` blocks and `style` attributes, and `charts_spec.validate` judges every `data-chart`. It
runs on every slide save, so it has a budget (< 50 ms on the client reference slides) and it never raises.

**Policy, decided by Peter on 2026-09-18 (hand-off §10.3): warn only.** A lint *error* never blocks a
Path A export. It means "Path A will approximate or drop something here" — WP8's readiness panel
turns that into "use the Claude path for this export". Nothing in `engine/` raises or exits non-zero
because of a finding, which is why this module returns a report and has no `strict` mode.

Two design notes worth keeping:

* **Selectors are resolved, not guessed.** Geometry and font rules need to know what applies to an
  element, and slides put half of it in a class rule and half in a `style` attribute. A deliberately
  small cascade (tag / `.class` / `#id` selectors, source order, inline last) is enough for the
  static rules and cannot produce the false positives a "search the whole stylesheet" scan would.
* **Unknown SVG elements warn rather than error.** An error routes the whole slide to the Claude
  path; an unrecognised `<animate>` only means the engine will drop one decoration. Errors are
  reserved for things that make the fast path lie about what it produced.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

# a type for html5lib's tree; parses nothing (bandit B405)
from xml.etree.ElementTree import Element as EtElement  # nosec B405

import html5lib
import tinycss2

from app.engine import charts_spec
from app.engine.manifest import Manifest
from app.engine.reports import LintFinding, LintReport

# ------------------------------------------------------------------------------- the vocabulary

#: Everything the contract allows in the HTML tree, plus the document scaffolding every slide has.
ALLOWED_HTML_TAGS: frozenset[str] = frozenset({
    "html", "head", "body", "meta", "title", "style", "link", "base",
    # text
    "b", "strong", "i", "em", "u", "s", "span", "a", "sup", "sub", "br", "mark",
    # blocks
    "div", "p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
    # tables
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
    # media and the notes block
    "img", "svg", "aside",
})

#: SVG elements the extractor turns into native PowerPoint geometry or text.
ALLOWED_SVG_TAGS: frozenset[str] = frozenset({
    "svg", "rect", "circle", "ellipse", "line", "polyline", "polygon", "path", "g", "text",
    "tspan", "defs", "lineargradient", "radialgradient", "stop", "marker", "use", "symbol",
    "title", "desc", "style", "clippath", "metadata",
})

#: SVG elements the contract declares raster-only: they come back as a picture with a diagnostic.
RASTER_SVG_TAGS: frozenset[str] = frozenset({
    "filter", "mask", "pattern", "textpath", "foreignobject", "image",
})

#: CSS values that force a raster (contract §Boxes and decoration).
RASTER_CSS_VALUES: tuple[tuple[str, str], ...] = (
    ("conic-gradient", "conic-gradient"),
    ("image-set(", "image-set()"),
    ("element(", "element()"),
)

#: Properties whose mere presence forces a raster, whatever the value.
RASTER_CSS_PROPERTIES: dict[str, str] = {
    "filter": "filter",
    "backdrop-filter": "backdrop-filter",
    "mix-blend-mode": "mix-blend-mode",
    "background-blend-mode": "background-blend-mode",
    "mask": "mask",
    "mask-image": "mask-image",
    "-webkit-mask": "-webkit-mask",
    "-webkit-mask-image": "-webkit-mask-image",
    "-webkit-text-stroke": "-webkit-text-stroke",
    "-webkit-text-stroke-width": "-webkit-text-stroke",
    "writing-mode": "writing-mode",
}

#: Font families that name a class of fonts rather than a face; never checked against the theme.
GENERIC_FAMILIES: frozenset[str] = frozenset({
    "serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui", "ui-serif",
    "ui-sans-serif", "ui-monospace", "ui-rounded", "math", "emoji", "fangsong",
    "inherit", "initial", "unset", "revert", "revert-layer",
})

#: The only remote origin the contract tolerates (as a warning): Google Fonts.
GOOGLE_FONT_HOSTS: tuple[str, ...] = ("fonts.googleapis.com", "fonts.gstatic.com")

#: Icon-font family names. Their glyphs live in the Private Use Area of a font that is not installed
#: at export time, so measurement substitutes a text face and the icon renders as a wrong letter or
#: tofu — a generic `font-not-theme` warning does not tell the author what actually broke or how to
#: fix it. Matched as a substring of the lower-cased family, so version suffixes ("Font Awesome 6
#: Free") still hit.
ICON_FONT_HINTS: tuple[str, ...] = (
    "font awesome", "fontawesome", "material icons", "material symbols", "glyphicon",
    "bootstrap-icons", "bootstrap icons", "ionicons", "remixicon", "lucide", "phosphor",
)

#: Slides address project assets through the app's URL shape; anything else is not an asset.
ASSET_URL_RE = re.compile(r"^/api/projects/[^/]+/assets/[^/]+$")

_URL_RE = re.compile(r"url\(\s*(?P<q>['\"]?)(?P<url>[^'\")]+)(?P=q)\s*\)", re.IGNORECASE)
_PX_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*px\s*$", re.IGNORECASE)
_NUMBER_TEXT_RE = re.compile(r"^[^\d+-]{0,3}[+-]?\d[\d,.\s]*\s*[%a-zA-Z€$£]{0,4}$")
_SVG_NS = "{http://www.w3.org/2000/svg}"


# ------------------------------------------------------------------------------------- plumbing


@dataclass(slots=True)
class _Rule:
    """One `selector { … }` block, flattened to the parts the static rules can resolve."""

    tags: frozenset[str]
    classes: frozenset[str]
    ids: frozenset[str]
    universal: bool
    declarations: list[tuple[str, str]]
    order: int
    #: `"before"`/`"after"` when the prelude names that pseudo-element (either colon form), else None.
    pseudo: str | None = None


@dataclass(slots=True)
class _Doc:
    """A parsed slide: the tree, the stylesheet, and the raw text for line numbers."""

    source: str
    root: EtElement
    rules: list[_Rule] = field(default_factory=list)
    findings: list[LintFinding] = field(default_factory=list)
    seen: set[tuple[str, str, int | None]] = field(default_factory=set)
    #: `_matching_declarations`' answers, keyed by what it reads.
    matched: dict[tuple[Any, ...], list[tuple[str, str]]] = field(default_factory=dict)

    def add(self, level: str, rule: str, message: str, snippet: str | None = None) -> None:
        """Record a finding once. The same rule with the same message on the same line is one problem."""
        line = self.line_of(snippet) if snippet else None
        key = (rule, message, line)
        if key in self.seen:
            return
        self.seen.add(key)
        self.findings.append(
            LintFinding(level=level, rule=rule, message=message, line=line,  # type: ignore[arg-type]
                        snippet=(snippet[:120] if snippet else None))
        )

    def line_of(self, snippet: str) -> int | None:
        """1-based line of the first occurrence of `snippet` in the source, when it is findable."""
        if not snippet:
            return None
        index = self.source.find(snippet[:80])
        if index < 0:
            return None
        return self.source.count("\n", 0, index) + 1


def _tag_of(element: EtElement) -> tuple[str, bool]:
    """`(lower-case local name, is it in the SVG namespace)`. Comments and PIs come back as ("", False)."""
    tag = element.tag
    if not isinstance(tag, str):
        return "", False
    if tag.startswith(_SVG_NS):
        return tag[len(_SVG_NS):].lower(), True
    if tag.startswith("{"):
        return tag.rpartition("}")[2].lower(), False
    return tag.lower(), False


def _walk(element: EtElement, in_svg: bool = False) -> Iterator[tuple[EtElement, str, bool]]:
    """Every element with its local tag name and whether it sits inside an `<svg>` root."""
    tag, is_svg_ns = _tag_of(element)
    inside = in_svg or is_svg_ns or tag == "svg"
    yield element, tag, inside
    for child in element:
        yield from _walk(child, inside)


def _attributes(element: EtElement) -> dict[str, str]:
    """Attributes with namespaces stripped and names lower-cased (`xlink:href` → `href`)."""
    out: dict[str, str] = {}
    for name, value in element.attrib.items():
        key = name.rpartition("}")[2] if name.startswith("{") else name
        out[key.lower()] = value
    return out


def _declarations(css: str) -> list[tuple[str, str]]:
    """`[(property, value text)]` from a declaration list, lower-cased property names."""
    parse = getattr(tinycss2, "parse_blocks_contents", None) or tinycss2.parse_declaration_list
    out: list[tuple[str, str]] = []
    for node in parse(css, skip_comments=True, skip_whitespace=True):
        if node.type == "declaration":
            out.append((node.lower_name, tinycss2.serialize(node.value).strip()))
    return out


_SELECTOR_PART_RE = re.compile(r"(?P<kind>[.#]?)(?P<name>[A-Za-z_][\w-]*)")


def _selector_parts(prelude: str) -> tuple[frozenset[str], frozenset[str], frozenset[str], bool]:
    """Tags, classes and ids named anywhere in a selector list, plus whether `*` appears.

    Deliberately coarse: it over-approximates which elements a rule may hit. The rules that consume
    it (geometry, fonts) are then only ever *more* informed than a scan of the whole stylesheet
    would be, and an inline `style` attribute still has the last word.
    """
    universal = "*" in prelude
    # Attribute selectors and pseudo-classes carry names that are not element names.
    cleaned = re.sub(r"\[[^\]]*\]", " ", prelude)
    cleaned = re.sub(r"::?[A-Za-z-]+(\([^)]*\))?", " ", cleaned)
    tags: set[str] = set()
    classes: set[str] = set()
    ids: set[str] = set()
    for match in _SELECTOR_PART_RE.finditer(cleaned):
        kind, name = match.group("kind"), match.group("name").lower()
        if kind == ".":
            classes.add(name)
        elif kind == "#":
            ids.add(name)
        else:
            tags.add(name)
    return frozenset(tags), frozenset(classes), frozenset(ids), universal


def _parse_rules(css: str) -> list[Any]:
    """Top-level rules, with `@media` blocks flattened in (their content still applies to the slide)."""
    out: list[Any] = []
    for node in tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True):
        out.append(node)
        if node.type == "at-rule" and node.lower_at_keyword in ("media", "supports", "layer") and node.content:
            out.extend(_parse_rules(tinycss2.serialize(node.content)))
    return out


def _collect_rules(document: _Doc) -> None:
    """Flatten every `<style>` block into `_Rule`s in source order, linting the at-rules on the way."""
    order = 0
    for element, tag, _ in _walk(document.root):
        if tag != "style":
            continue
        css = "".join(element.itertext())
        for node in _parse_rules(css):
            if node.type == "qualified-rule":
                prelude = tinycss2.serialize(node.prelude).strip()
                # Read before `_selector_parts` strips pseudo-elements from the selector.
                named = re.search(r"::?(before|after)\b", prelude, re.IGNORECASE)
                pseudo = named.group(1).lower() if named else None
                tags, classes, ids, universal = _selector_parts(prelude)
                declarations = _declarations(tinycss2.serialize(node.content))
                document.rules.append(_Rule(tags, classes, ids, universal, declarations, order, pseudo))
                order += 1
                if pseudo:
                    # Once per rule, not per element it matches: the picture is in the rule itself.
                    for name, value in declarations:
                        if name == "content" and re.search(r"url\(|image-set\(|gradient\(", value, re.IGNORECASE):
                            document.add("warn", "pseudo-content-image",
                                         f"an image drawn by ::{pseudo} is left out ({prelude})",
                                         f"content:{value}"[:120])
            elif node.type == "at-rule":
                _lint_at_rule(document, node)


def _rule_applies(rule: _Rule, tag: str, classes: set[str] | frozenset[str], element_id: str) -> bool:
    """Whether `rule` reaches an element with this tag, lower-cased classes and id."""
    return bool(
        (rule.classes and classes & rule.classes)
        or (rule.ids and element_id in rule.ids)
        or (not rule.classes and not rule.ids and tag in rule.tags)
        or (rule.universal and not rule.classes and not rule.ids and not rule.tags)
    )


def _matching_declarations(document: _Doc, tag: str, attributes: dict[str, str]) -> list[tuple[str, str]]:
    """The declarations that apply to this element: stylesheet rules in order, `style` attribute last.

    Memoised per document on what the match reads (three passes ask for every element); the list
    returned is shared, so callers read it and never change it."""
    key = (tag, attributes.get("class"), attributes.get("id"), attributes.get("style"))
    cached = document.matched.get(key)
    if cached is None:
        cached = document.matched[key] = _match_declarations(document, tag, attributes)
    return cached


def _match_declarations(document: _Doc, tag: str, attributes: dict[str, str]) -> list[tuple[str, str]]:
    classes = {c.lower() for c in (attributes.get("class") or "").split()}
    element_id = (attributes.get("id") or "").lower()
    applied: list[tuple[str, str]] = []
    for rule in document.rules:
        if _rule_applies(rule, tag, classes, element_id):
            applied.extend(rule.declarations)
    if attributes.get("style"):
        applied.extend(_declarations(attributes["style"]))
    return applied


# ---------------------------------------------------------------------------------------- rules


def _is_remote(url: str) -> bool:
    return url.strip().lower().startswith(("http://", "https://", "//"))


def _is_google_font(url: str) -> bool:
    return any(host in url.lower() for host in GOOGLE_FONT_HOSTS)


def _lint_url(document: _Doc, url: str, *, where: str, snippet: str) -> None:
    """One URL, judged by the contract: Google Fonts warns, any other remote resource is an error."""
    if not url or url.strip().lower().startswith(("data:", "#")):
        return
    if not _is_remote(url):
        return
    if _is_google_font(url):
        document.add("warn", "google-fonts",
                     f"{where} loads a Google Font ({url}); measurement forces the theme fonts anyway",
                     snippet)
    else:
        document.add("error", "remote-resource",
                     f"{where} loads a remote resource ({url}); only project assets and installed "
                     f"fonts exist at export time; by policy the export blocks remote resources (or "
                     f"rasterises the browser's rendering when SLIDE_ENGINE_REMOTE_RESOURCES=raster)",
                     snippet)


def _first_url(text: str) -> str | None:
    match = _URL_RE.search(text)
    return match.group("url") if match else None


def _urls_in(text: str) -> list[str]:
    return [m.group("url") for m in _URL_RE.finditer(text)]


def _lint_at_rule(document: _Doc, node: Any) -> None:
    """`@import` and `@font-face` are the two at-rules that can reach off the machine."""
    keyword = node.lower_at_keyword
    prelude = tinycss2.serialize(node.prelude).strip()
    if keyword == "import":
        url = _first_url(prelude) or prelude.strip("\"' ")
        _lint_url(document, url, where="@import", snippet=f"@import {prelude}"[:120])
    elif keyword == "font-face" and node.content:
        for name, value in _declarations(tinycss2.serialize(node.content)):
            if name == "src":
                for url in _urls_in(value):
                    _lint_url(document, url, where="@font-face src", snippet=value[:120])


def _has_blur(text_shadow: str) -> bool:
    """A text-shadow with a third length carries a blur radius; two lengths is a hard offset.

    Colours are removed first — `rgba(0, 0, 0, .4)` contributes four numbers that are not lengths,
    and a unitless `0` offset is legal, so counting "numbers that are not part of a colour" is the
    only reading that gets both `0 1px 3px rgba(…)` and `1px 1px black` right.
    """
    cleaned = re.sub(r"(?:rgba?|hsla?|color-mix|var)\([^)]*\)", " ", text_shadow, flags=re.IGNORECASE)
    cleaned = re.sub(r"#[0-9a-fA-F]{3,8}", " ", cleaned)
    return len(re.findall(r"-?\d*\.?\d+(?:px|pt|em|rem|%)?", cleaned)) >= 3


def _is_mirror_scale(argument: str) -> bool:
    """`scale(-1)` / `scale(-1, 1)` map onto flipH/flipV; any other factor does not."""
    parts = [p.strip() for p in argument.split(",") if p.strip()]
    try:
        return bool(parts) and all(abs(abs(float(p)) - 1.0) < 1e-6 for p in parts)
    except ValueError:
        return False


def _lint_declaration(document: _Doc, name: str, value: str, origin: str,
                      manifest: Manifest | None = None) -> None:
    """The property scan: raster-only constructs, ellipsis, remote urls, unsupported transforms."""
    lowered = value.lower()

    if name == "font-weight" and manifest is not None:
        _lint_font_weight(document, lowered.strip(), manifest, origin)

    if name in RASTER_CSS_PROPERTIES and lowered not in ("none", "normal", "initial", "unset", ""):
        document.add("warn", "raster-css",
                     f"{RASTER_CSS_PROPERTIES[name]} on {origin} is raster-only: the element is "
                     f"exported as an image", f"{name}:{value}"[:120])
    for needle, label in RASTER_CSS_VALUES:
        if needle in lowered:
            document.add("warn", "raster-css",
                         f"{label} on {origin} is raster-only: the element is exported as an image",
                         f"{name}:{value}"[:120])

    if name == "text-overflow" and "ellipsis" in lowered:
        document.add("warn", "text-overflow",
                     "text-overflow: ellipsis — the export keeps the visible text and the ellipsis the "
                     "browser drew; the hidden words are not in the deck — shorten the copy",
                     f"{name}:{value}"[:120])
    if name in ("-webkit-line-clamp", "line-clamp") and lowered not in ("none", "initial", "unset", ""):
        document.add("warn", "text-overflow",
                     f"{name}: the export keeps the visible text and the ellipsis the browser drew; the "
                     f"hidden words are not in the deck — shorten the copy", f"{name}:{value}"[:120])

    if name == "font-variant-numeric" and lowered.strip() not in ("normal", "initial", "unset", "inherit", ""):
        document.add("warn", "type-features",
                     f"font-variant-numeric: {value.strip()} on {origin} — PowerPoint cannot ask for figure "
                     f"styles, so the digits draw in the font's default forms (Georgia's are old-style); "
                     f"the preview and the export both show that", f"{name}:{value}"[:120])
    if name == "font-feature-settings" and lowered.strip() not in ("normal", "initial", "unset", "inherit", ""):
        document.add("warn", "type-features",
                     f"font-feature-settings on {origin} — PowerPoint cannot ask for OpenType features, "
                     f"so the text draws in the font's default forms; the preview and the export both "
                     f"show that", f"{name}:{value}"[:120])

    if name in ("animation", "animation-iteration-count") and "infinite" in lowered:
        document.add("warn", "animation",
                     "a looping animation is exported at its un-animated state",
                     f"{name}:{value}"[:120])

    if name == "text-shadow" and lowered not in ("none", "") and _has_blur(lowered):
        document.add("warn", "raster-css",
                     "text-shadow with a blur radius is raster-only on text; that text box is "
                     "exported as an image", f"{name}:{value}"[:120])

    if name in ("box-shadow", "-webkit-box-shadow") and "inset" in lowered:
        document.add("warn", "raster-css",
                     "an inset box-shadow is raster-only (PowerPoint has only outer shadows)",
                     f"{name}:{value}"[:120])

    if name == "transform" and lowered not in ("none", ""):
        unsupported = [
            function for function in re.findall(r"([a-z0-9-]+)\(", lowered)
            if function not in ("rotate", "scale", "scalex", "scaley", "translate",
                                "translatex", "translatey")
        ]
        if any(not _is_mirror_scale(s) for s in re.findall(r"scale[xy]?\(([^)]*)\)", lowered)):
            unsupported.append("scale")
        if unsupported:
            document.add("warn", "raster-css",
                         f"CSS transform {value!r} goes beyond rotate()/scale(-1): the element is "
                         f"exported as an image", f"{name}:{value}"[:120])

    # Colour syntaxes that can name a colour outside sRGB: the extractor clamps each channel while
    # the browser gamut-maps, so a saturated one can differ by a few units (14-WPE-paint §1).
    # `color-mix()`, `hsl()`, `hwb()` and `color(srgb …)` are sRGB and exact, so they say nothing.
    if re.search(r"(?<![\w-])(?:ok)?(?:lab|lch)\(|color\(\s*(?:display-p3|rec2020|a98-rgb|prophoto-rgb|xyz)",
                 lowered):
        document.add("info", "colour-gamut",
                     "a colour that may be outside the range PowerPoint can show; the nearest colour "
                     "is used", f"{name}:{value}"[:120])

    if name in ("background-clip", "-webkit-background-clip") and re.search(r"(?<![\w-])text(?![\w-])", lowered):
        document.add("info", "gradient-text",
                     "gradient text is exported as a gradient text fill; previews draw its first colour",
                     f"{name}:{value}"[:120])

    if name.startswith("background") and "url(" in lowered:
        for url in _urls_in(value):
            _lint_url(document, url, where=f"{name} on {origin}", snippet=f"{name}:{value}"[:120])
    if name in ("background", "background-image") and lowered.count("url(") + lowered.count("gradient(") > 1:
        document.add("warn", "raster-css",
                     "multiple background layers are raster-only; use one background per element",
                     f"{name}:{value}"[:120])


def _icon_font_family(value: str) -> str | None:
    """The first icon-font family a `font-family` value names (lower-cased), else None."""
    families = [f.strip().strip("\"'").lower() for f in value.split(",") if f.strip()]
    return next((family for family in families if any(hint in family for hint in ICON_FONT_HINTS)), None)


def _lint_icon_font(document: _Doc, value: str, origin: str) -> None:
    """An icon font named in a `font-family` — it will not survive the export as text.

    Independent of the manifest (it is wrong on every master), and reported once per declaration with
    the fix: an icon has to be an inline `<svg>` for the emitter to turn it into native shapes.
    """
    family = _icon_font_family(value)
    if family is not None:
        document.add("warn", "icon-font",
                     f"{origin} uses the icon font {family!r}; its glyphs are not installed at "
                     f"export time and export as a wrong letter or a blank box — draw the icon as "
                     f"an inline <svg> so it becomes native shapes", f"font-family:{value}"[:120])


#: Weights every family is expected to have (regular and bold); anything else needs a static face.
_BASE_WEIGHTS: frozenset[int] = frozenset({400, 700})

#: How far the nearest static face may be from the asked weight before the design and the export
#: stop drawing the same thing (the browser and PowerPoint both take the nearest face).
_WEIGHT_TOLERANCE = 50


def _lint_font_weight(document: _Doc, value: str, manifest: Manifest, origin: str) -> None:
    """A numeric weight neither theme family ships as a static face *on this machine*.

    The contract says 300/500/600 only when the font ships those faces (03 §Text). The export names
    the face the browser drew (`emit.text.emitted_face`), so where no static face lies within
    `_WEIGHT_TOLERANCE` the slide shows a weight PowerPoint will not — Peter #9: judged against the
    faces installed where the server runs, which is where the export runs. `normal`/`bold` and the
    relative `lighter`/`bolder` are not checked.
    """
    from app.engine.emit.text import face_for_exact

    if not re.fullmatch(r"\d{1,3}", value):
        return
    weight = int(value)
    if weight in _BASE_WEIGHTS:
        return
    families: list[str] = []
    for slot in ("major", "minor"):
        family = str(manifest.fonts.get(slot) or "").strip()
        if family and family not in families:
            families.append(family)
    if not families:
        return
    nearest = []
    for family in families:
        face = face_for_exact(family, weight)
        if face is not None and not face.variable and abs(face.weight - weight) <= _WEIGHT_TOLERANCE:
            return
        if face is not None:
            nearest.append((abs(face.weight - weight), families.index(family) != len(families) - 1, face))
    names = " nor ".join(families)
    subject = f"neither {names} ships" if len(families) > 1 else f"{families[0]} does not ship"
    if nearest:
        face = min(nearest, key=lambda item: (item[0], item[1]))[2]
        outcome = f"the export uses the nearest ({face.typographic_family} {face.subfamily})"
    else:
        outcome = "no theme font is installed here, so the viewer's machine decides"
    document.add("warn", "font-weight",
                 f"font-weight {weight} on {origin} — {subject} a static {weight} face here; {outcome}",
                 f"font-weight:{value}"[:120])


def _lint_fonts(document: _Doc, value: str, manifest: Manifest | None, origin: str) -> None:
    """A declared family that is neither generic nor a theme font is a substitution waiting to happen."""
    if manifest is None:
        return
    theme = {str(f).strip().lower() for f in manifest.fonts.values() if f}
    if not theme:
        return
    families = [f.strip().strip("\"'").lower() for f in value.split(",") if f.strip()]
    real = [f for f in families if f and f not in GENERIC_FAMILIES and not f.startswith("var(")]
    if not real or any(f in theme for f in real):
        return
    document.add("warn", "font-not-theme",
                 f"{origin} asks for {real[0]!r}, which is not a theme font "
                 f"({', '.join(sorted(theme))}); measurement forces the theme font",
                 f"font-family:{value}"[:120])


# ------------------------------------------------------------------------ glyphs the font lacks


_HEADING_TAGS: frozenset[str] = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

#: Subtrees whose text is never drawn on the slide in a theme font: the head, and the speaker notes
#: (`aside.notes`, handled by class), which go to the notes page.
_NO_SLIDE_TEXT: frozenset[str] = frozenset({"head", "style", "script", "title", "template", "noscript",
                                            "desc", "metadata"})

#: Codepoints no face is expected to carry: they shape or join their neighbours rather than draw.
_INVISIBLE: frozenset[int] = frozenset({0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD})

#: How many missing characters one finding lists before it says "and N more".
_GLYPHS_LISTED = 12


@lru_cache(maxsize=16)
def _cmap_of(path: str, index: int) -> frozenset[int] | None:
    """Every codepoint this font file maps to a glyph (`None` when fontTools cannot read it)."""
    from fontTools.ttLib import TTFont

    try:
        font = TTFont(path, fontNumber=index, lazy=True)
        try:
            return frozenset((font.getBestCmap() or {}).keys())
        finally:
            font.close()
    except Exception:  # noqa: BLE001 — lint never raises; an unreadable font means "cannot say"
        return None


def _theme_cmap(family: str) -> tuple[str, frozenset[int]] | None:
    """`(face name, codepoints)` for the regular face of `family` installed here, else None."""
    from app.engine.emit.text import face_for_exact

    face = face_for_exact(family, 400)
    if face is None:
        return None
    cmap = _cmap_of(str(face.path), face.index)
    return (face.path.name, cmap) if cmap else None


#: The BMP Private Use Area, where icon fonts put their glyphs. In text set in an icon font
#: `glyph-missing` leaves these to the `icon-font` rule, so one icon is not reported twice.
_PUA_FIRST, _PUA_LAST = 0xE000, 0xF8FF


def _drawable(char: str) -> bool:
    code = ord(char)
    if char.isspace() or code in _INVISIBLE or 0xFE00 <= code <= 0xFE0F:
        return False
    return unicodedata.category(char) not in ("Cc", "Cf", "Cs")


#: The family part of a `font` shorthand: whatever follows the size (and an optional `/line-height`).
_FONT_SHORTHAND_FAMILY_RE = re.compile(
    r"(?:^|\s)[+-]?(?:\d*\.)?\d+(?:px|pt|pc|em|rem|ex|ch|%|vw|vh|q|mm|cm|in)?"
    r"(?:\s*/\s*[^\s,]+)?\s+(?P<family>[^\s].*)$", re.IGNORECASE)


def _declared_family(declarations: Sequence[tuple[str, str]]) -> str | None:
    """The `font-family` these declarations end on (a `font` shorthand counts), or None."""
    family: str | None = None
    for name, value in declarations:
        if name == "font-family":
            family = value
        elif name == "font":
            found = _FONT_SHORTHAND_FAMILY_RE.search(value.strip())
            if found:
                family = found.group("family")
    return family


def _names_major(value: str, major: str) -> bool | None:
    """Whether a `font-family` value's **first** family is the theme major font, as the font pre-pass
    judges it (`FONT_PREPASS_JS`); None for a keyword that inherits the parent's answer."""
    first = value.split(",")[0].strip().strip("\"'").strip().lower()
    if first in ("inherit", "unset", "revert", "revert-layer"):
        return None
    if first.startswith("var("):
        return "--engine-font-major" in first
    return bool(major) and first == major


def _theme_major(manifest: Manifest | None) -> str:
    """The theme's major font, lowercased — or "" when it is also the minor font (nothing to tell apart)."""
    if manifest is None:
        return ""
    fonts = manifest.fonts
    major = str(fonts.get("major") or fonts.get("minor") or "").strip().strip("\"'").lower()
    minor = str(fonts.get("minor") or fonts.get("major") or "").strip().strip("\"'").lower()
    return major if major != minor else ""


def _slide_text(document: _Doc, icon_elements: list[tuple[str, frozenset[str], str]],
                manifest: Manifest | None = None) -> Iterator[tuple[str, bool, bool]]:
    """`(text, set in the major font, set in an icon font)` for every text node the slide draws.

    Measurement forces the theme fonts whatever the slide asks for (`font_forcing_css`): the major
    font on headings and the title placeholder and everything inside them, and on every element
    whose own `font-family` — declared, or inherited from its parent — names the major font first
    (the font pre-pass); the minor font on the rest. So that is the font whose cmap decides, not the
    slide's own `font-family`. The cascade is this module's small one (`_matching_declarations`).
    The icon-font flag is the `icon-font` rule's check on the inherited `font-family`; every element
    it holds for is appended to `icon_elements` as `(tag, classes, id)` so `_pseudo_text` can match
    rules to it.
    """
    major = _theme_major(manifest)

    def visit(element: EtElement, heading: bool, inherited: bool, icon: bool
              ) -> Iterator[tuple[str, bool, bool]]:
        tag, _ = _tag_of(element)
        attributes = _attributes(element)
        if tag in _NO_SLIDE_TEXT or (tag == "aside" and "notes" in attributes.get("class", "").split()):
            return
        heading = heading or tag in _HEADING_TAGS or attributes.get("data-placeholder") == "title"
        declared = _declared_family(_matching_declarations(document, tag, attributes))
        if declared is None and attributes.get("font-family"):
            declared = attributes["font-family"]           # an SVG presentation attribute
        own = _names_major(declared, major) if declared is not None and major else None
        family_major = inherited if own is None else own
        here = heading or family_major
        in_icon = icon if declared is None else _icon_font_family(declared) is not None
        if in_icon:
            icon_elements.append((tag, frozenset(c.lower() for c in attributes.get("class", "").split()),
                                  attributes.get("id", "").lower()))
        if element.text:
            yield element.text, here, in_icon
        for child in element:
            if isinstance(child.tag, str):
                yield from visit(child, heading, family_major, in_icon)
            if child.tail:
                yield child.tail, here, in_icon

    yield from visit(document.root, False, False, False)


def _pseudo_text(document: _Doc, icon_elements: list[tuple[str, frozenset[str], str]],
                 manifest: Manifest | None = None) -> Iterator[tuple[str, bool, bool]]:
    """The strings `::before`/`::after` rules draw (`content: "\2192"`), escapes resolved — in the
    major font when the rule is on a heading or declares the major font first itself.

    Set in an icon font when the rule itself names one, or (naming none) reaches an element in one.
    """
    major = _theme_major(manifest)
    for rule in document.rules:
        if not rule.pseudo:
            continue
        declared = _declared_family(rule.declarations)
        in_major = bool(rule.tags & _HEADING_TAGS) or bool(major and declared and _names_major(declared, major))
        icon = (_icon_font_family(declared) is not None) if declared is not None else any(
            _rule_applies(rule, tag, classes, element_id) for tag, classes, element_id in icon_elements)
        for name, value in rule.declarations:
            if name != "content":
                continue
            for token in tinycss2.parse_component_value_list(value):
                if token.type == "string" and token.value:
                    yield token.value, in_major, icon


def _lint_glyphs(document: _Doc, manifest: Manifest) -> None:
    """Characters the theme font has no glyph for: the browser borrows them, PowerPoint draws a box.

    Chromium falls back per character (an arrow or a check mark comes from Segoe UI Symbol) so the
    design looks right, while PowerPoint sets the run in the theme font and shows an empty box — the
    static Lexend build in `tools/fonts/` has no arrows, check marks, ●, ≤/≥ or ▶/►. Judged against
    the regular face installed where the server runs; a theme font that is not installed is skipped
    (the substitution is reported elsewhere, and there is no cmap to judge by).
    """
    slots = {"major": str(manifest.fonts.get("major") or manifest.fonts.get("minor") or "").strip(),
             "minor": str(manifest.fonts.get("minor") or manifest.fonts.get("major") or "").strip()}
    cmaps = {slot: _theme_cmap(family) if family else None for slot, family in slots.items()}
    if not any(cmaps.values()):
        return
    missing: dict[str, list[str]] = {}          # per family: the major and minor may be one font
    icon_elements: list[tuple[str, frozenset[str], str]] = []
    slide_text = list(_slide_text(document, icon_elements, manifest))  # fills icon_elements for _pseudo_text
    for text, major, icon in [*slide_text, *_pseudo_text(document, icon_elements, manifest)]:
        slot = "major" if major else "minor"
        known = cmaps[slot]
        if known is None:
            continue
        chars = missing.setdefault(slots[slot], [])
        for char in text:
            if icon and _PUA_FIRST <= ord(char) <= _PUA_LAST:
                continue                        # an icon-font glyph: `icon-font` reports it
            if _drawable(char) and ord(char) not in known[1] and char not in chars:
                chars.append(char)
    for family, chars in missing.items():
        if not chars:
            continue
        face = next(cmaps[slot][0] for slot in ("major", "minor")  # type: ignore[index]
                    if slots[slot] == family and cmaps[slot])
        listed = ", ".join(f"{c} (U+{ord(c):04X})" for c in chars[:_GLYPHS_LISTED])
        more = f" and {len(chars) - _GLYPHS_LISTED} more" if len(chars) > _GLYPHS_LISTED else ""
        document.add("warn", "glyph-missing",
                     f"{family} ({face}) has no glyph for {listed}{more}: the browser borrows "
                     f"them from another font, PowerPoint draws an empty box — use a character the "
                     f"font has, or an SVG/image", chars[0])


def _px(value: str | None) -> float | None:
    if not value:
        return None
    match = _PX_RE.match(value)
    return float(match.group(1)) if match else None


def _lint_geometry(
    document: _Doc,
    tag: str,
    declarations: Sequence[tuple[str, str]],
    manifest: Manifest | None,
    snippet: str,
) -> None:
    """An absolutely-positioned block whose px box leaves the canvas will be cut off on export."""
    if manifest is None or tag in ("html", "body", "head", "style", "meta", "title", "aside"):
        return
    resolved: dict[str, str] = {}
    for name, value in declarations:
        resolved[name] = value
    if resolved.get("position") not in ("absolute", "fixed"):
        return
    left, top = _px(resolved.get("left")), _px(resolved.get("top"))
    width, height = _px(resolved.get("width")), _px(resolved.get("height"))
    canvas_w, canvas_h = manifest.canvas_w, manifest.canvas_h
    problems: list[str] = []
    if left is not None and (left < -1 or left > canvas_w + 1):
        problems.append(f"left {left:g}px")
    if top is not None and (top < -1 or top > canvas_h + 1):
        problems.append(f"top {top:g}px")
    if left is not None and width is not None and left + width > canvas_w + 1:
        problems.append(f"right edge {left + width:g}px")
    if top is not None and height is not None and top + height > canvas_h + 1:
        problems.append(f"bottom edge {top + height:g}px")
    if problems:
        document.add("warn", "outside-canvas",
                     f"<{tag}> sits outside the {canvas_w}x{canvas_h} canvas ({', '.join(problems)}); "
                     f"the slide edge cuts it", snippet)


def _title_zone_words(manifest: Manifest | None, layout_id: str | None) -> str:
    """`the layout's title zone (x 48, y 41.3, width 1185.6 px)`, or without a rect when it is unknown."""
    if manifest is not None and layout_id:
        try:
            zone = manifest.layout(layout_id).placeholder("title")
        except Exception:  # noqa: BLE001 — an unknown layout is not lint's to report
            zone = None
        if zone is not None and zone.w > 0:
            return (f"{layout_id}'s title zone (x {zone.x:.4g}, y {zone.y:.4g}, width {zone.w:.5g} px; "
                    f"position:absolute; left:{zone.x:.4g}px; top:{zone.y:.4g}px; width:{zone.w:.5g}px)")
    return "the layout's title zone"


def _lint_title_position(document: _Doc, manifest: Manifest | None, layout_id: str | None) -> None:
    """`title-unpositioned`: a `[data-placeholder=title]` left in normal flow (render check d0_s03).

    Top-level blocks are absolutely positioned (contract §Canvas). A title in flow is laid out at the
    slide's top-left corner across the whole canvas — where the design preview shows it, and where
    PowerPoint would clip it — so the export pins it to the layout's title zone instead (the font
    pre-pass in `extract/html.py`). Static judgement, as the pre-pass's: the title and every ancestor
    between it and `<body>` have no `position` but `static` and no `transform`/`translate`/`rotate`/
    `scale`, and the title is not floated. The finding names the zone, so the fix is one line.
    """
    def moved(declarations: dict[str, str]) -> bool:
        if declarations.get("position", "static").strip().lower() not in ("static", "initial", "unset", ""):
            return True
        return any(declarations.get(name, "none").strip().lower() not in ("none", "initial", "unset", "")
                   for name in ("transform", "translate", "rotate", "scale"))

    def visit(element: EtElement, free: bool) -> None:
        tag, in_svg = _tag_of(element)
        if not tag or in_svg or tag == "svg" or tag in _NO_SLIDE_TEXT:
            return
        attributes = _attributes(element)
        canvas = tag in ("html", "body")                 # the canvas itself, positioned or not
        declarations = {} if canvas else dict(_matching_declarations(document, tag, attributes))
        here = free and not moved(declarations)
        if here and not canvas and attributes.get("data-placeholder", "").strip() == "title" \
                and declarations.get("float", "none").strip().lower() in ("none", ""):
            written = re.search(rf"<{tag}\b[^>]*data-placeholder\s*=\s*[\"']?title\b[^>]*>", document.source,
                                re.IGNORECASE)
            document.add("warn", "title-unpositioned",
                         f'<{tag} data-placeholder="title"> is in normal flow, not positioned: the browser '
                         f"lays it out at the slide's top-left corner across the whole width. The export "
                         f"places it at {_title_zone_words(manifest, layout_id)} — position it there so the "
                         f"design shows what the deck will",
                         written.group(0) if written else f"<{tag}")
        for child in element:
            if isinstance(child.tag, str):
                visit(child, here)

    visit(document.root, True)


def _lint_chart(document: _Doc, raw: str) -> None:
    """A `data-chart` must parse and pass `charts_spec.validate` — that is what the emitter runs.

    What the chart model accepted but read one way (a numeric string, an alias type name, a second
    totals spelling, a `#RGB` colour — `charts_spec.advisories`) is a `chart-advisory` warning: the
    chart exports, and the readiness panel says how it was read without blocking (WP-C §4.5).
    """
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as error:
        document.add("error", "chart-spec", f"data-chart is not valid JSON: {error}", raw[:120])
        return
    for problem in charts_spec.validate(spec):
        document.add("error", "chart-spec", f"data-chart: {problem}", raw[:120])
    for advice in charts_spec.advisories(spec):
        document.add("warn", "chart-advisory", f"data-chart: {advice}", raw[:120])


def _float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(re.sub(r"[a-z%]+$", "", (value or "").strip(), flags=re.IGNORECASE) or default)
    except ValueError:
        return default


def _numeric_texts(svg: EtElement) -> int:
    """How many `<text>` nodes in this SVG read as a number — the data half of the chart heuristic."""
    count = 0
    for element in svg.iter():
        tag, _ = _tag_of(element)
        if tag != "text":
            continue
        content = "".join(element.itertext()).strip()
        if content and _NUMBER_TEXT_RE.match(content):
            count += 1
    return count


def _shares_a_line(values: Sequence[float], tolerance: float = 0.75) -> bool:
    """True when at least three of these coordinates agree — bars on a baseline, dots on an axis."""
    return any(sum(1 for other in values if abs(other - value) <= tolerance) >= 3 for value in values)


def _snippet_of(element: EtElement) -> str:
    """A short string that also exists verbatim in the source, so `line_of` can find the element."""
    attributes = _attributes(element)
    tag, _ = _tag_of(element)
    for name, written in (("id", "id"), ("class", "class"), ("viewbox", "viewBox"), ("d", "d")):
        value = attributes.get(name)
        if value:
            return f'{written}="{value}"'
    return f"<{tag}"


def _lint_hand_drawn_chart(document: _Doc, svg: EtElement) -> None:
    """The cheap heuristic from the contract: ≥ 3 aligned rects/circles plus numeric labels."""
    if _numeric_texts(svg) < 2:
        return
    for parent in svg.iter():
        rects: list[float] = []
        circles: list[float] = []
        for child in parent:
            tag, _ = _tag_of(child)
            attributes = _attributes(child)
            if tag == "rect":
                rects.append(_float(attributes.get("y")) + _float(attributes.get("height")))
            elif tag == "circle":
                circles.append(_float(attributes.get("cy")))
        if (len(rects) >= 3 and _shares_a_line(rects)) or (len(circles) >= 3 and _shares_a_line(circles)):
            document.add(
                "warn", "hand-drawn-chart",
                "this <svg> looks like a hand-drawn chart (3+ aligned rects/circles with numeric "
                "labels); author it as a data-chart element so the export is a real chart object",
                _snippet_of(svg),
            )
            return


def _lint_image(document: _Doc, attributes: dict[str, str]) -> None:
    """`<img>` may only point at a project asset; an `.svg` asset is rasterised at displayed size."""
    src = (attributes.get("src") or "").strip()
    if not src:
        document.add("error", "img-not-asset", "<img> has no src", "<img")
        return
    if _is_remote(src):
        _lint_url(document, src, where="<img>", snippet=src[:120])
        return
    if src.lower().startswith("data:"):
        document.add("warn", "raster-css",
                     "<img> with a data: URI is embedded as a picture, not as native shapes", src[:60])
        return
    if not ASSET_URL_RE.match(src) and "/" in src.strip("/"):
        document.add("error", "img-not-asset",
                     f"<img src={src!r}> is not a project asset "
                     f"(/api/projects/<id>/assets/<file>); the exporter cannot resolve it", src[:120])
        return
    if src.lower().endswith(".svg"):
        document.add("warn", "raster-svg",
                     f"<img src={src!r}> is an SVG asset: python-pptx cannot embed SVG, so it is "
                     f"rasterised at its displayed size", src[:120])


def _lint_clip_path(document: _Doc, root: EtElement, value: str, tag: str) -> None:
    """A `clipPath` of a single `<rect>` becomes a real clip; anything else forces a raster."""
    reference = _first_url(value) or value
    target = reference.strip().lstrip("#").strip("\"'")
    for element in root.iter():
        element_tag, _ = _tag_of(element)
        if element_tag != "clippath" or _attributes(element).get("id") != target:
            continue
        children = [c for c in element if isinstance(c.tag, str)]
        if len(children) == 1 and _tag_of(children[0])[0] == "rect":
            return
        document.add("warn", "raster-svg",
                     f"SVG <{tag}> is clipped by a non-rectangular clipPath ({target!r}): that "
                     f"subtree is exported as an image", f"clip-path:{value}"[:120])
        return


# ----------------------------------------------------------------------------------------- lint


def lint(html: str, manifest: Manifest | None = None, *, layout_id: str | None = None) -> LintReport:
    """Every static finding in this slide's HTML. Never raises; a parse failure is itself a finding.

    `manifest` enables the rules that need to know the deck — "font not in the theme", "element
    outside the canvas", the font weights and glyphs. Without it those are skipped rather than
    guessed. `layout_id` (the slide's layout) lets `title-unpositioned` name the title zone's rect.
    """
    report = LintReport()
    text = html if isinstance(html, str) else str(html)
    try:
        root = html5lib.parse(text, treebuilder="etree", namespaceHTMLElements=False)
    except Exception as error:  # noqa: BLE001 — a document we cannot parse is a finding, not a crash
        report.findings.append(
            LintFinding(level="error", rule="parse", message=f"the HTML could not be parsed: {error}")
        )
        return report

    document = _Doc(source=text, root=root)
    _collect_rules(document)

    for element, tag, in_svg in _walk(root):
        if not tag:
            continue
        attributes = _attributes(element)

        # -- tags -------------------------------------------------------------------------------
        if in_svg:
            if tag in RASTER_SVG_TAGS:
                document.add("warn", "raster-svg",
                             f"SVG <{tag}> is raster-only: that subtree is exported as an image",
                             _snippet_of(element))
            elif tag not in ALLOWED_SVG_TAGS:
                document.add("warn", "unsupported-svg",
                             f"SVG <{tag}> is not supported natively and will be dropped or rasterised",
                             _snippet_of(element))
        elif tag == "script":
            document.add("error", "script",
                         "stored slide HTML must not contain <script>; the app injects the chart "
                         "preview itself", _snippet_of(element))
        elif tag not in ALLOWED_HTML_TAGS:
            document.add("error", "disallowed-tag",
                         f"<{tag}> is outside the authoring contract; the exporter has no rule for it",
                         _snippet_of(element))

        # -- attributes ---------------------------------------------------------------------------
        for name, value in attributes.items():
            if name.startswith("on") and name not in ("opacity",):
                document.add("error", "script",
                             f"inline event handler {name!r} is JavaScript; stored slide HTML must "
                             f"not contain any", f"{name}=")
            if name in ("href", "src") and value.strip().lower().startswith("javascript:"):
                document.add("error", "script", f'{name}="javascript:…" is JavaScript', value[:120])

        if tag == "img" and not in_svg:
            _lint_image(document, attributes)
        elif tag == "link":
            _lint_url(document, attributes.get("href", ""), where="<link>",
                      snippet=attributes.get("href", "")[:120])

        if "data-chart" in attributes:
            _lint_chart(document, attributes["data-chart"])

        if in_svg and tag == "svg":
            _lint_hand_drawn_chart(document, element)

        # -- declarations ---------------------------------------------------------------------------
        declarations = _matching_declarations(document, tag, attributes)
        origin = f"<{tag}>"
        for name, value in declarations:
            _lint_declaration(document, name, value, origin, manifest)
            if name == "font-family":
                _lint_icon_font(document, value, origin)
                _lint_fonts(document, value, manifest, origin)
        if in_svg:
            if attributes.get("font-family"):
                _lint_icon_font(document, attributes["font-family"], f"SVG <{tag}>")
                _lint_fonts(document, attributes["font-family"], manifest, f"SVG <{tag}>")
            if attributes.get("filter") or attributes.get("mask"):
                document.add("warn", "raster-svg",
                             f"SVG <{tag}> uses a filter/mask attribute: that subtree is exported "
                             f"as an image", _snippet_of(element))
            if attributes.get("clip-path") and "url(" in attributes["clip-path"]:
                _lint_clip_path(document, root, attributes["clip-path"], tag)
        else:
            _lint_geometry(document, tag, declarations, manifest, _snippet_of(element))

    _lint_title_position(document, manifest, layout_id)
    if manifest is not None:
        _lint_glyphs(document, manifest)

    report.findings = document.findings
    return report
