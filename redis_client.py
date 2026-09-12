"""Thin async Redis wrapper for ReedMuhn's fast/temporary state layer.

SQLite (db.py / voicelink/sqlite_db.py) stays the source of truth for
anything permanent - warnings, cases, tickets, economy balances, music
settings, etc. Redis is only for data that's fine to lose: cooldowns,
rate limits, spam/automod counters, and short-lived cross-process
signaling between the bot and the WebUI.

Redis is optional. If REDIS_URL isn't set, or the server is unreachable,
every method here degrades to a local in-memory fallback (a plain dict
with manual TTL bookkeeping) so a missing/misbehaving Redis instance
never takes the bot down - it just loses the "survives a restart" and
"shared across processes" benefits until Redis is back.
"""
from __future__ import annotations

import json
import logging
import time
from typing import AsyncIterator, Optional

logger = logging.getLogger("redis_client")

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover - exercised only if the dependency is missing
    aioredis = None


class _MemoryFallback:
    """Dict-backed stand-in used when Redis is disabled or unreachable.

    Implements the same surface as RedisClient's real-Redis path, but
    entirely in-process. Pub/sub (`publish`/`listen`) is intentionally a
    no-op here: there is no cross-process signaling without a real Redis
    server, so callers relying on wake signals should keep working off
    their normal polling loop in that case.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, Optional[float]]] = {}

    def _purge_expired(self, key: str) -> None:
        entry = self._store.get(key)
        if entry is not None and entry[1] is not None and entry[1] <= time.monotonic():
            self._store.pop(key, None)

    async def get(self, key: str) -> Optional[str]:
        self._purge_expired(key)
        entry = self._store.get(key)
        return entry[0] if entry is not None else None

    async def set(self, key: str, value: str, ex: Optional[int] = None) -> None:
        expires_at = time.monotonic() + ex if ex is not None else None
        self._store[key] = (str(value), expires_at)

    async def incr(self, key: str) -> int:
        self._purge_expired(key)
        entry = self._store.get(key)
        current = int(entry[0]) if entry is not None else 0
        current += 1
        expires_at = entry[1] if entry is not None else None
        self._store[key] = (str(current), expires_at)
        return current

    async def expire(self, key: str, seconds: int) -> None:
        entry = self._store.get(key)
        if entry is not None:
            self._store[key] = (entry[0], time.monotonic() + seconds)

    async def ttl(self, key: str) -> int:
        self._purge_expired(key)
        entry = self._store.get(key)
        if entry is None:
            return -2
        if entry[1] is None:
            return -1
        return max(0, int(entry[1] - time.monotonic()))

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def publish(self, channel: str, message: str) -> None:
        # No cross-process pub/sub without a real Redis server. Callers
        # (webui wake, bot wake loop) should fall back to their own
        # polling in this case - see docstring above.
        return

    async def publish_json(self, channel: str, payload: dict) -> None:
        return

    async def listen(self, channel: str) -> AsyncIterator[dict]:
        return
        yield  # pragma: no cover - makes this an async generator; unreachable

    async def close(self) -> None:
        return


class RedisClient:
    """Public async Redis wrapper. Connects lazily via `connect()` and
    degrades to `_MemoryFallback` if REDIS_URL is unset, the redis
    package isn't installed, the initial connection fails, or any
    later call raises."""

    def __init__(self, url: Optional[str]) -> None:
        self.url = url
        self.enabled = False
        self._client: object = _MemoryFallback()

    async def connect(self) -> None:
        if not self.url or aioredis is None:
            self.enabled = False
            self._client = _MemoryFallback()
            return
        try:
            client = aioredis.from_url(self.url, decode_responses=True)
            await client.ping()
        except Exception:
            logger.exception("Redis connection failed; falling back to memory")
            self._client = _MemoryFallback()
            self.enabled = False
            return
        self._client = client
        self.enabled = True

    def _fallback(self) -> _MemoryFallback:
        if not isinstance(self._client, _MemoryFallback):
            self._client = _MemoryFallback()
        self.enabled = False
        return self._client

    async def get(self, key: str) -> Optional[str]:
        try:
            return await self._client.get(key)
        except Exception:
            logger.exception("Redis GET failed; falling back to memory")
            return await self._fallback().get(key)

    async def set(self, key: str, value: str, ex: Optional[int] = None) -> None:
        try:
            await self._client.set(key, value, ex=ex)
        except Exception:
            logger.exception("Redis SET failed; falling back to memory")
            await self._fallback().set(key, value, ex=ex)

    async def incr(self, key: str) -> int:
        try:
            return await self._client.incr(key)
        except Exception:
            logger.exception("Redis INCR failed; falling back to memory")
            return await self._fallback().incr(key)

    async def expire(self, key: str, seconds: int) -> None:
        try:
            await self._client.expire(key, seconds)
        except Exception:
            logger.exception("Redis EXPIRE failed; falling back to memory")
            await self._fallback().expire(key, seconds)

    async def ttl(self, key: str) -> int:
        try:
            return await self._client.ttl(key)
        except Exception:
            logger.exception("Redis TTL failed; falling back to memory")
            return await self._fallback().ttl(key)

    async def delete(self, key: str) -> None:
        try:
            await self._client.delete(key)
        except Exception:
            logger.exception("Redis DELETE failed; falling back to memory")
            await self._fallback().delete(key)

    async def publish(self, channel: str, message: str) -> None:
        if not self.enabled:
            return
        try:
            await self._client.publish(channel, message)
        except Exception:
            logger.exception("Redis PUBLISH failed; disabling Redis until reconnect")
            self._fallback()

    async def publish_json(self, channel: str, payload: dict) -> None:
        if not self.enabled:
            return
        await self.publish(channel, json.dumps(payload, separators=(",", ":")))

    async def listen(self, channel: str) -> AsyncIterator[dict]:
        if not self.enabled or self._client is None:
            return
        pubsub = self._client.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                raw = message.get("data")
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    logger.warning("Ignoring malformed Redis JSON message on %s", channel)
                    continue
                if isinstance(payload, dict):
                    yield payload
        finally:
            try:
                await pubsub.unsubscribe(channel)
            finally:
                await pubsub.close()

    async def close(self) -> None:
        if self.enabled and self._client is not None:
            try:
                await self._client.close()
            except Exception:
                logger.exception("Error while closing Redis client")
        self._client = _MemoryFallback()
        self.enabled = False

    async def seconds_remaining(self, key: str) -> int:
        """Convenience for cooldown checks: 0 if the key doesn't exist or
        has expired, otherwise the whole seconds left on its TTL."""
        ttl = await self.ttl(key)
        return max(0, ttl)

    async def start_cooldown(self, key: str, seconds: int) -> None:
        """Set a cooldown marker that expires on its own after `seconds`."""
        await self.set(key, "1", ex=seconds)
