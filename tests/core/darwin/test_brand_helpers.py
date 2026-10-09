"""The pure helpers behind Darwin's brand, admin, analytics and userinfo routes (feature/darwin-brands):
JavaScript semantics, the kit's write side, the archetype/furniture mapping, the guidelines extraction,
the userinfo decoder, the admin metrics aggregation and the analytics batch."""

from __future__ import annotations

import base64
import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.brand import guidelines
from app.core.brand.kit import (
    KitError,
    merge_kit,
    normalize_heading_placeholder,
    normalize_kit,
    sanitize_kit_patch,
    valid_name,
)
from app.core.brand.legacy_mapping import (
    align_previews_to_layouts,
    default_export_furniture,
    furniture_for_archetypes,
    layout_slug,
    parse_archetype_layouts,
    rendered_layouts,
    slug_to_name,
)
from app.core.darwin import admin, analytics, userinfo
from app.core.darwin.js import js_is_integer, js_number, node_base64
from app.core.storage.models import (
    AdminActivity,
    AdminDeckRow,
    AdminOverview,
    AdminUsageRow,
    AnalyticsEvent,
    Profile,
)
from app.core.storage.ports import Unavailable
from tests.fakes.general_service import FakeGeneralService
from tests.fakes.storyline_model import ScriptedStorylineModel, reply


# ------------------------------------------------------------------------------------ JavaScript
@pytest.mark.parametrize(("value", "expected"), [
    (None, 0.0), ("", 0.0), ("  7 ", 7.0), ("1e3", 1000.0), ("0x10", 16.0), ("-0", -0.0), ([], 0.0), (["5"], 5.0),
    (True, 1.0), ("Infinity", math.inf),
])
def test_js_number(value: Any, expected: float) -> None:
    assert js_number(value) == expected


@pytest.mark.parametrize("value", ["abc", "1_000", "nan", "inf", "0x", [1, 2], {}, "1 2"])
def test_js_number_nan(value: Any) -> None:
    assert math.isnan(js_number(value))


def test_js_is_integer() -> None:
    assert js_is_integer(2) and js_is_integer(2.0) and js_is_integer(-0.0)
    assert not any(js_is_integer(v) for v in (True, "2", 1.5, math.nan, math.inf, None))


def test_node_base64_is_lenient_like_buffer_from() -> None:
    raw = b"\x89PNG-bytes"
    text = base64.b64encode(raw).decode()
    assert node_base64(text) == raw
    assert node_base64(" \n".join(text)) == raw  # whitespace skipped
    assert node_base64(base64.urlsafe_b64encode(raw).decode()) == raw  # url-safe alphabet accepted
    assert node_base64(text + "=garbage") == raw  # stops at the first '='
    assert node_base64("!!!") == b"" and node_base64("A") == b""  # never raises


# ------------------------------------------------------------------------------------------- kit
def test_sanitize_kit_patch_canonicalises_asset_keys_and_keeps_nulls() -> None:
    out = sanitize_kit_patch({"logoKey": "attacker/path.png", "furnitureKey": 1, "zzz": None,
                              "furnitureArchetypes": [], "layouts": "anything"}, "owner1", "b1")
    assert out == {"logoKey": "brand/owner1/b1/logo.png", "furnitureKey": "brand/owner1/b1/furniture.json",
                   "zzz": None, "furnitureArchetypes": [], "layouts": "anything"}


@pytest.mark.parametrize(("kit", "message"), [
    ({"masterKey": ""}, "masterKey must be a truthy value or null"),
    ({"primaryColor": "#12345G"}, "primaryColor must be a hex color like #1A2B3C"),
    ({"bodyFont": ["x"]}, "bodyFont must be a string"),
    ({"a": 1, "b": 2}, "Unknown kit field: a"),
])
def test_sanitize_kit_patch_rejections(kit: dict[str, Any], message: str) -> None:
    with pytest.raises(KitError, match=f"^{message}$"):
        sanitize_kit_patch(kit, "o", "b")


