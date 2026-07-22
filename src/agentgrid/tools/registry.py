"""Tool registry - discovery, registration, and invocation."""

from __future__ import annotations

import logging
from typing import Any

from agentgrid.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Central registry for tool management.

    Handles tool registration, lookup, and parameter validation.
    Agents use the registry to discover and invoke available tools.
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool. Raises ValueError if name is taken."""
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        logger.info("Tool registered: %s", tool.name)

    def unregister(self, name: str) -> bool:
        """Remove a tool by name. Returns True if found and removed."""
        if name in self._tools:
            del self._tools[name]
            logger.info("Tool unregistered: %s", name)
            return True
        return False

    def get(self, name: str) -> BaseTool | None:
        """Look up a tool by name."""
        return self._tools.get(name)

    async def invoke(self, name: str, **kwargs: Any) -> ToolResult:
        """Invoke a tool by name with parameters.

        Validates kwargs against the tool's parameters_schema before execution.
        Returns ToolResult.fail() if the tool is not found or validation fails.
        """
        tool = self.get(name)
        if tool is None:
            return ToolResult.fail(f"Tool not found: {name}")

        validation_error = self._validate_params(tool, kwargs)
        if validation_error:
            return ToolResult.fail(validation_error)

        logger.info("Invoking tool: %s", name)
        return await tool.execute(**kwargs)

    @staticmethod
    def _validate_params(tool: BaseTool, kwargs: dict[str, Any]) -> str | None:
        """Validate kwargs against a tool's parameters_schema.

        Returns an error message string if validation fails, None otherwise.
        Performs lightweight checks: required fields and basic type validation.
        """
        schema = tool.parameters_schema
        if not schema or schema.get("type") != "object":
            return None

        properties = schema.get("properties", {})
        required = schema.get("required", [])

        for field_name in required:
            if field_name not in kwargs:
                return f"Missing required parameter '{field_name}' for tool '{tool.name}'"

        for param_name in kwargs:
            if param_name in properties:
                expected_type = properties[param_name].get("type")
                if expected_type:
                    value = kwargs[param_name]
                    type_map = {
                        "string": str,
                        "integer": int,
                        "number": (int, float),
                        "boolean": bool,
                        "array": list,
                        "object": dict,
                    }
                    expected = type_map.get(expected_type)
                    if expected and not isinstance(value, expected):
                        return (
                            f"Parameter '{param_name}' expected type '{expected_type}' "
                            f"but got '{type(value).__name__}'"
                        )

        return None

    def list_tools(self) -> list[dict[str, Any]]:
        """Return metadata for all registered tools."""
        return [tool.to_dict() for tool in self._tools.values()]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
