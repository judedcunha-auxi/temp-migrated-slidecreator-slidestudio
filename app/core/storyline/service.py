"""The storyline draft: one structured model call, read under the density's rules, repaired.

Darwin `_shared/anthropic.ts: generateStoryline` + `storyline-background.ts` (dividers) and Slide Studio
`server/storyline/service.py: draft`, merged. What it does not do: the per-slide image prompt Darwin's job
result carries (`ports.SlidePrompter`, supplied by the route layer) and persistence (the job record holds the
result; `job.py`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config.storyline import StorylineSettings, storyline_settings
from app.core.storyline import prompts
from app.core.storyline.models import Storyline, StorylineInputs
from app.core.storyline.ports import ModelUsage, StorylineModel, StructuredRequest
from app.core.storyline.repair import repair
from app.core.storyline.validation import StorylineValidationError, parse_storyline
from app.core.storyline.vocabulary import DIVIDER_MODES

_log = logging.getLogger(__name__)

REFUSED_MESSAGE = "The model declined to draft this storyline. Try rewording the topic or the brief."


class StorylineRefused(Exception):
    """The model refused (stop reason `refusal`). `str(exc)` is safe to show."""

    def __init__(self, message: str = REFUSED_MESSAGE) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class DraftResult:
    storyline: Storyline
    warnings: list[str] = field(default_factory=list)
    usage: ModelUsage = field(default_factory=ModelUsage)
    model: str = ""


def storyline_request(inputs: StorylineInputs, settings: StorylineSettings, sources: str = "") -> StructuredRequest:
    """The one model call a storyline draft makes. Pure: the tests inspect it."""
    return StructuredRequest(
        purpose="storyline",
        model=settings.model,
        system=prompts.storyline_system(inputs.density, inputs.language),
        messages=[{"role": "user", "content": prompts.storyline_user(inputs, sources)}],
        schema_name="storyline",
        schema=prompts.storyline_schema(inputs.density),
        max_tokens=settings.max_tokens,
        effort=settings.effort,  # type: ignore[arg-type]  # validated by check_storyline_config
    )


async def draft_storyline(
    model: StorylineModel,
    inputs: StorylineInputs,
    *,
    settings: StorylineSettings | None = None,
    sources: str = "",
) -> DraftResult:
    """Draft, read and repair a storyline. Raises `StorylineValidationError` (incomplete or invalid answer,
    including a reply cut off at `max_tokens`), `StorylineRefused`, or the port's `ModelError`s."""
    s = settings or storyline_settings
    reply = await model.structured(storyline_request(inputs, s, sources))
    if reply.stop_reason == "refusal":
        _log.warning("storyline: the model refused (model=%s)", reply.model or s.model)
        raise StorylineRefused()
    if reply.data is None:
        raise StorylineValidationError([f"no JSON answer (stop_reason={reply.stop_reason})"])
    try:
        storyline = parse_storyline(reply.data, inputs.density)
    except StorylineValidationError as exc:
        _log.warning("storyline: invalid answer (stop_reason=%s): %s", reply.stop_reason, "; ".join(exc.issues[:10]))
        raise
    storyline, warnings = repair(storyline, density=inputs.density, dividers=inputs.mode in DIVIDER_MODES,
                                 max_slides=s.max_slides)
    return DraftResult(storyline=storyline, warnings=warnings, usage=reply.usage, model=reply.model or s.model)


def revalidate(raw: object, density: str = "standard") -> tuple[Storyline, list[str]]:
    """A storyline a person edited (Darwin `/api/generate`'s payload; Slide Studio's review page): read under the
    density's rules and repaired without adding dividers back. Raises `StorylineValidationError`."""
    storyline = parse_storyline(raw, density)
    return repair(storyline, density=density, dividers=False)
