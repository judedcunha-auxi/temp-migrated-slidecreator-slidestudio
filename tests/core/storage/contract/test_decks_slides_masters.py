"""Contract: decks, slides with append-only versions, masters, blobs, exports."""

from __future__ import annotations

import pytest

from app.core.storage.models import (
    BrandCreate,
    DeckCreate,
    DeckPatch,
    ExportCreate,
    MasterCreate,
    SlideSpec,
    SlideStatusPatch,
    VersionCreate,
)
from app.core.storage.ports import Conflict, Forbidden, InvalidInput, NotFound
from tests.core.storage.contract.conftest import Harness

pytestmark = pytest.mark.asyncio

SLIDES = [SlideSpec(number=1, title="Cover", type="title"), SlideSpec(number=2, title="Body", type="content")]


async def _deck(harness: Harness, subject: str = "alice"):
    ctx, profile = await harness.user(subject)
    deck = await harness.storage.decks.create(
        ctx, DeckCreate(title="Q3", inputs={"topic": "Q3", "language": "ar", "brandId": "b1"}, status="generating"),
        SLIDES)
    return ctx, profile, deck


# ------------------------------------------------------------------ decks
async def test_deck_create_get_list(harness: Harness):
    s = harness.storage
    ctx, profile, deck = await _deck(harness)
    assert (deck.owner_id, deck.status, deck.language, deck.slide_count) == (profile.id, "generating", "ar", 2)
    assert (await s.decks.get(ctx, deck.id)).title == "Q3"
    harness.clock.advance(seconds=1)
    newer = await s.decks.create(ctx, DeckCreate(title="Newer"), [])
    page = await s.decks.list_mine(ctx)
    assert [d.id for d in page.items] == [newer.id, deck.id] and page.next_cursor is None


async def test_deck_list_pages(harness: Harness):
    s = harness.storage
    ctx, _ = await harness.user("alice")
    ids = []
    for i in range(5):
        harness.clock.advance(seconds=1)
        ids.append((await s.decks.create(ctx, DeckCreate(title=str(i)), [])).id)
    first = await s.decks.list_mine(ctx, limit=2)
    second = await s.decks.list_mine(ctx, limit=2, cursor=first.next_cursor)
    third = await s.decks.list_mine(ctx, limit=2, cursor=second.next_cursor)
    assert [d.id for d in first.items + second.items + third.items] == ids[::-1]
    assert third.next_cursor is None
    with pytest.raises(InvalidInput):
        await s.decks.list_mine(ctx, cursor="../1")
    with pytest.raises(InvalidInput):
        await s.decks.list_mine(ctx, limit=0)


async def test_another_users_deck_is_not_found(harness: Harness):
    s = harness.storage
    _, _, deck = await _deck(harness)
    bob, _ = await harness.user("bob")
    for call in (
        s.decks.get(bob, deck.id),
        s.decks.update(bob, deck.id, DeckPatch(title="mine")),
        s.slides.list_for_deck(bob, deck.id),
        s.slides.get(bob, deck.id, 1),
        s.slides.append_version(bob, deck.id, 1, VersionCreate(mode="image", image_ref="r")),
        s.slides.set_current(bob, deck.id, 1, 1),
        s.exports.record(bob, ExportCreate(deck_id=deck.id, kind="pptx", slide_numbers=[1])),
        s.transcripts.upsert_slide_edit(bob, deck.id, 1, [], 0),
    ):
        with pytest.raises(NotFound):
            await call
    await s.decks.delete(bob, deck.id)  # silent
    assert (await s.decks.list_mine(bob)).items == []


async def test_deck_update_revision_and_conflict(harness: Harness):
    s = harness.storage
    ctx, _, deck = await _deck(harness)
    updated = await s.decks.update(ctx, deck.id, DeckPatch(status="done", master_used=True),
                                   expected_revision=deck.revision)
    assert (updated.status, updated.master_used, updated.revision) == ("done", True, deck.revision + 1)
    with pytest.raises(Conflict):
        await s.decks.update(ctx, deck.id, DeckPatch(title="stale"), expected_revision=deck.revision)
    with pytest.raises(ValueError):
        DeckPatch.model_validate({"owner_id": "someone"})


async def test_deck_create_is_idempotent_by_key(harness: Harness):
    s = harness.storage
    ctx, _ = await harness.user("alice")
    data = DeckCreate(title="Same")
    first = await s.decks.create(ctx, data, SLIDES, idempotency_key="k1")
    again = await s.decks.create(ctx, data, SLIDES, idempotency_key="k1")
    assert again.id == first.id
    assert len((await s.decks.list_mine(ctx)).items) == 1
    with pytest.raises(Conflict):
        await s.decks.create(ctx, DeckCreate(title="Different"), SLIDES, idempotency_key="k1")
    bob, _ = await harness.user("bob")  # keys are per caller
    assert (await s.decks.create(bob, data, SLIDES, idempotency_key="k1")).id != first.id


