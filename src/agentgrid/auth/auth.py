"""Authentication for agent API access."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from agentgrid.auth.permissions import Permission


@dataclass
class AgentIdentity:
    """An authenticated agent identity."""

    agent_id: str
    api_key_hash: str
    permissions: list[Permission] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


class Authenticator:
    """API key-based authentication for agents.

    In production, swap this out for OAuth2/OIDC.
    This provides the minimal auth layer for the open-source core.

    Example:
        auth = Authenticator()
        api_key = auth.create_identity("agent-1")
        identity = auth.verify(api_key)
    """

    def __init__(self) -> None:
        self._identities: dict[str, AgentIdentity] = {}
        self._key_to_id: dict[str, str] = {}

    def create_identity(
        self,
        agent_id: str,
        permissions: list[Permission] | None = None,
    ) -> str:
        """Create a new identity and return the raw API key."""
        api_key = "fg_" + secrets.token_hex(32)
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()

        identity = AgentIdentity(
            agent_id=agent_id,
            api_key_hash=key_hash,
            permissions=permissions or [],
        )
        self._identities[agent_id] = identity
        self._key_to_id[key_hash] = agent_id
        return api_key

    def verify(self, api_key: str) -> AgentIdentity | None:
        """Verify an API key and return the identity, or None."""
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        agent_id = self._key_to_id.get(key_hash)
        if agent_id:
            return self._identities.get(agent_id)
        return None

    def revoke(self, agent_id: str) -> bool:
        """Revoke an identity."""
        identity = self._identities.pop(agent_id, None)
        if identity:
            self._key_to_id.pop(identity.api_key_hash, None)
            return True
        return False