def test_kit_size_is_measured_in_utf16_units() -> None:
    # 1 astral character is 2 UTF-16 units (JavaScript's .length), 4 UTF-8 bytes.
    near = {"styleTemplate": "\U0001F600" * (131_072 - 12)}
    sanitize_kit_patch(near, "o", "b")
    with pytest.raises(KitError, match="too large"):
        sanitize_kit_patch({"styleTemplate": "\U0001F600" * 131_072}, "o", "b")


def test_merge_kit_and_valid_name() -> None:
    assert merge_kit({"a": 1, "b": 2}, {"b": None, "c": 3}) == {"a": 1, "c": 3}
    assert valid_name("  x  ") == "x"
    with pytest.raises(KitError):
        valid_name("x" * 121)


def test_normalize_heading_placeholder() -> None:
    assert normalize_heading_placeholder({"left": None, "top": "0.1", "width": 0.5, "height": 1, "size": 28,
                                          "font": "  Georgia ", "color": "red"}) == {
        "left": 0, "top": 0.1, "width": 0.5, "height": 1, "sizePt": 28, "font": "Georgia"}
    assert normalize_heading_placeholder({"top": 0, "width": 1, "height": 1}) is None  # left missing: NaN
    assert normalize_heading_placeholder({"left": 0, "top": 0, "width": 0, "height": 1}) is None
    assert normalize_heading_placeholder([1, 2]) is None


def test_normalize_kit_reads_the_passthrough_fields() -> None:
    kit = normalize_kit({"layouts": [], "allColors": [{"role": "accent1", "hex": "#112233"}], "styleTemplate": " ",
                         "headingPlaceholders": {"cover": {"left": 0, "top": 0, "width": 1, "height": 1}, "x": {}},
                         "neutralColor": "#ABCDEF", "typographyScale": "junk"})
    assert kit.layouts is None and kit.all_colors == ({"role": "accent1", "hex": "#112233"},)
    assert kit.style_template.startswith("Make the slide executive-ready")
    assert kit.heading_placeholders == {"cover": {"left": 0, "top": 0, "width": 1, "height": 1}}
    assert kit.neutral_color == "#ABCDEF" and kit.typography_scale is None


# -------------------------------------------------------------------------------- legacy mapping
def test_rendered_layouts_sort_naturally_and_are_zero_based() -> None:
    files = {"layout-10-blank.png": b"j", "layout-02-title.png": b"b", "layout-01-cover.png": b"a",
             "readme.txt": b"x", "layout-00-bad.png": b"z"}
    assert rendered_layouts(files) == [(0, "cover", b"a"), (1, "title", b"b"), (9, "blank", b"j")]


def test_align_previews_matches_slugs_whatever_the_renderer() -> None:
    layouts = [{"index": 0, "name": "Half / Half"}, {"index": 1, "name": "Title"}, {"index": 2, "name": "Title"}]
    assert layout_slug("Half / Half") == layout_slug("half---half") == "half-half"
    assert align_previews_to_layouts([(4, "title"), (0, "half---half"), (7, "title")], layouts) == {0: 0, 4: 1, 7: 2}
    assert align_previews_to_layouts([(0, "nope")], layouts) is None
    assert align_previews_to_layouts([(0, "title")], [{"index": "0", "name": "Title"}]) is None
    assert slug_to_name("title-and_content.png") == "Title And Content" and slug_to_name("") == ""


def test_furniture_helpers() -> None:
    assert parse_archetype_layouts({"cover": 0, "divider": 1.0, "content": 2, "x": "y"}) == {
        "cover": 0, "divider": 1, "content": 2}
    assert parse_archetype_layouts({"cover": True, "divider": 1, "content": 2}) is None
    assert furniture_for_archetypes({"layouts": {}}, {"cover": 0, "divider": 0, "content": 0}) is None
    assert furniture_for_archetypes(5, {"cover": 0, "divider": 0, "content": 0}) is None
    with pytest.raises(TypeError):
        furniture_for_archetypes(None, {"cover": 0, "divider": 0, "content": 0})
    plain = {"layouts": {"cover": {}}}
    assert default_export_furniture(plain) is plain  # no per-layout capture: as is


