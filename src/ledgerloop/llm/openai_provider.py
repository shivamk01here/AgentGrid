"""OpenAI provider - GPT via the official async SDK."""

from __future__ import annotations

import logging
import json
from typing import TYPE_CHECKING, Any

from ledgerloop.llm.provider import LLMError, LLMResponse, TokenUsage, ToolCall

if TYPE_CHECKING:
    from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o"

class OpenAIProvider:
    """Calls OpenAI through the OpenAI async SDK.
    
    Example:
        provider = OpenAIProvider()
        response = await provider.complete(
            system="You reconcile settlement files.",
            messages=[{"role": "user", "content": "Start."}],
        )
    """

    def __init__(
        self,
        *,
        client: AsyncOpenAI | None = None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        if client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise LLMError(
                    "The openai package is required for OpenAIProvider. "
                    "Install it with: pip install openai"
                ) from exc
            client = AsyncOpenAI()
        self._client = client
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self,
        *,
        system: str,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
        effort: str = "high",
        max_tokens: int = 4096,
    ) -> LLMResponse:
        openai_messages = []
        if system:
            openai_messages.append({"role": "system", "content": system})
        openai_messages.extend(messages)
        
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": openai_messages,
        }
        if tools:
            request["tools"] = [self._to_openai_tool(t) for t in tools]

        try:
            response = await self._client.chat.completions.create(**request)
        except Exception as exc:
            raise self._translate_error(exc) from exc

        return self._to_response(response)



    def build_tool_result_messages(
        self, results: list[tuple[str, str, bool]]
    ) -> list[Any]:
        """Pack every tool result into separate tool messages for OpenAI."""
        return [
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": content,
            }
            for call_id, content, _is_error in results
        ]

    @staticmethod
    def _to_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
            },
        }

    def _to_response(self, response: Any) -> LLMResponse:
        message = response.choices[0].message
        text_parts = []
        if message.content:
            text_parts.append(message.content)

        tool_calls = []
        if message.tool_calls:
            for call in message.tool_calls:
                # OpenAI returns arguments as a JSON string, we need to parse it
                try:
                    args = json.loads(call.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                tool_calls.append(
                    ToolCall(id=call.id, name=call.function.name, arguments=args)
                )

        usage = response.usage
        return LLMResponse(
            text="".join(text_parts),
            tool_calls=tuple(tool_calls),
            stop_reason=response.choices[0].finish_reason or "end_turn",
            usage=TokenUsage(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            ),
            raw_content=message.model_dump(exclude_none=True),
        )

    @staticmethod
    def _translate_error(exc: Exception) -> LLMError:
        try:
            import openai
        except ImportError:
            return LLMError(f"Model call failed: {exc}")

        if isinstance(exc, openai.AuthenticationError):
            return LLMError("OpenAI credentials are missing or invalid")
        if isinstance(exc, openai.RateLimitError):
            return LLMError(f"Rate limited by OpenAI: {exc}", retryable=True)
        if isinstance(exc, openai.APIConnectionError):
            return LLMError(f"Could not reach OpenAI: {exc}", retryable=True)
        if isinstance(exc, openai.APIStatusError):
            return LLMError(
                f"OpenAI returned {exc.status_code}: {exc}",
                retryable=exc.status_code >= 500,
            )
        return LLMError(f"Model call failed: {exc}")
