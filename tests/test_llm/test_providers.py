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

class MockAnthropicMessage:
    def __init__(self, content, stop_reason, usage):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage

class MockAnthropicStream:
    def __init__(self, message):
        self.message = message
        
    async def __aenter__(self):
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass
        
    async def get_final_message(self):
        return self.message

class MockAnthropicMessages:
    def __init__(self, message_to_return):
        self.message_to_return = message_to_return
        self.last_kwargs = None
        
    def stream(self, **kwargs):
        self.last_kwargs = kwargs
        return MockAnthropicStream(self.message_to_return)

class MockAsyncAnthropic:
    def __init__(self, message_to_return):
        self.messages = MockAnthropicMessages(message_to_return)

class MockAnthropicBlock:
    def __init__(self, type_name, text="", thinking="", id="", name="", input=None):
        self.type = type_name
        self.text = text
        self.thinking = thinking
        self.id = id
        self.name = name
        self.input = input or {}

class MockUsage:
    def __init__(self, input_tokens=10, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens

class TestAnthropicProvider:
    @pytest.mark.asyncio
    async def test_complete_text_only(self):
        from ledgerloop.llm.anthropic_provider import AnthropicProvider
        
        block = MockAnthropicBlock(type_name="text", text="Hello world")
        message = MockAnthropicMessage(content=[block], stop_reason="end_turn", usage=MockUsage())
        
        client = MockAsyncAnthropic(message)
        provider = AnthropicProvider(client=client)
        
        response = await provider.complete(
            system="System prompt",
            messages=[{"role": "user", "content": "Hi"}],
        )
        
        assert response.text == "Hello world"
        assert not response.tool_calls
        assert response.stop_reason == "end_turn"
        assert client.messages.last_kwargs["model"] == "claude-opus-5"
        assert client.messages.last_kwargs["system"][0]["text"] == "System prompt"

    @pytest.mark.asyncio
    async def test_complete_with_tool_call_and_thinking(self):
        from ledgerloop.llm.anthropic_provider import AnthropicProvider
        
        thinking_block = MockAnthropicBlock(type_name="thinking", thinking="I should use the tool")
        tool_block = MockAnthropicBlock(type_name="tool_use", id="call_1", name="my_tool", input={"arg1": "val1"})
        
        message = MockAnthropicMessage(
            content=[thinking_block, tool_block],
            stop_reason="tool_use",
            usage=MockUsage()
        )
        
        client = MockAsyncAnthropic(message)
        provider = AnthropicProvider(client=client)
        
        response = await provider.complete(
            system="",
            messages=[{"role": "user", "content": "Do it"}],
            tools=[{"name": "my_tool", "description": "Does something"}]
        )
        
        assert response.reasoning == "I should use the tool"
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "my_tool"
        assert response.tool_calls[0].arguments == {"arg1": "val1"}
        assert response.stop_reason == "tool_use"
        
        assert "tools" in client.messages.last_kwargs
        assert len(client.messages.last_kwargs["tools"]) == 1
