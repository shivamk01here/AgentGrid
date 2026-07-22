"""Built-in tools example - using DateTimeTool, TextTransformTool, and CounterTool."""

import asyncio
from agentgrid import (
    Agent,
    AgentConfig,
    AgentRuntime,
    ToolRegistry,
    DateTimeTool,
    TextTransformTool,
    CounterTool,
    EventBus,
)


class ToolAgent(Agent):
    """Agent that uses built-in tools via a ToolRegistry."""

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(AgentConfig(name="tool-agent"))
        self._registry = registry

    async def run(self, input_data: str = "") -> str:
        dt_result = await self._registry.invoke("datetime")
        text_result = await self._registry.invoke(
            "text_transform", text=input_data, operation="upper"
        )
        counter_result = await self._registry.invoke(
            "counter", counter_name="requests", action="increment"
        )
        return (
            f"Time: {dt_result.output}\n"
            f"Upper: {text_result.output}\n"
            f"Request count: {counter_result.output}"
        )


async def main():
    registry = ToolRegistry()
    registry.register(DateTimeTool())
    registry.register(TextTransformTool())
    registry.register(CounterTool())

    agent = ToolAgent(registry)
    bus = EventBus()
    agent.attach_event_bus(bus)

    runtime = AgentRuntime(agent)
    result = await runtime.execute("hello agentgrid")
    print(result["output"])

    history = bus.get_history()
    for event in history:
        print(f"  Event: {event.topic}")


if __name__ == "__main__":
    asyncio.run(main())
