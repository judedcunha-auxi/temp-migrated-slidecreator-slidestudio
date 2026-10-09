"""The port operations Darwin's admin routes need (feature/darwin-brands): org brand listing, the per-domain
account count, and the admin dashboard's overview and activity reads. Admin only, every one."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.storage.models import (
    AnalyticsEventCreate,
    BrandCreate,
    DeckCreate,
    IntakeTranscriptUpsert,
    UsageCreate,
)
from app.core.storage.ports import Forbidden, InvalidInput
from tests.core.storage.contract.conftest import Harness

pytestmark = pytest.mark.asyncio


async def test_list_org_is_admin_only_and_by_name(harness: Harness):
    s = harness.storage
    admin, _ = await harness.user("admin", admin=True)
    alice, _ = await harness.user("alice", email="alice@acme.com")
    await s.brands.create_org(admin, BrandCreate(name="Zeta"))
    await s.brands.create_org(admin, BrandCreate(name="Acme"))
    await s.brands.create(alice, BrandCreate(name="Personal"))
    assert [b.name for b in await s.brands.list_org(admin)] == ["Acme", "Zeta"]
    with pytest.raises(Forbidden):
        await s.brands.list_org(alice)


async def test_count_accounts_is_case_insensitive_and_admin_only(harness: Harness):
    s = harness.storage
    admin, _ = await harness.user("admin", admin=True)
    member, _ = await harness.user("one", email="one@acme.com")
    await harness.user("two", email="Two@ACME.com", verified=False)  # a sign-up snapshot: unverified counts
    await harness.user("three", email="three@notacme.com")
    await harness.user("four")
    assert await s.orgs.count_accounts(admin, "acme.com") == 2
    assert await s.orgs.count_accounts(admin, "ACME.COM ") == 2
    assert await s.orgs.count_accounts(admin, "nobody.org") == 0
    with pytest.raises(Forbidden):
        await s.orgs.count_accounts(member, "acme.com")


async def test_admin_overview_and_activity(harness: Harness):
    s = harness.storage
    admin, _ = await harness.user("admin", admin=True)
    alice, alice_profile = await harness.user("alice", email="alice@acme.com")
    old = await s.decks.create(alice, DeckCreate(title="old"), [])
    await s.usage.append(alice, UsageCreate(est_cost_usd=0.25))
    await s.analytics.record_event(alice, AnalyticsEventCreate(event_name="old_event"))
    harness.clock.advance(days=2)
    since = harness.clock.now - timedelta(hours=1)
    new = await s.decks.create(alice, DeckCreate(title="new", creation_method="wizard"), [])
    await s.usage.append(alice, UsageCreate(est_cost_usd=0.5))
    await s.analytics.record_event(alice, AnalyticsEventCreate(event_name="element_clicked", properties={"text": "Go"}))
    await s.transcripts.upsert_intake(alice, IntakeTranscriptUpsert(session_key="k1", messages=[{"role": "user"}]))
    await s.transcripts.upsert_slide_edit(alice, new.id, 1, [{"role": "user"}], 2)

    overview = await s.analytics.admin_overview(admin, since=since)
    assert {p.subject for p in overview.profiles} == {"admin", "alice"}
    assert [d.id for d in overview.decks_since] == [new.id]
    assert overview.decks_since[0].creation_method == "wizard" and overview.decks_since[0].owner_id == alice_profile.id
    assert [u.est_cost_usd for u in overview.usage_since] == [0.5]
    assert [d.id for d in overview.recent_decks] == [new.id, old.id]  # newest first, ignoring `since`
    assert len((await s.analytics.admin_overview(admin, since=since, recent_decks=1)).recent_decks) == 1

    activity = await s.analytics.admin_activity(admin, since=since)
    assert [e.event_name for e in activity.events] == ["element_clicked"]
    assert [(t.user_id, t.messages) for t in activity.intake] == [(alice_profile.id, [{"role": "user"}])]
    assert activity.intake[0].id  # every row has an id
    assert [(t.deck_id, t.slide_number, t.refine_count) for t in activity.slide_edits] == [(new.id, 1, 2)]
    assert len((await s.analytics.admin_activity(admin, since=since - timedelta(days=3), events=1)).events) == 1

    for call in (s.analytics.admin_overview(alice, since=since), s.analytics.admin_activity(alice, since=since)):
        with pytest.raises(Forbidden):
            await call
    with pytest.raises(InvalidInput):
        await s.analytics.admin_activity(admin, since=since, events=0)
