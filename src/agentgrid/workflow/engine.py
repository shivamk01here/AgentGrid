"""Workflow engine - orchestrates multi-step agent workflows."""

from __future__ import annotations

import logging
from typing import Any

from agentgrid.workflow.step import Step, StepResult

logger = logging.getLogger(__name__)


class WorkflowEngine:
    """Executes a sequence of steps with dependency resolution.

    Steps are executed in topological order based on their declared
    dependencies. Steps without dependencies run first.

    Example:
        engine = WorkflowEngine()
        engine.add_step(Step(name="fetch", handler=fetch_data))
        engine.add_step(Step(name="process", handler=process_data, dependencies=["fetch"]))
        results = await engine.run()
    """

    def __init__(self, name: str = "unnamed-workflow") -> None:
        self.name = name
        self._steps: dict[str, Step] = {}
        self._execution_order: list[str] = []

    def add_step(self, step: Step) -> None:
        """Register a step in the workflow."""
        if step.name in self._steps:
            raise ValueError(f"Step already registered: {step.name}")
        self._steps[step.name] = step
        self._execution_order = self._resolve_order()

    def remove_step(self, name: str) -> bool:
        """Remove a step by name."""
        if name in self._steps:
            del self._steps[name]
            self._execution_order = self._resolve_order()
            return True
        return False

    async def run(self, initial_context: dict[str, Any] | None = None) -> dict[str, StepResult]:
        """Execute all steps in dependency order.

        Args:
            initial_context: Starting context passed to all steps.

        Returns:
            A dict mapping step names to their results.
        """
        context = initial_context or {}
        results: dict[str, StepResult] = {}

        logger.info(
            "Workflow '%s' starting with %d steps",
            self.name,
            len(self._execution_order),
        )

        for step_name in self._execution_order:
            step = self._steps.get(step_name)
            if step is None:
                continue

            step_result = await step.execute(context)
            results[step_name] = step_result

            if step_result.success:
                context[step_name] = step_result.output
                logger.info("Step '%s' completed", step_name)
            else:
                logger.error("Step '%s' failed: %s", step_name, step_result.error)
                break

        logger.info("Workflow '%s' finished", self.name)
        return results

    def _resolve_order(self) -> list[str]:
        """Topological sort of steps based on dependencies."""
        resolved: list[str] = []
        visited: set[str] = set()
        visiting: set[str] = set()

        def dfs(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise ValueError(f"Circular dependency detected: {name}")
            visiting.add(name)
            step = self._steps.get(name)
            if step:
                for dep in step.dependencies:
                    dfs(dep)
            visiting.remove(name)
            visited.add(name)
            resolved.append(name)

        for name in self._steps:
            dfs(name)

        return resolved
