"""SlideForge service: config/engine. Every path, constant and threshold the export engine uses.

Two kinds of value live here:

* **Constants** (geometry, calibration tables). They are part of the engine's contract and are not
  configurable: changing one changes every export. They are plain module-level names.
* **Settings** (`EngineSettings`), read from `SLIDE_ENGINE_*` environment variables (or `.env`) and
  validated by pydantic. Every one is listed in `.env.example`, and `check_engine_config()` (called
  from `app.config.settings.check_config`) reports the invalid ones at startup.

The engine reads both as module attributes (`config.RENDERER_URL`, `config.GATE_BLUR`), so the
settings are also exposed as module-level names, copied from `engine_settings` at import. Tests that
need another value monkeypatch the module attribute.

Ported from Slide Studio `engine/config.py` (migration plan §4.2). Dropped on the way: the per-machine
paths (projects folder, CLI output folder, brand-fonts folder, the StageFlow source path, the client
fixture) and the search of a local plugin cache for the OOXML validator. Derived images no longer go
to one process-wide folder: each extraction names its own workspace (`app.engine.extract`).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# app/config/engine.py -> app/config/ -> app/ -> project root
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent
ENGINE_DIR: Path = PROJECT_ROOT / "app" / "engine"

# ------------------------------------------------------------------------------------ geometry
# One CSS pixel is 1/96 in on every master, so the canvas is the physical slide size in inches x 96
# and a pixel is exactly 9525 EMU. Do not "tune" these per deck.
PX_PER_IN: int = 96
EMU_PER_IN: int = 914400
EMU_PER_PX: int = EMU_PER_IN // PX_PER_IN  # 9525
PT_PER_PX: float = 0.75
#: Font sizes are written at 0.01 pt (`sz = round(px * FONT_SZ_PER_PX)`); rounding to 0.5 pt
#: re-breaks the browser's lines. The emitter's contract depends on it.
FONT_SZ_PER_PX: int = 75

#: The chart approximation the browser draws so the design (and the gate's reference render) shows a
#: chart where the export will put a native one. A line-for-line transliteration of
#: `app/engine/chart_model.py`; `tests/engine/test_chart_parity.py` holds the two equal.
CHART_PREVIEW_JS: Path = ENGINE_DIR / "chart-preview.js"

FONT_SUFFIXES: tuple[str, ...] = (".ttf", ".otf", ".ttc", ".otc")

#: Renderer endpoints the engine knows how to call. The deployed PptxRender serves only
#: `render-layouts`; `render` and `verify` exist in local PptxRender builds and are what the visual
#: gate (`app/engine/verify/gate.py`) needs.
RENDERER_ENDPOINTS: tuple[str, ...] = ("render-layouts", "render", "verify")


class EngineSettings(BaseSettings):
    """The engine's environment-driven settings. Every field is `SLIDE_ENGINE_<NAME>`."""

    model_config = SettingsConfigDict(
        env_prefix="SLIDE_ENGINE_",
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    # The external PptxRender HTTP service (decision D3b). Empty means no renderer: master import
    # cannot render layout backgrounds and says so; nothing falls back silently.
    renderer_url: str = ""
    # Seconds before a renderer call is considered hung (a cold start plus a large master is slow).
    renderer_timeout_s: float = 300.0
    # Which PptxRender endpoints the configured service serves, comma separated. The deployed build
    # serves only render-layouts; the tests that need render or verify skip unless they are listed.
    renderer_endpoints: str = "render-layouts"

    # What the measuring browser may fetch from outside the machine. "block" (the default): every
    # host but the slide's own assets is refused and a remote image is reported as not loaded.
    # "raster" (trusted machines only): a public http(s) URL loads.
    remote_resources: str = "block"

    # The Playwright browser. "chromium" (or empty) is Playwright's bundled build, which is what the
    # container runs; "msedge" is an installed Edge channel.
    browser_channel: str = "chromium"
    # Engine threads, each owning one Chromium (app/core/browser_pool.py). Size it to memory.
    browser_pool_size: int = 1

    # Where installed fonts live, separated by os.pathsep. Empty means the platform's default folders.
    font_dirs: str = ""

    # Optional OOXML validator (the pptx skill's validate.py, with its schemas/ beside it). Empty
    # means not installed: the suite reports the check as skipped.
    validate_py: str = ""

    # Where the torture fixtures and the synthetic test masters live. Empty means tests/engine/fixtures
    # in the checkout (tests and CI only; the deploy package does not carry them).
    fixtures_dir: str = ""

    # Text fitting: how much of a predicted over-width is closed with negative letter spacing
    # (1.0 = all of it), and the slack added to a measured text box's width.
    width_lock_factor: float = 1.0
    text_box_width_slack: float = 1.02

    # The visible-defect gate. Pinned to PptxRender's own defaults; changing one makes every
    # recorded number incomparable.
    gate_blur: float = 2.0
    gate_delta_e: float = 8.0
    gate_min_area: int = 60
    gate_slack: int = 2
    # Non-chart defect-area target at 1280x720, scaled by canvas area. 0 disables the target.
    gate_target_px2_1280: float = 6000.0

    # Side-by-side text checks (app/engine/verify/text_deck.py): row overlap share, and position and
    # alignment tolerances in px.
    text_row_share: float = 0.5
    text_position_tol_px: float = 1.5
    text_align_tol_px: float = 1.5

    @field_validator("remote_resources", "browser_channel", "renderer_endpoints")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return value.strip().lower()

    @property
    def endpoints(self) -> frozenset[str]:
        return frozenset(e.strip() for e in self.renderer_endpoints.split(",") if e.strip())


def _default_font_dirs() -> tuple[Path, ...]:
    """Where installed fonts live, by platform (fontTools name-table scan; see `font_files`)."""
    if sys.platform == "win32":
        system_root = os.environ.get("SystemRoot") or r"C:\Windows"
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return (Path(system_root) / "Fonts", Path(local) / "Microsoft" / "Windows" / "Fonts")
    if sys.platform == "darwin":
        return (Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library" / "Fonts")
    return (Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".local" / "share" / "fonts")


def check_engine_config(s: EngineSettings) -> list[str]:
    """Problems in the engine settings (empty means all good). Messages name the variable only."""
    problems: list[str] = []
    if s.remote_resources not in ("block", "raster"):
        problems.append("SLIDE_ENGINE_REMOTE_RESOURCES must be 'block' or 'raster'.")
    unknown = sorted(s.endpoints - set(RENDERER_ENDPOINTS))
    if unknown:
        problems.append(f"SLIDE_ENGINE_RENDERER_ENDPOINTS has unknown entries {unknown}; "
                        f"known: {list(RENDERER_ENDPOINTS)}.")
    if s.renderer_url and not s.renderer_url.startswith(("http://", "https://")):
        problems.append("SLIDE_ENGINE_RENDERER_URL must be an http(s) URL.")
    if s.renderer_timeout_s <= 0:
        problems.append("SLIDE_ENGINE_RENDERER_TIMEOUT_S must be positive.")
    if not 1 <= s.browser_pool_size <= 16:
        problems.append("SLIDE_ENGINE_BROWSER_POOL_SIZE must be between 1 and 16.")
    if not 0 <= s.width_lock_factor <= 1:
        problems.append("SLIDE_ENGINE_WIDTH_LOCK_FACTOR must be between 0 and 1.")
    if not 1 <= s.text_box_width_slack <= 1.2:
        problems.append("SLIDE_ENGINE_TEXT_BOX_WIDTH_SLACK must be between 1 and 1.2.")
    return problems


def check_engine_production(s: EngineSettings) -> list[str]:
    """What production additionally requires of the engine settings."""
    problems: list[str] = []
    if not s.renderer_url:
        problems.append("SLIDE_ENGINE_RENDERER_URL is empty in production; master import could not "
                        "render layout backgrounds.")
    elif not s.renderer_url.startswith("https://"):
        problems.append("SLIDE_ENGINE_RENDERER_URL must use https in production.")
    if s.remote_resources != "block":
        problems.append("SLIDE_ENGINE_REMOTE_RESOURCES must be 'block' in production.")
    return problems


engine_settings = EngineSettings()

# --------------------------------------------------------------------- settings, as module names
RENDERER_URL: str | None = engine_settings.renderer_url or None
RENDERER_TIMEOUT_S: float = engine_settings.renderer_timeout_s
REMOTE_RESOURCES: str = engine_settings.remote_resources
EDGE_CHANNEL: str = engine_settings.browser_channel
WIDTH_LOCK_FACTOR: float = engine_settings.width_lock_factor
TEXT_BOX_WIDTH_SLACK: float = engine_settings.text_box_width_slack
GATE_BLUR: float = engine_settings.gate_blur
GATE_DELTA_E: float = engine_settings.gate_delta_e
GATE_MIN_AREA: int = engine_settings.gate_min_area
GATE_SLACK: int = engine_settings.gate_slack
GATE_TARGET_PX2_1280: float | None = engine_settings.gate_target_px2_1280 or None
TEXT_ROW_SHARE: float = engine_settings.text_row_share
TEXT_POSITION_TOL_PX: float = engine_settings.text_position_tol_px
TEXT_ALIGN_TOL_PX: float = engine_settings.text_align_tol_px

FONT_DIRS: tuple[Path, ...] = (
    tuple(Path(p) for p in engine_settings.font_dirs.split(os.pathsep) if p)
    if engine_settings.font_dirs
    else _default_font_dirs()
)

FIXTURES_DIR: Path = (
    Path(engine_settings.fixtures_dir) if engine_settings.fixtures_dir
    else PROJECT_ROOT / "tests" / "engine" / "fixtures"
)
TORTURE_FIXTURE: Path = FIXTURES_DIR / "torture"
MASTERS_FIXTURE: Path = FIXTURES_DIR / "masters"
#: Extra masters the torture suite also runs against (the committed ones are synthetic).
EXTRA_MASTERS: Path = MASTERS_FIXTURE / "extra"

#: Where `validate_py()` points when no validator is configured: a path that does not exist, so
#: callers test `.exists()` rather than crash on `None`.
VALIDATE_PY_MISSING = Path("validate.py-not-configured")
VALIDATE_PY: Path = (
    Path(engine_settings.validate_py).expanduser() if engine_settings.validate_py else VALIDATE_PY_MISSING
)


def validate_py() -> Path:
    """The optional OOXML validator, or `VALIDATE_PY_MISSING`."""
    return VALIDATE_PY


def extra_masters() -> list[Path]:
    """Every `.pptx` in `EXTRA_MASTERS`, sorted (deterministic order for the torture report)."""
    if not EXTRA_MASTERS.is_dir():
        return []
    return sorted(p for p in EXTRA_MASTERS.glob("*.pptx") if not p.name.startswith("~$"))


def font_files() -> list[Path]:
    """Every font file under `FONT_DIRS`, subfolders included, in a stable order.

    Recursive because Linux keeps nothing at the top of `/usr/share/fonts` (Debian puts Carlito in
    `truetype/crosextra/`). A flat listing finds no font there at all, every run measures as the
    last-resort average advance, and the width lock squeezes each line toward that guess.
    """
    files: list[Path] = []
    for directory in FONT_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.suffix.lower() in FONT_SUFFIXES and path.is_file():
                files.append(path)
    return files


# ------------------------------------------------------------------------------ text calibration
#: First-baseline residual as a fraction of font size, per face: what is left over after
#: `app.engine.emit.text` has placed the box from the IR's own line boxes (positive lifts the box).
#: Keys are a plain family ("Calibri") or a face ("Calibri|700", "Arial|400|i"); `text.dy_factor`
#: takes the most specific match. Key "" is the fallback for families with no measurement.
#: Measured in Slide Studio against PptxRender renders (Pillow raster of the same string; residual
#: within 2 px at 14-56 px). Carlito is metric-compatible with Calibri, so a Calibri run measured in
#: Carlito keeps Calibri's value.
TEXT_DY_BY_FONT: dict[str, float] = {
    "": -0.028,
    "Arial": -0.045,
    "Calibri": -0.098,
    "Calibri Light": -0.115,
    "Times New Roman": -0.056,
    "Segoe UI": 0.004,
    "Lexend": -0.028,
    "Crimson Pro": -0.028,
}

#: Placeholder types whose boxes are painted out on both images before the gate compares them: the
#: layout PNG shows a static page number/date/footer, a real slide shows its own.
GATE_MASK_PLACEHOLDERS: tuple[str, ...] = ("sldNum", "dt", "ftr")


def gate_target_px2(canvas_w: int, canvas_h: int) -> float | None:
    """The defect-area target for this canvas: `T(canvas) = T_1280 x (w*h)/(1280*720)`."""
    if GATE_TARGET_PX2_1280 is None:
        return None
    return GATE_TARGET_PX2_1280 * (canvas_w * canvas_h) / (1280 * 720)
