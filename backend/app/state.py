"""Redis-backed sessions, fixed-window limits, and expiring concurrency leases."""
import asyncio
import json
import time
from typing import Protocol


class State(Protocol):
    async def allow(self, key: str, limit: int, seconds: int) -> bool: ...
    async def get(self, key: str) -> dict | None: ...
    async def put(self, key: str, value: dict, ttl: int) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def acquire(self, key: str, token: str, limit: int, ttl: int) -> bool: ...
    async def release(self, key: str, token: str) -> None: ...
    async def ping(self) -> None: ...
    async def close(self) -> None: ...


class RedisState:
    def __init__(self, url: str):
        from redis.asyncio import Redis
        self.redis = Redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)

    async def allow(self, key, limit, seconds):
        script = """
        local n = redis.call('INCR', KEYS[1])
        if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
        return n <= tonumber(ARGV[2]) and 1 or 0
        """
        bucket = int(time.time()) // seconds
        return bool(await self.redis.eval(script, 1, f'limit:{key}:{bucket}', seconds + 1, limit))

    async def get(self, key):
        value = await self.redis.get(key)
        return json.loads(value) if value else None

    async def put(self, key, value, ttl):
        await self.redis.set(key, json.dumps(value), ex=ttl)

    async def delete(self, key):
        await self.redis.delete(key)

    async def acquire(self, key, token, limit, ttl):
        script = """
        local t = redis.call('TIME')
        local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
        redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
        if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
        redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[3])
        redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]) + 1)
        return 1
        """
        return bool(await self.redis.eval(script, 1, 'lease:' + key, limit, ttl, token))

    async def release(self, key, token):
        await self.redis.zrem('lease:' + key, token)

    async def ping(self):
        await self.redis.ping()

    async def close(self):
        await self.redis.aclose()


class MemoryState:
    """Test-only implementation; production never falls back to process-local limits."""
    def __init__(self):
        self.values = {}
        self.counts = {}
        self.leases = {}
        self.lock = asyncio.Lock()

    async def allow(self, key, limit, seconds):
        async with self.lock:
            k = (key, int(time.time()) // seconds)
            self.counts[k] = self.counts.get(k, 0) + 1
            return self.counts[k] <= limit

    async def get(self, key):
        value = self.values.get(key)
        return value[0] if value and value[1] > time.time() else None

    async def put(self, key, value, ttl):
        self.values[key] = (value, time.time() + ttl)

    async def delete(self, key):
        self.values.pop(key, None)

    async def acquire(self, key, token, limit, ttl):
        async with self.lock:
            live = {k: v for k, v in self.leases.get(key, {}).items() if v > time.time()}
            self.leases[key] = live
            if len(live) >= limit:
                return False
            live[token] = time.time() + ttl
            return True

    async def release(self, key, token):
        self.leases.get(key, {}).pop(token, None)

    async def ping(self):
        pass

    async def close(self):
        pass
