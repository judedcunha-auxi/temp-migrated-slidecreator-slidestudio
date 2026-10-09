"""
SlideForge service: core/storage/factory. STORAGE_BACKEND -> a `ports.Storage`.

    fake     InMemoryStorage: nothing persists; development and tests.
    local    LocalFsStorage under <SCRATCH_ROOT>/store; development only.
    general  GeneralServiceStorage: the General service. A stub until its repo
             exists (D5), so building it raises NotImplementedError.

check_config refuses fake and local in production, so a deployed instance with
either stays out of service at /readyz.
"""

from __future__ import annotations

from pathlib import Path

from app.config.settings import Settings
from app.core.storage.general_service import GeneralServiceStorage
from app.core.storage.local_fs import LocalFsStorage
from app.core.storage.memory import InMemoryStorage
from app.core.storage.paths import default_scratch_root, resolve_root
from app.core.storage.ports import Storage

LOCAL_STORE_DIRNAME = "store"


def scratch_root(s: Settings) -> Path:
    """The resolved scratch root (created if missing). Every local write is under it."""
    return resolve_root(s.scratch_root or default_scratch_root())


def build_storage(s: Settings) -> Storage:
    backend = s.storage_backend
    if backend == "fake":
        return InMemoryStorage()
    if backend == "local":
        return LocalFsStorage(scratch_root(s) / LOCAL_STORE_DIRNAME)
    if backend == "general":
        return GeneralServiceStorage(s.general_service_url)  # type: ignore[return-value]
    raise ValueError("unknown STORAGE_BACKEND")
