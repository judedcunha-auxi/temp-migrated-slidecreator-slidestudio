"""Contract: users, org domains, brands and brand assets. Every adapter must pass."""

from __future__ import annotations

import pytest

from app.core.storage.models import BrandCreate, BrandPatch, CallerContext, CounterBump, IdentityClaims, ProfilePatch
from app.core.storage.ports import Conflict, Forbidden, InvalidInput, NotFound
from tests.core.storage.contract.conftest import Harness

pytestmark = pytest.mark.asyncio

SYSTEM = CallerContext.service("req-sys")


# ------------------------------------------------------------------ users
async def test_get_or_create_is_stable_and_refreshes_email(harness: Harness):
    s = harness.storage
    ctx, first = await harness.user("sub-a", email="a@old.com")
    again = await s.users.get_or_create(ctx, IdentityClaims(email="a@new.com", email_verified=True))
    assert again.id == first.id and again.email == "a@new.com"
    assert (await s.users.get_me(ctx)).email == "a@new.com"


async def test_unknown_caller_has_no_profile(harness: Harness):
    ctx = CallerContext(subject="nobody", request_id="r")
    with pytest.raises(NotFound):
        await harness.storage.users.get_me(ctx)
    with pytest.raises(Forbidden):
        await harness.storage.decks.list_mine(ctx)


async def test_system_caller_cannot_act_as_a_user(harness: Harness):
    with pytest.raises(Forbidden):
        await harness.storage.users.get_or_create(SYSTEM, IdentityClaims())
    with pytest.raises(Forbidden):
        await harness.storage.brands.list_visible(SYSTEM)


async def test_update_me_changes_only_the_brand_kit(harness: Harness):
    ctx, _ = await harness.user("sub-a")
    updated = await harness.storage.users.update_me(ctx, ProfilePatch(brand_kit={"primaryColor": "#123456"}))
    assert updated.brand_kit == {"primaryColor": "#123456"} and updated.is_admin is False
    with pytest.raises(ValueError):  # is_admin is not a patchable field at all
        ProfilePatch.model_validate({"is_admin": True})


async def test_counters_and_daily_reserve(harness: Harness):
    s = harness.storage
    ctx, _ = await harness.user("sub-a")
    profile = await s.users.bump_counters(ctx, CounterBump(slides_generated=3, exports=1))
    assert (profile.total_slides_generated, profile.total_exports) == (3, 1)
    assert [await s.users.reserve_daily(ctx, "decks", 2) for _ in range(3)] == [True, True, False]
    assert (await s.users.get_me(ctx)).lifetime_decks == 2
    harness.clock.advance(days=1)  # the UTC day rolls over
    assert await s.users.reserve_daily(ctx, "decks", 2) is True
    assert await s.users.reserve_daily(ctx, "intake_turns", 1) is True
    assert await s.users.reserve_daily(ctx, "intake_turns", 1) is False


async def test_touch_last_seen(harness: Harness):
    ctx, _ = await harness.user("sub-a")
    await harness.storage.users.touch_last_seen(ctx)
    assert (await harness.storage.users.get_me(ctx)).last_seen_at == harness.clock.now


# ------------------------------------------------------------------ personal brands
async def test_personal_brand_is_its_owners_alone(harness: Harness):
    s = harness.storage
    alice, _ = await harness.user("alice")
    bob, _ = await harness.user("bob")
    brand = await s.brands.create(alice, BrandCreate(name="Mine", kit={"bodyFont": "Inter"}))
    assert (await s.brands.get(alice, brand.id)).can_edit is True
    with pytest.raises(NotFound):
        await s.brands.get(bob, brand.id)
    with pytest.raises(NotFound):
        await s.brands.update(bob, brand.id, BrandPatch(name="stolen"))
    with pytest.raises(NotFound):
        await s.brands.get_asset(bob, brand.id, "logo")
    await s.brands.delete(bob, brand.id)  # a foreign delete is a silent no-op
    assert (await s.brands.get(alice, brand.id)).brand.name == "Mine"
    assert [a.brand.id for a in await s.brands.list_visible(bob)] == []


