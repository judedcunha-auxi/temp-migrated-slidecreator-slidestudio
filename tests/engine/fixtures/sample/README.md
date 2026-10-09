# The synthetic sample project

A small project in the shape the export pipeline reads (`app/engine/pipeline.py`): `project.json`
and two slides of HTML. Everything here is invented for the tests; no client material.

The rest of the project is **generated** at test time by `tests/engine/sample_project.py`
(session fixture `sample_dir` in `tests/engine/conftest.py`):

- `master.pptx`: the synthetic `masters/test-16x9.pptx` with its theme fonts set to Arial;
- `manifest.json`, `layouts/`: imported with `import_master`, blank backgrounds
  (`BlankLayoutsRenderer`, no PptxRender needed);
- `assets/asset-logo.png`, `assets/asset-cover-photo.jpg`: drawn with Pillow.

It replaces Slide Studio's client fixture, which is not ported (plan R6).
