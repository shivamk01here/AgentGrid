"""LLM provider protocol - the model boundary.

Ledgerloop talks to models through this protocol only. Nothing above this
layer imports a vendor SDK, so a provider can be swapped without touching
the agent loop, the audit trail, or the approval gates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolCall:
    """A model's request to invoke one tool.

    `arguments` is always a parsed mapping - never a raw JSON string - so
    callers never string-match on serialized input.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class TokenUsage:
    """Token accounting for a single model call.

    Recorded per step so a run's cost can be reconstructed from the ledger.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(frozen=True)
class LLMResponse:
    """One model turn, normalized across providers.

    `raw_content` carries the provider's native assistant content. The agent
    loop appends it to the transcript verbatim and never inspects it - some
    providers require their own blocks (reasoning, signatures) to be echoed
    back byte-for-byte on the following request.
    """

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = "end_turn"
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw_content: Any = None
    reasoning: str = ""

    @property
    def wants_tools(self) -> bool:
        """True when the model is waiting on tool results to continue."""
        return bool(self.tool_calls)

    @property
    def refused(self) -> bool:
        """True when the model declined the request on policy grounds."""
        return self.stop_reason == "refusal"


class LLMError(RuntimeError):
    """A model call failed.

    `retryable` tells the runtime whether another attempt could succeed -
    rate limits and transport faults are retryable, malformed requests are not.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol for a chat-completion backend that supports tool use.

    Implement this to add a provider. The agent loop depends on nothing else.
    """

    @property
    def model(self) -> str:
        """Identifier of the model this provider calls."""
        ...

    async def complete(
        self,
        *,
        system: str,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
        effort: str = "high",
        max_tokens: int = 16000,
    ) -> LLMResponse:
        """Run one model turn.

        Args:
            system: System prompt. Kept stable across turns so it stays cached.
            messages: Transcript. Assistant entries hold provider-native content
                previously returned as `LLMResponse.raw_content`.
            tools: Tool definitions as `{name, description, parameters}`.
            effort: Reasoning depth - low, medium, high, xhigh, or max.
            max_tokens: Ceiling for this response.

        Returns:
            The normalized turn.

        Raises:
            LLMError: The call failed. Check `.retryable` before retrying.
        """
        ...

    def build_tool_result_message(
        self, results: list[tuple[str, str, bool]]
    ) -> Any:
        """Pack executed tool results into one provider-native user turn.

        Args:
            results: One `(tool_call_id, content, is_error)` per call the model
                made this turn - all of them, including failures.

        Returns:
            A single message to append to the transcript.
        """
        ...
