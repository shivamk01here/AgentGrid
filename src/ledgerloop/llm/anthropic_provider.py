"""Anthropic provider - Claude via the official async SDK."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ledgerloop.llm.provider import LLMError, LLMResponse, TokenUsage, ToolCall

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"


class AnthropicProvider:
    """Calls Claude through the Anthropic async SDK.

    Streams every request and resolves the final message, so a long
    reasoning turn cannot trip an HTTP timeout.

    Reasoning is requested as a summary rather than omitted: in a
    money-movement run, why the model chose an action belongs in the
    audit trail alongside what it did.

    Example:
        provider = AnthropicProvider()
        response = await provider.complete(
            system="You reconcile settlement files.",
            messages=[{"role": "user", "content": "Start."}],
        )
    """

    def __init__(
        self,
        *,
        client: AsyncAnthropic | None = None,
        model: str = DEFAULT_MODEL,
        reasoning_display: str = "summarized",
    ) -> None:
        if client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover - depends on install extras
                raise LLMError(
                    "The anthropic package is required for AnthropicProvider. "
                    "Install it with: pip install 'ledgerloop[anthropic]'"
                ) from exc
            client = AsyncAnthropic()
        self._client = client
        self._model = model
        self._reasoning_display = reasoning_display

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
        max_tokens: int = 16000,
    ) -> LLMResponse:
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": messages,
            # Adaptive thinking; no temperature - sampling params are rejected
            # on this model family.
            "thinking": {"type": "adaptive", "display": self._reasoning_display},
            "output_config": {"effort": effort},
        }
        if system:
            # Cached as a stable prefix - the system prompt does not vary per turn.
            request["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        if tools:
            request["tools"] = [self._to_anthropic_tool(t) for t in tools]

        try:
            async with self._client.messages.stream(**request) as stream:
                message = await stream.get_final_message()
        except Exception as exc:
            raise self._translate_error(exc) from exc

        return self._to_response(message)

    def build_tool_result_message(
        self, results: list[tuple[str, str, bool]]
    ) -> dict[str, Any]:
        """Pack every tool result into one user message.

        All results for a turn must travel together - splitting them across
        messages teaches the model to stop requesting calls in parallel.
        """
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": content,
                    "is_error": is_error,
                }
                for call_id, content, is_error in results
            ],
        }

    @staticmethod
    def _to_anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "input_schema": tool.get("parameters") or {"type": "object", "properties": {}},
        }

    def _to_response(self, message: Any) -> LLMResponse:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in message.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking":
                thought = getattr(block, "thinking", "")
                if thought:
                    reasoning_parts.append(thought)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
                )

        usage = message.usage
        return LLMResponse(
            text="".join(text_parts),
            tool_calls=tuple(tool_calls),
            stop_reason=message.stop_reason or "end_turn",
            usage=TokenUsage(
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            ),
            # Echoed back unchanged on the next turn - reasoning blocks carry
            # signatures that are invalidated by any edit.
            raw_content=message.content,
            reasoning="\n".join(reasoning_parts),
        )

    @staticmethod
    def _translate_error(exc: Exception) -> LLMError:
        """Map SDK exceptions onto LLMError, tagging what is worth retrying."""
        try:
            import anthropic
        except ImportError:  # pragma: no cover - only when the SDK is absent
            return LLMError(f"Model call failed: {exc}")

        if isinstance(exc, anthropic.NotFoundError):
            return LLMError(f"Unknown model or endpoint: {exc}")
        if isinstance(exc, anthropic.AuthenticationError):
            return LLMError("Anthropic credentials are missing or invalid")
        if isinstance(exc, anthropic.PermissionDeniedError):
            return LLMError("Anthropic credentials lack the required permissions")
        if isinstance(exc, anthropic.BadRequestError):
            return LLMError(f"Malformed model request: {exc}")
        if isinstance(exc, anthropic.RateLimitError):
            return LLMError(f"Rate limited by Anthropic: {exc}", retryable=True)
        if isinstance(exc, anthropic.APIStatusError):
            return LLMError(
                f"Anthropic returned {exc.status_code}: {exc}",
                retryable=exc.status_code >= 500,
            )
        if isinstance(exc, anthropic.APIConnectionError):
            return LLMError(f"Could not reach Anthropic: {exc}", retryable=True)
        return LLMError(f"Model call failed: {exc}")
