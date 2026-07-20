# Getting Started with AgentGrid

## Installation

```bash
pip install agentgrid
```

For optional dependencies:

```bash
pip install agentgrid[all]    # Everything
pip install agentgrid[memory] # SQLite memory backend
pip install agentgrid[http]   # HTTP client for tool integrations
```

## Quick Start

```python
from agentgrid import Agent, AgentConfig

class MyAgent(Agent):
    async def run(self, input_data: str = "") -> str:
        return f"Hello from {self.name}!"

agent = MyAgent(AgentConfig(name="my-agent"))
result = await agent.run("start")
print(result)
```

## Adding Tools

```python
from agentgrid.tools.base import BaseTool, ToolResult
from agentgrid.tools.registry import ToolRegistry

class SearchTool(BaseTool):
    @property
    def name(self) -> str:
        return "search"

    @property
    def description(self) -> str:
        return "Searches the web"

    async def execute(self, **kwargs) -> ToolResult:
        query = kwargs.get("query", "")
        # Your search logic here
        return ToolResult.ok(f"Results for: {query}")

registry = ToolRegistry()
registry.register(SearchTool())
```

## Using Memory

```python
from agentgrid.memory.engine import MemoryEngine

memory = MemoryEngine(namespace="my-agent")

await memory.store("user preferences", {"theme": "dark"})
prefs = await memory.retrieve("user preferences")
```

## Event Bus

```python
from agentgrid.events.bus import Event, EventBus

bus = EventBus()

async def on_event(event: Event):
    print(f"Event: {event.topic}")

bus.subscribe("agent.*", on_event)
await bus.emit(Event(topic="agent.run.started", payload={"input": "hello"}))
```

## Next Steps

- Read the [Architecture Guide](architecture.md)
- Check out the [Examples](../examples/)
- Browse the [API Reference](api.md)
