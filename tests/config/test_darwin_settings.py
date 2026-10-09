"""Darwin's settings (app/config/darwin.py): every field in .env.example, and the startup checks."""

from __future__ import annotations

import re

from app.config.darwin import DarwinSettings, check_darwin_config
from tests.config.test_env_example import _example_keys
from tests.conftest import build_darwin_settings


def test_every_darwin_setting_is_in_env_example():
    keys = _example_keys()
    assert not DarwinSettings.model_config.get("env_prefix")
    missing = sorted(name.upper() for name in DarwinSettings.model_fields if name.upper() not in keys)
    assert not missing, f".env.example is missing: {missing}"


def test_the_openai_key_is_an_empty_placeholder():
    assert _example_keys()["OPENAI_API_KEY"] == ""


def test_defaults_match_darwin_and_keep_the_per_user_caps_off():
    s = build_darwin_settings()
    assert (s.global_images_per_day, s.intake_turns_per_user_per_day, s.brand_extracts_per_user_per_day) == (400, 120, 10)
    assert not s.intake_cap_enabled and not s.brand_extract_cap_enabled
    assert (s.openai_image_model, s.openai_image_quality) == ("gpt-image-2", "medium")
    assert check_darwin_config(s) == []


def test_bad_values_are_named_never_echoed():
    s = build_darwin_settings(openai_image_model="dall-e-9", openai_image_quality="ultra", global_images_per_day=-1,
                              openai_max_retries=99)
    problems = check_darwin_config(s)
    text = " ".join(problems)
    for name in ("OPENAI_IMAGE_MODEL", "OPENAI_IMAGE_QUALITY", "GLOBAL_IMAGES_PER_DAY", "OPENAI_MAX_RETRIES"):
        assert name in text
    assert not re.search("dall-e-9|ultra", text.replace("OPENAI_IMAGE_QUALITY", ""))


def test_the_key_is_required_only_in_production():
    assert check_darwin_config(build_darwin_settings(), production=False) == []
    assert any("OPENAI_API_KEY" in p for p in check_darwin_config(build_darwin_settings(), production=True))
