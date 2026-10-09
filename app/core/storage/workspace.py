"""
SlideForge service: core/storage/workspace. A scratch directory per job, backed by the port.

The engine works on files (python-pptx, Playwright), the service's data lives in
the General service. A job workspace bridges the two (plan §2.1):

    async with job_workspace(storage.blobs, ctx, root, job.id, inputs={"master.pptx": ref}) as ws:
        run_engine(ws.input_path("master.pptx"), ws.output_path("deck.pptx"))
    ws.persisted["deck.pptx"]   # the AssetInfo the output was stored as

1. materialise: every named input blob is fetched through the port (so the
   caller's ownership applies) and written to `<ws>/in/<name>`;
2. the job writes its results to `<ws>/out/`;
3. persist: when the block ends without an exception, every regular file under
   `out/` is stored through the port and recorded in `ws.persisted`;
4. cleanup: the directory is removed however the block ends.

Each workspace is `<scratch root>/jobs/<job id>-<random>/`, so two attempts of one
job (a re-queued lease) never share files. Names are single safe segments
(paths.safe_join), and an output that resolves outside the workspace (a
symlink) is skipped, never followed.
"""

from __future__ import annotations

import logging
import mimetypes
import secrets
import shutil
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from app.core.storage.models import AssetInfo, CallerContext
from app.core.storage.paths import check_segment, is_within, resolve_root, safe_join
from app.core.storage.ports import BlobPort

_log = logging.getLogger(__name__)

JOBS_DIRNAME = "jobs"
DEFAULT_CONTENT_TYPE = "application/octet-stream"
_CONTENT_TYPES = {
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".html": "text/html",
    ".json": "application/json",
}


def content_type_for(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return _CONTENT_TYPES.get(suffix) or mimetypes.guess_type(name)[0] or DEFAULT_CONTENT_TYPE


@dataclass
class Workspace:
    root: Path
    inputs_dir: Path
    outputs_dir: Path
    materialised: dict[str, Path] = field(default_factory=dict)
    persisted: dict[str, AssetInfo] = field(default_factory=dict)

    def input_path(self, name: str) -> Path:
        return safe_join(self.inputs_dir, name)

    def output_path(self, name: str) -> Path:
        return safe_join(self.outputs_dir, name)


@asynccontextmanager
async def job_workspace(
    blobs: BlobPort,
    ctx: CallerContext,
    scratch_root: Path,
    job_id: str,
    *,
    inputs: Mapping[str, str] | None = None,
    persist: bool = True,
) -> AsyncIterator[Workspace]:
    """A fresh workspace for one job attempt. See the module docstring."""
    check_segment(job_id)
    for name in inputs or {}:
        check_segment(name)
    root = resolve_root(scratch_root)
    jobs_dir = safe_join(root, JOBS_DIRNAME)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    ws_root = safe_join(jobs_dir, f"{job_id}-{secrets.token_hex(4)}")
    ws_root.mkdir()
    ws = Workspace(root=ws_root, inputs_dir=safe_join(ws_root, "in"), outputs_dir=safe_join(ws_root, "out"))
    ws.inputs_dir.mkdir()
    ws.outputs_dir.mkdir()
    try:
        for name, ref in (inputs or {}).items():
            stored = await blobs.get(ctx, ref)
            path = ws.input_path(name)
            path.write_bytes(stored.data)
            ws.materialised[name] = path
        yield ws
        if persist:
            await _persist_outputs(blobs, ctx, ws)
    finally:
        shutil.rmtree(ws_root, ignore_errors=True)


async def _persist_outputs(blobs: BlobPort, ctx: CallerContext, ws: Workspace) -> None:
    for path in sorted(ws.outputs_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if not is_within(path.resolve(), ws.outputs_dir):
            _log.warning("workspace: skipped an output that resolves outside the workspace")
            continue
        name = path.relative_to(ws.outputs_dir).as_posix()
        ws.persisted[name] = await blobs.put(ctx, path.read_bytes(), content_type_for(name))
