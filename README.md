# AgentGrid

> Open infrastructure for production AI agents.

---

## What is AgentGrid?

A modular Python framework for building, running, and observing AI agents in production. Not another wrapper. A foundation.

---

## Features

- **Agent Runtime** - Execute agents with retries, timeouts, and lifecycle management
- **Tool Registry** - Register, discover, and invoke tools with a clean abstraction
- **Memory Engine** - Durable agent memory with TTL, namespacing, and search
- **Event Bus** - Async pub/sub for inter-agent communication and observability
- **Workflow Engine** - Multi-step workflows with dependency resolution
- **Scheduler** - Periodic and delayed task execution
- **Observability** - Structured logging and metrics out of the box
- **Auth** - API key authentication and permission controls

---

## Quick Start

```bash
pip install agentgrid
```

```python
from agentgrid import Agent, AgentConfig

class MyAgent(Agent):
    async def run(self, input_data: str = "") -> str:
        return f"Hello from {self.name}!"

agent = MyAgent(AgentConfig(name="my-agent"))
result = await agent.run("start")
```

---

## Project Structure

```
src/agentgrid/
├── agent/          # Core agent abstractions
├── tools/          # Tool registry and base classes
├── memory/         # Durable memory engine
├── events/         # Async event bus
├── workflow/       # Workflow orchestration
├── scheduler/      # Task scheduling
├── observability/  # Logging and metrics
├── auth/           # Authentication and permissions
└── utils/          # Configuration helpers
```

---

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check src/ tests/

# Type check
mypy src/agentgrid/
```

---

## License

MIT
