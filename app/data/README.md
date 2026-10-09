# app/data: reference data shared across the core

One copy of each data set that more than one area of `app/core` reads. Code that reads a file here
owns only its loader, never a second copy of the data.

| File | What | Read by |
|---|---|---|
| `archetypes.json` | The 1,240 slide-layout archetypes in 42 categories (Darwin's `deckArchetypes.json`, de-identified by Slide Studio's `scripts/sync_design_refs.py`). The same content as Slide Studio `server/design_refs/archetypes.json` (stored with LF line endings). | `app/core/storyline/archetypes.py` (the storyline's layout catalog and `archetypeId` checks); `app/core/design_refs` should point its `DATA` path here rather than ship its own copy. |
| `frameworks.json` | The 65 canonical framework names, in Darwin's order, and the 284 alias spellings (Darwin `frameworks.ts`; identical to Slide Studio `design_refs/vocabulary.py`). Names and aliases only: the per-framework hints are prompt material and stay with the prompts that use them. | `app/core/storyline/vocabulary.py`; `app/core/design_refs/vocabulary.py` can load `ALIASES` from here. |

**No client material.** `archetypes.json` carries no deck names, page numbers or reference images:
each archetype has a neutral id (`<category>-<nn>`) and `src`, a one-way SHA-256 digest (16 hex
characters) of Darwin's original id, so a storyline saved with an old id (`<deck-slug>--pNNN`) still
resolves without the deck slug being stored anywhere in this repo.
