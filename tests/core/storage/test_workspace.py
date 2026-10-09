"""core/storage/workspace: materialise from the port, persist to it, always clean up."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.storage.models import CallerContext
from app.core.storage.ports import NotFound
from app.core.storage.workspace import content_type_for, job_workspace
from tests.fakes.general_service import FakeGeneralService

pytestmark = pytest.mark.asyncio


async def _setup() -> tuple[FakeGeneralService, CallerContext, str]:
    store = FakeGeneralService()
    ctx, _ = await store.make_user("alice")
    info = await store.blobs.put(ctx, b"PK master", "application/octet-stream")
    return store, ctx, info.ref


async def test_materialise_work_persist_cleanup(tmp_path: Path):
    store, ctx, ref = await _setup()
    async with job_workspace(store.blobs, ctx, tmp_path, "job-1", inputs={"master.pptx": ref}) as ws:
        assert ws.root.parent == (tmp_path / "jobs").resolve()
        assert ws.input_path("master.pptx").read_bytes() == b"PK master"
        ws.output_path("deck.pptx").write_bytes(b"deck")
        (ws.outputs_dir / "previews").mkdir()
        (ws.outputs_dir / "previews" / "1.png").write_bytes(b"png")
        root = ws.root
    assert not root.exists()
    assert set(ws.persisted) == {"deck.pptx", "previews/1.png"}
    deck = await store.blobs.get(ctx, ws.persisted["deck.pptx"].ref)
    assert (deck.data, deck.info.content_type) == (
        b"deck", "application/vnd.openxmlformats-officedocument.presentationml.presentation")
    assert (await store.blobs.get(ctx, ws.persisted["previews/1.png"].ref)).info.content_type == "image/png"


async def test_failure_persists_nothing_and_still_cleans_up(tmp_path: Path):
    store, ctx, ref = await _setup()
    with pytest.raises(RuntimeError):
        async with job_workspace(store.blobs, ctx, tmp_path, "job-2", inputs={"m.pptx": ref}) as ws:
            ws.output_path("half.pptx").write_bytes(b"half")
            raise RuntimeError("engine crashed")
    assert ws.persisted == {}
    assert not ws.root.exists()


async def test_inputs_go_through_the_callers_ownership(tmp_path: Path):
    store, _, ref = await _setup()
    bob, _ = await store.make_user("bob")
    with pytest.raises(NotFound):
        async with job_workspace(store.blobs, bob, tmp_path, "job-3", inputs={"m.pptx": ref}):
            pass
    assert list((tmp_path / "jobs").iterdir()) == []  # cleaned up on the way out


async def test_two_attempts_of_one_job_never_share_a_directory(tmp_path: Path):
    store, ctx, _ = await _setup()
    async with job_workspace(store.blobs, ctx, tmp_path, "job-4") as a:
        async with job_workspace(store.blobs, ctx, tmp_path, "job-4") as b:
            assert a.root != b.root


async def test_persist_can_be_turned_off(tmp_path: Path):
    store, ctx, _ = await _setup()
    async with job_workspace(store.blobs, ctx, tmp_path, "job-5", persist=False) as ws:
        ws.output_path("x.json").write_text("{}", encoding="utf-8")
    assert ws.persisted == {}


async def test_content_types():
    assert content_type_for("a.pdf") == "application/pdf"
    assert content_type_for("noext") == "application/octet-stream"
