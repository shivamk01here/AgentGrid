"""Tests for built-in tools."""

import pytest
from ledgerloop.tools.builtins import CounterTool, DateTimeTool, TextTransformTool, Base64Tool, HashTool, UUIDTool, MathTool, RegexTool, JsonPathTool, HttpTool, SleepTool, StringLengthTool, UrlEncodeTool, UrlDecodeTool, RandomIntTool, StringSplitTool, StringReplaceTool, RandomFloatTool, StringTrimTool, DictKeysTool, DictValuesTool, StringJoinTool, StringLowerTool, StringUpperTool, ListLengthTool, ListReverseTool, ListSortTool, StringCapitalizeTool, ListUniqueTool, MathAbsTool, MathRoundTool, StringContainsTool, ListSumTool, StringStartsWithTool, StringEndsWithTool, ListMaxTool, ListMinTool, ListAverageTool, DictMergeTool, DictGetTool, ListContainsTool, TypeOfTool, StringRepeatTool, MathClampTool


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

class TestRegexTool:
    @pytest.mark.asyncio
    async def test_match(self):
        tool = RegexTool()
        result = await tool.execute(text="The quick brown fox jumps over 42 dogs", pattern="\\d+")
        assert result.success is True
        assert result.output == ["42"]

    @pytest.mark.asyncio
    async def test_no_match(self):
        tool = RegexTool()
        result = await tool.execute(text="hello world", pattern="[0-9]+")
        assert result.success is True
        assert result.output == []

    @pytest.mark.asyncio
    async def test_invalid_pattern(self):
        tool = RegexTool()
        result = await tool.execute(text="hello", pattern="[unclosed")
        assert result.success is False
        assert "Invalid regex pattern" in result.error

class TestJsonPathTool:
    @pytest.mark.asyncio
    async def test_extract(self):
        tool = JsonPathTool()
        result = await tool.execute(json_string='{"status": "ok", "value": 42}', key="value")
        assert result.success is True
        assert result.output == 42

    @pytest.mark.asyncio
    async def test_missing_key(self):
        tool = JsonPathTool()
        result = await tool.execute(json_string='{"status": "ok"}', key="value")
        assert result.success is False
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_invalid_json(self):
        tool = JsonPathTool()
        result = await tool.execute(json_string='{status: "ok"}', key="status")
        assert result.success is False
        assert "Invalid JSON" in result.error

import io
from unittest.mock import patch, MagicMock

class TestHttpTool:
    @pytest.mark.asyncio
    async def test_fetch_success(self):
        tool = HttpTool()
        
        mock_response = MagicMock()
        mock_response.read.return_value = b"Hello from the web"
        mock_response.__enter__.return_value = mock_response
        
        with patch("urllib.request.urlopen", return_value=mock_response):
            result = await tool.execute(url="http://example.com")
            
        assert result.success is True
        assert result.output == "Hello from the web"

    @pytest.mark.asyncio
    async def test_fetch_failure(self):
        import urllib.error
        tool = HttpTool()
        
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Not found")):
            result = await tool.execute(url="http://example.com/bad")
            
        assert result.success is False
        assert "Not found" in result.error

class TestSleepTool:
    @pytest.mark.asyncio
    async def test_sleep_success(self):
        tool = SleepTool()
        # Mock asyncio.sleep to not actually wait
        with patch('asyncio.sleep', new_callable=MagicMock) as mock_sleep:
            result = await tool.execute(seconds=2.5)
            mock_sleep.assert_called_once_with(2.5)
            assert result.success is True
            assert "2.5" in result.output

    @pytest.mark.asyncio
    async def test_sleep_limits(self):
        tool = SleepTool()
        result = await tool.execute(seconds=-1)
        assert result.success is False
        
        result = await tool.execute(seconds=100)
        assert result.success is False

class TestStringLengthTool:
    @pytest.mark.asyncio
    async def test_length_success(self):
        tool = StringLengthTool()
        result = await tool.execute(text="hello")
        assert result.success is True
        assert result.output == 5
        
    @pytest.mark.asyncio
    async def test_length_invalid_input(self):
        tool = StringLengthTool()
        result = await tool.execute(text=123)
        assert result.success is False

