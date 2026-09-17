import pytest
from typing import Any
from ledgerloop.llm.openai_provider import OpenAIProvider
from ledgerloop.llm.provider import ToolCall

class DummyMessage:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.model_dump = lambda exclude_none: {"role": "assistant"}

class DummyChoice:
    def __init__(self, message, finish_reason="stop"):
        self.message = message
        self.finish_reason = finish_reason

class DummyResponse:
    def __init__(self, choices, usage=None):
        self.choices = choices
        self.usage = usage

class DummyUsage:
    def __init__(self, prompt_tokens=10, completion_tokens=20):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

class DummyToolCallFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments

class DummyToolCall:
    def __init__(self, id, function):
        self.id = id
        self.function = function

class DummyCompletions:
    async def create(self, **kwargs):
        return DummyResponse(
            choices=[DummyChoice(DummyMessage(content="Hello from OpenAI"))],
            usage=DummyUsage()
        )

class DummyChat:
    def __init__(self):
        self.completions = DummyCompletions()

class DummyOpenAIClient:
    def __init__(self):
        self.chat = DummyChat()

@pytest.mark.asyncio
async def test_openai_provider_complete():
    client = DummyOpenAIClient()
    provider = OpenAIProvider(client=client, model="gpt-4o")
    
    response = await provider.complete(
        system="You are an assistant",
        messages=[{"role": "user", "content": "Hi"}],
        max_tokens=100
    )
    
    assert response.text == "Hello from OpenAI"
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 20
    assert response.stop_reason == "stop"
    assert len(response.tool_calls) == 0

@pytest.mark.asyncio
async def test_openai_provider_tool_calls():
    client = DummyOpenAIClient()
    
    class ToolCompletions:
        async def create(self, **kwargs):
            func = DummyToolCallFunction(name="get_weather", arguments='{"location": "London"}')
            call = DummyToolCall(id="call_123", function=func)
            return DummyResponse(
                choices=[DummyChoice(DummyMessage(content=None, tool_calls=[call]), finish_reason="tool_calls")],
                usage=DummyUsage()
            )
            
    client.chat.completions = ToolCompletions()
    
    provider = OpenAIProvider(client=client, model="gpt-4o")
    response = await provider.complete(
        system="",
        messages=[{"role": "user", "content": "weather"}],
        tools=[{"name": "get_weather", "description": "Gets weather"}]
    )
    
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "get_weather"
    assert response.tool_calls[0].arguments == {"location": "London"}

def test_openai_build_tool_result_messages():
    provider = OpenAIProvider(client=DummyOpenAIClient())
    results = [("call_123", "Sunny", False), ("call_456", "Error!", True)]
    
    msgs = provider.build_tool_result_messages(results)
    
    assert len(msgs) == 2
    assert msgs[0] == {"role": "tool", "tool_call_id": "call_123", "content": "Sunny"}
    assert msgs[1] == {"role": "tool", "tool_call_id": "call_456", "content": "Error!"}
