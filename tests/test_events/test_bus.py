"""Tests for the event bus."""

import pytest

from ledgerloop.events.bus import Event, EventBus


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
    async def test_duplicate_subscription_runs_once(self, event_bus):
        received = []

        async def handler(event: Event):
            received.append(event)

        event_bus.subscribe("test", handler)
        event_bus.subscribe("test", handler)
        count = await event_bus.emit(Event(topic="test"))
        assert count == 1
        assert len(received) == 1

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
    async def test_get_history_zero_limit(self, event_bus):
        await event_bus.emit(Event(topic="a"))
        await event_bus.emit(Event(topic="b"))
        assert event_bus.get_history(limit=0) == []
        assert event_bus.get_history(limit=-1) == []

    @pytest.mark.asyncio
    async def test_handler_may_subscribe_while_the_event_is_being_delivered(self, event_bus):
        received = []

        async def late(event: Event):
            received.append("late")

        async def subscribing(event: Event):
            received.append("subscribing")
            event_bus.subscribe("other", late)

        event_bus.subscribe("test", subscribing)
        count = await event_bus.emit(Event(topic="test"))

        assert count == 1
        assert received == ["subscribing"]
        # And the new subscriber works from the next event on.
        await event_bus.emit(Event(topic="other"))
        assert received == ["subscribing", "late"]

    @pytest.mark.asyncio
    async def test_handler_may_unsubscribe_while_the_event_is_being_delivered(self, event_bus):
        received = []

        async def second(event: Event):
            received.append("second")

        async def cancelling(event: Event):
            received.append("cancelling")
            assert event_bus.unsubscribe("test", second) is True

        event_bus.subscribe("test", cancelling)
        event_bus.subscribe("test", second)
        count = await event_bus.emit(Event(topic="test"))

        # The snapshot was taken at publish time, so the pair that was
        # subscribed then is still delivered to.
        assert count == 2
        assert received == ["cancelling", "second"]

    @pytest.mark.asyncio
    async def test_a_handler_can_emit_from_inside_itself(self, event_bus):
        seen = []

        async def outer(event: Event):
            seen.append(event.topic)
            if event.topic == "a":
                await event_bus.emit(Event(topic="b"))

        async def inner(event: Event):
            seen.append(event.topic)

        event_bus.subscribe("a", outer)
        event_bus.subscribe("b", inner)
        await event_bus.emit(Event(topic="a"))
        assert seen == ["a", "b"]

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

    @pytest.mark.asyncio
    async def test_get_history_empty_string_topic_filters(self, event_bus):
        # topic="" is a valid filter (no events will have an empty topic),
        # not the same as passing topic=None which returns everything.
        await event_bus.emit(Event(topic="a.b"))
        await event_bus.emit(Event(topic="a.c"))
        history = event_bus.get_history(topic="")
        assert history == []