class TestUrlEncodeTool:
    @pytest.mark.asyncio
    async def test_encode_success(self):
        tool = UrlEncodeTool()
        result = await tool.execute(text="hello world/foo")
        assert result.success is True
        assert result.output == "hello%20world/foo" or result.output == "hello%20world%2Ffoo"
        
    @pytest.mark.asyncio
    async def test_encode_invalid_input(self):
        tool = UrlEncodeTool()
        result = await tool.execute(text=123)
        assert result.success is False

class TestUrlDecodeTool:
    @pytest.mark.asyncio
    async def test_decode_success(self):
        tool = UrlDecodeTool()
        result = await tool.execute(text="hello%20world%2Ffoo")
        assert result.success is True
        assert result.output == "hello world/foo"
        
    @pytest.mark.asyncio
    async def test_decode_invalid_input(self):
        tool = UrlDecodeTool()
        result = await tool.execute(text=123)
        assert result.success is False

class TestRandomIntTool:
    @pytest.mark.asyncio
    async def test_random_success(self):
        tool = RandomIntTool()
        result = await tool.execute(min=1, max=10)
        assert result.success is True
        assert 1 <= result.output <= 10
        
    @pytest.mark.asyncio
    async def test_random_invalid_input(self):
        tool = RandomIntTool()
        result = await tool.execute(min=10, max=1)
        assert result.success is False

class TestStringSplitTool:
    @pytest.mark.asyncio
    async def test_split_success(self):
        tool = StringSplitTool()
        result = await tool.execute(text="a,b,c", delimiter=",")
        assert result.success is True
        assert result.output == ["a", "b", "c"]
        
    @pytest.mark.asyncio
    async def test_split_invalid_input(self):
        tool = StringSplitTool()
        result = await tool.execute(text=123, delimiter=",")
        assert result.success is False

class TestStringReplaceTool:
    @pytest.mark.asyncio
    async def test_replace_success(self):
        tool = StringReplaceTool()
        result = await tool.execute(text="hello world", old="world", new="there")
        assert result.success is True
        assert result.output == "hello there"
        
    @pytest.mark.asyncio
    async def test_replace_invalid_input(self):
        tool = StringReplaceTool()
        result = await tool.execute(text=123, old="1", new="2")
        assert result.success is False

class TestRandomFloatTool:
    @pytest.mark.asyncio
    async def test_random_success(self):
        tool = RandomFloatTool()
        result = await tool.execute(min=1.0, max=10.0)
        assert result.success is True
        assert 1.0 <= result.output <= 10.0
        
    @pytest.mark.asyncio
    async def test_random_invalid_input(self):
        tool = RandomFloatTool()
        result = await tool.execute(min=10.0, max=1.0)
        assert result.success is False

class TestStringTrimTool:
    @pytest.mark.asyncio
    async def test_trim_success(self):
        tool = StringTrimTool()
        result = await tool.execute(text="  hello world  \n")
        assert result.success is True
        assert result.output == "hello world"
        
    @pytest.mark.asyncio
    async def test_trim_invalid_input(self):
        tool = StringTrimTool()
        result = await tool.execute(text=123)
        assert result.success is False

class TestDictKeysTool:
    @pytest.mark.asyncio
    async def test_keys_success(self):
        tool = DictKeysTool()
        result = await tool.execute(json_string='{"a": 1, "b": 2}')
        assert result.success is True
        assert result.output == ["a", "b"]
        
    @pytest.mark.asyncio
    async def test_keys_invalid_input(self):
        tool = DictKeysTool()
        result = await tool.execute(json_string="[1, 2]")
        assert result.success is False

class TestDictValuesTool:
    @pytest.mark.asyncio
    async def test_values_success(self):
        tool = DictValuesTool()
        result = await tool.execute(json_string='{"a": 1, "b": 2}')
        assert result.success is True
        assert result.output == [1, 2]
        
    @pytest.mark.asyncio
    async def test_values_invalid_input(self):
        tool = DictValuesTool()
        result = await tool.execute(json_string="[1, 2]")
        assert result.success is False

class TestStringJoinTool:
    @pytest.mark.asyncio
    async def test_join_success(self):
        tool = StringJoinTool()
        result = await tool.execute(parts=["a", "b", "c"], delimiter="-")
        assert result.success is True
        assert result.output == "a-b-c"
        
    @pytest.mark.asyncio
    async def test_join_invalid_input(self):
        tool = StringJoinTool()
        result = await tool.execute(parts="not a list", delimiter="-")
        assert result.success is False

