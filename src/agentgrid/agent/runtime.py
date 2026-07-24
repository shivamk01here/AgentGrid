"""Agent runtime - handles execution, retries, and lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from agentgrid.agent.base import Agent, AgentConfig

if TYPE_CHECKING:
    from agentgrid.ratelimit.limiter import RateLimiter

logger = logging.getLogger(__name__)


class AgentRuntime:
    """Manages the execution lifecycle of an agent.

    Handles:
    - Initialization and teardown
    - Iteration control
    - Error handling and retries
    - Event emission (when an EventBus is attached to the agent)
    - Optional per-agent rate limiting
    - Logging
    """

    def __init__(
        self,
        agent: Agent,
        *,
        max_retries: int = 3,
        timeout_seconds: float | None = None,
        rate_limiter: RateLimiter | None = None,
        rate_limit_key: str | None = None,
    ) -> None:
        self.agent = agent
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds
        self._rate_limiter = rate_limiter
        self._rate_limit_key = rate_limit_key or agent.id
        self._iteration = 0

    async def _emit(self, topic: str, payload: dict[str, Any] | None = None) -> None:
        """Emit an event if the agent has an EventBus attached."""
        bus = self.agent._event_bus
        if bus is None:
            return
        try:
            from agentgrid.events.bus import Event
            await bus.emit(Event(topic=topic, payload=payload, source=self.agent.id))
        except Exception:
            logger.debug("Failed to emit event topic=%s", topic)

    async def execute(self, input_data: str = "") -> dict[str, Any]:
        """Run the agent with full lifecycle management.

        Args:
            input_data: The initial prompt or input.

        Returns:
            A result dict with keys: output, iterations, success, error.
        """
        if self._rate_limiter is not None:
            rl_result = await self._rate_limiter.try_acquire(self._rate_limit_key)
            if not rl_result.allowed:
                logger.warning(
                    "Rate limit exceeded for agent=%s key=%s",
                    self.agent.name,
                    self._rate_limit_key,
                )
                return {
                    "output": None,
                    "iterations": 0,
                    "success": False,
                    "error": f"Rate limit exceeded (retry after {rl_result.retry_after:.2f}s)",
                    "attempt": 0,
                }

        logger.info(
            "Runtime starting agent=%s iteration_limit=%d",
            self.agent.name,
            self.agent.config.max_iterations,
        )
        await self._emit("agent.run.started", {"input": input_data})

        last_error: str | None = None
        start_time = time.monotonic()

        for attempt in range(1, self.max_retries + 1):
            self._iteration += 1
            try:
                if self.timeout_seconds is not None:
                    output = await asyncio.wait_for(
                        self.agent.run(input_data), timeout=self.timeout_seconds
                    )
                else:
                    output = await self.agent.run(input_data)

                duration = time.monotonic() - start_time
                logger.info(
                    "Runtime completed agent=%s iterations=%d",
                    self.agent.name,
                    self._iteration,
                )
                result = {
                    "output": output,
                    "iterations": self._iteration,
                    "success": True,
                    "error": None,
                    "attempt": attempt,
                }
                await self._emit("agent.run.completed", {
                    "output": output,
                    "iterations": self._iteration,
                    "duration": duration,
                    "attempt": attempt,
                })
                return result
            except NotImplementedError:
                raise
            except asyncio.TimeoutError:
                last_error = f"Timed out after {self.timeout_seconds}s"
                logger.warning(
                    "Runtime attempt %d/%d timed out after %ss",
                    attempt,
                    self.max_retries,
                    self.timeout_seconds,
                )
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "Runtime attempt %d/%d failed: %s",
                    attempt,
                    self.max_retries,
                    last_error,
                )

        duration = time.monotonic() - start_time
        await self._emit("agent.run.failed", {
            "error": last_error,
            "iterations": self._iteration,
            "duration": duration,
        })
        return {
            "output": None,
            "iterations": self._iteration,
            "success": False,
            "error": last_error,
            "attempt": self.max_retries,
        }

    def reset(self) -> None:
        """Reset runtime state for a new execution."""
        self._iteration = 0
