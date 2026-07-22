"""Memory backend protocol - pluggable storage backends."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from agentgrid.memory.store import MemoryEntry


@runtime_checkable
class MemoryBackend(Protocol):
    """Protocol for pluggable memory storage backends.

    Implement this interface to add SQLite, Redis, Postgres, or any
    other storage backend to the MemoryEngine.
    """

    async def store(self, key: str, value: Any, entry: MemoryEntry) -> None:
        """Store a value with its full entry metadata."""
        ...

    async def retrieve(self, key: str) -> MemoryEntry | None:
        """Retrieve an entry by key. Returns None if missing."""
        ...

    async def delete(self, key: str) -> bool:
        """Delete a key. Returns True if it existed."""
        ...

    async def list_all(self) -> list[MemoryEntry]:
        """Return all non-expired entries."""
        ...

    async def clear(self) -> int:
        """Clear all entries. Returns count removed."""
        ...

    @property
    def size(self) -> int:
        """Number of entries in the backend."""
        ...
