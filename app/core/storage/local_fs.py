"""
SlideForge service: core/storage/local_fs. The local dev adapter (STORAGE_BACKEND=local).

The reference semantics (core/storage/reference.py) over files, so a developer's
decks, brands and jobs survive a restart without the General service:

    <root>/records/<collection>/<key>.json   one JSON record per file
    <root>/blobs/<ref>.bin                    binary content

It never writes outside `<root>`. Every path is built by paths.safe_join, which
refuses anything but a single plain segment per part and re-checks the resolved
path (so a symlink planted under the root cannot lead out of it). Record keys that
are not plain segments (composite keys like "<deck>:<n>", domains, subjects) are
stored under their SHA-256, never under the raw text. Writes go to a temporary
file in the same directory and are swapped in with os.replace, so a crash leaves
the old record or the new one, never half of one.

Development only: one process, one lock, lists are directory scans. check_config
refuses it in production.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app.core.storage.models import HealthReport
from app.core.storage.paths import PathEscape, check_segment, resolve_root, safe_join
from app.core.storage.reference import Clock, ReferenceStorage, utcnow

_SUFFIX = ".json"


def _file_key(key: str) -> str:
    """A record key as a file name: itself when it is a plain segment, else its hash."""
    try:
        check_segment(key)
    except PathEscape:
        return "h-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    return key


class LocalFsBackend:
    name = "local"

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = resolve_root(root)

    # -- paths ---------------------------------------------------------------
    def _record_path(self, collection: str, key: str) -> Path:
        return safe_join(self.root, "records", collection, _file_key(key) + _SUFFIX)

    def _blob_path(self, ref: str) -> Path:
        return safe_join(self.root, "blobs", check_segment(ref) + ".bin")

    def _write_atomic(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # The temporary file is created in the target's own directory (already
        # proven inside the root), never in the system temp directory.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- records -------------------------------------------------------------
    def get(self, collection: str, key: str) -> dict[str, Any] | None:
        path = self._record_path(collection, key)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        value = raw.get("value") if isinstance(raw, dict) else None
        return value if isinstance(value, dict) else None

    def put(self, collection: str, key: str, value: dict[str, Any]) -> None:
        body = json.dumps({"key": key, "value": value}, ensure_ascii=False, default=str)
        self._write_atomic(self._record_path(collection, key), body.encode("utf-8"))

    def delete(self, collection: str, key: str) -> bool:
        try:
            self._record_path(collection, key).unlink()
        except FileNotFoundError:
            return False
        return True

    def scan(self, collection: str) -> list[dict[str, Any]]:
        folder = safe_join(self.root, "records", collection)
        if not folder.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for path in sorted(folder.glob("*" + _SUFFIX)):
            if path.name.startswith("."):
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            value = raw.get("value") if isinstance(raw, dict) else None
            if isinstance(value, dict):
                out.append(value)
        return out

    # -- bytes ---------------------------------------------------------------
    def put_bytes(self, ref: str, data: bytes) -> None:
        self._write_atomic(self._blob_path(ref), data)

    def get_bytes(self, ref: str) -> bytes | None:
        try:
            return self._blob_path(ref).read_bytes()
        except FileNotFoundError:
            return None

    def delete_bytes(self, ref: str) -> None:
        try:
            self._blob_path(ref).unlink()
        except FileNotFoundError:
            pass

    def ping(self) -> HealthReport:
        # Readable and writable, or raise.
        probe = safe_join(self.root, "health-probe")
        self._write_atomic(probe, b"ok")
        return HealthReport(status="ok", backend=self.name)


class LocalFsStorage(ReferenceStorage):
    def __init__(self, root: str | os.PathLike[str], *, clock: Clock = utcnow) -> None:
        self.fs = LocalFsBackend(root)
        super().__init__(self.fs, clock=clock)

    @property
    def root(self) -> Path:
        return self.fs.root
