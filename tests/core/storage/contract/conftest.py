"""The contract suite's adapters. Every test in this folder runs once per adapter.

To add an adapter, add a branch to `harness` and a value to ADAPTERS. The General
service adapter is listed but skipped until its repo exists (D5); when it arrives
its branch provisions users through a test tenant and promotes admins out of band
(the port deliberately has no "make admin" operation).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.core.storage.local_fs import LocalFsStorage
from app.core.storage.models import CallerContext, IdentityClaims, Profile
from app.core.storage.ports import Storage
from app.core.storage.reference import ReferenceStorage
from tests.fakes.general_service import FakeClock, FakeGeneralService

ADAPTERS = (
    "fake",
    "local_fs",
    pytest.param("general", marks=pytest.mark.skip(reason="General service repo does not exist yet (D5)")),
)


@dataclass
class Harness:
    name: str
    storage: Storage
    clock: FakeClock
    promote: Callable[[str], None]
    root: Path | None = None
    _n: list[int] = field(default_factory=lambda: [0])

    async def user(
        self, subject: str, *, email: str | None = None, verified: bool = True, admin: bool = False
    ) -> tuple[CallerContext, Profile]:
        self._n[0] += 1
        ctx = CallerContext(subject=subject, request_id=f"req-{self._n[0]}")
        profile = await self.storage.users.get_or_create(ctx, IdentityClaims(email=email, email_verified=verified))
        if admin:
            self.promote(profile.id)
            profile = await self.storage.users.get_me(ctx)
        return ctx, profile


def _promoter(store: ReferenceStorage) -> Callable[[str], None]:
    backend = store._core.backend

    def promote(user_id: str) -> None:
        raw = backend.get("profiles", user_id)
        assert raw is not None
        raw["is_admin"] = True
        backend.put("profiles", user_id, raw)

    return promote


@pytest.fixture(params=ADAPTERS)
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> Harness:
    clock = FakeClock()
    if request.param == "fake":
        fake = FakeGeneralService(clock)
        return Harness("fake", fake, clock, _promoter(fake))
    if request.param == "local_fs":
        local = LocalFsStorage(tmp_path / "root", clock=clock)
        return Harness("local_fs", local, clock, _promoter(local), root=local.root)
    raise NotImplementedError(request.param)


@pytest.fixture
def storage(harness: Harness) -> Storage:
    return harness.storage
