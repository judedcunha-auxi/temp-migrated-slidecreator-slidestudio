"""SlideForge service: config/storyline. The storyline and intake settings (`STORYLINE_*`).

Every field is listed in `.env.example` (`tests/config/test_storyline_settings.py` checks it), and
`check_storyline_config()` (called from `app.config.settings.check_config`) reports the invalid ones at
startup. Messages name the variable, never its value.

`STORYLINE_MODEL` keeps the name Darwin's Netlify functions used (`_shared/config.ts`), so the same
variable carries over. The default moves from Sonnet 4.6 to Sonnet 5.5 (`claude-sonnet-5-5`).
"""

from __future__ import annotations

import re

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config.engine import PROJECT_ROOT

#: Effort levels the model call accepts (`output_config.effort`).
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

#: A model id: lower-case letters, digits, dots and dashes (`claude-sonnet-5-5`).
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9.\-]{2,99}$")

#: The largest `max_tokens` the current models accept.
MAX_OUTPUT_TOKENS = 128_000


class StorylineSettings(BaseSettings):
    """The storyline's environment-driven settings. Every field is `STORYLINE_<NAME>`."""

    model_config = SettingsConfigDict(
        env_prefix="STORYLINE_",
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    # The model for the storyline draft and the intake turn (Darwin used one model for both).
    model: str = "claude-sonnet-5-5"
    # Output budget for one storyline draft. Thinking counts against it. Darwin: 64000.
    max_tokens: int = 64_000
    # How hard the model thinks on the storyline: the argument is the product (Slide Studio: high).
    effort: str = "high"
    # Output budget for one intake turn: a question plus the brief fields (Darwin: 1024, without thinking).
    intake_max_tokens: int = 4_096
    # An intake question is quick (Slide Studio: low).
    intake_effort: str = "low"
    # After this many assistant replies the intake wraps up without a model call (Darwin: 12).
    intake_max_assistant_turns: int = 12
    # Text characters an intake transcript may carry into a paid prompt (Darwin: 30000).
    intake_max_transcript_chars: int = 30_000
    # Most slides one storyline may hold, dividers excluded; 0 = no cap (Darwin removed its cap).
    max_slides: int = 0
    # The storyline job: seconds per attempt, and attempts before it is an error.
    job_timeout_s: float = 600.0
    job_max_attempts: int = 2


def check_storyline_config(s: StorylineSettings) -> list[str]:
    """Problems in the storyline settings (empty means all good)."""
    problems: list[str] = []
    if not _MODEL_ID.match(s.model):
        problems.append("STORYLINE_MODEL must be a model id such as claude-sonnet-5-5.")
    for name, value in (("STORYLINE_MAX_TOKENS", s.max_tokens), ("STORYLINE_INTAKE_MAX_TOKENS", s.intake_max_tokens)):
        if not 1 <= value <= MAX_OUTPUT_TOKENS:
            problems.append(f"{name} must be between 1 and {MAX_OUTPUT_TOKENS}.")
    for name, effort in (("STORYLINE_EFFORT", s.effort), ("STORYLINE_INTAKE_EFFORT", s.intake_effort)):
        if effort not in EFFORTS:
            problems.append(f"{name} must be one of {list(EFFORTS)}.")
    if s.intake_max_assistant_turns < 1:
        problems.append("STORYLINE_INTAKE_MAX_ASSISTANT_TURNS must be at least 1.")
    if s.intake_max_transcript_chars < 1:
        problems.append("STORYLINE_INTAKE_MAX_TRANSCRIPT_CHARS must be at least 1.")
    if s.max_slides < 0:
        problems.append("STORYLINE_MAX_SLIDES must be 0 (no cap) or more.")
    if s.job_timeout_s <= 0:
        problems.append("STORYLINE_JOB_TIMEOUT_S must be positive.")
    if s.job_max_attempts < 1:
        problems.append("STORYLINE_JOB_MAX_ATTEMPTS must be at least 1.")
    return problems


storyline_settings = StorylineSettings()
