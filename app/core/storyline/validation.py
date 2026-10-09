"""Reading a storyline from the model (or a browser) and the `/api/storyline` request checks.

Two readers, one per density:

* `standard` is Darwin's `_shared/storylineSchema.ts` (zod), rule for rule: a title, at least one slide;
  per slide a positive integer number, a title, a known type (default `framework`), a framework, a
  description, 0-4 non-empty bullets (3-4 on a body slide), optional chart data with at least one category
  and one series of numbers. Unknown keys are dropped. Anything else raises `StorylineValidationError`.
* `dense` is Slide Studio's `schema.parse`: it salvages what it can (an unknown type becomes `framework`, an
  untitled slide is dropped, bullets are trimmed to 6, chart values are coerced, slides are renumbered) and
  raises only when nothing usable is left. A thin body slide is a warning in `repair`, not an error.

`StorylineValidationError` carries Darwin's user-facing message (the one `storyline-background.ts` stores
for a zod failure) and the list of issues for the logs. The route layer maps it: `/api/storyline-status`
shows the message; `/api/generate` answers 500 "Internal error" (a contract quirk the Connector relies on).
"""

from __future__ import annotations

import math
from typing import Any

from app.core.storyline.models import ChartData, ChartSeries, Storyline, StorylineSlide
from app.core.storyline.vocabulary import FURNITURE, MAX_BULLETS, MIN_BODY_BULLETS, SLIDE_TYPES

#: Darwin's message for a storyline that fails validation (storyline-background.ts).
INCOMPLETE_STORYLINE = (
    "Claude returned an incomplete storyline — please try again or reduce the number of slides"
)

# Slide Studio's salvage limits (dense).
MAX_TEXT = 600
MAX_DESCRIPTION = 1200
MAX_CHART_POINTS = 40


class StorylineValidationError(ValueError):
    """The storyline is not usable. `str(exc)` is safe to show; `issues` is for the logs."""

    def __init__(self, issues: list[str], message: str = INCOMPLETE_STORYLINE) -> None:
        super().__init__(message)
        self.issues = issues


class StorylineRequestError(ValueError):
    """A `/api/storyline` body the route answers with 400 and this message (Darwin `storyline.ts`)."""


def check_storyline_request(body: Any) -> None:
    """Darwin's foreground checks, in its order: a truthy topic, then `numSlides < 1` as JavaScript compares
    it (missing passes; null, 0, negatives, false and "" fail; a non-numeric string passes)."""
    if not isinstance(body, dict) or not _truthy(body.get("topic")):
        raise StorylineRequestError("Topic is required")
    if "numSlides" in body and _js_less_than_one(body["numSlides"]):
        raise StorylineRequestError("Slides must be at least 1")


def _truthy(value: Any) -> bool:
    if isinstance(value, float) and math.isnan(value):
        return False
    return value not in (None, False, 0, "") if not isinstance(value, (list, dict)) else True


def _js_less_than_one(value: Any) -> bool:
    """`value < 1` with JavaScript's coercion. `undefined < 1` is false (handled by the caller)."""
    if value is None or isinstance(value, bool):
        return value is None or value is False  # null -> 0, false -> 0, true -> 1
    if isinstance(value, (int, float)):
        return not math.isnan(value) and value < 1
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return True  # "" -> 0
        try:
            number = float(text)
        except ValueError:
            return False  # NaN < 1 is false
        return not math.isnan(number) and number < 1
    if isinstance(value, list):  # Number([]) is 0, Number([x]) is Number(x), longer arrays are NaN
        return True if not value else len(value) == 1 and _js_less_than_one(value[0])
    return False  # an object is NaN


# --------------------------------------------------------------------------------------- standard (strict)
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and len(value) >= 1


def _strict_chart(raw: Any, where: str, issues: list[str]) -> ChartData | None:
    if not isinstance(raw, dict):
        issues.append(f"{where}.chartData: expected an object")
        return None
    cats = raw.get("categories")
    series = raw.get("series")
    ok = True
    if not isinstance(cats, list) or not cats or not all(isinstance(c, str) for c in cats):
        issues.append(f"{where}.chartData.categories: at least one string")
        ok = False
    if not isinstance(series, list) or not series:
        issues.append(f"{where}.chartData.series: at least one series")
        return None
    out: list[ChartSeries] = []
    for i, s in enumerate(series):
        if not isinstance(s, dict) or not isinstance(s.get("name"), str):
            issues.append(f"{where}.chartData.series[{i}].name: a string")
            ok = False
            continue
        values = s.get("values")
        if not isinstance(values, list) or not all(_is_number(v) for v in values):
            issues.append(f"{where}.chartData.series[{i}].values: numbers")
            ok = False
            continue
        labels = s.get("dataLabels")
        if labels is not None and (not isinstance(labels, list) or not all(isinstance(x, str) for x in labels)):
            issues.append(f"{where}.chartData.series[{i}].dataLabels: strings")
            ok = False
            continue
        out.append(ChartSeries(name=s["name"], values=list(values), data_labels=list(labels) if labels else None))
    return ChartData(categories=list(cats), series=out) if ok else None  # type: ignore[arg-type]


