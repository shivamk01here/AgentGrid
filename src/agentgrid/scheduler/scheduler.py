"""Task scheduler for periodic and delayed agent tasks."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

TaskFn = Callable[[], Coroutine[Any, Any, None]]


@dataclass
class ScheduledTask:
    """A scheduled task definition."""

    name: str
    handler: TaskFn
    interval_seconds: float = 60.0
    delay_seconds: float = 0.0
    enabled: bool = True
    _last_run: float = 0.0
    _scheduled_at: float = field(default_factory=time.time, repr=False)
    _run_count: int = 0

    @property
    def should_run(self) -> bool:
        if not self.enabled:
            return False
        now = time.time()
        if self._last_run == 0:
            return (now - self._scheduled_at) >= self.delay_seconds
        return (now - self._last_run) >= self.interval_seconds


class Scheduler:
    """Runs periodic tasks in the background.

    Tasks are registered with an interval and the scheduler loop
    checks them each tick.

    Example:
        scheduler = Scheduler()
        scheduler.add_task(ScheduledTask(name="cleanup", handler=cleanup_fn, interval_seconds=300))
        await scheduler.start()
    """

    def __init__(self, tick_interval: float = 1.0) -> None:
        self.tick_interval = tick_interval
        self._tasks: dict[str, ScheduledTask] = {}
        self._running = False
        self._task: asyncio.Task[None] | None = None

    def add_task(self, task: ScheduledTask) -> None:
        """Register a scheduled task."""
        self._tasks[task.name] = task
        logger.info("Scheduled task: %s (every %ss)", task.name, task.interval_seconds)

    def remove_task(self, name: str) -> bool:
        if name in self._tasks:
            del self._tasks[name]
            return True
        return False

    async def start(self) -> None:
        """Start the scheduler loop in the background."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Scheduler started")

    async def stop(self) -> None:
        """Stop the scheduler loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Scheduler stopped")

    async def _loop(self) -> None:
        while self._running:
            for task in self._tasks.values():
                if task.should_run:
                    try:
                        await task.handler()
                        task._last_run = time.time()
                        task._run_count += 1
                    except Exception:
                        logger.exception("Scheduled task '%s' failed", task.name)
            await asyncio.sleep(self.tick_interval)