class TestStringLowerTool:
    @pytest.mark.asyncio
    async def test_lower_success(self):
        tool = StringLowerTool()
        result = await tool.execute(text="HeLlO wOrLd")
        assert result.success is True
        assert result.output == "hello world"
        
    @pytest.mark.asyncio
    async def test_lower_invalid_input(self):
        tool = StringLowerTool()
        result = await tool.execute(text=123)
        assert result.success is False

class TestStringUpperTool:
    @pytest.mark.asyncio
    async def test_upper_success(self):
        tool = StringUpperTool()
        result = await tool.execute(text="HeLlO wOrLd")
        assert result.success is True
        assert result.output == "HELLO WORLD"
        
    @pytest.mark.asyncio
    async def test_upper_invalid_input(self):
        tool = StringUpperTool()
        result = await tool.execute(text=123)
        assert result.success is False

class TestListLengthTool:
    @pytest.mark.asyncio
    async def test_length_success(self):
        tool = ListLengthTool()
        result = await tool.execute(items=["a", "b", "c"])
        assert result.success is True
        assert result.output == 3
        
    @pytest.mark.asyncio
    async def test_length_invalid_input(self):
        tool = ListLengthTool()
        result = await tool.execute(items="not a list")
        assert result.success is False

class TestListReverseTool:
    @pytest.mark.asyncio
    async def test_reverse_success(self):
        tool = ListReverseTool()
        result = await tool.execute(items=["a", "b", "c"])
        assert result.success is True
        assert result.output == ["c", "b", "a"]
        
    @pytest.mark.asyncio
    async def test_reverse_invalid_input(self):
        tool = ListReverseTool()
        result = await tool.execute(items="not a list")
        assert result.success is False

class TestListSortTool:
    @pytest.mark.asyncio
    async def test_sort_success(self):
        tool = ListSortTool()
        result = await tool.execute(items=[3, 1, 2])
        assert result.success is True
        assert result.output == [1, 2, 3]
        
    @pytest.mark.asyncio
    async def test_sort_mixed_types(self):
        tool = ListSortTool()
        result = await tool.execute(items=[3, "a", 2])
        assert result.success is False
        assert "cannot sort items" in result.error

class TestStringCapitalizeTool:
    @pytest.mark.asyncio
    async def test_capitalize_success(self):
        tool = StringCapitalizeTool()
        result = await tool.execute(text="hello world")
        assert result.success is True
        assert result.output == "Hello world"
        
    @pytest.mark.asyncio
    async def test_capitalize_invalid_input(self):
        tool = StringCapitalizeTool()
        result = await tool.execute(text=123)
        assert result.success is False

class TestListUniqueTool:
    @pytest.mark.asyncio
    async def test_unique_success(self):
        tool = ListUniqueTool()
        result = await tool.execute(items=["a", "b", "a", "c", "b"])
        assert result.success is True
        assert result.output == ["a", "b", "c"]
        
    @pytest.mark.asyncio
    async def test_unique_unhashable(self):
        tool = ListUniqueTool()
        result = await tool.execute(items=[{"a": 1}, {"a": 1}])
        assert result.success is False
        assert "hashable" in result.error

class TestMathAbsTool:
    @pytest.mark.asyncio
    async def test_abs_success(self):
        tool = MathAbsTool()
        result = await tool.execute(value=-42.5)
        assert result.success is True
        assert result.output == 42.5
        
    @pytest.mark.asyncio
    async def test_abs_invalid_input(self):
        tool = MathAbsTool()
        result = await tool.execute(value="not a number")
        assert result.success is False

class TestMathRoundTool:
    @pytest.mark.asyncio
    async def test_round_to_decimal(self):
        tool = MathRoundTool()
        result = await tool.execute(value=3.14159, decimal_places=2)
        assert result.success is True
        assert result.output == 3.14
        
    @pytest.mark.asyncio
    async def test_round_to_zero(self):
        tool = MathRoundTool()
        result = await tool.execute(value=3.7)
        assert result.success is True
        assert result.output == 4
        
    @pytest.mark.asyncio
    async def test_round_invalid_input(self):
        tool = MathRoundTool()
        result = await tool.execute(value="not a number")
        assert result.success is False

