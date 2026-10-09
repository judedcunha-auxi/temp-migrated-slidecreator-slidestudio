"""A design deck as one stored blob: how a designed slide outlives its job workspace.

A design deck lives in a job's scratch workspace, which is removed when the job ends. To be stitched
into a deck later (or refined in a later job), it is packed into a zip and stored through the
storage port (the job workspace persists everything under `out/`). `unpack` restores it into another
job's workspace.

Unpacking is defensive: every entry must be a plain relative path inside the destination (no `..`,
no absolute paths, no drive letters, no links), the entry count and total size are bounded, and the
result must be a design deck (`project.json`).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path, PurePosixPath

from app.core.design.deck import DesignDeck
from app.core.storage.paths import is_within

#: What goes in the bundle (scratch folders such as `.import`, `export` stay out).
INCLUDE = ("project.json", "manifest.json", "master.pptx", "history.json", "transcript.json", "diagnostics.json")
INCLUDE_DIRS = ("slides", "layouts", "assets", "attachments")
MAX_ENTRIES = 5000
MAX_TOTAL_BYTES = 300 * 1024 * 1024


class BundleError(ValueError):
    """The blob is not a usable design deck bundle."""


def pack(deck: DesignDeck) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in INCLUDE:
            path = deck.dir / name
            if path.is_file():
                archive.write(path, name)
        for folder in INCLUDE_DIRS:
            root = deck.dir / folder
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    archive.write(path, path.relative_to(deck.dir).as_posix())
    return buffer.getvalue()


def unpack(data: bytes, dest: Path) -> DesignDeck:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BundleError("not a deck bundle") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
            raise BundleError("the deck bundle is too large")
        for info in infos:
            name = PurePosixPath(info.filename)
            if info.is_dir():
                continue
            if name.is_absolute() or ".." in name.parts or ":" in info.filename or "\\" in info.filename:
                raise BundleError("the deck bundle holds an unsafe path")
            if name.parts[0] not in INCLUDE_DIRS and str(name) not in INCLUDE:
                raise BundleError("the deck bundle holds an unexpected file")
            target = (root / Path(*name.parts)).resolve()
            if not is_within(target, root):
                raise BundleError("the deck bundle holds an unsafe path")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
    for folder in ("layouts", "assets", "slides", "attachments"):
        (root / folder).mkdir(exist_ok=True)
    try:
        return DesignDeck(root)
    except KeyError as exc:
        raise BundleError("the bundle is not a design deck") from exc
