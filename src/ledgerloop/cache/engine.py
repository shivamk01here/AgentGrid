"""Cache engine - in-memory cache with pluggable backends."""

from __future__ import annotations

import logging
import time
from typing import Any

from ledgerloop.cache.backend import CacheBackend
from ledgerloop.cache.entry import CacheEntry

logger = logging.getLogger(__name__)


class InMemoryCache:
    """Default in-memory cache storage backend."""

    def __init__(self) -> None:
        self._store: dict[str, CacheEntry] = {}

    async def get(self, key: str) -> CacheEntry | None:
        return self._store.get(key)

    async def set(self, key: str, entry: CacheEntry) -> None:
        self._store[key] = entry

    async def delete(self, key: str) -> bool:
        if key in self._store:
            del self._store[key]
            return True
        return False

    async def clear(self) -> int:
        count = len(self._store)
        self._store.clear()
        return count

    async def list_all(self) -> list[CacheEntry]:
        expired = [key for key, entry in self._store.items() if entry.is_expired()]
        for key in expired:
            del self._store[key]
        return list(self._store.values())

    @property
    def size(self) -> int:
        return len(self._store)


class CacheEngine:
    """Provides a cache with TTL support and optional custom backends.

    Uses an in-memory cache by default. Pass a custom CacheBackend
    for Redis, Memcached, or other storage backends.
    """

    def __init__(
        self,
        namespace: str = "default",
        backend: CacheBackend | None = None,
    ) -> None:
        self.namespace = namespace
        self._backend: CacheBackend = backend or InMemoryCache()

    async def get(self, key: str) -> Any | None:
        """Retrieve a value by key. Returns None if missing or expired."""
        prefixed = self._prefixed(key)
        entry = await self._backend.get(prefixed)
        if entry is None:
            return None
        if entry.is_expired():
            await self._backend.delete(prefixed)
            return None
        return entry.value

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl: float | None = None,
        tags: list[str] | None = None,
    ) -> CacheEntry:
        """Store a value in the cache.

        Args:
            key: Unique key within the namespace.
            value: The value to cache.
            ttl: Optional time-to-live in seconds. None means permanent.
            tags: Optional tags for filtering.
        """
        prefixed = self._prefixed(key)
        entry = CacheEntry(
            key=prefixed,
            value=value,
            ttl=ttl,
            tags=tags or [],
            created_at=time.time(),
        )
        await self._backend.set(prefixed, entry)
        logger.debug("Cached key=%s namespace=%s", key, self.namespace)
        return entry

    async def delete(self, key: str) -> bool:
        """Delete a key. Returns True if it existed."""
        return await self._backend.delete(self._prefixed(key))

    async def clear(self) -> int:
        """Clear all entries in this namespace. Returns count removed."""
        entries = await self._backend.list_all()
        count = 0
        for entry in entries:
            if not entry.key.startswith(f"{self.namespace}:"):
                continue
            if await self._backend.delete(entry.key):
                count += 1
        return count

    async def search(self, *, tag: str | None = None, prefix: str = "") -> list[CacheEntry]:
        """Search cache entries by tag or key prefix within this namespace."""
        all_entries = await self._backend.list_all()
        results = []
        for entry in all_entries:
            if not entry.key.startswith(f"{self.namespace}:"):
                continue
            raw_key = entry.key[len(self.namespace) + 1:]
            if tag and tag not in entry.tags:
                continue
            if prefix and not raw_key.startswith(prefix):
                continue
            results.append(CacheEntry(
                key=raw_key,
                value=entry.value,
                ttl=entry.ttl,
                tags=entry.tags,
                created_at=entry.created_at,
            ))
        return results

    async def invalidate(self, *, tag: str | None = None, prefix: str = "") -> int:
        """Remove entries matching a tag or key prefix. Returns count removed."""
        entries = await self.search(tag=tag, prefix=prefix)
        count = 0
        for entry in entries:
            if await self._backend.delete(self._prefixed(entry.key)):
                count += 1
        return count

    @property
    def size(self) -> int:
        return self._backend.size

    def _prefixed(self, key: str) -> str:
        return f"{self.namespace}:{key}"
