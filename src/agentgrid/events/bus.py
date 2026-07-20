"""Event bus - async pub/sub for agent observability and coordination."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

EventHandler = Callable[["Event"], Coroutine[Any, Any, None]]


@dataclass
class Event:
    """An event in the system."""

    topic: str
    payload: Any = None
    source: str = ""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    timestamp: float = field(default_factory=time.time)

    def __repr__(self) -> str:
        return f"Event(topic={self.topic!r}, id={self.event_id!r})"


class EventBus:
    """Async publish-subscribe event bus.

    Agents and components emit events. Observers subscribe to topics.
    Supports wildcard subscriptions with '*'.

    Example:
        bus = EventBus()
        bus.subscribe("agent.run.*", my_handler)
        await bus.emit(Event(topic="agent.run.started"))
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}
        self._history: list[Event] = []
        self._max_history = 1000

    def subscribe(self, topic: str, handler: EventHandler) -> None:
        """Subscribe a handler to a topic pattern."""
        self._handlers.setdefault(topic, []).append(handler)
        logger.debug("Subscribed to topic=%s", topic)

    def unsubscribe(self, topic: str, handler: EventHandler) -> bool:
        """Remove a handler from a topic. Returns True if found."""
        handlers = self._handlers.get(topic, [])
        if handler in handlers:
            handlers.remove(handler)
            return True
        return False

    async def emit(self, event: Event) -> int:
        """Emit an event to all matching subscribers.

        Returns the number of handlers that were invoked.
        """
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        invoked = 0
        for topic_pattern, handlers in self._handlers.items():
            if self._matches(topic_pattern, event.topic):
                for handler in handlers:
                    try:
                        await handler(event)
                        invoked += 1
                    except Exception:
                        logger.exception(
                            "Handler error for event=%s topic=%s",
                            event.event_id,
                            event.topic,
                        )
        return invoked

    def get_history(self, topic: str | None = None, limit: int = 50) -> list[Event]:
        """Retrieve recent events, optionally filtered by topic."""
        events = self._history
        if topic:
            events = [e for e in events if self._matches(topic, e.topic)]
        return events[-limit:]

    @staticmethod
    def _matches(pattern: str, topic: str) -> bool:
        """Simple topic matching with '*' wildcard support."""
        if pattern == "*":
            return True
        pattern_parts = pattern.split(".")
        topic_parts = topic.split(".")
        if len(pattern_parts) != len(topic_parts):
            return False
        return all(
            p == "*" or p == t
            for p, t in zip(pattern_parts, topic_parts)
        )