async def test_duplicate_slide_numbers_are_refused(harness: Harness):
    ctx, _ = await harness.user("alice")
    with pytest.raises(InvalidInput):
        await harness.storage.decks.create(ctx, DeckCreate(), [SlideSpec(number=1), SlideSpec(number=1)])


async def test_deck_delete_cascades(harness: Harness):
    s = harness.storage
    ctx, _, deck = await _deck(harness)
    await s.exports.record(ctx, ExportCreate(deck_id=deck.id, kind="pdf", slide_numbers=[1, 2]))
    await s.transcripts.upsert_slide_edit(ctx, deck.id, 1, [{"role": "user"}], 1)
    await s.decks.delete(ctx, deck.id)
    await s.decks.delete(ctx, deck.id)  # idempotent
    with pytest.raises(NotFound):
        await s.slides.list_for_deck(ctx, deck.id)
    # A new deck never sees the old one's rows.
    assert (await s.decks.list_mine(ctx)).items == []


# ------------------------------------------------------------------ slides + versions
async def test_versions_are_append_only_with_set_current(harness: Harness):
    s = harness.storage
    ctx, _, deck = await _deck(harness)
    slides = await s.slides.list_for_deck(ctx, deck.id)
    assert [(x.number, x.status, x.current, x.versions) for x in slides] == [(1, "idle", None, []),
                                                                             (2, "idle", None, [])]
    v1 = await s.slides.append_version(ctx, deck.id, 1, VersionCreate(mode="image", image_ref="img1", cost_usd=0.04))
    harness.clock.advance(seconds=5)
    v2 = await s.slides.append_version(ctx, deck.id, 1,
                                       VersionCreate(mode="html", html_ref="h2", instruction="fewer words"))
    assert (v1.v, v2.v, v2.mode, v2.instruction) == (1, 2, "html", "fewer words")
    slide = await s.slides.get(ctx, deck.id, 1)
    assert (slide.status, slide.current, [v.v for v in slide.versions]) == ("done", 2, [1, 2])
    assert slide.versions[0].created_at < slide.versions[1].created_at
    reverted = await s.slides.set_current(ctx, deck.id, 1, 1)
    assert reverted.current == 1 and [v.v for v in reverted.versions] == [1, 2]  # nothing removed
    v3 = await s.slides.append_version(ctx, deck.id, 1, VersionCreate(mode="image", image_ref="i3", make_current=False))
    after = await s.slides.get(ctx, deck.id, 1)
    assert (v3.v, after.current) == (3, 1)
    with pytest.raises(NotFound):
        await s.slides.set_current(ctx, deck.id, 1, 9)
    with pytest.raises(NotFound):
        await s.slides.get(ctx, deck.id, 99)


async def test_version_append_is_idempotent_by_key(harness: Harness):
    s = harness.storage
    ctx, _, deck = await _deck(harness)
    data = VersionCreate(mode="image", image_ref="img")
    first = await s.slides.append_version(ctx, deck.id, 2, data, idempotency_key="render-2")
    again = await s.slides.append_version(ctx, deck.id, 2, data, idempotency_key="render-2")
    assert first == again
    assert len((await s.slides.get(ctx, deck.id, 2)).versions) == 1
    with pytest.raises(Conflict):
        await s.slides.append_version(ctx, deck.id, 2, VersionCreate(mode="image", image_ref="other"),
                                      idempotency_key="render-2")


async def test_a_version_needs_the_ref_its_mode_names():
    with pytest.raises(ValueError):
        VersionCreate(mode="html", image_ref="x")
    with pytest.raises(ValueError):
        VersionCreate(mode="image")


async def test_slide_status_and_refinement_counters(harness: Harness):
    s = harness.storage
    ctx, _, deck = await _deck(harness)
    slide = await s.slides.update_status(ctx, deck.id, 2, SlideStatusPatch(status="error", error="boom",
                                                                          prompt="p", master_kind="layout"))
    assert (slide.status, slide.error, slide.spec.prompt, slide.master_kind) == ("error", "boom", "p", "layout")
    await s.slides.record_refinement(ctx, deck.id, 2)
    refined = await s.slides.record_refinement(ctx, deck.id, 2)
    assert refined.refine_count == 2 and refined.last_refined_at == harness.clock.now
    assert (await s.decks.get(ctx, deck.id)).refine_count == 2
    assert (await s.users.get_me(ctx)).total_refinements == 2


