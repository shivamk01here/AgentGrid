"""Built-in tools - ready-to-use tools for common tasks."""

from __future__ import annotations

import asyncio
import datetime
import time
from collections import Counter as _Counter
from typing import Any

from ledgerloop.tools.base import BaseTool, ToolResult


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
import base64

class Base64Tool(BaseTool):
    """Encodes or decodes text using Base64."""

    @property
    def name(self) -> str:
        return "base64"

    @property
    def description(self) -> str:
        return "Base64 encode or decode text"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The input text"},
                "action": {
                    "type": "string",
                    "description": "One of: encode, decode",
                },
            },
            "required": ["text", "action"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        text = kwargs["text"]
        action = kwargs["action"]
        
        if action == "encode":
            output = base64.b64encode(text.encode("utf-8")).decode("ascii")
        elif action == "decode":
            try:
                output = base64.b64decode(text.encode("ascii")).decode("utf-8")
            except Exception as exc:
                return ToolResult.fail(f"Decode error: {exc}")
        else:
            return ToolResult.fail("Unknown action. Choose from: encode, decode")

        return ToolResult.ok(output, action=action)

import hashlib

class HashTool(BaseTool):
    """Computes cryptographic hashes of text."""

    @property
    def name(self) -> str:
        return "hash"

    @property
    def description(self) -> str:
        return "Compute SHA256 or MD5 hash of text"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The input text"},
                "algorithm": {
                    "type": "string",
                    "description": "One of: sha256, md5",
                },
            },
            "required": ["text", "algorithm"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        text = kwargs["text"]
        algorithm = kwargs["algorithm"]
        
        if algorithm == "sha256":
            output = hashlib.sha256(text.encode("utf-8")).hexdigest()
        elif algorithm == "md5":
            output = hashlib.md5(text.encode("utf-8")).hexdigest()
        else:
            return ToolResult.fail("Unknown algorithm. Choose from: sha256, md5")

        return ToolResult.ok(output, algorithm=algorithm)

import uuid

class UUIDTool(BaseTool):
    """Generates UUIDs."""

    @property
    def name(self) -> str:
        return "uuid"

    @property
    def description(self) -> str:
        return "Generate a random UUID version 4"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        output = str(uuid.uuid4())
        return ToolResult.ok(output)

import operator

class MathTool(BaseTool):
    """Performs basic arithmetic operations."""

    @property
    def name(self) -> str:
        return "math"

    @property
    def description(self) -> str:
        return "Basic arithmetic operations (add, subtract, multiply, divide)"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "First operand"},
                "b": {"type": "number", "description": "Second operand"},
                "operation": {
                    "type": "string",
                    "description": "One of: add, subtract, multiply, divide",
                },
            },
            "required": ["a", "b", "operation"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        a = kwargs["a"]
        b = kwargs["b"]
        operation = kwargs["operation"]
        
        ops = {
            "add": operator.add,
            "subtract": operator.sub,
            "multiply": operator.mul,
            "divide": operator.truediv,
        }

        fn = ops.get(operation)
        if fn is None:
            return ToolResult.fail("Unknown operation. Choose from: add, subtract, multiply, divide")

        if operation == "divide" and b == 0:
            return ToolResult.fail("Division by zero")

        output = fn(a, b)
        return ToolResult.ok(output, operation=operation)

import re

class RegexTool(BaseTool):
    """Matches regular expressions against text."""

    @property
    def name(self) -> str:
        return "regex"

    @property
    def description(self) -> str:
        return "Find all matches of a regular expression pattern in text"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The input text"},
                "pattern": {"type": "string", "description": "The regex pattern to match"},
            },
            "required": ["text", "pattern"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        text = kwargs["text"]
        pattern = kwargs["pattern"]
        
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return ToolResult.fail(f"Invalid regex pattern: {exc}")

        matches = compiled.findall(text)
        return ToolResult.ok(matches, pattern=pattern)

import json

class JsonPathTool(BaseTool):
    """Extracts values from JSON strings."""

    @property
    def name(self) -> str:
        return "json_extract"

    @property
    def description(self) -> str:
        return "Extract a specific key from a JSON object string"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "json_string": {"type": "string", "description": "The JSON string"},
                "key": {"type": "string", "description": "The top-level key to extract"},
            },
            "required": ["json_string", "key"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        json_string = kwargs["json_string"]
        key = kwargs["key"]
        
        try:
            parsed = json.loads(json_string)
        except json.JSONDecodeError as exc:
            return ToolResult.fail(f"Invalid JSON string: {exc}")
        except TypeError:
            return ToolResult.fail("Invalid input type for JSON parsing")

        if not isinstance(parsed, dict):
            return ToolResult.fail("Parsed JSON is not an object")

        if key not in parsed:
            return ToolResult.fail(f"Key '{key}' not found in JSON object")

        return ToolResult.ok(parsed[key])

import urllib.request
import urllib.error

class HttpTool(BaseTool):
    """Fetches text content from URLs."""

    @property
    def name(self) -> str:
        return "http_fetch"

    @property
    def description(self) -> str:
        return "Fetch text content from a URL via HTTP GET"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to fetch"},
                "timeout": {"type": "number", "description": "Timeout in seconds (default 10)"},
            },
            "required": ["url"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        url = kwargs["url"]
        timeout = kwargs.get("timeout", 10.0)
        
        # Run synchronous urlopen in a thread to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        
        def _fetch():
            req = urllib.request.Request(
                url, 
                headers={'User-Agent': 'Ledgerloop-Agent/1.0'}
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    return response.read().decode('utf-8')
            except urllib.error.URLError as e:
                raise RuntimeError(f"Failed to fetch {url}: {e.reason}")
            except Exception as e:
                raise RuntimeError(f"Error fetching {url}: {e}")

        try:
            content = await loop.run_in_executor(None, _fetch)
            return ToolResult.ok(content)
        except Exception as e:
            return ToolResult.fail(str(e))

class SleepTool(BaseTool):
    """Pauses agent execution for a duration."""

    @property
    def name(self) -> str:
        return "sleep"

    @property
    def description(self) -> str:
        return "Pause execution for a specified number of seconds"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "seconds": {"type": "number", "description": "Seconds to sleep (max 60)"},
            },
            "required": ["seconds"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        seconds = kwargs["seconds"]
        if not isinstance(seconds, (int, float)):
            return ToolResult.fail("seconds must be a number")
        if seconds < 0:
            return ToolResult.fail("Cannot sleep for a negative duration")
        if seconds > 60:
            return ToolResult.fail("Cannot sleep for more than 60 seconds")
            
        await asyncio.sleep(seconds)
        return ToolResult.ok(f"Slept for {seconds} seconds")

class StringLengthTool(BaseTool):
    """Calculates the length of a string."""

    @property
    def name(self) -> str:
        return "string_length"

    @property
    def description(self) -> str:
        return "Calculate the length of a given text string"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The string to measure"},
            },
            "required": ["text"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        text = kwargs["text"]
        if not isinstance(text, str):
            return ToolResult.fail("text must be a string")
        return ToolResult.ok(len(text))

import urllib.parse

class UrlEncodeTool(BaseTool):
    """URL encodes a string."""

    @property
    def name(self) -> str:
        return "url_encode"

    @property
    def description(self) -> str:
        return "URL encode a given text string"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The string to encode"},
            },
            "required": ["text"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        text = kwargs["text"]
        if not isinstance(text, str):
            return ToolResult.fail("text must be a string")
        return ToolResult.ok(urllib.parse.quote(text))

class UrlDecodeTool(BaseTool):
    """URL decodes a string."""

    @property
    def name(self) -> str:
        return "url_decode"

    @property
    def description(self) -> str:
        return "URL decode a given text string"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The string to decode"},
            },
            "required": ["text"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        text = kwargs["text"]
        if not isinstance(text, str):
            return ToolResult.fail("text must be a string")
        return ToolResult.ok(urllib.parse.unquote(text))

import random

class RandomIntTool(BaseTool):
    """Generates a random integer."""

    @property
    def name(self) -> str:
        return "random_int"

    @property
    def description(self) -> str:
        return "Generate a random integer between min and max (inclusive)"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "min": {"type": "integer", "description": "Minimum value"},
                "max": {"type": "integer", "description": "Maximum value"},
            },
            "required": ["min", "max"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        min_val = kwargs["min"]
        max_val = kwargs["max"]
        if not isinstance(min_val, int) or not isinstance(max_val, int):
            return ToolResult.fail("min and max must be integers")
        if min_val > max_val:
            return ToolResult.fail("min cannot be greater than max")
        return ToolResult.ok(random.randint(min_val, max_val))

class StringSplitTool(BaseTool):
    """Splits a string by a delimiter."""

    @property
    def name(self) -> str:
        return "string_split"

    @property
    def description(self) -> str:
        return "Split a string into a list of parts using a delimiter"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The string to split"},
                "delimiter": {"type": "string", "description": "The delimiter to split by"},
            },
            "required": ["text", "delimiter"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        text = kwargs["text"]
        delimiter = kwargs["delimiter"]
        if not isinstance(text, str) or not isinstance(delimiter, str):
            return ToolResult.fail("text and delimiter must be strings")
        return ToolResult.ok(text.split(delimiter))

class StringReplaceTool(BaseTool):
    """Replaces occurrences of a substring."""

    @property
    def name(self) -> str:
        return "string_replace"

    @property
    def description(self) -> str:
        return "Replace all occurrences of a substring with a new string"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The input string"},
                "old": {"type": "string", "description": "The substring to replace"},
                "new": {"type": "string", "description": "The replacement string"},
            },
            "required": ["text", "old", "new"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        text = kwargs["text"]
        old = kwargs["old"]
        new = kwargs["new"]
        if not isinstance(text, str) or not isinstance(old, str) or not isinstance(new, str):
            return ToolResult.fail("all arguments must be strings")
        return ToolResult.ok(text.replace(old, new))

class RandomFloatTool(BaseTool):
    """Generates a random float."""

    @property
    def name(self) -> str:
        return "random_float"

    @property
    def description(self) -> str:
        return "Generate a random float between min and max"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "min": {"type": "number", "description": "Minimum value"},
                "max": {"type": "number", "description": "Maximum value"},
            },
            "required": ["min", "max"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        min_val = kwargs["min"]
        max_val = kwargs["max"]
        if not isinstance(min_val, (int, float)) or not isinstance(max_val, (int, float)):
            return ToolResult.fail("min and max must be numbers")
        if min_val > max_val:
            return ToolResult.fail("min cannot be greater than max")
        return ToolResult.ok(random.uniform(min_val, max_val))

class StringTrimTool(BaseTool):
    """Trims whitespace from a string."""

    @property
    def name(self) -> str:
        return "string_trim"

    @property
    def description(self) -> str:
        return "Remove leading and trailing whitespace from a string"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The input string"},
            },
            "required": ["text"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        text = kwargs["text"]
        if not isinstance(text, str):
            return ToolResult.fail("text must be a string")
        return ToolResult.ok(text.strip())

class DictKeysTool(BaseTool):
    """Extracts keys from a JSON object."""

    @property
    def name(self) -> str:
        return "dict_keys"

    @property
    def description(self) -> str:
        return "Extract a list of top-level keys from a JSON object string"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "json_string": {"type": "string", "description": "The JSON object string"},
            },
            "required": ["json_string"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        json_string = kwargs["json_string"]
        
        try:
            parsed = json.loads(json_string)
        except json.JSONDecodeError as exc:
            return ToolResult.fail(f"Invalid JSON string: {exc}")
        except TypeError:
            return ToolResult.fail("Invalid input type for JSON parsing")

        if not isinstance(parsed, dict):
            return ToolResult.fail("Parsed JSON is not an object")

        return ToolResult.ok(list(parsed.keys()))

class DictValuesTool(BaseTool):
    """Extracts values from a JSON object."""

    @property
    def name(self) -> str:
        return "dict_values"

    @property
    def description(self) -> str:
        return "Extract a list of top-level values from a JSON object string"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "json_string": {"type": "string", "description": "The JSON object string"},
            },
            "required": ["json_string"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        json_string = kwargs["json_string"]
        
        try:
            parsed = json.loads(json_string)
        except json.JSONDecodeError as exc:
            return ToolResult.fail(f"Invalid JSON string: {exc}")
        except TypeError:
            return ToolResult.fail("Invalid input type for JSON parsing")

        if not isinstance(parsed, dict):
            return ToolResult.fail("Parsed JSON is not an object")

        return ToolResult.ok(list(parsed.values()))

class StringJoinTool(BaseTool):
    """Joins a list of strings."""

    @property
    def name(self) -> str:
        return "string_join"

    @property
    def description(self) -> str:
        return "Join a list of strings using a delimiter"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "parts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The list of strings to join"
                },
                "delimiter": {"type": "string", "description": "The delimiter to join them with"},
            },
            "required": ["parts", "delimiter"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        parts = kwargs["parts"]
        delimiter = kwargs["delimiter"]
        if not isinstance(parts, list) or not all(isinstance(p, str) for p in parts):
            return ToolResult.fail("parts must be a list of strings")
        if not isinstance(delimiter, str):
            return ToolResult.fail("delimiter must be a string")
        return ToolResult.ok(delimiter.join(parts))

class StringLowerTool(BaseTool):
    """Converts a string to lowercase."""

    @property
    def name(self) -> str:
        return "string_lower"

    @property
    def description(self) -> str:
        return "Convert a string to lowercase"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The input string"},
            },
            "required": ["text"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        text = kwargs["text"]
        if not isinstance(text, str):
            return ToolResult.fail("text must be a string")
        return ToolResult.ok(text.lower())

class StringUpperTool(BaseTool):
    """Converts a string to uppercase."""

    @property
    def name(self) -> str:
        return "string_upper"

    @property
    def description(self) -> str:
        return "Convert a string to uppercase"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The input string"},
            },
            "required": ["text"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        text = kwargs["text"]
        if not isinstance(text, str):
            return ToolResult.fail("text must be a string")
        return ToolResult.ok(text.upper())

class ListLengthTool(BaseTool):
    """Calculates the length of a list."""

    @property
    def name(self) -> str:
        return "list_length"

    @property
    def description(self) -> str:
        return "Calculate the number of items in a list"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {},
                    "description": "The list to measure"
                },
            },
            "required": ["items"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        items = kwargs["items"]
        if not isinstance(items, list):
            return ToolResult.fail("items must be a list")
        return ToolResult.ok(len(items))

class ListReverseTool(BaseTool):
    """Reverses the order of a list."""

    @property
    def name(self) -> str:
        return "list_reverse"

    @property
    def description(self) -> str:
        return "Reverse the order of items in a list"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {},
                    "description": "The list to reverse"
                },
            },
            "required": ["items"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        items = kwargs["items"]
        if not isinstance(items, list):
            return ToolResult.fail("items must be a list")
        return ToolResult.ok(list(reversed(items)))

class ListSortTool(BaseTool):
    """Sorts a list of primitive values."""

    @property
    def name(self) -> str:
        return "list_sort"

    @property
    def description(self) -> str:
        return "Sort a list of primitive values (strings or numbers) in ascending order"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {},
                    "description": "The list to sort"
                },
            },
            "required": ["items"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        items = kwargs["items"]
        if not isinstance(items, list):
            return ToolResult.fail("items must be a list")
        try:
            return ToolResult.ok(sorted(items))
        except TypeError as exc:
            return ToolResult.fail(f"cannot sort items of mixed or complex types: {exc}")
