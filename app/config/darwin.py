"""SlideForge service: config/darwin. Settings for Darwin's `/api/*` routes.

Names keep what Darwin's Netlify functions read (`netlify/functions/_shared/config.ts`), so the same
variables carry over: `GLOBAL_IMAGES_PER_DAY`, `INTAKE_TURNS_PER_USER_PER_DAY`,
`BRAND_EXTRACTS_PER_USER_PER_DAY`, `OPENAI_API_KEY`. Every field is listed in `.env.example`
(`tests/config/test_darwin_settings.py` checks it); `check_darwin_config()` is called from
`app.config.settings.check_config`.

**Caps (decision D12).** Darwin defines four caps but enforces one: the global image cap
(`reserveImageSlot`). `reserveIntakeTurn` and `reserveBrandExtract` exist and are never called
(contract INDEX quirk 8). The same is true here by default: the two per-user caps are wired
(`app/core/darwin/caps.py`) and switched OFF. Turning one on adds a new 429 to a route, a behaviour
change to list in the release notes (D33).
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config.engine import PROJECT_ROOT

#: Image models the adapter is written for (custom sizes such as 2560x1440 need gpt-image-2).
IMAGE_MODELS: tuple[str, ...] = ("gpt-image-2", "gpt-image-1")
IMAGE_QUALITIES: tuple[str, ...] = ("low", "medium", "high", "auto")


class DarwinSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    # --- image generation (app/core/darwin/image_gen.py; D28: OpenAI behind a port) ----------
    # Empty: image generation is unavailable (the routes that need it fail their job, never boot).
    openai_api_key: SecretStr = SecretStr("")
    # Darwin hard-coded gpt-image-2 at quality medium (`_shared/openai.ts`).
    openai_image_model: str = "gpt-image-2"
    openai_image_quality: str = "medium"
    # One image request, in seconds; the SDK retries 429 / 5xx / network errors itself.
    openai_request_timeout_s: float = 180.0
    openai_max_retries: int = 3

    # --- caps (app/core/darwin/caps.py) -------------------------------------------------------
    # Images generated per UTC day across all users. Enforced (Darwin's reserveImageSlot).
    global_images_per_day: int = 400
    # Per-user intake turns per UTC day. OFF by default, as in Darwin today.
    intake_cap_enabled: bool = False
    intake_turns_per_user_per_day: int = 120
    # Per-user guidelines-PDF extractions per UTC day. OFF by default, as in Darwin today.
    brand_extract_cap_enabled: bool = False
    brand_extracts_per_user_per_day: int = 10

    @property
    def openai_key(self) -> str:
        return self.openai_api_key.get_secret_value()


def check_darwin_config(s: DarwinSettings, production: bool = False) -> list[str]:
    """Problems in Darwin's settings (empty means all good). Messages name the variable, never its value."""
    problems: list[str] = []
    if s.openai_image_model not in IMAGE_MODELS:
        problems.append(f"OPENAI_IMAGE_MODEL must be one of {list(IMAGE_MODELS)}.")
    if s.openai_image_quality not in IMAGE_QUALITIES:
        problems.append(f"OPENAI_IMAGE_QUALITY must be one of {list(IMAGE_QUALITIES)}.")
    if not 1 <= s.openai_request_timeout_s <= 900:
        problems.append("OPENAI_REQUEST_TIMEOUT_S must be between 1 and 900.")
    if not 0 <= s.openai_max_retries <= 10:
        problems.append("OPENAI_MAX_RETRIES must be between 0 and 10.")
    for name, value in (("GLOBAL_IMAGES_PER_DAY", s.global_images_per_day),
                        ("INTAKE_TURNS_PER_USER_PER_DAY", s.intake_turns_per_user_per_day),
                        ("BRAND_EXTRACTS_PER_USER_PER_DAY", s.brand_extracts_per_user_per_day)):
        if value < 0:
            problems.append(f"{name} must be 0 or more.")
    if production and not s.openai_key:
        problems.append("OPENAI_API_KEY is empty in production; image generation would fail.")
    return problems


darwin_settings = DarwinSettings()
