# Getting Started with Ledgerloop

## Installation

```bash
pip install ledgerloop
```

For optional dependencies:

```bash
pip install ledgerloop[all]    # Everything
pip install ledgerloop[memory] # SQLite memory backend
pip install ledgerloop[http]   # HTTP client for tool integrations
```

## Quick Start

```python
from ledgerloop import Agent, AgentConfig

class MyAgent(Agent):
    async def run(self, input_data: str = "") -> str:
        return f"Hello from {self.name}!"

agent = MyAgent(AgentConfig(name="my-agent"))
result = await agent.run("start")
print(result)
```

## Adding Tools

```python
from ledgerloop.tools.base import BaseTool, ToolResult
from ledgerloop.tools.registry import ToolRegistry

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
from ledgerloop.memory.engine import MemoryEngine

memory = MemoryEngine(namespace="my-agent")

await memory.store("user preferences", {"theme": "dark"})
prefs = await memory.retrieve("user preferences")
```

## Event Bus

```python
from ledgerloop.events.bus import Event, EventBus

bus = EventBus()

async def on_event(event: Event):
    print(f"Event: {event.topic}")

bus.subscribe("agent.*", on_event)
await bus.emit(Event(topic="agent.run.started", payload={"input": "hello"}))
```

## Rolling a Run Back

When a run fails after it has already moved money, the compensator walks the
effects back out. It works off the run's own hash-chained ledger, so it
reverses what actually happened rather than what some in-memory list thinks
happened.

```python
from ledgerloop import Compensator

compensator = Compensator(
    runs=run_store,
    ledger=ledger,
    dispatcher=psp,
    idempotency=idempotency_store,
    clock=clock,
)

report = await compensator.compensate(run)

if report.complete:
    ...  # run is COMPENSATED, nothing left applied
else:
    ...  # run is FAILED, report.stranded effects need a human
```

Each reversal takes its own idempotency claim, derived from the original
effect's key, so re-running a rollback after a crash reverses each effect
once rather than twice.

Three things it will not do:

- reverse an action kind that has no reversal, such as a payout
- reverse an effect whose outcome was never determined — reconcile it first
- report a provider's refusal to reverse as a successful rollback

See `examples/rolled_back_batch.py` for a full run of this in memory.

## Next Steps

- Read the [Architecture Guide](architecture.md)
- Check out the [Examples](../examples/)
- Browse the [API Reference](api.md)
