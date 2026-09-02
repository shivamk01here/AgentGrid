"""Base agent abstractions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ledgerloop.llm.provider import LLMProvider
    from ledgerloop.tools.base import BaseTool

VALID_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})


@dataclass
class AgentConfig:
    """Configuration for an agent instance.

    There is no temperature setting: the current model family rejects
    sampling parameters. Reasoning depth is controlled by `effort` instead.
    """

    name: str = "unnamed-agent"
    model: str = "claude-opus-5"
    max_iterations: int = 10
    system_prompt: str = ""
    effort: str = "high"
    max_tokens: int = 16000
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.effort not in VALID_EFFORT_LEVELS:
            raise ValueError(
                f"effort must be one of {sorted(VALID_EFFORT_LEVELS)}, got {self.effort!r}"
            )
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be at least 1")


class Agent:
    """An agent: a model, a set of tools, and a loop that connects them.

    Out of the box `run()` executes a tool-calling loop against the attached
    provider. Subclasses override `run()` only when they need control flow
    the loop does not express.

    Example:
        agent = Agent(AgentConfig(name="reconciler", system_prompt="..."))
        agent.attach_provider(AnthropicProvider())
        agent.attach_tool(SettlementFileTool())
        output = await agent.run("Reconcile yesterday's batch.")
    """

    def __init__(self, config: AgentConfig | None = None) -> None:
        self.config = config or AgentConfig()
        self.id: str = uuid.uuid4().hex[:12]
        self._tools: list[BaseTool] = []
        self._memory: Any = None
        self._event_bus: Any = None
        self._provider: LLMProvider | None = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def provider(self) -> LLMProvider | None:
        """The attached model provider, if any."""
        return self._provider

    @property
    def tools(self) -> tuple[BaseTool, ...]:
        """Tools available to this agent."""
        return tuple(self._tools)

    def attach_provider(self, provider: LLMProvider) -> None:
        """Attach the model provider the default loop will call."""
        self._provider = provider

    def attach_tool(self, tool: BaseTool) -> None:
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

        Runs the default tool-calling loop against the attached provider.
        Override to implement custom agent logic.

        Args:
            input_data: The initial input/prompt for the agent.

        Returns:
            The agent's final output as a string.

        Raises:
            RuntimeError: No provider is attached.
            LLMError: The model call failed.
        """
        result = await self.run_loop(input_data)
        return result.output

    async def run_loop(self, input_data: str = "") -> Any:
        """Run the default loop and return the full `LoopResult`.

        Use this instead of `run()` when you need the step ledger, token
        usage, or the reason the loop stopped - not just the final text.
        """
        from ledgerloop.agent.loop import AgentLoop

        if self._provider is None:
            raise RuntimeError(
                f"Agent {self.name!r} has no provider attached. "
                "Call attach_provider(...) before run(), or override run() "
                "to implement your own execution."
            )
        return await AgentLoop(self).execute(input_data)

    def __repr__(self) -> str:
        return f"Agent(id={self.id!r}, name={self.name!r})"
