"""The test fake's own controls, the General service stub, and the backend factory.

The requirements tests here are what keeps docs/general-service-requirements.md
honest: every port operation must have an endpoint in REQUIRED_ENDPOINTS and a
row in the document.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app.config.settings import check_config
from app.core.storage.factory import build_storage
from app.core.storage.general_service import (
    CALL_CONVENTIONS,
    NOT_IMPLEMENTED_MESSAGE,
    REQUIRED_ENDPOINTS,
    GeneralServiceStorage,
)
from app.core.storage.local_fs import LocalFsStorage
from app.core.storage.memory import InMemoryStorage
from app.core.storage.models import BrandCreate, CallerContext
from app.core.storage.ports import PORTS, Storage
from tests.conftest import build_settings
from tests.fakes.general_service import FakeGeneralService

ROOT = Path(__file__).resolve().parents[3]
REQUIREMENTS_DOC = ROOT / "docs" / "general-service-requirements.md"


def _port_operations() -> set[str]:
    ops: set[str] = set()
    for area, proto in PORTS:
        for name, member in inspect.getmembers(proto):
            if not name.startswith("_") and inspect.iscoroutinefunction(member):
                ops.add(f"{area}.{name}")
    return ops


# ------------------------------------------------------------------ the fake
@pytest.mark.asyncio
async def test_the_fake_records_the_request_id_of_every_call():
    fake = FakeGeneralService()
    ctx, _ = await fake.make_user("alice", request_id="req-123")
    await fake.brands.create(ctx, BrandCreate(name="b"))
    assert [(c.operation, c.subject, c.request_id) for c in fake.calls] == [
        ("users.get_or_create", "alice", "req-123"),
        ("brands.create", "alice", "req-123"),
    ]


@pytest.mark.asyncio
async def test_the_fake_can_be_made_unreachable():
    fake = FakeGeneralService()
    assert (await fake.health.ping(CallerContext.service("r"))).status == "skipped"
    fake.fail_health = True
    with pytest.raises(ConnectionError):
        await fake.health.ping(CallerContext.service("r"))


@pytest.mark.asyncio
async def test_make_user_admin():
    fake = FakeGeneralService()
    _, profile = await fake.make_user("root", admin=True)
    assert profile.is_admin


def test_every_adapter_satisfies_the_storage_protocol(tmp_path: Path):
    for store in (InMemoryStorage(), FakeGeneralService(), LocalFsStorage(tmp_path / "s")):
        assert isinstance(store, Storage)
        for area, proto in PORTS:
            assert isinstance(getattr(store, area), proto), f"{type(store).__name__}.{area}"


# ------------------------------------------------------------------ the stub
def test_the_general_adapter_is_a_stub_with_a_clear_message():
    with pytest.raises(NotImplementedError) as exc:
        GeneralServiceStorage("https://general.example")
    assert "D5" in str(exc.value) and exc.value.args[0] == NOT_IMPLEMENTED_MESSAGE


def test_every_port_operation_has_exactly_one_required_endpoint():
    listed = [e.operation for e in REQUIRED_ENDPOINTS]
    assert len(listed) == len(set(listed)), "an operation is listed twice"
    assert set(listed) == _port_operations()
    for e in REQUIRED_ENDPOINTS:
        assert e.method in ("GET", "POST", "PUT", "PATCH", "DELETE") and e.path.startswith("/v1/")


def test_the_requirements_doc_covers_every_operation_and_convention():
    doc = REQUIREMENTS_DOC.read_text(encoding="utf-8")
    missing = sorted(op for op in _port_operations() if f"`{op}`" not in doc)
    assert not missing, f"docs/general-service-requirements.md does not describe: {missing}"
    assert "PLACEHOLDER" in doc
    for header in ("X-Request-ID", "Idempotency-Key", "If-Match"):
        assert header in doc and any(header in c for c in CALL_CONVENTIONS)


def test_every_port_docstring_is_marked_placeholder():
    for _, proto in PORTS:
        assert re.search(r"PLACEHOLDER: confirm against General service repo", proto.__doc__ or ""), proto


# ------------------------------------------------------------------ the factory
def test_factory_builds_each_backend(tmp_path: Path):
    assert isinstance(build_storage(build_settings(storage_backend="fake")), InMemoryStorage)
    local = build_storage(build_settings(storage_backend="local", scratch_root=str(tmp_path)))
    assert isinstance(local, LocalFsStorage) and local.root == (tmp_path / "store").resolve()
    with pytest.raises(NotImplementedError):
        build_storage(build_settings(storage_backend="general", general_service_url="https://g.example"))
    with pytest.raises(ValueError):
        build_storage(build_settings(storage_backend="sql"))


def test_check_config_storage_rules(tmp_path: Path):
    prod = {"environment": "production", "redis_url": "rediss://:x@r:6380/0",
            "applicationinsights_connection_string": "InstrumentationKey=00000000-0000-0000-0000-000000000000",
            "cors_allowed_origins": "https://darwin.example"}
    for backend in ("fake", "local"):
        problems = check_config(build_settings(storage_backend=backend, **prod))
        assert any(f"STORAGE_BACKEND={backend}" in p for p in problems)
        assert not check_config(build_settings(storage_backend=backend, environment="development"))
    general = check_config(build_settings(storage_backend="general", general_service_url="http://g", **prod))
    assert any("not written yet" in p for p in general)
    assert any("GENERAL_SERVICE_URL must use https" in p for p in general)
    assert any("GENERAL_SERVICE_URL must be" in p
               for p in check_config(build_settings(storage_backend="general", environment="development")))
    assert any("STORAGE_BACKEND must be one of" in p for p in check_config(build_settings(storage_backend="s3")))
    assert any("SCRATCH_ROOT" in p for p in check_config(build_settings(scratch_root="relative/dir")))
    assert not check_config(build_settings(scratch_root=str(tmp_path)))