# ---------------------------------------------------------------------------------- guidelines
def test_sanitize_extract_keeps_valid_fields_only() -> None:
    assert guidelines.sanitize_extract({"company": "x" * 121, "primaryColor": "#0B2D4F", "accentColor": "#0b2d4",
                                        "headingFont": "Georgia", "bodyFont": 5, "styleTemplate": "t"}) == {
        "primaryColor": "#0B2D4F", "headingFont": "Georgia", "styleTemplate": "t"}
    assert guidelines.sanitize_extract("nope") == {}


@pytest.mark.asyncio
async def test_extract_guidelines_sends_the_pdf_as_a_document() -> None:
    model = ScriptedStorylineModel({"company": "Acme"}, reply(None, stop_reason="refusal"))
    result = await guidelines.extract_guidelines(model, b"%PDF-1", model_name="claude-sonnet-5-5")
    request = model.requests[0]
    assert result.fields == {"company": "Acme"} and result.usage.cost_usd > 0
    assert request.purpose == "brand_guidelines" and request.model == "claude-sonnet-5-5"
    document = request.messages[0]["content"][0]
    assert document["type"] == "document" and base64.b64decode(document["source"]["data"]) == b"%PDF-1"
    with pytest.raises(guidelines.GuidelinesError, match="did not return an extraction tool call"):
        await guidelines.extract_guidelines(model, b"%PDF-1", model_name="m")


# ------------------------------------------------------------------------------------ userinfo
def _token(payload: Any) -> str:
    segment = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode().rstrip("=")
    return f"Bearer h.{segment}.s"


def test_atob_follows_node() -> None:
    assert userinfo.atob("aGk=") == "hi" and userinfo.atob(" a G k ") == "hi" and userinfo.atob("aGk") == "hi"
    for bad, text in (("a=Gk", "Invalid character"), ("aG===", "Invalid character"), ("aGk==", "Invalid character"),
                      ("abcde", "not correctly encoded"), ("ab$d", "Invalid character")):
        with pytest.raises(userinfo.JsError, match=text):
            userinfo.atob(bad)


def test_userinfo_claim_precedence_and_javascript_truthiness() -> None:
    assert userinfo.answer(_token({"emails": "x@y.z", "oid": "", "sub": 0})).body == {"sub": 0, "email": "x"}
    assert userinfo.answer(_token({"emails": [], "email": 7})).body == {"email": 7}
    assert userinfo.answer(_token({"emails": None, "upn": "u@corp"})).body == {"email": "u@corp"}
    assert userinfo.answer(_token(["a"])).status == 400
    assert userinfo.answer(_token({"email": "a@b", "name": None})).body == {"email": "a@b", "name": None}
    assert userinfo.answer("Bearer a.e30=").status == 400  # {}: no email
    bad_json = userinfo.answer("Bearer a." + base64.b64encode(b"{oops").decode())
    assert bad_json.status == 401 and bad_json.body["detail"].startswith("SyntaxError: ")
    assert userinfo.answer("Bearer a.").body["detail"] == "SyntaxError: Unexpected end of JSON input"


# ------------------------------------------------------------------------------- admin metrics
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _profile(pid: str, email: str | None, decks: int = 0) -> Profile:
    return Profile(id=pid, subject=pid, email=email, created_at=NOW - timedelta(days=40), lifetime_decks=decks)


def _event(uid: str, name: str, props: dict[str, Any], path: str | None = None) -> AnalyticsEvent:
    return AnalyticsEvent(id=f"e-{uid}-{len(props)}", user_id=uid, event_name=name, properties=props, page_path=path,
                          created_at=NOW)


