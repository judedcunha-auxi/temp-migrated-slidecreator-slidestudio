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


@pytest.mark.asyncio
async def test_queue_primitives():
    store = RedisClient(fakeredis.FakeAsyncRedis(decode_responses=True))
    assert await store.set_if_absent("lock", "a", ttl_ms=10_000) is True
    assert await store.set_if_absent("lock", "b", ttl_ms=10_000) is False
    assert await store.get("lock") == "a"
    assert await store.pexpire("lock", 20_000) is True
    assert await store.expire("lock", 30) is True
    assert await store.zadd("z", {"x": 2.0, "y": 1.0}) == 2
    assert await store.zadd("z", {"nope": 0.0}, only_existing=True) == 0
    assert await store.zrange("z", 0, -1) == ["y", "x"]
    assert await store.zcard("z") == 2
    assert await store.zremrangebyscore("z", float("-inf"), 1.5) == 1
    assert await store.zrem("z", "x") == 1 and await store.zrem("z") == 0
    assert await store.hset("h", {"a": "1"}) == 1
    assert await store.hincrby("h", "a", 2) == 3
    assert await store.hgetall("h") == {"a": "3"}
    assert [await store.incr("seq"), await store.incr("seq")] == [1, 2]
    await store.close()
