"""Tests for the agent module."""

import pytest
from ledgerloop.agent.base import Agent, AgentConfig


class TestAgent:
    def test_agent_default_config(self, agent):
        assert agent.name == "test-agent"
        assert agent.id is not None
        assert len(agent.id) == 12

    def test_agent_custom_config(self):
        config = AgentConfig(name="custom", max_iterations=5)
        agent = Agent(config)
        assert agent.name == "custom"
        assert agent.config.max_iterations == 5

    def test_agent_attach_tool(self, agent):
        class DummyTool:
            name = "dummy"
        agent.attach_tool(DummyTool())
        assert len(agent._tools) == 1

    def test_agent_attach_memory(self, agent, memory_engine):
        agent.attach_memory(memory_engine)
        assert agent._memory is memory_engine

    def test_agent_attach_event_bus(self, agent, event_bus):
        agent.attach_event_bus(event_bus)
        assert agent._event_bus is event_bus

    def test_agent_repr(self, agent):
        r = repr(agent)
        assert "test-agent" in r
        assert agent.id in r

    def test_effort_must_be_a_known_level(self):
        with pytest.raises(ValueError, match="effort"):
            AgentConfig(effort="turbo")

    def test_effort_accepts_every_supported_level(self):
        for level in ("low", "medium", "high", "xhigh", "max"):
            assert AgentConfig(effort=level).effort == level

    def test_max_iterations_must_be_positive(self):
        with pytest.raises(ValueError, match="max_iterations"):
            AgentConfig(max_iterations=0)

    def test_max_tokens_must_be_positive(self):
        with pytest.raises(ValueError, match="max_tokens"):
            AgentConfig(max_tokens=0)

    def test_default_model_is_the_current_claude_model(self):
        assert AgentConfig().model == "claude-opus-5"

    @pytest.mark.asyncio
    async def test_agent_run_without_provider_explains_itself(self):
        agent = Agent()
        with pytest.raises(RuntimeError, match="no provider attached"):
            await agent.run("hello")
