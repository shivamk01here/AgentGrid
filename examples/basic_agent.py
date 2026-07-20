"""Basic agent example - the simplest possible AgentGrid agent."""

import asyncio
from agentgrid import Agent, AgentConfig


class SimpleAgent(Agent):
    """A minimal agent that echoes input with a prefix."""

    async def run(self, input_data: str = "") -> str:
        return f"[SimpleAgent] Received: {input_data}"


async def main():
    agent = SimpleAgent(AgentConfig(name="greeter"))
    result = await agent.run("Hello AgentGrid!")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
