"""Permission model for agent governance."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PermissionLevel(Enum):
    """Access levels for agent operations."""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    ADMIN = "admin"


@dataclass(frozen=True)
class Permission:
    """A single permission grant."""

    resource: str
    level: PermissionLevel
    description: str = ""

    def allows(self, required_level: PermissionLevel) -> bool:
        """Check if this permission satisfies a required level."""
        hierarchy = {
            PermissionLevel.READ: 0,
            PermissionLevel.WRITE: 1,
            PermissionLevel.EXECUTE: 2,
            PermissionLevel.ADMIN: 3,
        }
        return hierarchy[self.level] >= hierarchy[required_level]