async def test_brand_update_list_order_and_delete(harness: Harness):
    s = harness.storage
    alice, _ = await harness.user("alice")
    first = await s.brands.create(alice, BrandCreate(name="First"))
    harness.clock.advance(seconds=1)
    second = await s.brands.create(alice, BrandCreate(name="Second"))
    harness.clock.advance(seconds=1)
    await s.brands.update(alice, first.id, BrandPatch(kit={"x": 1}))
    assert [a.brand.id for a in await s.brands.list_visible(alice)] == [first.id, second.id]
    await s.brands.delete(alice, first.id)
    await s.brands.delete(alice, first.id)  # idempotent
    with pytest.raises(NotFound):
        await s.brands.get(alice, first.id)


async def test_twenty_personal_brands_at_most(harness: Harness):
    alice, _ = await harness.user("alice")
    for i in range(20):
        await harness.storage.brands.create(alice, BrandCreate(name=f"b{i}"))
    with pytest.raises(Conflict):
        await harness.storage.brands.create(alice, BrandCreate(name="one too many"))


async def test_garbage_brand_id_is_not_found(harness: Harness):
    alice, _ = await harness.user("alice")
    for bad in ("../etc/passwd", "", "x" * 300, "a b"):
        with pytest.raises(NotFound):
            await harness.storage.brands.get(alice, bad)


# ------------------------------------------------------------------ org brands
async def _org_setup(harness: Harness):
    s = harness.storage
    admin, admin_profile = await harness.user("admin", email="root@auxi.ai", admin=True)
    member, _ = await harness.user("member", email="Jane@Acme.com", verified=True)
    unverified, _ = await harness.user("unverified", email="joe@acme.com", verified=False)
    outsider, _ = await harness.user("outsider", email="x@other.com")
    brand = await s.brands.create_org(admin, BrandCreate(name="Acme", kit={"primaryColor": "#f00"}))
    await s.orgs.map_domain(admin, "acme.com", brand.id)
    return admin, admin_profile, member, unverified, outsider, brand


async def test_org_brand_is_read_only_for_verified_members(harness: Harness):
    s = harness.storage
    admin, admin_profile, member, unverified, outsider, brand = await _org_setup(harness)
    assert brand.is_org and brand.owner_id == admin_profile.id
    assert await s.orgs.my_org_brand_ids(member) == [brand.id]
    access = await s.brands.get(member, brand.id)
    assert (access.can_edit, access.brand.kit) == (False, {"primaryColor": "#f00"})
    with pytest.raises(Forbidden):
        await s.brands.update(member, brand.id, BrandPatch(name="mine now"))
    with pytest.raises(Forbidden):
        await s.brands.put_asset(member, brand.id, "logo", b"png", "image/png")
    with pytest.raises(Forbidden):
        await s.brands.delete_asset(member, brand.id, "logo")
    with pytest.raises(Forbidden):
        await s.brands.delete(member, brand.id)
    listed = await s.brands.list_visible(member)
    assert [(a.brand.id, a.can_edit) for a in listed] == [(brand.id, False)]
    # Unverified email or another domain: the org brand does not exist for them.
    for caller in (unverified, outsider):
        assert await s.orgs.my_org_brand_ids(caller) == []
        with pytest.raises(NotFound):
            await s.brands.get(caller, brand.id)


async def test_org_brand_assets_live_under_the_owner_and_members_can_read_them(harness: Harness):
    s = harness.storage
    admin, admin_profile, member, _, outsider, brand = await _org_setup(harness)
    info = await s.brands.put_asset(admin, brand.id, "logo", b"\x89PNG", "image/png")
    assert info.owner_id == admin_profile.id
    got = await s.brands.get_asset(member, brand.id, "logo")
    assert (got.data, got.info.content_type) == (b"\x89PNG", "image/png")
    assert set(await s.brands.list_assets(member, brand.id)) == {"logo"}
    with pytest.raises(NotFound):  # the raw blob is not the member's
        await s.blobs.get(member, info.ref)
    with pytest.raises(NotFound):
        await s.brands.get_asset(outsider, brand.id, "logo")


