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

## Capping What a Run Can Move

Give a run a value ceiling and the coordinator holds every value-moving
proposal against it:

```python
from ledgerloop.core import Currency, Money, RunBudget, RunSpec

spec = RunSpec(
    tenant_id=tenant,
    objective="Refund today's duplicates",
    budget=RunBudget(max_value_moved=Money.from_major("10000", Currency.INR)),
)
```

An action that would take the run past it raises `BudgetExhaustedError`, and
the run ends `FAILED` with `BUDGET_EXHAUSTED` before anything is dispatched.
What counts is the run's ledger: settled effects and ones still waiting on an
answer. Holds and voids carry amounts but move nothing, so they never count.

## Runs That Nobody Comes Back To

A run halted on a policy gate waits indefinitely. That is the right default
for a signature that arrives on Tuesday, and the wrong one for a case that
stopped mattering last week — the run sits in `AWAITING_APPROVAL` and its
request sits in somebody's queue.

The reaper sweeps the halted runs whose deadline has passed.

```python
from ledgerloop import Reaper

reaper = Reaper(
    runs=run_store,
    approvals=gateway,
    ledger=ledger,
    clock=clock,
)

report = await reaper.sweep()
print(report)  # checked=4 expired=4 approvals_retired=3 skipped=0
```

Each expired run ends `EXPIRED` with `DEADLINE_EXCEEDED`, the request behind
it is retired, and both land in the run's own hash-chained ledger. Run it on
a schedule, next to the reconciler.

Two things to know before you rely on it:

- expiry is opt-in. A run whose `RunSpec` has no `deadline` is never swept,
  because a caller who set none has not said when the case stops mattering.
- it reverses nothing. A run can have moved money before it halted; that is
  the compensator's job, and an expired run is a good candidate for it.

See `examples/expired_approval.py` for a full run of this in memory.

## Next Steps

- Read the [Architecture Guide](architecture.md)
- Check out the [Examples](../examples/)
- Browse the [API Reference](api.md)
