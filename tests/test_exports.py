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

    def test_runtime_symbols(self):
        for name in [
            "ActionExecutor",
            "RunCoordinator",
            "Reconciler",
            "Compensator",
            "Reaper",
            "ReaperReport",
            "CompensationReport",
            "AppliedEffect",
            "replay_effects",
        ]:
            assert hasattr(ledgerloop, name)

    def test_observability_symbols(self):
        assert hasattr(ledgerloop, "MetricsCollector")
        assert hasattr(ledgerloop, "get_logger")

    def test_adapters_symbols(self):
        for name in [
            "InMemoryUnitOfWork",
            "RecordingDispatcher",
        ]:
            assert hasattr(ledgerloop, name)

    def test_auth_symbols(self):
        assert hasattr(ledgerloop, "Authenticator")
        assert hasattr(ledgerloop, "Permission")
        assert hasattr(ledgerloop, "PermissionLevel")

    def test_the_pieces_every_example_is_built_from_are_in_all(self):
        # Importable by name was never the problem; `__all__` is what
        # `import *`, documentation tools, and linters read, and these were
        # imported at the top of the package and then left out of it.
        for name in [
            "ManualClock",
            "SystemClock",
            "ApproverDirectory",
            "InMemoryApprovalGateway",
            "InMemoryIdempotencyStore",
            "InMemoryLedgerStore",
            "InMemoryRunStore",
            "InMemoryStepStore",
            "PolicyRule",
            "ThresholdPolicy",
            "ThresholdPolicyEngine",
        ]:
            assert name in ledgerloop.__all__, name

    def test_all_matches_exports(self):
        for name in ledgerloop.__all__:
            assert hasattr(ledgerloop, name), f"missing export: {name}"
