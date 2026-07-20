"""Base tool abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """Standardized result from a tool execution."""

    success: bool
    output: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, output: Any, **metadata: Any) -> ToolResult:
        return cls(success=True, output=output, metadata=metadata)

    @classmethod
    def fail(cls, error: str, **metadata: Any) -> ToolResult:
        return cls(success=False, error=error, metadata=metadata)


class BaseTool(ABC):
    """Abstract base class for all AgentGrid tools.

    Every tool must define:
    - name: unique identifier
    - description: what the tool does (used by agents for tool selection)
    - execute(): the actual tool logic

    Tools should be stateless. Use the agent's memory for state.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool identifier."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what this tool does."""

    @property
    def parameters_schema(self) -> dict[str, Any]:
        """JSON Schema for the tool's parameters. Override to add params."""
        return {"type": "object", "properties": {}}

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """Run the tool with the given parameters.

        Args:
            **kwargs: Tool-specific parameters.

        Returns:
            A ToolResult with the outcome.
        """

    def to_dict(self) -> dict[str, Any]:
        """Serialize tool metadata for API responses."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters_schema,
        }

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
