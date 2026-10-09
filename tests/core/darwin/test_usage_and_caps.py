"""core/darwin/usage (the C10 fix) and core/darwin/caps (Darwin's reserve* functions)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import fakeredis
import pytest

from app.core.darwin import caps
from app.core.darwin.usage import EST_COST_MASTER, KIND_IMAGE, record_usage, record_usage_quietly
from app.core.redis_client import RedisClient
from app.core.storage.ports import Unavailable
from tests.conftest import build_darwin_settings
from tests.fakes.general_service import FakeGeneralService

pytestmark = pytest.mark.asyncio


async def test_c10_the_cost_lands_in_est_cost_usd_and_the_model_in_model():
    store = FakeGeneralService()
    ctx, profile = await store.make_user("alice")
    event = await record_usage(store.usage, ctx, model="gpt-image-2", cost_usd=EST_COST_MASTER,
                               deck_id="d1", slide_number=3)
    assert (event.model, event.est_cost_usd, event.kind) == ("gpt-image-2", 0.16, KIND_IMAGE)
    assert (event.user_id, event.deck_id, event.slide_number) == (profile.id, "d1", 3)
    spend = await store.usage.spend_for_user(ctx, event.created_at.date())
    assert spend.cost_usd == 0.16 and spend.events == 1  # admin totals are no longer $0


async def test_the_ledger_takes_keywords_only():
    store = FakeGeneralService()
    ctx, _ = await store.make_user("alice")
    with pytest.raises(TypeError):
        await record_usage(store.usage, ctx, "gpt-image-2", 0.08)  # type: ignore[misc]


async def test_a_failed_ledger_write_is_logged_not_raised():
    store = FakeGeneralService()
    ctx, _ = await store.make_user("alice")

    async def down(*_a: object, **_k: object) -> None:
        raise Unavailable("down")

    store.usage.append = down  # type: ignore[method-assign]
    assert await record_usage_quietly(store.usage, ctx, model="m", cost_usd=0.1) is None


async def test_the_global_image_cap_is_enforced_per_utc_day():
    redis = RedisClient(fakeredis.FakeAsyncRedis(decode_responses=True))
    s = build_darwin_settings(global_images_per_day=2)
    day = [datetime(2026, 10, 9, 23, 59, tzinfo=UTC)]

    def clock() -> datetime:
        return day[0]

    assert [await caps.reserve_image_slot(redis, s, clock=clock) for _ in range(3)] == [True, True, False]
    assert await caps.images_used_today(redis, clock=clock) == 3
    day[0] += timedelta(minutes=2)  # the next UTC day
    assert await caps.reserve_image_slot(redis, s, clock=clock) is True


async def test_the_per_user_caps_are_off_by_default_as_in_darwin():
    store = FakeGeneralService()
    ctx, _ = await store.make_user("alice")
    off = build_darwin_settings(intake_turns_per_user_per_day=0, brand_extracts_per_user_per_day=0)
    assert await caps.reserve_intake_turn(store.users, ctx, off) is True
    assert await caps.reserve_brand_extract(store.users, ctx, off) is True


async def test_a_per_user_cap_switched_on_is_enforced_through_the_port():
    store = FakeGeneralService()
    ctx, _ = await store.make_user("alice")
    on = build_darwin_settings(intake_cap_enabled=True, intake_turns_per_user_per_day=2,
                               brand_extract_cap_enabled=True, brand_extracts_per_user_per_day=1)
    assert [await caps.reserve_intake_turn(store.users, ctx, on) for _ in range(3)] == [True, True, False]
    assert [await caps.reserve_brand_extract(store.users, ctx, on) for _ in range(2)] == [True, False]
