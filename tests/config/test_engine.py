"""The engine's settings (app/config/engine.py): typed, validated, and wired into check_config()."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import engine as engine_config
from app.config.engine import check_engine_config, check_engine_production
from app.config.settings import check_config
from tests.conftest import build_engine_settings, build_settings


def test_defaults_are_valid_and_need_no_renderer_outside_production():
    s = build_engine_settings()
    assert check_engine_config(s) == []
    assert s.browser_channel == "chromium"
    assert s.endpoints == frozenset({"render-layouts"})
    assert check_config(build_settings(), s) == []


def test_settings_read_the_prefixed_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SLIDE_ENGINE_RENDERER_URL", "https://render.example")
    monkeypatch.setenv("SLIDE_ENGINE_RENDERER_ENDPOINTS", "Render-Layouts, render, verify")
    monkeypatch.setenv("SLIDE_ENGINE_BROWSER_POOL_SIZE", "3")
    s = build_engine_settings()
    assert s.renderer_url == "https://render.example"
    assert s.endpoints == frozenset({"render-layouts", "render", "verify"})
    assert s.browser_pool_size == 3


def test_a_value_of_the_wrong_type_is_rejected_without_echoing_it():
    with pytest.raises(ValidationError) as caught:
        build_engine_settings(renderer_timeout_s="not-a-number")
    assert "not-a-number" not in str(caught.value)


@pytest.mark.parametrize(
    ("overrides", "variable"),
    [
        ({"remote_resources": "allow"}, "SLIDE_ENGINE_REMOTE_RESOURCES"),
        ({"renderer_endpoints": "render-layouts,info"}, "SLIDE_ENGINE_RENDERER_ENDPOINTS"),
        ({"renderer_url": "ftp://render"}, "SLIDE_ENGINE_RENDERER_URL"),
        ({"renderer_timeout_s": 0}, "SLIDE_ENGINE_RENDERER_TIMEOUT_S"),
        ({"browser_pool_size": 0}, "SLIDE_ENGINE_BROWSER_POOL_SIZE"),
        ({"browser_pool_size": 64}, "SLIDE_ENGINE_BROWSER_POOL_SIZE"),
        ({"width_lock_factor": 1.5}, "SLIDE_ENGINE_WIDTH_LOCK_FACTOR"),
        ({"text_box_width_slack": 0.9}, "SLIDE_ENGINE_TEXT_BOX_WIDTH_SLACK"),
    ],
)
def test_invalid_values_are_problems_in_every_environment(overrides: dict[str, object], variable: str):
    problems = check_engine_config(build_engine_settings(**overrides))
    assert any(variable in p for p in problems), problems
    # ...and reach the service's startup check.
    assert any(variable in p for p in check_config(build_settings(), build_engine_settings(**overrides)))


def test_production_requires_an_https_renderer_and_blocked_remote_resources():
    assert any("RENDERER_URL is empty" in p for p in check_engine_production(build_engine_settings()))
    assert any("https" in p for p in check_engine_production(build_engine_settings(renderer_url="http://r.example")))
    assert any("REMOTE_RESOURCES" in p for p in check_engine_production(
        build_engine_settings(renderer_url="https://r.example", remote_resources="raster")))
    assert check_engine_production(build_engine_settings(renderer_url="https://r.example")) == []
    production = check_config(build_settings(environment="production"), build_engine_settings())
    assert any("SLIDE_ENGINE_RENDERER_URL" in p for p in production)


def test_problems_name_the_variable_never_the_value():
    s = build_engine_settings(renderer_url="ftp://user:secret@render")
    assert all("secret" not in p for p in check_engine_config(s))


def test_geometry_constants_are_fixed():
    assert engine_config.PX_PER_IN == 96
    assert engine_config.EMU_PER_PX == 9525
    assert engine_config.EMU_PER_IN == 914400


def test_the_validator_is_optional():
    assert engine_config.validate_py() in (engine_config.VALIDATE_PY_MISSING, engine_config.VALIDATE_PY)
    assert not Path("validate.py-not-configured").exists()


def test_font_files_scan_is_recursive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    nested = tmp_path / "truetype" / "crosextra"
    nested.mkdir(parents=True)
    (nested / "Carlito-Regular.ttf").write_bytes(b"")
    (tmp_path / "readme.txt").write_text("not a font", encoding="utf-8")
    monkeypatch.setattr(engine_config, "FONT_DIRS", (tmp_path,))
    assert engine_config.font_files() == [nested / "Carlito-Regular.ttf"]


def test_gate_target_scales_with_canvas_area():
    target = engine_config.gate_target_px2(1280, 720)
    assert target == engine_config.GATE_TARGET_PX2_1280
    half = engine_config.gate_target_px2(640, 720)
    assert target is not None and half == pytest.approx(target / 2)
