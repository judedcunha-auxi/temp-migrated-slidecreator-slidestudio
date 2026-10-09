"""core/storage/local_fs: it never writes outside its root, whatever the input.

Phase 4 exit criterion: "no writes outside the scratch root". The scenario test
drives every port through the local adapter (and a job workspace) with hostile
names wherever a caller supplies one, then proves that every file that appeared
anywhere under the test's directory is inside the root.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.storage.local_fs import LocalFsBackend, LocalFsStorage
from app.core.storage.models import (
    AnalyticsEventCreate,
    BrandCreate,
    CallerContext,
    DeckCreate,
    ExportCreate,
    IdentityClaims,
    IntakeTranscriptUpsert,
    JobCreate,
    MasterCreate,
    SlideSpec,
    UsageCreate,
    VersionCreate,
)
from app.core.storage.paths import PathEscape, check_segment, resolve_root, safe_join
from app.core.storage.ports import InvalidInput, NotFound, StorageError
from app.core.storage.workspace import job_workspace

HOSTILE = [
    "../escape", "..\\escape", "../../../../etc/passwd", "/abs/path", "C:\\Windows\\x", "C:x", "a/../../b",
    "..", ".", "", "x\x00y", "nul", "CON.txt", "~", "\\\\server\\share", "a" * 300, ".hidden",
]


def _snapshot(base: Path) -> set[Path]:
    return {p for p in base.rglob("*")}


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "scratch"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_text("untouched", encoding="utf-8")
    return root, outside


@pytest.mark.parametrize("name", HOSTILE)
def test_safe_join_refuses_anything_but_a_plain_segment(tmp_path: Path, name: str):
    root = resolve_root(tmp_path / "r")
    with pytest.raises(PathEscape):
        safe_join(root, name)
    with pytest.raises(PathEscape):
        safe_join(root, "ok", name)


def test_safe_join_accepts_plain_segments(tmp_path: Path):
    root = resolve_root(tmp_path / "r")
    assert safe_join(root, "records", "decks", "abc-1.json") == root / "records" / "decks" / "abc-1.json"
    with pytest.raises(PathEscape):
        safe_join(root)
    assert check_segment("layout-preview-3.png") == "layout-preview-3.png"


@pytest.mark.parametrize("name", HOSTILE)
def test_the_backend_refuses_hostile_collections_and_refs(layout: tuple[Path, Path], name: str):
    root, outside = layout
    backend = LocalFsBackend(root)
    before = _snapshot(outside)
    with pytest.raises(PathEscape):
        backend.put(name, "k", {"v": 1})
    with pytest.raises(PathEscape):
        backend.put_bytes(name, b"x")
    # A hostile KEY is fine: it is stored under its hash, inside the root.
    backend.put("things", name, {"v": 1})
    assert backend.get("things", name) == {"v": 1}
    assert _snapshot(outside) == before


def test_a_symlink_inside_the_root_cannot_lead_out(layout: tuple[Path, Path]):
    root, outside = layout
    backend = LocalFsBackend(root)
    records = backend.root / "records"
    records.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(outside, records / "decks", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        if os.name != "nt":
            pytest.skip(f"cannot create a symlink here: {exc}")
        # Windows without symlink rights: a directory junction needs none, and
        # escapes the same way.
        import _winapi

        _winapi.CreateJunction(str(outside), str(records / "decks"))
    with pytest.raises(PathEscape):
        backend.put("decks", "d1", {"v": 1})
    assert sorted(p.name for p in outside.iterdir()) == ["sentinel.txt"]


@pytest.mark.asyncio
async def test_no_writes_outside_the_scratch_root(layout: tuple[Path, Path]):
    root, outside = layout
    base = root.parent
    before = _snapshot(base)
    store = LocalFsStorage(root / "store")
    ctx = CallerContext(subject="../../evil-subject", request_id="r1")
    admin_ctx = CallerContext(subject="admin\\..\\..", request_id="r2")
    await store.users.get_or_create(ctx, IdentityClaims(email="a@acme.com", email_verified=True))
    admin = await store.users.get_or_create(admin_ctx, IdentityClaims(email="b@auxi.ai"))
    raw = store.fs.get("profiles", admin.id)
    assert raw is not None
    raw["is_admin"] = True
    store.fs.put("profiles", admin.id, raw)

    brand = await store.brands.create(ctx, BrandCreate(name="../../brand", kit={"k": "../../v"}))
    org = await store.brands.create_org(admin_ctx, BrandCreate(name="org"))
    await store.orgs.map_domain(admin_ctx, "acme.com", org.id)
    await store.brands.put_asset(ctx, brand.id, "logo", b"png", "image/png")
    master = await store.masters.create(ctx, brand.id, MasterCreate(name="m"), idempotency_key="../../k")
    await store.masters.put_file(ctx, master.id, "layout", b"l", "image/png", layout_index=3)
    deck = await store.decks.create(ctx, DeckCreate(title="..\\..\\t"), [SlideSpec(number=1)],
                                    idempotency_key="..\\..\\..\\idem")
    await store.slides.append_version(ctx, deck.id, 1, VersionCreate(mode="html", html_ref="../../h"),
                                      idempotency_key="/etc/passwd")
    blob = await store.blobs.put(ctx, b"bytes", "application/octet-stream")
    await store.blobs.link(ctx, blob.ref)
    await store.jobs.create(ctx, JobCreate(type="t", inputs={"p": "../../x"}, idempotency_key="C:\\x"))
    await store.exports.record(ctx, ExportCreate(deck_id=deck.id, kind="pptx", slide_numbers=[1]))
    await store.usage.append(ctx, UsageCreate(model="../../m"), idempotency_key="../u")
    await store.transcripts.upsert_intake(ctx, IntakeTranscriptUpsert(session_key="../../../session"))
    await store.transcripts.put_design_history(ctx, deck.id, ["../../x"])
    await store.analytics.record_event(ctx, AnalyticsEventCreate(event_name="../../e", page_path="/../../p"))
    await store.health.ping(CallerContext.service("r3"))

    # Every caller-named thing that could reach a path is refused, not followed.
    # (An asset NAME never becomes a path, so "nul" is a legal, harmless name.)
    await store.brands.put_asset(ctx, brand.id, "nul", b"x", "image/png")
    for hostile in HOSTILE:
        attempts = [
            store.blobs.get(ctx, hostile),
            store.decks.get(ctx, hostile),
            store.jobs.get(ctx, hostile),
            store.masters.get(ctx, hostile),
        ]
        if hostile != "nul":
            attempts.append(store.brands.put_asset(ctx, brand.id, hostile, b"x", "image/png"))
        for attempt in attempts:
            with pytest.raises((StorageError, PathEscape)):
                await attempt

    # A job workspace under the same scratch root, with hostile names too.
    async with job_workspace(store.blobs, ctx, root, "job-1", inputs={"in.bin": blob.ref}) as ws:
        ws.output_path("deck.pptx").write_bytes(b"pptx")
        for hostile in HOSTILE:
            with pytest.raises(PathEscape):
                ws.output_path(hostile)
    for hostile in ("../job", "a/b", ""):
        with pytest.raises(PathEscape):
            async with job_workspace(store.blobs, ctx, root, hostile):
                pass

    resolved_root = root.resolve()
    new_files = [p for p in _snapshot(base) - before if p.is_file()]
    assert new_files, "the scenario wrote nothing; the test proves nothing"
    escaped = [p for p in new_files if not p.resolve().is_relative_to(resolved_root)]
    assert escaped == [], f"written outside the scratch root: {escaped}"
    assert sorted(p.name for p in outside.iterdir()) == ["sentinel.txt"]
    assert (outside / "sentinel.txt").read_text(encoding="utf-8") == "untouched"


@pytest.mark.asyncio
async def test_records_survive_a_restart(tmp_path: Path):
    ctx = CallerContext(subject="alice", request_id="r")
    first = LocalFsStorage(tmp_path / "store")
    await first.users.get_or_create(ctx, IdentityClaims())
    deck = await first.decks.create(ctx, DeckCreate(title="kept"), [])
    second = LocalFsStorage(tmp_path / "store")
    assert (await second.decks.get(ctx, deck.id)).title == "kept"


@pytest.mark.asyncio
async def test_a_corrupt_record_file_is_skipped_by_scans(tmp_path: Path):
    store = LocalFsStorage(tmp_path / "store")
    ctx = CallerContext(subject="alice", request_id="r")
    await store.users.get_or_create(ctx, IdentityClaims())
    await store.decks.create(ctx, DeckCreate(title="ok"), [])
    (store.root / "records" / "decks" / "garbage.json").write_text("{not json", encoding="utf-8")
    assert [d.title for d in (await store.decks.list_mine(ctx)).items] == ["ok"]


@pytest.mark.asyncio
async def test_oversized_and_odd_inputs_are_refused(tmp_path: Path):
    store = LocalFsStorage(tmp_path / "store")
    ctx = CallerContext(subject="alice", request_id="r")
    await store.users.get_or_create(ctx, IdentityClaims())
    with pytest.raises(InvalidInput):
        await store.blobs.put(ctx, b"x", "not a type")
    with pytest.raises(InvalidInput):
        await store.brands.create(ctx, BrandCreate(name="big", kit={"x": "y" * 2_100_000}))
    with pytest.raises(NotFound):
        await store.decks.get(ctx, "../../decks")
