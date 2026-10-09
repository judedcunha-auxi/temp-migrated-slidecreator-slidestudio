"""Darwin's caps (`_shared/db.ts` reserve* + `_shared/config.ts` loadCaps), D12-aware.

What Darwin enforces today, and so what this enforces by default (contract INDEX quirk 8):

| Cap | Darwin | Here | Where |
|---|---|---|---|
| Global images per UTC day (`GLOBAL_IMAGES_PER_DAY`, 400) | enforced: `reserveImageSlot` before every image (not in quick-generate-background, C12) | **on** | Redis counter |
| Intake turns per user per day (`INTAKE_TURNS_PER_USER_PER_DAY`, 120) | defined, never called | **off** (`INTAKE_CAP_ENABLED`) | storage port |
| Guidelines extractions per user per day (`BRAND_EXTRACTS_PER_USER_PER_DAY`, 10) | defined, never called | **off** (`BRAND_EXTRACT_CAP_ENABLED`) | storage port |
| Decks per user per day (`DECKS_PER_USER_PER_DAY`, 2) | defined, never called | not wired (no route of this branch needs it) | storage port |

"Redis plus ledger": the global image cap is an atomic Redis counter per UTC day
(`sf:caps:images:<yyyy-mm-dd>`, INCR then compare, expiring after two days), replacing Postgres'
`reserve_image` row lock; the cost of each image is then written to the ledger
(`app/core/darwin/usage.py`). The per-user caps are the storage port's atomic `reserve_daily`, the
durable per-profile counters Darwin kept in `profiles` (`reserve_intake_turn`,
`reserve_brand_extract`).

Turning a per-user cap on is a behaviour change: the route gains a 429 it never had (D12/D33).
`CAP_REACHED` is the text the jobs already use for the image cap; the per-user caps' 429 texts are
new (`INTAKE_CAP_MESSAGE`, `BRAND_EXTRACT_CAP_MESSAGE`) and must go in the release notes.

Over-the-limit tests per route (the sixth negative test) are TODO-P5, with Redis rate limiting.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from app.config.darwin import DarwinSettings, darwin_settings
from app.core.redis_client import RedisStore
from app.core.storage.models import CallerContext
from app.core.storage.ports import UserPort

#: Darwin's slide error when the global image cap is reached (`generateCore.ts`, `refine.ts`).
CAP_REACHED = "Free capacity reached, try again tomorrow."
#: New 429 texts, used only when the per-user caps are switched on.
INTAKE_CAP_MESSAGE = "Daily intake limit reached, try again tomorrow."
BRAND_EXTRACT_CAP_MESSAGE = "Daily guidelines extraction limit reached, try again tomorrow."

IMAGE_KEY_PREFIX = "sf:caps:images:"
_TWO_DAYS_S = 2 * 24 * 3600


def _today(clock: Callable[[], datetime]) -> str:
    return clock().astimezone(UTC).date().isoformat()


async def reserve_image_slot(
    redis: RedisStore,
    settings: DarwinSettings | None = None,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> bool:
    """Take one of today's global image slots (`reserveImageSlot`). False when the cap is reached.

    INCR first, then compare: two racing callers can never both take the last slot. A refused call
    leaves the counter one over the cap, which changes nothing (every later call is refused too)."""
    s = settings if settings is not None else darwin_settings
    key = IMAGE_KEY_PREFIX + _today(clock)
    taken = await redis.incr(key)
    if taken == 1:
        await redis.expire(key, _TWO_DAYS_S)
    return taken <= s.global_images_per_day


async def images_used_today(redis: RedisStore, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> int:
    raw = await redis.get(IMAGE_KEY_PREFIX + _today(clock))
    return int(raw) if raw and raw.isdigit() else 0


async def reserve_intake_turn(users: UserPort, ctx: CallerContext, settings: DarwinSettings | None = None) -> bool:
    """`reserveIntakeTurn`. Always True while `INTAKE_CAP_ENABLED` is off (Darwin today)."""
    s = settings if settings is not None else darwin_settings
    if not s.intake_cap_enabled:
        return True
    return await users.reserve_daily(ctx, "intake_turns", s.intake_turns_per_user_per_day)


async def reserve_brand_extract(users: UserPort, ctx: CallerContext, settings: DarwinSettings | None = None) -> bool:
    """`reserveBrandExtract`. Always True while `BRAND_EXTRACT_CAP_ENABLED` is off (Darwin today)."""
    s = settings if settings is not None else darwin_settings
    if not s.brand_extract_cap_enabled:
        return True
    return await users.reserve_daily(ctx, "brand_extracts", s.brand_extracts_per_user_per_day)
