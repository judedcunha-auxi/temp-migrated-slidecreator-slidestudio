"""core/redis_client: the small Redis interface, exercised over fakeredis."""

from __future__ import annotations

import fakeredis
import pytest

from app.core.redis_client import (
    COMMAND_SOCKET_TIMEOUT_S,
    POOL_MAX_CONNECTIONS,
    RedisClient,
    RedisStore,
)
from tests.conftest import build_settings


def test_the_client_satisfies_the_interface():
    assert isinstance(RedisClient(fakeredis.FakeAsyncRedis()), RedisStore)


@pytest.mark.asyncio
async def test_ping_get_set_delete():
    store = RedisClient(fakeredis.FakeAsyncRedis(decode_responses=True))
    assert await store.ping() is True
    assert await store.get("k") is None
    await store.set("k", "v", ttl_seconds=60)
    assert await store.get("k") == "v"
    assert await store.delete("k") == 1
    assert await store.get("k") is None
    await store.close()


@pytest.mark.asyncio
async def test_from_settings_sets_explicit_timeouts_and_pool_cap():
    store = RedisClient.from_settings(build_settings(redis_url="redis://127.0.0.1:6399/2", redis_protocol=2))
    pool = store._client.connection_pool
    kwargs = pool.connection_kwargs
    assert kwargs["socket_timeout"] == COMMAND_SOCKET_TIMEOUT_S
    assert kwargs["socket_connect_timeout"] is not None
    assert kwargs["db"] == 2
    assert pool.max_connections == POOL_MAX_CONNECTIONS
    await store.close()
