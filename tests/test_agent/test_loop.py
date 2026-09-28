"""Tests for the agent tool-calling loop."""

from __future__ import annotations

import pytest

from ledgerloop.agent.base import Agent, AgentConfig
from ledgerloop.agent.loop import AgentLoop
from ledgerloop.llm.provider import LLMResponse, ToolCall
from ledgerloop.tools.base import BaseTool, ToolResult


class ScriptedProvider:
    """Replays a fixed list of turns and records the transcript it was given."""

    model = "scripted"

    def __init__(self, turns: list[LLMResponse]) -> None:
        self._turns = turns
        self.seen: list[list] = []

    async def complete(self, *, system, messages, tools=None, effort="high", max_tokens=16000):
        self.seen.append([dict(m) for m in messages])
        return self._turns[len(self.seen) - 1]

    def build_tool_result_messages(self, results):
        return [
            {"role": "tool", "tool_call_id": call_id, "content": content}
            for call_id, content, _is_error in results
        ]


class EchoTool(BaseTool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echoes its argument back"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult.ok(kwargs.get("text", ""))


def _agent(provider: ScriptedProvider) -> AgentLoop:
    agent = Agent(AgentConfig(name="echo-agent", system_prompt="be brief"))
    agent.attach_provider(provider)
    agent.attach_tool(EchoTool())
    return AgentLoop(agent)


class TestTranscriptEcho:
    @pytest.mark.asyncio
    async def test_assistant_turn_lands_in_the_next_request_unchanged(self):
        provider = ScriptedProvider(
            [
                LLMResponse(
                    text="",
                    tool_calls=(ToolCall(id="call_1", name="echo", arguments={"text": "hi"}),),
                    stop_reason="tool_calls",
                    raw_content={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "echo", "arguments": '{"text": "hi"}'},
                            }
                        ],
                    },
                ),
                LLMResponse(
                    text="hi",
                    stop_reason="end_turn",
                    raw_content={"role": "assistant", "content": "hi"},
                ),
            ]
        )

        result = await _agent(provider).execute("say hi")

        assert result.output == "hi"
        assert len(provider.seen) == 2
        second_request = provider.seen[1]
        # The provider's own turn, not a re-wrapped copy of its content.
        assert second_request[1] == {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"text": "hi"}'},
                }
            ],
        }
        assert second_request[2] == {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "hi",
        }

    @pytest.mark.asyncio
    async def test_bare_content_still_gets_an_assistant_envelope(self):
        provider = ScriptedProvider(
            [
                LLMResponse(
                    text="",
                    tool_calls=(ToolCall(id="call_1", name="echo", arguments={"text": "hi"}),),
                    stop_reason="tool_calls",
                    raw_content=[{"type": "text", "text": ""}],
                ),
                LLMResponse(text="bye", stop_reason="end_turn", raw_content="bye"),
            ]
        )

        await _agent(provider).execute("hi")

        assert provider.seen[1][0] == {"role": "user", "content": "hi"}
        assert provider.seen[1][1] == {
            "role": "assistant",
            "content": [{"type": "text", "text": ""}],
        }
