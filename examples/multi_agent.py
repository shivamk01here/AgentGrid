"""Multi-agent example - agents using tools, memory, and events."""

import asyncio
from agentgrid import Agent, AgentConfig
from agentgrid.tools.base import BaseTool, ToolResult
from agentgrid.tools.registry import ToolRegistry
from agentgrid.memory.engine import MemoryEngine
from agentgrid.events.bus import Event, EventBus


# --- Define tools ---
class CalculatorTool(BaseTool):
    @property
    def name(self) -> str:
        return "calculator"

    @property
    def description(self) -> str:
        return "Evaluates a simple math expression"

    async def execute(self, **kwargs) -> ToolResult:
        expression = kwargs.get("expression", "")
        try:
            result = eval(expression)  # noqa: S307
            return ToolResult.ok(result)
        except Exception as e:
            return ToolResult.fail(str(e))


# --- Define agents ---
class MathAgent(Agent):
    def __init__(self, registry: ToolRegistry, memory: MemoryEngine, bus: EventBus):
        super().__init__(AgentConfig(name="math-agent"))
        self.registry = registry
        self.memory = memory
        self.bus = bus

    async def run(self, input_data: str = "") -> str:
        await self.bus.emit(Event(topic="agent.run.started", source=self.name))

        result = await self.registry.invoke("calculator", expression=input_data)
        if result.success:
            await self.memory.store(f"calc:{input_data}", result.output)
            await self.bus.emit(Event(
                topic="agent.run.completed",
                source=self.name,
                payload={"expression": input_data, "result": result.output},
            ))
            return f"{input_data} = {result.output}"
        else:
            return f"Error: {result.error}"


async def main():
    # Set up infrastructure
    registry = ToolRegistry()
    memory = MemoryEngine(namespace="demo")
    bus = EventBus()

    # Register tools
    registry.register(CalculatorTool())

    # Subscribe to events for observability
    async def log_event(event: Event):
        print(f"  [event] {event.topic}: {event.payload}")

    bus.subscribe("agent.*", log_event)

    # Create and run agent
    agent = MathAgent(registry, memory, bus)

    expressions = ["2 + 2", "10 * 5", "100 / 3"]
    for expr in expressions:
        output = await agent.run(expr)
        print(output)

    # Check memory
    stored = await memory.search(prefix="calc:")
    print(f"\nMemory entries: {len(stored)}")
    for entry in stored:
        print(f"  {entry.key} = {entry.value}")


if __name__ == "__main__":
    asyncio.run(main())
