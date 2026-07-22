"""Tests for the agent runtime."""

import pytest
from agentgrid.agent.base import Agent, AgentConfig
from agentgrid.agent.runtime import AgentRuntime
from agentgrid.events.bus import EventBus


class DummyAgent(Agent):
    async def run(self, input_data: str = "") -> str:
        return f"echo: {input_data}"


class FailingAgent(Agent):
    def __init__(self):
        super().__init__(AgentConfig(name="failing"))
        self._attempts = 0

    async def run(self, input_data: str = "") -> str:
        self._attempts += 1
        if self._attempts < 3:
            raise RuntimeError("transient failure")
        return "recovered"


class TestAgentRuntime:
    @pytest.mark.asyncio
    async def test_successful_execution(self):
        agent = DummyAgent()
        runtime = AgentRuntime(agent)
        result = await runtime.execute("hello")
        assert result["success"] is True
        assert result["output"] == "echo: hello"
        assert result["attempt"] == 1

    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        agent = FailingAgent()
        runtime = AgentRuntime(agent, max_retries=3)
        result = await runtime.execute("test")
        assert result["success"] is True
        assert result["output"] == "recovered"
        assert result["attempt"] == 3

    @pytest.mark.asyncio
    async def test_all_retries_exhausted(self):
        class AlwaysFail(Agent):
            async def run(self, input_data: str = "") -> str:
                raise RuntimeError("always fails")

        agent = AlwaysFail()
        runtime = AgentRuntime(agent, max_retries=2)
        result = await runtime.execute("test")
        assert result["success"] is False
        assert "always fails" in result["error"]
        assert result["attempt"] == 2

    def test_reset(self):
        agent = DummyAgent()
        runtime = AgentRuntime(agent)
        runtime._iteration = 5
        runtime.reset()
        assert runtime._iteration == 0

    @pytest.mark.asyncio
    async def test_emits_events_on_success(self):
        agent = DummyAgent()
        bus = EventBus()
        agent.attach_event_bus(bus)
        runtime = AgentRuntime(agent)
        await runtime.execute("hello")
        history = bus.get_history()
        topics = [e.topic for e in history]
        assert "agent.run.started" in topics
        assert "agent.run.completed" in topics

    @pytest.mark.asyncio
    async def test_emits_events_on_failure(self):
        class AlwaysFail(Agent):
            async def run(self, input_data: str = "") -> str:
                raise RuntimeError("boom")

        agent = AlwaysFail()
        bus = EventBus()
        agent.attach_event_bus(bus)
        runtime = AgentRuntime(agent, max_retries=2)
        await runtime.execute("test")
        history = bus.get_history()
        topics = [e.topic for e in history]
        assert "agent.run.started" in topics
        assert "agent.run.failed" in topics
        assert "agent.run.completed" not in topics

    @pytest.mark.asyncio
    async def test_no_events_without_event_bus(self):
        agent = DummyAgent()
        runtime = AgentRuntime(agent)
        result = await runtime.execute("hello")
        assert result["success"] is True
