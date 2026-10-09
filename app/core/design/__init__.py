"""The design loop: a model writes slides as HTML on the customer's master, checks them, and fixes them.

The model and its cost live in `app/core/llm`; this package is everything the design turn knows about
slides. Modules, leaves first:

* `deck`          a design deck: one directory in a job workspace, laid out as the engine reads it.
* `context`       what a turn runs with (deck, settings, providers, the measuring services).
* `layouts`       the canvas, a blank slide, and layouts named the way the person sees them.
* `prompts`       the system prompt (the authoring contract and the quality bar).
* `tool_schemas`  the tools as the model is offered them.
* `reply_text`    the streamed reply, in the person's words.
* `slide_lint`    the export lint and the design review, as the tail of a tool result.
* `turn_state`    what one turn remembers between tool calls.
* `briefs`        `write_brief`: the per-slide brief, and the grounding check.
* `slide_tools`   `save_slide`, `edit_slide`, `get_component`.
* `critic`        the independent critic (a separate, fresh-context model call).
* `preview_tool`  `preview_slide`: the render, the findings and the critic's review (the critic loop).
* `system_prompt` the system prompt for a deck, the layout pictures, the workspace block.
* `design_turn`   the turn itself: tool dispatch over the provider-agnostic loop.
* `generate`      parallel Generate with a bounded fan-out.
* `bundle`        a design deck packed as one stored blob, so it outlives its job.

Ported from Slide Studio `server/chat/*`, `critic.py`, `prompts.py` (migration plan §4.1, K/R).
"""
