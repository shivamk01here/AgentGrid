"""Cache data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CacheEntry:
    """A single cache entry with optional TTL."""

    key: str
    value: Any
    ttl: float | None = None
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0

    def is_expired(self) -> bool:
        """Check if this entry has expired based on TTL."""
        if self.ttl is None:
            return False
        import time

        return time.time() > self.created_at + self.ttl
