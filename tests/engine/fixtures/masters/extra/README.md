# Extra masters — drop folder

Drop any `.pptx` **master template** in here and the torture suite will pick it up as an additional
target (`python -m engine torture`): the engine imports it, renders its layouts, runs the torture
slides on it and reports per-master results. Nothing else is needed — no manifest, no renaming.

Why this exists: Path A must work on *any* master, not just the one it was built against. A client master (not in this repo) plus the two generated masters (`test-16x9`, `test-4x3`, which
prove size generality) are the committed baseline; a second brand master is the strongest evidence
that the engine generalises.

The four committed here are **synthetic** — invented layouts, colours and metadata, no brand or client
material — written by `python -m engine.fixtures.masters.make_test_masters --extra-only` (offline,
byte-identical on every run). `engine/tests/test_import.py` (`COMMITTED_EXTRA_MASTERS`) imports each:

| file | page | layouts | what it exercises |
| --- | --- | --- | --- |
| `synthetic-89-layouts.pptx` | 1280 × 720 | 89 on one master | many layouts, dark backgrounds, pic/tbl/chart/obj zones, an angled custom-geometry picture fill, a repeated layout name, Georgia/Arial theme |
| `synthetic-14x8.5in.pptx` | 1344 × 816 | 8 | a 14 × 8.5 in page, a cover with no title, a title that inherits its box, a theme font that is not installed, a logo on the master |
| `synthetic-portrait-a4.pptx` | 720 × 1040 | 1 | portrait A4; the only layout's title inherits its box |
| `synthetic-widescreen.pptx` | 1280 × 720 | 10 | `ctrTitle`/`subTitle`, inherited titles, art-only layouts |

Useful properties in a deck dropped here:

- a different slide size (4:3, A4, 16:10) — exercises the canvas maths,
- more than one slide master, or layouts whose names repeat — exercises the `partName` join,
- placeholders that inherit their position from the master rather than declaring their own `xfrm`,
- theme fonts that are **not** installed on this PC — exercises substitution reporting.

Files here are committed with the repository, so keep them to templates that are fine to store in
this repo. A deck that must not be committed can live anywhere: point the suite at it with
`SLIDE_ENGINE_EXTRA_MASTERS=<dir>`.
