"""Memory engine - durable agent memory with pluggable backends."""

from __future__ import annotations

import logging
import time
from typing import Any

from agentgrid.memory.backend import MemoryBackend
from agentgrid.memory.store import MemoryEntry

logger = logging.getLogger(__name__)


class InMemoryBackend:
    """Default in-memory storage backend."""

    def __init__(self) -> None:
        self._store: dict[str, MemoryEntry] = {}

    async def store(self, key: str, value: Any, entry: MemoryEntry) -> None:
        self._store[key] = entry

    async def retrieve(self, key: str) -> MemoryEntry | None:
        return self._store.get(key)

    async def delete(self, key: str) -> bool:
        if key in self._store:
            del self._store[key]
            return True
        return False

    async def list_all(self) -> list[MemoryEntry]:
        return [e for e in self._store.values() if not e.is_expired()]

    async def clear(self) -> int:
        count = len(self._store)
        self._store.clear()
        return count

    @property
    def size(self) -> int:
        return len(self._store)


class MemoryEngine:
    """Provides durable memory for agents.

    Supports in-memory storage by default. Pass a custom MemoryBackend
    for SQLite, Redis, Postgres, or other storage backends.

    Memory is scoped to a namespace (typically an agent ID).
    """

    def __init__(
        self,
        namespace: str = "default",
        backend: MemoryBackend | None = None,
    ) -> None:
        self.namespace = namespace
        self._backend: MemoryBackend = backend or InMemoryBackend()

    async def store(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: int | None = None,
        tags: list[str] | None = None,
    ) -> MemoryEntry:
        """Store a value in memory.

        Args:
            key: Unique key within the namespace.
            value: The value to store (must be serializable).
            ttl_seconds: Optional time-to-live. None means permanent.
            tags: Optional tags for filtering.
        """
        entry = MemoryEntry(
            key=key,
            value=value,
            namespace=self.namespace,
            tags=tags or [],
            created_at=time.time(),
            expires_at=(time.time() + ttl_seconds) if ttl_seconds else None,
        )
        await self._backend.store(key, value, entry)
        logger.debug("Stored key=%s namespace=%s", key, self.namespace)
        return entry

    async def retrieve(self, key: str) -> Any | None:
        """Retrieve a value by key. Returns None if missing or expired."""
        entry = await self._backend.retrieve(key)
        if entry is None:
            return None
        if entry.is_expired():
            await self._backend.delete(key)
            return None
        return entry.value

    async def delete(self, key: str) -> bool:
        """Delete a key. Returns True if it existed."""
        return await self._backend.delete(key)

    async def search(self, *, tag: str | None = None, prefix: str = "") -> list[MemoryEntry]:
        """Search memory entries by tag or key prefix."""
        all_entries = await self._backend.list_all()
        results = []
        for entry in all_entries:
            if tag and tag not in entry.tags:
                continue
            if prefix and not entry.key.startswith(prefix):
                continue
            results.append(entry)
        return results

    async def list_all(self) -> list[MemoryEntry]:
        """Return all non-expired entries in this namespace."""
        return await self._backend.list_all()

    async def clear(self) -> int:
        """Clear all entries in this namespace. Returns count removed."""
        return await self._backend.clear()

    @property
    def size(self) -> int:
        return self._backend.size
