"""Memory engine - durable agent memory with pluggable backends."""

from __future__ import annotations

import logging
import time
from typing import Any

from agentgrid.memory.store import MemoryEntry

logger = logging.getLogger(__name__)


class MemoryEngine:
    """Provides durable memory for agents.

    Supports in-memory storage by default.
    Swap in SQLite, Redis, or Postgres backends via adapters.

    Memory is scoped to a namespace (typically an agent ID).
    """

    def __init__(self, namespace: str = "default") -> None:
        self.namespace = namespace
        self._store: dict[str, MemoryEntry] = {}

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
        self._store[key] = entry
        logger.debug("Stored key=%s namespace=%s", key, self.namespace)
        return entry

    async def retrieve(self, key: str) -> Any | None:
        """Retrieve a value by key. Returns None if missing or expired."""
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.is_expired():
            del self._store[key]
            return None
        return entry.value

    async def delete(self, key: str) -> bool:
        """Delete a key. Returns True if it existed."""
        if key in self._store:
            del self._store[key]
            return True
        return False

    async def search(self, *, tag: str | None = None, prefix: str = "") -> list[MemoryEntry]:
        """Search memory entries by tag or key prefix."""
        results = []
        for entry in self._store.values():
            if entry.is_expired():
                continue
            if tag and tag not in entry.tags:
                continue
            if prefix and not entry.key.startswith(prefix):
                continue
            results.append(entry)
        return results

    async def clear(self) -> int:
        """Clear all entries in this namespace. Returns count removed."""
        count = len(self._store)
        self._store.clear()
        return count

    @property
    def size(self) -> int:
        return len(self._store)
