"""Agent runtime - handles execution, retries, and lifecycle."""

from __future__ import annotations

import logging
from typing import Any

from agentgrid.agent.base import Agent, AgentConfig

logger = logging.getLogger(__name__)


class AgentRuntime:
    """Manages the execution lifecycle of an agent.

    Handles:
    - Initialization and teardown
    - Iteration control
    - Error handling and retries
    - Event emission
    - Logging
    """

    def __init__(
        self,
        agent: Agent,
        *,
        max_retries: int = 3,
        timeout_seconds: float | None = None,
    ) -> None:
        self.agent = agent
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds
        self._iteration = 0

    async def execute(self, input_data: str = "") -> dict[str, Any]:
        """Run the agent with full lifecycle management.

        Args:
            input_data: The initial prompt or input.

        Returns:
            A result dict with keys: output, iterations, success, error.
        """
        logger.info(
            "Runtime starting agent=%s iteration_limit=%d",
            self.agent.name,
            self.agent.config.max_iterations,
        )
        last_error: str | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                output = await self.agent.run(input_data)
                logger.info(
                    "Runtime completed agent=%s iterations=%d",
                    self.agent.name,
                    self._iteration,
                )
                return {
                    "output": output,
                    "iterations": self._iteration,
                    "success": True,
                    "error": None,
                    "attempt": attempt,
                }
            except NotImplementedError:
                raise
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "Runtime attempt %d/%d failed: %s",
                    attempt,
                    self.max_retries,
                    last_error,
                )

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
