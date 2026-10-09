# Licensing of third-party code

What in this repo is not ours to ship freely, and what has to happen before it can be.

## Licence-gated: StageFlow-derived code (decision D3)

Two engine modules are derived from **StageFlow**, third-party code whose licence terms have not
been granted. They were ported from Slide Studio as they are, so the engine works and its tests
run, but **they must not reach production until D3 is resolved**:

| Module | What is StageFlow's |
|---|---|
| `app/engine/emit/charts.py` | The chart-type tables, the derived-type rewriting (`_convert` and friends) and the transparent-background helpers, kept verbatim. The rest of the module (the option set, waterfalls, manual plot layout, overlay geometry) was written on top. |
| `app/engine/emit/draw.py` | The whole module: StageFlow's helper layer over python-pptx, copied verbatim. |

Each module says so in its docstring (`LICENCE-GATED (decision D3)`).

Two more modules name StageFlow as the origin of an idea or an algorithm, re-implemented rather
than copied (Slide Studio's own notes say "ported from"): `app/engine/emit/template.py` (master
handling, after StageFlow's `deckbase.py`) and `app/engine/verify/fit.py` (the fit report, after
StageFlow's `score.fit_report`). They carry no StageFlow code as far as the port could tell, but
include them in the D3 review. The chart-type *names* StageFlow uses also appear as data in
`app/engine/chart_model.py`, `app/engine/charts_spec.py` and `app/engine/chart-preview.js`.

**Resolving D3** (migration plan §10, risk R7):

1. **Terms granted.** Record them here (who, what, when), and drop the gate notices.
2. **No terms.** Rewrite `charts.py` and `draw.py` from the python-pptx documentation and the
   behaviour the tests pin (`tests/engine/emit/`), without reference to the StageFlow source.
   Estimated at about 3 engineer-weeks. The tests are the specification.

Until then: do not copy any more StageFlow code into the service, and do not deploy a build that
includes these modules to production.

## Not here on purpose

- **PptxRender** (the .NET renderer) is only called over HTTP (decision D3b). No code is reused,
  so no terms are needed.
- **Client material.** No client master, deck or slide is in this repository (risk R6): every
  test fixture is synthetic (`tests/engine/fixtures/`, `tests/engine/sample_project.py`).
- **Exemplar slides** for the AI design features are Phase 3 and are also gated by D3.
