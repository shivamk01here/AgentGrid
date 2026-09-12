"""The agent tool-calling loop.

Drives the conversation between a model and the agent's tools, recording
every model turn and every tool invocation as an ordered step ledger. The
ledger is the point: in a money-movement run, what the agent did and why
has to survive the run that did it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ledgerloop.events.bus import Event
from ledgerloop.llm.provider import LLMResponse, TokenUsage

if TYPE_CHECKING:
    from ledgerloop.agent.base import Agent
    from ledgerloop.llm.provider import ToolCall
    from ledgerloop.tools.base import ToolResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolInvocation:
    """One tool call and its outcome."""

    call_id: str
    name: str
    arguments: dict[str, Any]
    success: bool
    output: Any = None
    error: str | None = None
    duration: float = 0.0


@dataclass(frozen=True)
class LoopStep:
    """One iteration: a model turn plus any tools it invoked."""

    index: int
    text: str
    reasoning: str
    stop_reason: str
    usage: TokenUsage
    invocations: tuple[ToolInvocation, ...] = ()


@dataclass
class LoopResult:
    """The outcome of a complete loop execution."""

    output: str
    steps: list[LoopStep] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    stop_reason: str = "end_turn"
    duration: float = 0.0

    @property
    def iterations(self) -> int:
        """Number of model turns taken."""
        return len(self.steps)

    @property
    def hit_iteration_limit(self) -> bool:
        """True when the loop stopped because it ran out of iterations."""
        return self.stop_reason == "max_iterations"

    @property
    def refused(self) -> bool:
        """True when the model declined on policy grounds."""
        return self.stop_reason == "refusal"

    @property
    def invocations(self) -> list[ToolInvocation]:
        """Every tool invocation across every step, in order."""
        return [inv for step in self.steps for inv in step.invocations]


class AgentLoop:
    """Runs an agent's model/tool conversation to completion.

    Stops when the model returns a turn with no tool calls, when it refuses,
    or when `config.max_iterations` model turns have been taken.
    """

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def execute(self, input_data: str = "") -> LoopResult:
        """Run the loop until the model stops asking for tools.

        Args:
            input_data: The opening user message.

        Returns:
            The final output plus the full step ledger.

        Raises:
            RuntimeError: No provider is attached to the agent.
            LLMError: A model call failed and could not be completed.
        """
        agent = self._agent
        provider = agent.provider
        if provider is None:
            raise RuntimeError(f"Agent {agent.name!r} has no provider attached")

        config = agent.config
        tool_defs = [tool.to_dict() for tool in agent.tools]
        messages: list[Any] = [{"role": "user", "content": input_data}]

        steps: list[LoopStep] = []
        total_usage = TokenUsage()
        stop_reason = "max_iterations"
        started = time.monotonic()

        await self._emit("agent.loop.started", {"input": input_data})

        for index in range(1, config.max_iterations + 1):
            response = await provider.complete(
                system=config.system_prompt,
                messages=messages,
                tools=tool_defs or None,
                effort=config.effort,
                max_tokens=config.max_tokens,
            )
            total_usage = total_usage + response.usage

            # Appended verbatim: provider-native blocks must round-trip unedited.
            messages.append({"role": "assistant", "content": response.raw_content})

            if response.refused:
                steps.append(self._step(index, response, ()))
                stop_reason = "refusal"
                await self._emit("agent.loop.refused", {"iteration": index})
                break

            if not response.wants_tools:
                steps.append(self._step(index, response, ()))
                stop_reason = response.stop_reason
                break

            invocations = await self._run_tools(response.tool_calls)
            steps.append(self._step(index, response, invocations))

            messages.append(
                provider.build_tool_result_message(
                    [
                        (inv.call_id, self._render(inv), not inv.success)
                        for inv in invocations
                    ]
                )
            )
        else:
            logger.warning(
                "Agent %s hit its iteration limit of %d",
                agent.name,
                config.max_iterations,
            )

        result = LoopResult(
            output=steps[-1].text if steps else "",
            steps=steps,
            usage=total_usage,
            stop_reason=stop_reason,
            duration=time.monotonic() - started,
        )
        await self._emit(
            "agent.loop.completed",
            {
                "iterations": result.iterations,
                "stop_reason": result.stop_reason,
                "duration": result.duration,
            },
        )
        return result

    async def _run_tools(self, calls: tuple[ToolCall, ...]) -> tuple[ToolInvocation, ...]:
        """Execute every requested tool concurrently, preserving call order."""
        results = await asyncio.gather(*(self._run_one(call) for call in calls))
        return tuple(results)

    async def _run_one(self, call: ToolCall) -> ToolInvocation:
        started = time.monotonic()
        tool = next((t for t in self._agent.tools if t.name == call.name), None)

        if tool is None:
            logger.warning("Model requested unknown tool: %s", call.name)
            return ToolInvocation(
                call_id=call.id,
                name=call.name,
                arguments=call.arguments,
                success=False,
                error=f"Unknown tool: {call.name}",
                duration=time.monotonic() - started,
            )

        await self._emit(
            "tool.invocation.started", {"tool": call.name, "arguments": call.arguments}
        )
        try:
            result: ToolResult = await tool.execute(**call.arguments)
        except Exception as exc:
            # A raising tool is reported to the model, never propagated - the
            # model can correct itself, a crashed run cannot.
            logger.exception("Tool %s raised", call.name)
            invocation = ToolInvocation(
                call_id=call.id,
                name=call.name,
                arguments=call.arguments,
                success=False,
                error=f"Tool raised: {exc}",
                duration=time.monotonic() - started,
            )
        else:
            invocation = ToolInvocation(
                call_id=call.id,
                name=call.name,
                arguments=call.arguments,
                success=result.success,
                output=result.output,
                error=result.error,
                duration=time.monotonic() - started,
            )

        await self._emit(
            "tool.invocation.completed",
            {"tool": call.name, "success": invocation.success, "error": invocation.error},
        )
        return invocation

    @staticmethod
    def _step(
        index: int, response: LLMResponse, invocations: tuple[ToolInvocation, ...]
    ) -> LoopStep:
        return LoopStep(
            index=index,
            text=response.text,
            reasoning=response.reasoning,
            stop_reason=response.stop_reason,
            usage=response.usage,
            invocations=invocations,
        )

    @staticmethod
    def _render(invocation: ToolInvocation) -> str:
        """Render a tool outcome as the text the model will read."""
        if not invocation.success:
            return invocation.error or "Tool failed without an error message"
        output = invocation.output
        if isinstance(output, str):
            return output
        try:
            return json.dumps(output, default=str)
        except (TypeError, ValueError):
            return str(output)

    async def _emit(self, topic: str, payload: dict[str, Any]) -> None:
        """Emit an event if the agent has an EventBus attached."""
        bus = self._agent._event_bus
        if bus is None:
            return
        try:
            await bus.emit(Event(topic=topic, payload=payload, source=self._agent.id))
        except Exception:
            logger.exception("Failed to emit event topic=%s", topic)