# ------------------------------------------------------------------ exports
async def test_export_record_bumps_counters_and_is_idempotent(harness: Harness):
    s = harness.storage
    ctx, _, deck = await _deck(harness)
    data = ExportCreate(deck_id=deck.id, kind="pptx", slide_numbers=[1, 2], file_ref="f", cost_usd=0.1)
    first = await s.exports.record(ctx, data, idempotency_key="exp-1")
    again = await s.exports.record(ctx, data, idempotency_key="exp-1")
    assert first.id == again.id
    after = await s.decks.get(ctx, deck.id)
    assert (after.export_count, after.first_exported_at) == (1, harness.clock.now)
    assert (await s.users.get_me(ctx)).total_exports == 1
    assert [e.id for e in await s.exports.list_for_deck(ctx, deck.id)] == [first.id]


# ------------------------------------------------------------------ blobs
async def test_blob_round_trip_link_and_owner_only(harness: Harness):
    s = harness.storage
    alice, profile = await harness.user("alice")
    bob, _ = await harness.user("bob")
    info = await s.blobs.put(alice, b"%PDF-1.7", "application/pdf")
    assert (info.owner_id, info.size, info.content_type) == (profile.id, 8, "application/pdf")
    assert (await s.blobs.get(alice, info.ref)).data == b"%PDF-1.7"
    link = await s.blobs.link(alice, info.ref, ttl_seconds=60)
    assert info.ref in link.url and (link.expires_at - harness.clock.now).total_seconds() == 60
    for call in (s.blobs.get(bob, info.ref), s.blobs.link(bob, info.ref)):
        with pytest.raises(NotFound):
            await call
    with pytest.raises(InvalidInput):
        await s.blobs.link(alice, info.ref, ttl_seconds=86400)
    await s.blobs.delete(bob, info.ref)  # not bob's: silent, and nothing happens
    assert (await s.blobs.get(alice, info.ref)).data == b"%PDF-1.7"
    await s.blobs.delete(alice, info.ref)
    await s.blobs.delete(alice, info.ref)
    with pytest.raises(NotFound):
        await s.blobs.get(alice, info.ref)


@pytest.mark.parametrize("ref", ["../../secret", "..", "a/b", "C:\\x", "x\x00y", ""])
async def test_traversal_refs_are_not_found(harness: Harness, ref: str):
    alice, _ = await harness.user("alice")
    with pytest.raises(NotFound):
        await harness.storage.blobs.get(alice, ref)


# ------------------------------------------------------------------ masters
async def test_master_files_follow_the_brand_acl(harness: Harness):
    s = harness.storage
    admin, _ = await harness.user("admin", email="a@auxi.ai", admin=True)
    member, _ = await harness.user("member", email="m@acme.com")
    outsider, _ = await harness.user("outsider", email="o@else.com")
    brand = await s.brands.create_org(admin, BrandCreate(name="Acme"))
    await s.orgs.map_domain(admin, "acme.com", brand.id)
    master = await s.masters.create(admin, brand.id, MasterCreate(name="Acme master", manifest={"version": 2}),
                                    idempotency_key="m1")
    assert (await s.masters.create(admin, brand.id, MasterCreate(name="Acme master", manifest={"version": 2}),
                                   idempotency_key="m1")).id == master.id
    await s.masters.put_file(admin, master.id, "pptx", b"PK\x03\x04", "application/octet-stream")
    await s.masters.put_file(admin, master.id, "layout", b"png0", "image/png", layout_index=0)
    updated = await s.masters.update_manifest(admin, master.id, {"version": 2, "layouts": 1})
    assert updated.manifest == {"version": 2, "layouts": 1} and set(updated.layout_refs) == {"0"}
    # members read, never write
    assert (await s.masters.get_file(member, master.id, "layout", layout_index=0)).data == b"png0"
    assert [m.id for m in await s.masters.list_for_brand(member, brand.id)] == [master.id]
    with pytest.raises(Forbidden):
        await s.masters.put_file(member, master.id, "pptx", b"x", "application/octet-stream")
    with pytest.raises(Forbidden):
        await s.masters.create(member, brand.id, MasterCreate(name="x"))
    with pytest.raises(NotFound):
        await s.masters.get(outsider, master.id)
    with pytest.raises(InvalidInput):
        await s.masters.get_file(admin, master.id, "layout")
    with pytest.raises(NotFound):
        await s.masters.get_file(admin, master.id, "layout", layout_index=5)
    with pytest.raises(Forbidden):
        await s.masters.delete(member, master.id)
    await s.masters.delete(admin, master.id)
    with pytest.raises(NotFound):
        await s.masters.get(admin, master.id)
