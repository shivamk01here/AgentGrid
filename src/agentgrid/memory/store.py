"""Memory data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MemoryEntry:
    """A single memory entry."""

    key: str
    value: Any
    namespace: str = "default"
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0
    expires_at: float | None = None

    def is_expired(self) -> bool:
        """Check if this entry has expired."""
        if self.expires_at is None:
            return False
        import time

        return time.time() > self.expires_at
