"""Cache backend protocol - pluggable storage backends."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ledgerloop.cache.entry import CacheEntry


@runtime_checkable
class CacheBackend(Protocol):
    """Protocol for pluggable cache storage backends.

    Implement this interface to add Redis, Memcached, or any
    other storage backend to the CacheEngine.
    """

    async def get(self, key: str) -> CacheEntry | None:
        """Retrieve an entry by key. Returns None if missing."""
        ...

    async def set(self, key: str, entry: CacheEntry) -> None:
        """Store an entry."""
        ...

    async def delete(self, key: str) -> bool:
        """Delete a key. Returns True if it existed."""
        ...

    async def clear(self) -> int:
        """Clear all entries. Returns count removed."""
        ...

    async def list_all(self) -> list[CacheEntry]:
        """Return all non-expired entries."""
        ...

    @property
    def size(self) -> int:
        """Number of entries in the backend."""
        ...
