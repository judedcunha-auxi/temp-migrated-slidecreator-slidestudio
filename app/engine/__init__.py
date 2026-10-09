"""The deterministic export engine: slide HTML -> an editable `.pptx` on the customer's own master.

Slide HTML is measured in a real browser (Chromium, through Playwright) and turned into native
PowerPoint objects on the uploaded master: in seconds, at no model cost, identically every run.
Ported from Slide Studio's `engine/` (migration plan §4.2); `docs/architecture.md` ("The export
engine") is the map. Two boundaries hold the design up:

* **The IR is the seam.** `app.engine.ir` is the only thing extract, classify, emit and verify share.
* **The engine never imports the API layer** (`app.api`). Callers hand it paths and options; it
  reads its settings from `app.config.engine` and its browser from `app.core.browser_pool`.
"""

__all__ = ["ir", "manifest", "reports", "charts_spec", "renderer", "pipeline"]
