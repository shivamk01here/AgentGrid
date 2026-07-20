"""Tests for the event bus."""

import pytest
from agentgrid.events.bus import Event, EventBus


class TestEventBus:
    @pytest.mark.asyncio
    async def test_subscribe_and_emit(self, event_bus):
        received = []

        async def handler(event: Event):
            received.append(event)

        event_bus.subscribe("test.topic", handler)
        count = await event_bus.emit(Event(topic="test.topic", payload="data"))
        assert count == 1
        assert len(received) == 1
        assert received[0].payload == "data"

    @pytest.mark.asyncio
    async def test_wildcard_subscription(self, event_bus):
        received = []

        async def handler(event: Event):
            received.append(event.topic)

        event_bus.subscribe("agent.*", handler)
        await event_bus.emit(Event(topic="agent.run.started"))
        await event_bus.emit(Event(topic="agent.run.completed"))
        await event_bus.emit(Event(topic="system.boot"))
        assert received == ["agent.run.started", "agent.run.completed"]

    @pytest.mark.asyncio
    async def test_unsubscribe(self, event_bus):
        received = []

        async def handler(event: Event):
            received.append(event)

        event_bus.subscribe("test", handler)
        assert event_bus.unsubscribe("test", handler) is True
        await event_bus.emit(Event(topic="test"))
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_event_history(self, event_bus):
        await event_bus.emit(Event(topic="a"))
        await event_bus.emit(Event(topic="b"))
        await event_bus.emit(Event(topic="a"))
        history = event_bus.get_history(topic="a")
        assert len(history) == 2

    @pytest.mark.asyncio
    async def test_handler_error_does_not_crash(self, event_bus):
        async def bad_handler(event: Event):
            raise RuntimeError("oops")

        event_bus.subscribe("test", bad_handler)
        count = await event_bus.emit(Event(topic="test"))
        assert count == 0

    @pytest.mark.asyncio
    async def test_event_repr(self):
        e = Event(topic="test")
        assert "test" in repr(e)
        assert e.event_id in repr(e)
