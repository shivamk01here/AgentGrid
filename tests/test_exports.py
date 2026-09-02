"""Tests for the top-level package exports."""

import ledgerloop


class TestTopLevelExports:
    def test_core_symbols(self):
        for name in [
            "Agent",
            "AgentConfig",
            "AgentRuntime",
            "ToolRegistry",
            "MemoryEngine",
            "EventBus",
            "Scheduler",
            "WorkflowEngine",
        ]:
            assert hasattr(ledgerloop, name)

    def test_observability_symbols(self):
        assert hasattr(ledgerloop, "MetricsCollector")
        assert hasattr(ledgerloop, "get_logger")

    def test_auth_symbols(self):
        assert hasattr(ledgerloop, "Authenticator")
        assert hasattr(ledgerloop, "Permission")
        assert hasattr(ledgerloop, "PermissionLevel")

    def test_all_matches_exports(self):
        for name in ledgerloop.__all__:
            assert hasattr(ledgerloop, name), f"missing export: {name}"
