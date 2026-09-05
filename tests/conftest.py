"""Shared test fixtures for ledgerloop."""

import pytest

from ledgerloop.agent.base import Agent, AgentConfig
from ledgerloop.tools.registry import ToolRegistry
from ledgerloop.memory.engine import MemoryEngine
from ledgerloop.events.bus import EventBus
from ledgerloop.observability.metrics import MetricsCollector


@pytest.fixture
def agent_config():
    return AgentConfig(name="test-agent", model="test-model")


@pytest.fixture
def agent(agent_config):
    return Agent(agent_config)


@pytest.fixture
def tool_registry():
    return ToolRegistry()


@pytest.fixture
def memory_engine():
    return MemoryEngine(namespace="test")


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def metrics():
    return MetricsCollector()
