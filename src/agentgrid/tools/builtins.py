"""Built-in tools - ready-to-use tools for common tasks."""

from __future__ import annotations

import asyncio
import datetime
import time
from collections import Counter as _Counter
from typing import Any

from agentgrid.tools.base import BaseTool, ToolResult


class DateTimeTool(BaseTool):
    """Returns the current date and time."""

    @property
    def name(self) -> str:
        return "datetime"

    @property
    def description(self) -> str:
        return "Returns the current UTC date and time, with optional format string"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "description": "strftime format string (default: ISO 8601)",
                },
            },
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        fmt = kwargs.get("format")
        now = datetime.datetime.now(datetime.timezone.utc)
        if fmt:
            try:
                output = now.strftime(fmt)
            except ValueError as exc:
                return ToolResult.fail(f"Invalid format string: {exc}")
        else:
            output = now.isoformat()
        return ToolResult.ok(output, timestamp=now.timestamp())


class TextTransformTool(BaseTool):
    """Transforms text in various ways."""

    @property
    def name(self) -> str:
        return "text_transform"

    @property
    def description(self) -> str:
        return "Transform text: uppercase, lowercase, reverse, or word count"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The input text"},
                "operation": {
                    "type": "string",
                    "description": "One of: upper, lower, reverse, word_count",
                },
            },
            "required": ["text", "operation"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        text = kwargs["text"]
        operation = kwargs["operation"]

        ops = {
            "upper": lambda t: t.upper(),
            "lower": lambda t: t.lower(),
            "reverse": lambda t: t[::-1],
            "word_count": lambda t: len(t.split()),
        }

        fn = ops.get(operation)
        if fn is None:
            return ToolResult.fail(
                f"Unknown operation '{operation}'. Choose from: {', '.join(ops)}"
            )

        return ToolResult.ok(fn(text), operation=operation)


class CounterTool(BaseTool):
    """Thread-safe in-memory counter."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "counter"

    @property
    def description(self) -> str:
        return "Manage named counters: increment, decrement, or read"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "counter_name": {
                    "type": "string",
                    "description": "Name of the counter",
                },
                "action": {
                    "type": "string",
                    "description": "One of: increment, decrement, read, reset",
                },
                "amount": {
                    "type": "integer",
                    "description": "Amount to increment/decrement (default: 1)",
                },
            },
            "required": ["counter_name", "action"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        counter_name = kwargs["counter_name"]
        action = kwargs["action"]
        amount = kwargs.get("amount", 1)

        async with self._lock:
            if action == "increment":
                self._counters[counter_name] = self._counters.get(counter_name, 0) + amount
            elif action == "decrement":
                self._counters[counter_name] = self._counters.get(counter_name, 0) - amount
            elif action == "read":
                pass
            elif action == "reset":
                self._counters[counter_name] = 0
            else:
                return ToolResult.fail(
                    f"Unknown action '{action}'. Choose from: increment, decrement, read, reset"
                )

            value = self._counters.get(counter_name, 0)

        return ToolResult.ok(value, counter=counter_name, action=action)

    @property
    def counters(self) -> dict[str, int]:
        """Read-only view of all counter values."""
        return dict(self._counters)
