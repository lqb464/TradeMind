"""Redis-first TTL cache with thread-safe resilient in-process fallback."""
from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Callable


class SmartCache:
    def __init__(self, maxsize: int = 256) -> None:
        self.maxsize = maxsize
        self._memory: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._redis: Any | None = None
        self._lock = threading.RLock()
        self._key_locks = tuple(threading.Lock() for _ in range(64))
        self._redis_error: str | None = None
        if url := os.getenv("REDIS_URL"):
            try:
                import redis

                self._redis = redis.Redis.from_url(
                    url,
                    decode_responses=True,
                    socket_connect_timeout=0.5,
                    socket_timeout=0.8,
                )
                self._redis.ping()
            except Exception as exc:
                self._redis_error = type(exc).__name__
                self._redis = None

    def _disable_redis(self, exc: Exception) -> None:
        self._redis_error = type(exc).__name__
        self._redis = None

    def get(self, key: str) -> Any | None:
        if self._redis is not None:
            try:
                value = self._redis.get(key)
                if value is not None:
                    return json.loads(value)
            except Exception as exc:
                self._disable_redis(exc)
        with self._lock:
            item = self._memory.get(key)
            if not item:
                return None
            expires, value = item
            if expires < time.time():
                self._memory.pop(key, None)
                return None
            self._memory.move_to_end(key)
            return value

    def set(self, key: str, value: Any, ttl: int) -> Any:
        if ttl <= 0:
            raise ValueError("cache ttl must be positive")
        if self._redis is not None:
            try:
                self._redis.setex(key, ttl, json.dumps(value, default=str))
                return value
            except Exception as exc:
                self._disable_redis(exc)
        with self._lock:
            self._memory[key] = (time.time() + ttl, value)
            self._memory.move_to_end(key)
            while len(self._memory) > self.maxsize:
                self._memory.popitem(last=False)
        return value

    def get_or_set(self, key: str, ttl: int, factory: Callable[[], Any]) -> Any:
        cached = self.get(key)
        if cached is not None:
            return cached
        # Prevent a provider stampede when many requests miss the same key.
        key_lock = self._key_locks[hash(key) % len(self._key_locks)]
        with key_lock:
            cached = self.get(key)
            if cached is not None:
                return cached
            return self.set(key, factory(), ttl)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "backend": "redis" if self._redis is not None else "memory",
                "memory_items": len(self._memory),
                "redis_error": self._redis_error,
            }


cache = SmartCache()
