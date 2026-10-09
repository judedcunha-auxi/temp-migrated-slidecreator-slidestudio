"""The generation batch's jobs on the fakes: darwin.generate (generate and retry), darwin.refine,
darwin.quick_generate. Every model and image call is a fake (tests/fakes)."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from app.core.darwin import prompt_text as text
from app.core.darwin.caps import CAP_REACHED
from app.core.darwin.deck_state import current_version, version_list
from app.core.darwin.generate import IMAGE_FAILED, resolve_master
from app.core.darwin.quick import QUICK_JOB
from app.core.darwin.refine import NO_SUCH_SLIDE, decode_attachment, valid_attachments
from app.core.storage.models import BrandCreate, BrandPatch, SlideSpec
from app.core.storage.models import Slide as SlideRow
from tests.fakes.darwin import DarwinEnv, darwin_env
from tests.fakes.darwin_decks import fill_image_cap, generated_deck, slide, storyline_payload, storyline_reply
from tests.fakes.image_gen import tiny_png

MASTERS = {"master": tiny_png(32, 18, (1, 1, 1)), "titleMaster": tiny_png(32, 18, (2, 2, 2))}


def brand(env: DarwinEnv, kit: dict[str, Any], assets: dict[str, bytes] | None = None, subject: str = "alice") -> str:
    env.user(subject)

    async def create() -> str:
        ctx = env.ctx(subject)
        created = await env.store.brands.create(ctx, BrandCreate(name="B", kit=kit))
        for name, data in (assets or {}).items():
            await env.store.brands.put_asset(ctx, created.id, name, data, "image/png")
        return created.id

    return env.call(create)


def deck_of(env: DarwinEnv, deck_id: str) -> Any:
    return env.call(env.store.decks.get, env.ctx("alice"), deck_id)


def ledger(env: DarwinEnv) -> list[Any]:
    return env.store.memory.scan("usage")


# ------------------------------------------------------------------------------------- generate
def test_masters_by_slide_type_and_the_ledger(tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        brand_id = brand(env, {"masterKey": "m", "titleMasterKey": "t"}, MASTERS)
        deck_id = generated_deck(env, "alice", slides=2, inputs={"topic": "Q3", "brandId": brand_id})
        calls = sorted(env.images.calls, key=lambda c: "Render ONLY the presentation title" not in c.prompt)
        assert [c.kind for c in calls] == ["edit", "edit"]
        assert all(c.prompt.startswith(text.MASTER_INSTRUCTION) for c in calls)
        assert slide(env, "alice", deck_id, 1).master_kind == "title"
        assert slide(env, "alice", deck_id, 2).master_kind == "layout"
        deck = deck_of(env, deck_id)
        assert deck.master_used is True and deck.master_skip_reason is None and deck.status == "done"
        rows = ledger(env)
        assert sorted((r["kind"], r["est_cost_usd"], r["model"], r["slide_number"]) for r in rows) == [
            ("image", 0.16, "fake-gpt-image", 1), ("image", 0.16, "fake-gpt-image", 2)]  # C10: cost in the cost column


@pytest.mark.parametrize(("kit", "assets", "reason"), [
    (None, None, "no-brand"),
    ({"primaryColor": "#000000"}, None, "no-master-uploaded"),
    ({"masterKey": "m"}, None, "blob-unavailable"),
])
def test_why_no_master(tmp_path: Path, kit: dict[str, Any] | None, assets: dict[str, bytes] | None,
                       reason: str) -> None:
    with darwin_env(tmp_path) as env:
        inputs: dict[str, Any] = {"topic": "Q3"}
        if kit is not None:
            inputs["brandId"] = brand(env, kit, assets)
        deck_id = generated_deck(env, "alice", inputs=inputs)
        deck = deck_of(env, deck_id)
        assert (deck.master_used, deck.master_skip_reason) == (False, reason)
        assert {c.kind for c in env.images.calls} == {"generate"}


def test_a_load_error_degrades_to_no_master(tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        brand_id = brand(env, {"masterKey": "m"}, MASTERS)

        async def boom(*_a: Any, **_k: Any) -> Any:
            raise ConnectionError("blob store down")

        env.store.brands.get_asset = boom  # type: ignore[method-assign]
        deck_id = generated_deck(env, "alice", inputs={"topic": "Q3", "brandId": brand_id})
        assert deck_of(env, deck_id).master_skip_reason == "load-error"
        assert slide(env, "alice", deck_id, 2).status == "done"


def test_layouts_without_a_master_condition_on_a_wireframe_in_arabic(tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        layouts = [{"name": "Title Slide", "index": 0, "placeholders": [
            {"type": "TITLE", "left": 0.1, "top": 0.3, "width": 0.8, "height": 0.2}]},
            {"name": "Title and Content", "index": 1, "placeholders": [
                {"type": "BODY", "left": 0.05, "top": 0.2, "width": 0.9, "height": 0.7}]}]
        brand_id = brand(env, {"layouts": layouts})
        generated_deck(env, "alice", inputs={"topic": "Q3", "brandId": brand_id, "language": "ar"})
        assert [c.kind for c in env.images.calls] == ["edit", "edit"]
        assert all(c.prompt.startswith(text.LAYOUT_WIREFRAME_INSTRUCTION_RTL) for c in env.images.calls)
        assert all(text.RTL_STYLE_OVERRIDE in c.prompt for c in env.images.calls)


def test_retry_keeps_the_decks_master_decision(tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        brand_id = brand(env, {"primaryColor": "#000000"})
        deck_id = generated_deck(env, "alice", inputs={"topic": "Q3", "brandId": brand_id})
        # the brand gains a master afterwards; a retried slide must still match its master-less siblings
        env.call(env.store.brands.update, env.ctx("alice"), brand_id, BrandPatch(kit={"masterKey": "m"}))
        env.call(env.store.brands.put_asset, env.ctx("alice"), brand_id, "master", MASTERS["master"], "image/png")
        env.images.calls.clear()
        r = env.client.post("/api/retry", json={"deckId": deck_id, "retryOnly": [2, "1", True]},
                            headers=env.auth("alice"))
        assert r.status_code == 202
        env.run_jobs()
        assert [c.kind for c in env.images.calls] == ["generate"]  # only slide 2 ("1" and true match nothing)
        assert current_version(slide(env, "alice", deck_id, 2)) == 2
        assert deck_of(env, deck_id).master_skip_reason == "no-master-uploaded"


def test_content_slides_of_a_workzone_brand_render_as_tiles(tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        kit = {"masterKey": "m", "workzone": {"left": 0.05, "top": 0.2, "width": 0.9, "height": 0.7},
               "furnitureArchetypes": ["content"]}
        brand_id = brand(env, kit, MASTERS)
        deck_id = generated_deck(env, "alice", inputs={"topic": "Q3", "brandId": brand_id})
        tile_call = next(c for c in env.images.calls if c.kind == "generate")
        assert tile_call.size == "2304x1008" and text.CONTENT_TILE_GUARDRAILS in tile_call.prompt
        content = slide(env, "alice", deck_id, 2)
        assert content.is_tile and content.master_kind is None
        assert not slide(env, "alice", deck_id, 1).is_tile


def test_image_failures(tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        deck_id = generated_deck(env, "alice", run=False)

        async def explode(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("secret internals at host:6379")

        env.images.generate = explode  # type: ignore[method-assign]
        env.run_jobs()
        failed = slide(env, "alice", deck_id, 1)
        assert (failed.status, failed.error) == ("error", IMAGE_FAILED)  # no internals
        assert deck_of(env, deck_id).status == "done"  # every slide terminal


def test_the_global_image_cap(tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        deck_id = generated_deck(env, "alice", run=False)
        fill_image_cap(env)
        env.run_jobs()
        assert slide(env, "alice", deck_id, 1).error == CAP_REACHED and not env.images.calls


def test_generate_persists_prompts_and_links_the_intake(tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        env.user("alice")
        r = env.client.post("/api/generate", json=storyline_payload(2, intakeSessionKey="sess-1"),
                            headers=env.auth("alice"))
        deck_id = r.json()["deckId"]
        assert "Non-negotiable rules: " in slide(env, "alice", deck_id, 2).spec.prompt
        profile = env.call(env.store.users.get_me, env.ctx("alice"))
        assert profile.lifetime_decks == 1


def test_master_resolution() -> None:
    masters = {"layout": b"L", "title": b"T"}
    assert resolve_master(masters, "title") == (b"T", "title")
    assert resolve_master(masters, "divider") == (b"L", "layout")
    assert resolve_master({}, "title") == (None, None)


# --------------------------------------------------------------------------------------- refine
def test_refine_reuses_the_slides_master_and_numbers_the_user_image(tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        brand_id = brand(env, {"masterKey": "m", "titleMasterKey": "t"}, MASTERS)
        deck_id = generated_deck(env, "alice", inputs={"topic": "Q3", "brandId": brand_id})
        env.images.calls.clear()
        r = env.client.post("/api/refine", json={"deckId": deck_id, "number": 1, "instruction": 'say "hi"',
                                                 "attachments": [{"type": "image", "media_type": "image/png",
                                                                  "data": base64.b64encode(tiny_png()).decode()}]},
                            headers=env.auth("alice"))
        assert r.status_code == 202
        env.run_jobs()
        call = env.images.calls[0]
        assert call.kind == "edit" and call.images == 2
        assert call.prompt.startswith(text.MASTER_INSTRUCTION) and "Image 2 is a reference provided by the user" \
            in call.prompt and 'Refinement requested by the user: "say hi"' in call.prompt
        refined = slide(env, "alice", deck_id, 1)
        assert refined.master_kind == "title" and refined.current == 2  # kept (Darwin dropped it)
        assert version_list(refined) == [{"v": 1, "instruction": None}, {"v": 2, "instruction": 'say "hi"'}]
        assert refined.refine_count == 1 and deck_of(env, deck_id).status == "done"


def test_refine_cap_and_a_missing_slide(tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        deck_id = generated_deck(env, "alice")
        fill_image_cap(env)
        env.client.post("/api/refine", json={"deckId": deck_id, "number": 2, "instruction": "x"},
                        headers=env.auth("alice"))
        env.client.post("/api/refine", json={"deckId": deck_id, "number": 9, "instruction": "x"},
                        headers=env.auth("alice"))
        env.run_jobs()
        assert slide(env, "alice", deck_id, 2).error == CAP_REACHED
        jobs = env.call(env.store.jobs.list_mine, env.ctx("alice"), type="darwin.refine").items
        assert sorted(j.error or "" for j in jobs) == ["", NO_SUCH_SLIDE]  # no phantom slide is created
        assert len(env.call(env.store.slides.list_for_deck, env.ctx("alice"), deck_id)) == 2


def test_attachment_helpers() -> None:
    with pytest.raises(TypeError):
        valid_attachments({"type": "image"})
    assert valid_attachments(None) == []
    assert valid_attachments([None, {"type": "image", "data": "x"}, {"type": "image", "data": "x",
                                                                   "media_type": "image/gif"}]) == [
        {"type": "image", "data": "x", "media_type": "image/gif"}]
    assert decode_attachment("aGk") == b"hi" and decode_attachment("a\nG k=") == b"hi" and decode_attachment("") == b""


# ---------------------------------------------------------------------------------------- quick
def test_quick_inline_layout_beats_the_brand_master_and_is_deleted(tmp_path: Path) -> None:
    with darwin_env(tmp_path) as env:
        brand_id = brand(env, {"masterKey": "m"}, MASTERS)
        env.model.steps.append(storyline_reply(2))
        layout = tiny_png(64, 36, (9, 9, 9))
        r = env.client.post("/api/quick-generate", json={"topic": "Q3", "brandId": brand_id,
                                                         "layoutImageB64": base64.b64encode(layout).decode()},
                            headers=env.auth("alice"))
        job_id = r.json()["jobId"]
        layout_ref = env.job("alice", job_id).inputs["layoutRef"]
        env.run_jobs()
        record = env.job("alice", job_id)
        assert record.type == QUICK_JOB and record.status == "done"
        assert [c.kind for c in env.images.calls] == ["edit", "edit"]
        with pytest.raises(Exception):  # noqa: B017 - the one-shot layout blob is gone
            env.call(env.store.blobs.get, env.ctx("alice"), layout_ref)
        kinds = sorted(r["kind"] for r in ledger(env))
        assert kinds == ["image", "image", "storyline"]


# ------------------------------------------------------------------------------------ deck state
def test_version_view_of_a_done_slide_without_versions() -> None:
    row = SlideRow(deck_id="d", number=1, spec=SlideSpec(number=1), status="done")
    assert version_list(row) == [{"v": 1, "instruction": None}] and current_version(row) == 1
    assert version_list(row.model_copy(update={"status": "error"})) == []
