"""The General service fake: every test, and Phase 4, uses it (plan §2.2).

`FakeGeneralService` is the in-memory store (app/core/storage/memory.py), so it has
the REAL port semantics (ownership, the org-brand ACL, idempotency, append-only
versions; the contract suite holds it to them), plus what tests need to steer it:

* `clock`: a settable clock, for TTLs and day rollovers;
* `make_user(...)`: provision a caller in one line, optionally an admin (admin
  is a server-only fact the port cannot set, as in production);
* `calls`: every port call as (operation, subject, request_id), to assert that
  the request id reaches the General service;
* `fail_health`: make the readiness ping raise, as an unreachable service would.

It is a test double: it lives under tests/, never ships, and the app's own
STORAGE_BACKEND=fake uses the plain InMemoryStorage.
"""

from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.storage.memory import InMemoryStorage
from app.core.storage.models import CallerContext, IdentityClaims, Profile
from app.core.storage.ports import PORTS


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now = self.now + timedelta(**delta)


@dataclass(frozen=True)
class Call:
    operation: str
    subject: str
    request_id: str


class _Recorder:
    """Wraps one port: records each call, then delegates to the real method."""

    def __init__(self, area: str, target: Any, log: list[Call]) -> None:
        self._area = area
        self._target = target
        self._log = log

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        if not inspect.iscoroutinefunction(attr):
            return attr

        @functools.wraps(attr)
        async def recorded(ctx: CallerContext, *args: Any, **kwargs: Any) -> Any:
            self._log.append(Call(f"{self._area}.{name}", ctx.subject, ctx.request_id))
            return await attr(ctx, *args, **kwargs)

        return recorded


class FakeGeneralService(InMemoryStorage):
    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock or FakeClock()
        super().__init__(clock=self.clock)
        self.calls: list[Call] = []
        self.fail_health = False
        for area, _ in PORTS:
            setattr(self, area, _Recorder(area, getattr(self, area), self.calls))
        health = self.health

        class _Health:
            async def ping(inner_self: Any, ctx: CallerContext) -> Any:
                if self.fail_health:
                    raise ConnectionError("general-service.internal:443 unreachable")
                return await health.ping(ctx)

        self.health = _Health()  # type: ignore[assignment]

    async def make_user(
        self,
        subject: str,
        *,
        email: str | None = None,
        verified: bool = True,
        admin: bool = False,
        request_id: str = "req-test",
    ) -> tuple[CallerContext, Profile]:
        ctx = CallerContext(subject=subject, request_id=request_id)
        profile = await self.users.get_or_create(ctx, IdentityClaims(email=email, email_verified=verified))
        if admin:
            raw = self.memory.get("profiles", profile.id)
            assert raw is not None
            raw["is_admin"] = True
            self.memory.put("profiles", profile.id, raw)
            profile = await self.users.get_me(ctx)
        return ctx, profile
