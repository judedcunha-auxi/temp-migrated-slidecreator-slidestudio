"""The storyline: Darwin's and Slide Studio's deck planning, merged (decision D11).

    vocabulary.py  slide types, modes, densities, languages, the framework names (app/data/frameworks.json)
    archetypes.py  a loader over the layout-archetype library (app/data/archetypes.json)
    models.py      the inputs, the storyline, the intake brief (camelCase on the wire)
    validation.py  reading a storyline (standard: Darwin's schema; dense: Slide Studio's salvage), request checks
    repair.py      coerce, don't reject: frameworks, chart data, archetype ids, dividers
    rtl.py         direction words mirrored for Arabic prompts
    prompts.py     the storyline and intake prompts and their output schemas
    ports.py       StorylineModel: the one model call the storyline needs (structured output)
    service.py     the storyline draft
    intake.py      the intake turn (one model call), the request checks, the transcript
    job.py         the `storyline` job handler and the `/api/storyline-status` view

Start with docs/architecture.md, "The storyline".
"""
