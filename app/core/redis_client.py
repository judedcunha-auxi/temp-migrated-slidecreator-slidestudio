"""
SlideForge service: core/redis_client. Redis behind a small interface.

Redis holds the job queue, the in-flight limiter and the counters (decision D6);
durable data goes to the General service, never here. Phase 1 needs only
reachability (for /readyz) and a few key operations; later phases add to the
`RedisStore` protocol rather than reaching for the raw client, so a test can swap
in fakeredis (tests/conftest.py) and every use stays visible in one place.

Explicit timeouts and pool caps, never redis-py's defaults: redis-py 8.0 changed
both (socket_timeout None -> 5s, pool unbounded -> 100), and each change caused an
incident in Auxi_Connector.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import redis.asyncio as aioredis

from app.config.settings import Settings

_log = logging.getLogger(__name__)

# Request-path commands are all small; a blackholed connection must not hang one.
COMMAND_SOCKET_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 5.0
# Readiness must answer well inside the platform's probe timeout.
PING_TIMEOUT_S = 2.0
POOL_MAX_CONNECTIONS = 100
HEALTH_CHECK_INTERVAL_S = 30  # keeps idle connections alive behind Azure's load balancer


@runtime_checkable
class RedisStore(Protocol):
    """What the service may ask of Redis. Add an operation here before using it."""

    async def ping(self) -> bool: ...

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None: ...

    async def delete(self, key: str) -> int: ...

    async def close(self) -> None: ...


class RedisClient:
    """`RedisStore` over a redis-py asyncio client (real Redis, or fakeredis in tests)."""

    def __init__(self, client: aioredis.Redis) -> None:
        self._client = client

    @classmethod
    def from_settings(cls, s: Settings) -> RedisClient:
        client = aioredis.from_url(
            s.redis_url,
            decode_responses=True,
            protocol=s.redis_protocol,
            socket_timeout=COMMAND_SOCKET_TIMEOUT_S,
            socket_connect_timeout=CONNECT_TIMEOUT_S,
            health_check_interval=HEALTH_CHECK_INTERVAL_S,
            max_connections=POOL_MAX_CONNECTIONS,
        )
        return cls(client)

    async def ping(self) -> bool:
        return bool(await self._client.ping())

    async def get(self, key: str) -> str | None:
        value = await self._client.get(key)
        return None if value is None else str(value)

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        await self._client.set(key, value, ex=ttl_seconds)

    async def delete(self, key: str) -> int:
        return int(await self._client.delete(key))

    async def close(self) -> None:
        await self._client.aclose()
