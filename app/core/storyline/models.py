"""The storyline's data: the inputs (Darwin's `WizardInputs` plus Slide Studio's mode and density), the
storyline itself, and the intake brief.

Field names are snake_case in Python and camelCase on the wire (Darwin's JSON), through the alias
generator. Dump with `wire()` so optional keys are omitted, never `null` (the contract's rule).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.core.storyline.vocabulary import (
    MODE_BOUNDS,
    MODES,
    Density,
    Language,
    Mode,
    SlideType,
)


class _Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    def wire(self) -> dict[str, Any]:
        """The JSON the API returns: camelCase keys, unset optionals omitted (never null)."""
        return self.model_dump(by_alias=True, exclude_none=True)


class ChartSeries(_Wire):
    name: str
    values: list[float | int]
    data_labels: list[str] | None = None


class ChartData(_Wire):
    categories: list[str]
    series: list[ChartSeries]


class StorylineSlide(_Wire):
    number: int
    title: str
    type: SlideType = "framework"
    section: str | None = None
    framework: str
    description: str
    bullets: list[str] = Field(default_factory=list)
    chart_data: ChartData | None = None
    archetype_id: str | None = None


class Storyline(_Wire):
    presentation_title: str
    slides: list[StorylineSlide]
    #: Slide Studio's ghost-deck test: the argument the titles make, read in order. Dense mode only; not part
    #: of the `/api/storyline-status` result today (an additive field there needs D33).
    executive_summary: str | None = None


class StorylineInputs(_Wire):
    """What a storyline is drafted from: Darwin's wizard fields (`_shared/anthropic.ts: WizardInputs`) plus
    Slide Studio's `mode` and `density`. Brand fields are carried for the downstream prompts, not used here."""

    topic: str
    company: str = ""
    audience: str = ""
    style: str = ""
    num_slides: int
    mode: Mode = "auto"
    density: Density = "standard"
    language: Language = "en"
    style_instructions: str = ""
    context: str = ""
    key_messages: list[str] = Field(default_factory=list)
    brand_id: str | None = None
    primary_color: str | None = None
    accent_color: str | None = None
    font_style: str | None = None

    @classmethod
    def from_wizard(cls, raw: dict[str, Any], *, max_slides: int = 0) -> StorylineInputs:
        """Read a `/api/storyline` body (or an intake brief) leniently: wrong-typed optional fields fall back to
        their defaults. The route's 400s ("Topic is required", "Slides must be at least 1") are
        `validation.check_storyline_request`; this assumes they passed."""

        def text(key: str, limit: int) -> str:
            value = raw.get(key)
            return " ".join(str(value).split())[:limit] if isinstance(value, (str, int, float)) and value != "" else ""

        mode_raw = raw.get("mode")
        mode: Mode = mode_raw if isinstance(mode_raw, str) and mode_raw in MODES else "auto"  # type: ignore[assignment]
        density: Density = "dense" if raw.get("density") == "dense" else "standard"
        language: Language = "ar" if raw.get("language") == "ar" else "en"
        messages = raw.get("keyMessages")
        key_messages = [" ".join(m.split()) for m in messages if isinstance(m, str) and m.strip()] \
            if isinstance(messages, list) else []
        context = raw.get("context")

        def opt(key: str) -> str | None:
            value = raw.get(key)
            return value if isinstance(value, str) and value else None

        return cls(
            topic=text("topic", 2000), company=text("company", 300), audience=text("audience", 300),
            style=text("style", 200), num_slides=clamp_slides(mode, raw.get("numSlides"), max_slides),
            mode=mode, density=density, language=language,
            style_instructions=text("styleInstructions", 2000),
            context=context.strip() if isinstance(context, str) else "",
            key_messages=key_messages, brand_id=opt("brandId"), primary_color=opt("primaryColor"),
            accent_color=opt("accentColor"), font_style=opt("fontStyle"),
        )


def clamp_slides(mode: str, value: Any, max_slides: int = 0) -> int:
    """The slide count for `mode`: the mode's default when missing or not a number, clamped to the mode's
    bounds (Slide Studio) and to `max_slides` when that is set (0 = no cap, Darwin)."""
    lo, hi, default = MODE_BOUNDS.get(mode, MODE_BOUNDS["auto"])
    n: int
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        n = default
    else:
        try:
            n = int(float(value))
        except ValueError:
            n = default
    n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    if max_slides > 0:
        n = min(n, max_slides)
    return n


class IntakeBrief(_Wire):
    """The deck brief the intake chat pins down (Darwin `_shared/intake.ts: IntakeBrief`). `format` is Slide
    Studio's and is only asked for when the caller opts in (`IntakeOptions.ask_format`)."""

    topic: str | None = None
    audience: str | None = None
    num_slides: int | None = None
    key_messages: list[str] | None = None
    context: str | None = None
    ready: bool | None = None
    format: str | None = None
