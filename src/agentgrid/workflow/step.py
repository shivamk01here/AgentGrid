"""Workflow step abstraction."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine


StepFn = Callable[["Step", dict[str, Any]], Coroutine[Any, Any, Any]]


@dataclass
class StepResult:
    """Result of a single workflow step execution."""

    step_name: str
    success: bool
    output: Any = None
    error: str | None = None
    duration_ms: float = 0.0

    @classmethod
    def ok(cls, name: str, output: Any, duration_ms: float = 0.0) -> StepResult:
        return cls(step_name=name, success=True, output=output, duration_ms=duration_ms)

    @classmethod
    def fail(cls, name: str, error: str, duration_ms: float = 0.0) -> StepResult:
        return cls(step_name=name, success=False, error=error, duration_ms=duration_ms)


@dataclass
class Step:
    """A single step in a workflow."""

    name: str
    handler: StepFn
    description: str = ""
    retry_count: int = 0
    timeout_seconds: float | None = None
    dependencies: list[str] = field(default_factory=list)

    async def execute(self, context: dict[str, Any]) -> StepResult:
        """Execute this step with the given context.

        Retries up to retry_count times on failure. If timeout_seconds is set,
        the handler is wrapped with asyncio.wait_for().
        """
        last_error: str | None = None
        attempts = self.retry_count + 1

        for _ in range(attempts):
            start = time.monotonic()
            try:
                if self.timeout_seconds is not None:
                    output = await asyncio.wait_for(
                        self.handler(self, context), timeout=self.timeout_seconds
                    )
                else:
                    output = await self.handler(self, context)
                duration = (time.monotonic() - start) * 1000
                return StepResult.ok(self.name, output, duration)
            except asyncio.TimeoutError:
                duration = (time.monotonic() - start) * 1000
                last_error = f"Step timed out after {self.timeout_seconds}s"
            except Exception as exc:
                duration = (time.monotonic() - start) * 1000
                last_error = str(exc)

        return StepResult.fail(self.name, last_error or "Unknown error")
