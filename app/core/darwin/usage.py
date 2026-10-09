"""The cost ledger: one `usage_events` row per paid call, written through the storage port.

Darwin's `recordUsage(userId, deckId, number, variant, model, estCost)` takes six arguments, but
`generateCore.ts:175` and `refine.ts:112` pass five, and `netlify/functions` is never type-checked
(C10). Every argument landed one column early: `variant` got the model name, `model` got the cost
as text, and `est_cost_usd` defaulted to 0. Every row since cost $0, so admin-metrics' totals are $0.

`record_usage` takes keyword arguments only, so the slip cannot recur: the model name goes to
`model`, the cost to `est_cost_usd` (rounded to Darwin's numeric(10,4) by the port). The A/B
`variant` column is gone (one image per slide, C3).

Darwin's estimates per image are kept (`EST_COST_*`): gpt-image does not report a price. Model
calls that do (the storyline, intake and design turns) pass their priced cost.
"""

from __future__ import annotations

import logging

from app.core.storage.models import CallerContext, UsageCreate, UsageEvent
from app.core.storage.ports import UsagePort

_log = logging.getLogger(__name__)

#: `generateCore.ts`: one generated image, and an edit conditioned on a master (about 2x the input).
EST_COST_PER_IMAGE = 0.08
EST_COST_MASTER = 0.16

#: Ledger `kind`s. "image" is Darwin's only kind today; the others are the paid calls the service adds.
KIND_IMAGE = "image"
KIND_STORYLINE = "storyline"
KIND_INTAKE = "intake"
KIND_DESIGN = "design"


async def record_usage(
    usage: UsagePort,
    ctx: CallerContext,
    *,
    model: str,
    cost_usd: float,
    kind: str = KIND_IMAGE,
    deck_id: str | None = None,
    slide_number: int | None = None,
    idempotency_key: str | None = None,
) -> UsageEvent:
    """Append one ledger row for the caller. Raises the port's errors (callers that must not fail on a
    ledger write, as Darwin's `.catch(log)` in refine, use `record_usage_quietly`)."""
    data = UsageCreate(deck_id=deck_id, slide_number=slide_number, kind=kind, model=model,
                       est_cost_usd=max(0.0, float(cost_usd)))
    return await usage.append(ctx, data, idempotency_key=idempotency_key)


async def record_usage_quietly(usage: UsagePort, ctx: CallerContext, **kwargs: object) -> UsageEvent | None:
    """`record_usage`, logging instead of raising: a paid call already happened, and the user's result
    must not be lost because the ledger write failed."""
    try:
        return await record_usage(usage, ctx, **kwargs)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - the ledger is best-effort here, by design
        _log.exception("usage: the ledger write failed")
        return None
