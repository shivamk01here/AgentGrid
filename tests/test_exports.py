"""Tests for the top-level package exports."""

import agentgrid


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
            assert hasattr(agentgrid, name)

    def test_observability_symbols(self):
        assert hasattr(agentgrid, "MetricsCollector")
        assert hasattr(agentgrid, "get_logger")

    def test_auth_symbols(self):
        assert hasattr(agentgrid, "Authenticator")
        assert hasattr(agentgrid, "Permission")
        assert hasattr(agentgrid, "PermissionLevel")

    def test_all_matches_exports(self):
        for name in agentgrid.__all__:
            assert hasattr(agentgrid, name), f"missing export: {name}"
