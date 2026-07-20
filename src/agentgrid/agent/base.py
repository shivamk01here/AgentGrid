"""Base agent abstractions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentConfig:
    """Configuration for an agent instance."""

    name: str = "unnamed-agent"
    model: str = "gpt-4"
    max_iterations: int = 10
    system_prompt: str = ""
    temperature: float = 0.7
    metadata: dict[str, Any] = field(default_factory=dict)


class Agent:
    """Base agent class.

    Provides the core contract that all agents implement.
    Handles identity, configuration, and lifecycle hooks.
    """

    def __init__(self, config: AgentConfig | None = None) -> None:
        self.config = config or AgentConfig()
        self.id: str = uuid.uuid4().hex[:12]
        self._tools: list[Any] = []
        self._memory: Any = None
        self._event_bus: Any = None

    @property
    def name(self) -> str:
        return self.config.name

    def attach_tool(self, tool: Any) -> None:
        """Register a tool with this agent."""
        self._tools.append(tool)

    def attach_memory(self, memory: Any) -> None:
        """Attach a memory engine to this agent."""
        self._memory = memory

    def attach_event_bus(self, bus: Any) -> None:
        """Attach an event bus for observability."""
        self._event_bus = bus

    async def run(self, input_data: str = "") -> str:
        """Execute the agent's main loop.

        Override this method to implement custom agent logic.
        Subclasses should call super().run() or implement their own loop.

        Args:
            input_data: The initial input/prompt for the agent.

        Returns:
            The agent's final output as a string.
        """
        raise NotImplementedError(
            "Agent.run() must be implemented by subclasses. "
            "Use AgentRuntime for a default execution loop."
        )

    def __repr__(self) -> str:
        return f"Agent(id={self.id!r}, name={self.name!r})"
