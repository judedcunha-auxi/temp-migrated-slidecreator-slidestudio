"""Darwin's backend logic, behind its `/api/*` routes (plan §4.3: `_shared/*` -> `app/core/darwin/*`).

    usage.py      the cost ledger (fixes C10: the cost lands in est_cost_usd, the model in model)
    image_gen.py  the image generation port (OpenAI gpt-image behind a protocol, D28)
    caps.py       Darwin's reserve* caps: the global image cap (enforced) and the per-user caps (off)
    jobs.py       job submit/status helpers: the status routes' shapes and quirks
    runtime.py    the job registry, queue and workers the routes enqueue on (app.state.darwin)
    exports.py    /api/pptx-* and /api/image-to-slide inputs for design_and_export / stitch_deck

Start with docs/darwin-api.md.
"""