class TestStringContainsTool:
    @pytest.mark.asyncio
    async def test_contains_success(self):
        tool = StringContainsTool()
        result = await tool.execute(text="Hello World", substring="World")
        assert result.success is True
        assert result.output is True
        
    @pytest.mark.asyncio
    async def test_case_insensitive(self):
        tool = StringContainsTool()
        result = await tool.execute(text="Hello World", substring="world", case_sensitive=False)
        assert result.success is True
        assert result.output is True
        
    @pytest.mark.asyncio
    async def test_not_found(self):
        tool = StringContainsTool()
        result = await tool.execute(text="Hello", substring="xyz")
        assert result.success is True
        assert result.output is False

class TestListSumTool:
    @pytest.mark.asyncio
    async def test_sum_success(self):
        tool = ListSumTool()
        result = await tool.execute(items=[1, 2, 3, 4.5])
        assert result.success is True
        assert result.output == 10.5
        
    @pytest.mark.asyncio
    async def test_sum_invalid_types(self):
        tool = ListSumTool()
        result = await tool.execute(items=[1, "two", 3])
        assert result.success is False
        assert "numbers" in result.error
        
    @pytest.mark.asyncio
    async def test_sum_empty_list(self):
        tool = ListSumTool()
        result = await tool.execute(items=[])
        assert result.success is True
        assert result.output == 0

class TestStringStartsWithTool:
    @pytest.mark.asyncio
    async def test_starts_with_true(self):
        tool = StringStartsWithTool()
        result = await tool.execute(text="hello world", prefix="hello")
        assert result.success is True
        assert result.output is True

    @pytest.mark.asyncio
    async def test_starts_with_false(self):
        tool = StringStartsWithTool()
        result = await tool.execute(text="hello world", prefix="world")
        assert result.success is True
        assert result.output is False

    @pytest.mark.asyncio
    async def test_starts_with_invalid_input(self):
        tool = StringStartsWithTool()
        result = await tool.execute(text=123, prefix="h")
        assert result.success is False

class TestStringEndsWithTool:
    @pytest.mark.asyncio
    async def test_ends_with_true(self):
        tool = StringEndsWithTool()
        result = await tool.execute(text="hello.json", suffix=".json")
        assert result.success is True
        assert result.output is True

    @pytest.mark.asyncio
    async def test_ends_with_false(self):
        tool = StringEndsWithTool()
        result = await tool.execute(text="hello.csv", suffix=".json")
        assert result.success is True
        assert result.output is False

    @pytest.mark.asyncio
    async def test_ends_with_invalid_input(self):
        tool = StringEndsWithTool()
        result = await tool.execute(text=123, suffix=".json")
        assert result.success is False

class TestListMaxTool:
    @pytest.mark.asyncio
    async def test_max_success(self):
        tool = ListMaxTool()
        result = await tool.execute(items=[3, 1, 7, 2])
        assert result.success is True
        assert result.output == 7

    @pytest.mark.asyncio
    async def test_max_empty_list(self):
        tool = ListMaxTool()
        result = await tool.execute(items=[])
        assert result.success is False

    @pytest.mark.asyncio
    async def test_max_invalid_types(self):
        tool = ListMaxTool()
        result = await tool.execute(items=[1, "two"])
        assert result.success is False

class TestListMinTool:
    @pytest.mark.asyncio
    async def test_min_success(self):
        tool = ListMinTool()
        result = await tool.execute(items=[3, 1, 7, 2])
        assert result.success is True
        assert result.output == 1

    @pytest.mark.asyncio
    async def test_min_empty_list(self):
        tool = ListMinTool()
        result = await tool.execute(items=[])
        assert result.success is False

    @pytest.mark.asyncio
    async def test_min_invalid_types(self):
        tool = ListMinTool()
        result = await tool.execute(items=[1, "two"])
        assert result.success is False

class TestListAverageTool:
    @pytest.mark.asyncio
    async def test_average_success(self):
        tool = ListAverageTool()
        result = await tool.execute(items=[1, 2, 3, 4, 5])
        assert result.success is True
        assert result.output == 3.0

    @pytest.mark.asyncio
    async def test_average_empty_list(self):
        tool = ListAverageTool()
        result = await tool.execute(items=[])
        assert result.success is False

    @pytest.mark.asyncio
    async def test_average_invalid_types(self):
        tool = ListAverageTool()
        result = await tool.execute(items=[1, "two"])
        assert result.success is False

