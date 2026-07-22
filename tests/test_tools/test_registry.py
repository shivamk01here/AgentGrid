"""Tests for the tool registry."""

import pytest
from agentgrid.tools.base import BaseTool, ToolResult
from agentgrid.tools.registry import ToolRegistry


class EchoTool(BaseTool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echoes input back"

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult.ok(kwargs.get("message", ""))


class FailTool(BaseTool):
    @property
    def name(self) -> str:
        return "fail"

    @property
    def description(self) -> str:
        return "Always fails"

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult.fail("intentional failure")


class TestToolRegistry:
    def test_register_tool(self, tool_registry):
        tool_registry.register(EchoTool())
        assert "echo" in tool_registry
        assert len(tool_registry) == 1

    def test_register_duplicate_raises(self, tool_registry):
        tool_registry.register(EchoTool())
        with pytest.raises(ValueError, match="already registered"):
            tool_registry.register(EchoTool())

    def test_unregister(self, tool_registry):
        tool_registry.register(EchoTool())
        assert tool_registry.unregister("echo") is True
        assert "echo" not in tool_registry

    def test_get_tool(self, tool_registry):
        tool_registry.register(EchoTool())
        tool = tool_registry.get("echo")
        assert tool is not None
        assert tool.name == "echo"

    @pytest.mark.asyncio
    async def test_invoke_tool(self, tool_registry):
        tool_registry.register(EchoTool())
        result = await tool_registry.invoke("echo", message="hello")
        assert result.success is True
        assert result.output == "hello"

    @pytest.mark.asyncio
    async def test_invoke_unknown_tool(self, tool_registry):
        result = await tool_registry.invoke("nonexistent")
        assert result.success is False
        assert "not found" in result.error

    def test_list_tools(self, tool_registry):
        tool_registry.register(EchoTool())
        tool_registry.register(FailTool())
        tools = tool_registry.list_tools()
        assert len(tools) == 2
        names = {t["name"] for t in tools}
        assert names == {"echo", "fail"}

    def test_tool_to_dict(self):
        tool = EchoTool()
        d = tool.to_dict()
        assert d["name"] == "echo"
        assert "description" in d
        assert "parameters" in d

    @pytest.mark.asyncio
    async def test_validation_rejects_missing_required_param(self, tool_registry):
        tool_registry.register(SchemaTool())
        result = await tool_registry.invoke("schema_tool")
        assert result.success is False
        assert "Missing required parameter" in result.error

    @pytest.mark.asyncio
    async def test_validation_accepts_valid_params(self, tool_registry):
        tool_registry.register(SchemaTool())
        result = await tool_registry.invoke("schema_tool", message="hello", count=3)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_validation_rejects_wrong_type(self, tool_registry):
        tool_registry.register(SchemaTool())
        result = await tool_registry.invoke("schema_tool", message=123, count=3)
        assert result.success is False
        assert "expected type 'string'" in result.error


class SchemaTool(BaseTool):
    @property
    def name(self) -> str:
        return "schema_tool"

    @property
    def description(self) -> str:
        return "Tool with schema validation"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["message"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult.ok(kwargs)
