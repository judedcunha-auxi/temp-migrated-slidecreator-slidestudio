"""
SlideForge service: core/storage/memory. The in-memory store (STORAGE_BACKEND=fake).

The reference semantics (core/storage/reference.py) over dictionaries. Records are
kept as JSON-shaped dicts and copied on the way in and out, so a caller mutating a
returned model can never change what is stored: the same isolation a remote
service gives. Nothing survives a restart; check_config refuses it in production.

tests/fakes/general_service.py builds on this with test controls (seeding admins,
a call log for request-id assertions, a settable clock, failure injection).
"""

from __future__ import annotations

import copy
from typing import Any

from app.core.storage.models import HealthReport
from app.core.storage.reference import Clock, ReferenceStorage, utcnow


class MemoryBackend:
    name = "memory"

    def __init__(self) -> None:
        self._records: dict[str, dict[str, dict[str, Any]]] = {}
        self._bytes: dict[str, bytes] = {}

    def get(self, collection: str, key: str) -> dict[str, Any] | None:
        value = self._records.get(collection, {}).get(key)
        return None if value is None else copy.deepcopy(value)

    def put(self, collection: str, key: str, value: dict[str, Any]) -> None:
        self._records.setdefault(collection, {})[key] = copy.deepcopy(value)

    def delete(self, collection: str, key: str) -> bool:
        return self._records.get(collection, {}).pop(key, None) is not None

    def scan(self, collection: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(v) for v in self._records.get(collection, {}).values()]

    def put_bytes(self, ref: str, data: bytes) -> None:
        self._bytes[ref] = bytes(data)

    def get_bytes(self, ref: str) -> bytes | None:
        return self._bytes.get(ref)

    def delete_bytes(self, ref: str) -> None:
        self._bytes.pop(ref, None)

    def ping(self) -> HealthReport:
        # Nothing to reach: /readyz treats "skipped" as neutral.
        return HealthReport(status="skipped", backend=self.name)


class InMemoryStorage(ReferenceStorage):
    def __init__(self, *, clock: Clock = utcnow) -> None:
        self.memory = MemoryBackend()
        super().__init__(self.memory, clock=clock)