class TestDictMergeTool:
    @pytest.mark.asyncio
    async def test_merge_success(self):
        tool = DictMergeTool()
        result = await tool.execute(base={"a": 1, "b": 2}, overrides={"b": 99, "c": 3})
        assert result.success is True
        assert result.output == {"a": 1, "b": 99, "c": 3}

    @pytest.mark.asyncio
    async def test_merge_invalid_base(self):
        tool = DictMergeTool()
        result = await tool.execute(base="not a dict", overrides={})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_merge_invalid_overrides(self):
        tool = DictMergeTool()
        result = await tool.execute(base={}, overrides="not a dict")
        assert result.success is False

class TestDictGetTool:
    @pytest.mark.asyncio
    async def test_get_existing_key(self):
        tool = DictGetTool()
        result = await tool.execute(data={"a": 1, "b": 2}, key="a")
        assert result.success is True
        assert result.output == 1

    @pytest.mark.asyncio
    async def test_get_missing_key_with_default(self):
        tool = DictGetTool()
        result = await tool.execute(data={"a": 1}, key="z", default="fallback")
        assert result.success is True
        assert result.output == "fallback"

    @pytest.mark.asyncio
    async def test_get_invalid_data(self):
        tool = DictGetTool()
        result = await tool.execute(data="not a dict", key="a")
        assert result.success is False

class TestListContainsTool:
    @pytest.mark.asyncio
    async def test_contains_found(self):
        tool = ListContainsTool()
        result = await tool.execute(items=[1, 2, 3], value=2)
        assert result.success is True
        assert result.output is True

    @pytest.mark.asyncio
    async def test_contains_not_found(self):
        tool = ListContainsTool()
        result = await tool.execute(items=[1, 2, 3], value=99)
        assert result.success is True
        assert result.output is False

    @pytest.mark.asyncio
    async def test_contains_invalid_list(self):
        tool = ListContainsTool()
        result = await tool.execute(items="not a list", value=1)
        assert result.success is False

class TestTypeOfTool:
    @pytest.mark.asyncio
    async def test_type_of_string(self):
        tool = TypeOfTool()
        result = await tool.execute(value="hello")
        assert result.success is True
        assert result.output == "str"

    @pytest.mark.asyncio
    async def test_type_of_int(self):
        tool = TypeOfTool()
        result = await tool.execute(value=42)
        assert result.success is True
        assert result.output == "int"

    @pytest.mark.asyncio
    async def test_type_of_list(self):
        tool = TypeOfTool()
        result = await tool.execute(value=[1, 2, 3])
        assert result.success is True
        assert result.output == "list"

class TestStringRepeatTool:
    @pytest.mark.asyncio
    async def test_repeat_success(self):
        tool = StringRepeatTool()
        result = await tool.execute(text="ab", count=3)
        assert result.success is True
        assert result.output == "ababab"

    @pytest.mark.asyncio
    async def test_repeat_zero(self):
        tool = StringRepeatTool()
        result = await tool.execute(text="ab", count=0)
        assert result.success is True
        assert result.output == ""

    @pytest.mark.asyncio
    async def test_repeat_negative(self):
        tool = StringRepeatTool()
        result = await tool.execute(text="ab", count=-1)
        assert result.success is False

class TestMathClampTool:
    @pytest.mark.asyncio
    async def test_clamp_below_min(self):
        tool = MathClampTool()
        result = await tool.execute(value=-5, min_val=0, max_val=100)
        assert result.success is True
        assert result.output == 0

    @pytest.mark.asyncio
    async def test_clamp_above_max(self):
        tool = MathClampTool()
        result = await tool.execute(value=200, min_val=0, max_val=100)
        assert result.success is True
        assert result.output == 100

    @pytest.mark.asyncio
    async def test_clamp_within_range(self):
        tool = MathClampTool()
        result = await tool.execute(value=50, min_val=0, max_val=100)
        assert result.success is True
        assert result.output == 50

    @pytest.mark.asyncio
    async def test_clamp_invalid_bounds(self):
        tool = MathClampTool()
        result = await tool.execute(value=50, min_val=100, max_val=0)
        assert result.success is False
