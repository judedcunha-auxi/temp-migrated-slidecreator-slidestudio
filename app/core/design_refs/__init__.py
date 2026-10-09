"""Reference material the design loop draws on: the visual vocabulary, the layout-archetype library,
the component snippets, the exhibit planner, the per-slide brief, the exemplar pictures and the design
lint (including the workzone and header-band checks).

Ported from Slide Studio `server/design_refs/` (migration plan §4.1, K). The archetype library and the
framework aliases are loaded from the shared `app/data/` (one copy, also read by the storyline); this
package ships no data of its own. Exemplar pictures are blocked by D3 and installed only from a
configured folder (`DESIGN_EXEMPLARS_DIR`).
"""