def _strict_slide(raw: Any, i: int, issues: list[str]) -> StorylineSlide | None:
    where = f"slides[{i}]"
    if not isinstance(raw, dict):
        issues.append(f"{where}: expected an object")
        return None
    before = len(issues)
    number: Any = raw.get("number")
    if not (_is_number(number) and float(number).is_integer() and number > 0):
        issues.append(f"{where}.number: a positive integer")
    for key in ("title", "framework", "description"):
        if not _nonempty_str(raw.get(key)):
            issues.append(f"{where}.{key}: a non-empty string")
    stype = raw.get("type", "framework")
    if stype not in SLIDE_TYPES:
        issues.append(f"{where}.type: one of the {len(SLIDE_TYPES)} slide types")
    section = raw.get("section")
    if section is not None and not isinstance(section, str):
        issues.append(f"{where}.section: a string")
    bullets = raw.get("bullets")
    if not isinstance(bullets, list) or not all(_nonempty_str(b) for b in bullets):
        issues.append(f"{where}.bullets: non-empty strings")
        bullets = []
    elif len(bullets) > MAX_BULLETS["standard"]:
        issues.append(f"{where}.bullets: at most {MAX_BULLETS['standard']}")
    elif stype not in FURNITURE and len(bullets) < MIN_BODY_BULLETS:
        issues.append(f"{where}.bullets: Content slides need 3-4 bullets")
    archetype = raw.get("archetypeId")
    if archetype is not None and not isinstance(archetype, str):
        issues.append(f"{where}.archetypeId: a string")
    chart = _strict_chart(raw["chartData"], where, issues) if raw.get("chartData") is not None else None
    if len(issues) > before:
        return None
    return StorylineSlide(
        number=int(number), title=raw["title"], type=stype, section=section, framework=raw["framework"],  # type: ignore[arg-type]
        description=raw["description"], bullets=list(bullets), chart_data=chart, archetype_id=archetype,
    )


def parse_standard(raw: Any) -> Storyline:
    """Darwin's `parseStoryline`: the storyline, or `StorylineValidationError` with every issue found."""
    issues: list[str] = []
    if not isinstance(raw, dict):
        raise StorylineValidationError(["storyline: expected an object"])
    if not _nonempty_str(raw.get("presentationTitle")):
        issues.append("presentationTitle: a non-empty string")
    slides_raw = raw.get("slides")
    if not isinstance(slides_raw, list) or not slides_raw:
        issues.append("slides: at least one slide")
        slides_raw = []
    slides = [_strict_slide(s, i, issues) for i, s in enumerate(slides_raw)]
    if issues:
        raise StorylineValidationError(issues)
    return Storyline(presentation_title=raw["presentationTitle"], slides=[s for s in slides if s is not None])


# ----------------------------------------------------------------------------------------- dense (salvage)
def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return " ".join(str(value).split())[:limit] if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _number(value: Any) -> int | float | None:
    """A chart value as a number: 12, 12.5 and "12.5" are; True, "n/a" and None are not."""
    if isinstance(value, bool):
        return None
    try:
        f = float(value.replace(",", "")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return int(f) if f.is_integer() else f


def _salvage_chart(raw: Any) -> ChartData | None:
    """Chart data or None. Series are trimmed to the category count; missing points become 0."""
    if not isinstance(raw, dict):
        return None
    cats = [c for c in (_text(x, 80) for x in raw.get("categories") or []) if c][:MAX_CHART_POINTS]
    out: list[ChartSeries] = []
    for s in raw.get("series") or []:
        if not isinstance(s, dict):
            continue
        numbers = [_number(v) for v in s.get("values") or []][:len(cats)]
        if not numbers or all(v is None for v in numbers):
            continue
        values: list[float | int] = [0 if v is None else v for v in numbers]
        labels = [_text(x, 40) for x in s.get("dataLabels") or []]
        out.append(ChartSeries(name=_text(s.get("name"), 80) or "Series", values=values,
                               data_labels=(labels + [""] * len(values))[:len(values)] if labels else None))
    if not cats or not out:
        return None
    return ChartData(categories=cats, series=out)


def _salvage_slide(raw: dict[str, Any], number: int) -> StorylineSlide:
    stype = _text(raw.get("type"), 40).lower()
    if stype not in SLIDE_TYPES:
        stype = "framework"
    bullets_raw = raw.get("bullets") if isinstance(raw.get("bullets"), list) else []
    bullets = [b for b in (_text(x) for x in bullets_raw or []) if b]
    return StorylineSlide(
        number=number, title=_text(raw.get("title")), type=stype,  # type: ignore[arg-type]
        section=_text(raw.get("section"), 80) or None, framework=_text(raw.get("framework"), 120),
        description=_text(raw.get("description"), MAX_DESCRIPTION), bullets=bullets[:MAX_BULLETS["dense"]],
        chart_data=_salvage_chart(raw.get("chartData")), archetype_id=_text(raw.get("archetypeId"), 120) or None,
    )


def parse_dense(raw: Any) -> Storyline:
    """Slide Studio's `schema.parse`: keep every titled slide, fix what can be fixed, number them 1..N."""
    if not isinstance(raw, dict):
        raise StorylineValidationError(["storyline: expected an object"])
    slides: list[StorylineSlide] = []
    items = raw.get("slides")
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            s = _salvage_slide(item, len(slides) + 1)
            if s.title:
                slides.append(s)
    if not slides:
        raise StorylineValidationError(["slides: no slide with a title"])
    return Storyline(
        presentation_title=_text(raw.get("presentationTitle"), 200) or slides[0].title, slides=slides,
        executive_summary=_text(raw.get("executiveSummary"), 1500) or None,
    )


def parse_storyline(raw: Any, density: str = "standard") -> Storyline:
    """Read a storyline under the rules of `density` (`standard` strict, `dense` salvage)."""
    return parse_dense(raw) if density == "dense" else parse_standard(raw)
