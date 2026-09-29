"""Tool registry - discovery, registration, and invocation."""

from __future__ import annotations

import logging
from typing import Any

from ledgerloop.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _matches_type(value: Any, type_name: str) -> bool:
    """True when `value` is a `type_name` in JSON Schema's terms.

    A bool is an int to Python and not to JSON Schema, so it only satisfies
    "boolean". A type name this validator does not know about is not its
    business to reject.
    """
    if type_name == "null":
        return value is None
    expected = _TYPE_MAP.get(type_name)
    if expected is None:
        return True
    if isinstance(value, bool) and expected is not bool:
        return False
    return isinstance(value, expected)


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
        try:
            return await tool.execute(**kwargs)
        except Exception as exc:
            logger.exception("Tool '%s' raised an exception", name)
            return ToolResult.fail(f"Tool '{name}' raised: {exc}")

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
            if param_name not in properties:
                continue
            declared = properties[param_name].get("type")
            # JSON Schema lets "type" be a list - ["string", "null"] is how an
            # optional parameter is usually written - and a list is not
            # hashable, so it cannot go through the lookup a lone name does.
            names = [declared] if isinstance(declared, str) else declared
            allowed = [n for n in names if isinstance(n, str)] if isinstance(names, list) else []
            if not allowed:
                continue

            value = kwargs[param_name]
            if any(_matches_type(value, name) for name in allowed):
                continue
            actual = "boolean" if isinstance(value, bool) else type(value).__name__
            return (
                f"Parameter '{param_name}' expected type '{' or '.join(allowed)}' "
                f"but got '{actual}'"
            )

        return None

    def list_tools(self) -> list[dict[str, Any]]:
        """Return metadata for all registered tools."""
        return [tool.to_dict() for tool in self._tools.values()]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
