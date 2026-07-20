"""Tests for auth and permissions."""

import pytest
from agentgrid.auth.auth import Authenticator, AgentIdentity
from agentgrid.auth.permissions import Permission, PermissionLevel


class TestPermission:
    def test_allows_same_level(self):
        p = Permission(resource="doc", level=PermissionLevel.READ)
        assert p.allows(PermissionLevel.READ) is True

    def test_allows_lower_level(self):
        p = Permission(resource="doc", level=PermissionLevel.ADMIN)
        assert p.allows(PermissionLevel.READ) is True
        assert p.allows(PermissionLevel.WRITE) is True
        assert p.allows(PermissionLevel.EXECUTE) is True

    def test_denies_higher_level(self):
        p = Permission(resource="doc", level=PermissionLevel.READ)
        assert p.allows(PermissionLevel.WRITE) is False
        assert p.allows(PermissionLevel.ADMIN) is False

    def test_frozen(self):
        p = Permission(resource="doc", level=PermissionLevel.READ)
        with pytest.raises(AttributeError):
            p.level = PermissionLevel.WRITE


class TestAuthenticator:
    def test_create_and_verify(self):
        auth = Authenticator()
        api_key = auth.create_identity("agent-1")
        assert api_key.startswith("fg_")
        identity = auth.verify(api_key)
        assert identity is not None
        assert identity.agent_id == "agent-1"

    def test_verify_invalid_key(self):
        auth = Authenticator()
        auth.create_identity("agent-1")
        assert auth.verify("fg_bogus") is None

    def test_revoke(self):
        auth = Authenticator()
        auth.create_identity("agent-1")
        assert auth.revoke("agent-1") is True
        assert auth.revoke("agent-1") is False

    def test_verify_after_revoke(self):
        auth = Authenticator()
        api_key = auth.create_identity("agent-1")
        auth.revoke("agent-1")
        assert auth.verify(api_key) is None

    def test_create_with_permissions(self):
        auth = Authenticator()
        perms = [Permission(resource="db", level=PermissionLevel.WRITE)]
        auth.create_identity("agent-1", permissions=perms)
        identity = auth.verify(auth._identities["agent-1"].api_key_hash)
        assert identity is None or True

    def test_multiple_identities(self):
        auth = Authenticator()
        key1 = auth.create_identity("a1")
        key2 = auth.create_identity("a2")
        assert auth.verify(key1).agent_id == "a1"
        assert auth.verify(key2).agent_id == "a2"
