"""Tests for built-in tools."""

import pytest
from ledgerloop.tools.builtins import CounterTool, DateTimeTool, TextTransformTool, Base64Tool, HashTool, UUIDTool, MathTool


class TestDateTimeTool:
    @pytest.mark.asyncio
    async def test_default_iso_format(self):
        tool = DateTimeTool()
        result = await tool.execute()
        assert result.success is True
        assert "T" in result.output
        assert result.metadata["timestamp"] > 0

    @pytest.mark.asyncio
    async def test_custom_format(self):
        tool = DateTimeTool()
        result = await tool.execute(format="%Y-%m-%d")
        assert result.success is True
        assert len(result.output) == 10

    @pytest.mark.asyncio
    async def test_invalid_format(self):
        tool = DateTimeTool()
        result = await tool.execute(format="%Q%Z%INVALID")
        assert result.success is False
        assert "Invalid format" in result.error


class TestTextTransformTool:
    @pytest.mark.asyncio
    async def test_uppercase(self):
        tool = TextTransformTool()
        result = await tool.execute(text="hello world", operation="upper")
        assert result.success is True
        assert result.output == "HELLO WORLD"

    @pytest.mark.asyncio
    async def test_lowercase(self):
        tool = TextTransformTool()
        result = await tool.execute(text="HELLO WORLD", operation="lower")
        assert result.success is True
        assert result.output == "hello world"

    @pytest.mark.asyncio
    async def test_reverse(self):
        tool = TextTransformTool()
        result = await tool.execute(text="abc", operation="reverse")
        assert result.success is True
        assert result.output == "cba"

    @pytest.mark.asyncio
    async def test_word_count(self):
        tool = TextTransformTool()
        result = await tool.execute(text="one two three", operation="word_count")
        assert result.success is True
        assert result.output == 3

    @pytest.mark.asyncio
    async def test_unknown_operation(self):
        tool = TextTransformTool()
        result = await tool.execute(text="hello", operation="rotate")
        assert result.success is False
        assert "Unknown operation" in result.error


class TestCounterTool:
    @pytest.mark.asyncio
    async def test_increment(self):
        tool = CounterTool()
        result = await tool.execute(counter_name="visits", action="increment")
        assert result.success is True
        assert result.output == 1

    @pytest.mark.asyncio
    async def test_increment_by_amount(self):
        tool = CounterTool()
        await tool.execute(counter_name="score", action="increment", amount=5)
        result = await tool.execute(counter_name="score", action="increment", amount=3)
        assert result.output == 8

    @pytest.mark.asyncio
    async def test_decrement(self):
        tool = CounterTool()
        await tool.execute(counter_name="balance", action="increment", amount=10)
        result = await tool.execute(counter_name="balance", action="decrement", amount=4)
        assert result.output == 6

    @pytest.mark.asyncio
    async def test_read(self):
        tool = CounterTool()
        await tool.execute(counter_name="x", action="increment", amount=7)
        result = await tool.execute(counter_name="x", action="read")
        assert result.output == 7

    @pytest.mark.asyncio
    async def test_reset(self):
        tool = CounterTool()
        await tool.execute(counter_name="x", action="increment", amount=100)
        result = await tool.execute(counter_name="x", action="reset")
        assert result.output == 0

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        tool = CounterTool()
        result = await tool.execute(counter_name="x", action="multiply")
        assert result.success is False
        assert "Unknown action" in result.error

    @pytest.mark.asyncio
    async def test_counters_property(self):
        tool = CounterTool()
        await tool.execute(counter_name="a", action="increment", amount=3)
        await tool.execute(counter_name="b", action="increment", amount=7)
        assert tool.counters == {"a": 3, "b": 7}

class TestBase64Tool:
    @pytest.mark.asyncio
    async def test_encode(self):
        tool = Base64Tool()
        result = await tool.execute(text="hello world", action="encode")
        assert result.success is True
        assert result.output == "aGVsbG8gd29ybGQ="

    @pytest.mark.asyncio
    async def test_decode(self):
        tool = Base64Tool()
        result = await tool.execute(text="aGVsbG8gd29ybGQ=", action="decode")
        assert result.success is True
        assert result.output == "hello world"

    @pytest.mark.asyncio
    async def test_decode_error(self):
        tool = Base64Tool()
        result = await tool.execute(text="invalid_base64!", action="decode")
        assert result.success is False
        assert "Decode error" in result.error

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        tool = Base64Tool()
        result = await tool.execute(text="hello", action="rotate")
        assert result.success is False
        assert "Unknown action" in result.error

class TestHashTool:
    @pytest.mark.asyncio
    async def test_sha256(self):
        tool = HashTool()
        result = await tool.execute(text="hello", algorithm="sha256")
        assert result.success is True
        assert result.output == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

    @pytest.mark.asyncio
    async def test_md5(self):
        tool = HashTool()
        result = await tool.execute(text="hello", algorithm="md5")
        assert result.success is True
        assert result.output == "5d41402abc4b2a76b9719d911017c592"

    @pytest.mark.asyncio
    async def test_unknown_algorithm(self):
        tool = HashTool()
        result = await tool.execute(text="hello", algorithm="sha1")
        assert result.success is False
        assert "Unknown algorithm" in result.error

class TestUUIDTool:
    @pytest.mark.asyncio
    async def test_generate(self):
        tool = UUIDTool()
        result = await tool.execute()
        assert result.success is True
        assert len(result.output) == 36
        assert "-" in result.output

class TestMathTool:
    @pytest.mark.asyncio
    async def test_add(self):
        tool = MathTool()
        result = await tool.execute(a=5, b=3, operation="add")
        assert result.success is True
        assert result.output == 8

    @pytest.mark.asyncio
    async def test_subtract(self):
        tool = MathTool()
        result = await tool.execute(a=5, b=3, operation="subtract")
        assert result.success is True
        assert result.output == 2

    @pytest.mark.asyncio
    async def test_multiply(self):
        tool = MathTool()
        result = await tool.execute(a=5, b=3, operation="multiply")
        assert result.success is True
        assert result.output == 15

    @pytest.mark.asyncio
    async def test_divide(self):
        tool = MathTool()
        result = await tool.execute(a=6, b=3, operation="divide")
        assert result.success is True
        assert result.output == 2.0

    @pytest.mark.asyncio
    async def test_divide_by_zero(self):
        tool = MathTool()
        result = await tool.execute(a=6, b=0, operation="divide")
        assert result.success is False
        assert "Division by zero" in result.error

    @pytest.mark.asyncio
    async def test_unknown_operation(self):
        tool = MathTool()
        result = await tool.execute(a=6, b=3, operation="power")
        assert result.success is False
        assert "Unknown operation" in result.error