def test_metrics_body_matches_darwins_aggregation() -> None:
    deck = AdminDeckRow(id="d1", owner_id="u1", title="", status="done", creation_method="intake_chat",
                        refine_count=2, export_count=1, created_at=NOW - timedelta(hours=1))
    older = deck.model_copy(update={"id": "d0", "creation_method": "wizard", "created_at": NOW - timedelta(days=2)})
    overview = AdminOverview(profiles=[_profile("u1", "a@acme.com", 3), _profile("u2", None, 1)],
                             decks_since=[deck, older], recent_decks=[deck],
                             usage_since=[AdminUsageRow(created_at=NOW, est_cost_usd=0.25),
                                          AdminUsageRow(created_at=NOW - timedelta(days=2), est_cost_usd=0.75)])
    activity = AdminActivity(intake=[], slide_edits=[], events=[
        _event("u1", "element_clicked", {"text": "Go", "path": "/a"}),
        _event("u1", "element_clicked", {"text": "Stop"}, "/a"),
        _event("u1", "element_clicked", {"text": "Go", "path": "/a", "x": 1}),
        _event("u2", "element_clicked", {"text": 5}),
        _event("ghost", "page_viewed", {}),
    ])
    body = admin.metrics_body(overview, activity, days=3, now=NOW)
    assert body["summary"] == {"totalUsers": 2, "totalDecks30d": 2, "decksToday": 1, "totalCostUsd": 1,
                               "usersWithDecks": 2, "powerUsers": 1, "chatDecks": 1, "wizardDecks": 1,
                               "totalRefines30d": 4, "totalExports30d": 2}
    assert body["dailyActivity"] == [{"date": "2026-10-07", "decks": 1, "costUsd": 0.75},
                                     {"date": "2026-10-08", "decks": 0, "costUsd": 0},
                                     {"date": "2026-10-09", "decks": 1, "costUsd": 0.25}]
    assert body["recentDecks"][0]["title"] == "Untitled" and body["recentDecks"][0]["ownerEmail"] == "a@acme.com"
    assert body["users"][1]["email"] == "—"
    assert body["topClicks"] == [{"text": "Go", "path": "/a", "count": 2}, {"text": "Stop", "path": "/a", "count": 1},
                                 {"text": "5", "path": "", "count": 1}]
    assert body["perUserClicks"] == [{"email": "a@acme.com", "total": 3, "topText": "Go", "topCount": 2},
                                     {"email": "—", "total": 1, "topText": "5", "topCount": 1}]
    assert body["events"][-1]["userEmail"] == "—"


def test_normalize_domain_and_js_string() -> None:
    assert admin.normalize_domain("  @Acme.COM ") == "acme.com"
    for raw, message in ((None, "domain is required"), ("acme", "Enter a bare domain"),
                         ("gmail.com", "public email provider")):
        with pytest.raises(admin.DomainError, match=message):
            admin.normalize_domain(raw)
    assert [admin.js_string(v) for v in (True, 1.0, 1.5, [1, None, "a"], {"a": 1})] == [
        "true", "1", "1.5", "1,,a", "[object Object]"]


# ------------------------------------------------------------------------------------ analytics
def test_js_iso() -> None:
    assert analytics.js_iso(datetime(2026, 10, 8, 11, 52, 3, 417_999, tzinfo=UTC)) == "2026-10-08T11:52:03.417Z"


@pytest.mark.asyncio
async def test_record_batch_maps_and_never_raises() -> None:
    store = FakeGeneralService()
    ctx, _ = await store.make_user("alice")
    await analytics.record_batch(store, ctx, [
        {"event_name": "page_exited", "session_id": "s1",
         "properties": {"path": "/x", "duration_seconds": 3.0, "scroll_depth_pct": "80", "referrer_path": "/"}},
        {"event_name": "page_exited", "properties": {"path": "/y"}},  # no session: the store refuses it, dropped
        {"event_name": "clicked", "properties": {"session_id": "s2", "text": "Go"}, "page_path": "/x"},
        {"event_name": 0}, {"event_name": "x" * 500},
    ])
    operations = [c.operation for c in store.calls]
    assert operations.count("analytics.record_page_view") == 1 and operations.count("analytics.record_event") == 1
    assert "users.touch_last_seen" in operations

    async def fail(*_a: Any, **_k: Any) -> Any:
        raise Unavailable("down")

    store.analytics.record_event = fail  # type: ignore[method-assign]
    store.users.touch_last_seen = fail  # type: ignore[method-assign]
    await analytics.record_batch(store, ctx, [{"event_name": "x"}])  # swallowed
