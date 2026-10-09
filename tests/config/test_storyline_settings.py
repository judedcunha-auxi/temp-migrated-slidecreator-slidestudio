"""The storyline settings (`STORYLINE_*`): every one is in .env.example, the defaults pass the startup check,
and `check_config` reports bad values by name only."""

from __future__ import annotations

import re
from pathlib import Path

from app.config.settings import Settings, check_config
from app.config.storyline import StorylineSettings, check_storyline_config

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def test_every_storyline_setting_is_in_env_example():
    keys = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M))
    prefix = StorylineSettings.model_config.get("env_prefix", "")
    assert prefix == "STORYLINE_"
    missing = sorted(f"{prefix}{n.upper()}" for n in StorylineSettings.model_fields if f"{prefix}{n.upper()}" not in keys)
    assert not missing, f".env.example is missing: {missing}"


def test_the_defaults_are_valid_and_name_sonnet_5_5():
    s = StorylineSettings(_env_file=None)  # type: ignore[call-arg]
    assert check_storyline_config(s) == []
    assert (s.model, s.max_tokens, s.intake_max_assistant_turns, s.intake_max_transcript_chars) == (
        "claude-sonnet-5-5", 64_000, 12, 30_000)


def test_bad_values_are_reported_by_name_without_the_value():
    s = StorylineSettings(_env_file=None, model="Claude Sonnet!", max_tokens=0, effort="extreme",  # type: ignore[call-arg]
                          intake_effort="none", intake_max_tokens=200_000, max_slides=-1, job_timeout_s=0,
                          job_max_attempts=0, intake_max_assistant_turns=0, intake_max_transcript_chars=0)
    problems = check_storyline_config(s)
    for name in ("STORYLINE_MODEL", "STORYLINE_MAX_TOKENS", "STORYLINE_EFFORT", "STORYLINE_INTAKE_EFFORT",
                 "STORYLINE_INTAKE_MAX_TOKENS", "STORYLINE_MAX_SLIDES", "STORYLINE_JOB_TIMEOUT_S",
                 "STORYLINE_JOB_MAX_ATTEMPTS", "STORYLINE_INTAKE_MAX_ASSISTANT_TURNS",
                 "STORYLINE_INTAKE_MAX_TRANSCRIPT_CHARS"):
        assert any(p.startswith(name) for p in problems), name
    assert not any("Claude Sonnet!" in p or "extreme" in p for p in problems)


def test_check_config_includes_the_storyline_rules(monkeypatch):
    import app.config.settings as settings_module

    bad = StorylineSettings(_env_file=None, effort="extreme")  # type: ignore[call-arg]
    monkeypatch.setattr(settings_module, "storyline_settings", bad)
    assert any(p.startswith("STORYLINE_EFFORT") for p in check_config(Settings(_env_file=None, environment="test")))  # type: ignore[call-arg]