async def test_any_admin_edits_an_org_brand(harness: Harness):
    s = harness.storage
    _, _, _, _, _, brand = await _org_setup(harness)
    other_admin, _ = await harness.user("admin2", email="b@auxi.ai", admin=True)
    updated = await s.brands.update(other_admin, brand.id, BrandPatch(name="Acme Corp"))
    assert updated.name == "Acme Corp"


async def test_domain_admin_rules(harness: Harness):
    s = harness.storage
    admin, _, member, _, _, brand = await _org_setup(harness)
    with pytest.raises(Forbidden):
        await s.orgs.list_domains(member)
    with pytest.raises(Forbidden):
        await s.orgs.map_domain(member, "evil.com", brand.id)
    with pytest.raises(Forbidden):
        await s.brands.create_org(member, BrandCreate(name="x"))
    # same brand again: a no-op; another brand: Conflict; a personal brand: Conflict
    assert (await s.orgs.map_domain(admin, "ACME.com", brand.id)).domain == "acme.com"
    other = await s.brands.create_org(admin, BrandCreate(name="Other"))
    with pytest.raises(Conflict):
        await s.orgs.map_domain(admin, "acme.com", other.id)
    personal = await s.brands.create(admin, BrandCreate(name="personal"))
    with pytest.raises(Conflict):
        await s.orgs.map_domain(admin, "personal.com", personal.id)
    with pytest.raises(InvalidInput):
        await s.orgs.map_domain(admin, "not a domain", brand.id)
    await s.orgs.map_domain(admin, "acme.co.uk", brand.id)
    assert [d.domain for d in await s.orgs.list_domains(admin)] == ["acme.co.uk", "acme.com"]
    await s.orgs.unmap_domain(admin, "acme.com")
    await s.orgs.unmap_domain(admin, "acme.com")  # idempotent
    assert await s.orgs.my_org_brand_ids(member) == []


async def test_deleting_an_org_brand_removes_its_domains(harness: Harness):
    s = harness.storage
    admin, _, member, _, _, brand = await _org_setup(harness)
    await s.brands.delete(admin, brand.id)
    assert await s.orgs.list_domains(admin) == []
    assert await s.orgs.my_org_brand_ids(member) == []


async def test_org_brands_do_not_count_against_the_personal_cap(harness: Harness):
    admin, _ = await harness.user("admin", admin=True)
    for i in range(20):
        await harness.storage.brands.create(admin, BrandCreate(name=f"p{i}"))
    await harness.storage.brands.create_org(admin, BrandCreate(name="org"))


# ------------------------------------------------------------------ brand assets
async def test_brand_asset_overwrite_list_delete(harness: Harness):
    s = harness.storage
    alice, _ = await harness.user("alice")
    brand = await s.brands.create(alice, BrandCreate(name="B"))
    await s.brands.put_asset(alice, brand.id, "layout-preview-0", b"one", "image/png")
    await s.brands.put_asset(alice, brand.id, "layout-preview-0", b"two", "image/png")
    assert (await s.brands.get_asset(alice, brand.id, "layout-preview-0")).data == b"two"
    await s.brands.put_asset(alice, brand.id, "furniture", b"{}", "application/json")
    assert set(await s.brands.list_assets(alice, brand.id)) == {"layout-preview-0", "furniture"}
    await s.brands.delete_asset(alice, brand.id, "furniture")
    await s.brands.delete_asset(alice, brand.id, "furniture")
    with pytest.raises(NotFound):
        await s.brands.get_asset(alice, brand.id, "furniture")


@pytest.mark.parametrize("name", ["../logo", "a/b", "", "logo.png\x00", "..", "x" * 65])
async def test_bad_asset_names_are_refused(harness: Harness, name: str):
    alice, _ = await harness.user("alice")
    brand = await harness.storage.brands.create(alice, BrandCreate(name="B"))
    with pytest.raises(InvalidInput):
        await harness.storage.brands.put_asset(alice, brand.id, name, b"x", "image/png")


async def test_bad_content_type_is_refused(harness: Harness):
    alice, _ = await harness.user("alice")
    brand = await harness.storage.brands.create(alice, BrandCreate(name="B"))
    with pytest.raises(InvalidInput):
        await harness.storage.brands.put_asset(alice, brand.id, "logo", b"x", "text/html\r\nX-Evil: 1")
