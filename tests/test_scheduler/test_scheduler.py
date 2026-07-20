"""Tests for the task scheduler."""

import asyncio
import time
import pytest
from agentgrid.scheduler.scheduler import Scheduler, ScheduledTask


async def noop_task():
    pass


class TestScheduledTask:
    def test_should_run_disabled(self):
        task = ScheduledTask(name="t", handler=noop_task, enabled=False)
        assert task.should_run is False

    def test_should_run_with_delay(self):
        task = ScheduledTask(name="t", handler=noop_task, delay_seconds=10.0)
        assert task.should_run is False

    def test_should_run_after_delay(self):
        task = ScheduledTask(name="t", handler=noop_task, delay_seconds=0.0)
        assert task.should_run is True

    def test_should_run_interval(self):
        task = ScheduledTask(name="t", handler=noop_task, interval_seconds=1.0)
        task._last_run = time.time() - 2.0
        assert task.should_run is True

    def test_should_not_run_too_soon(self):
        task = ScheduledTask(name="t", handler=noop_task, interval_seconds=10.0)
        task._last_run = time.time()
        assert task.should_run is False


class TestScheduler:
    @pytest.mark.asyncio
    async def test_add_and_remove_task(self):
        scheduler = Scheduler()
        task = ScheduledTask(name="t1", handler=noop_task)
        scheduler.add_task(task)
        assert scheduler.remove_task("t1") is True
        assert scheduler.remove_task("nonexistent") is False

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        scheduler = Scheduler(tick_interval=0.05)
        await scheduler.start()
        assert scheduler._running is True
        await scheduler.stop()
        assert scheduler._running is False

    @pytest.mark.asyncio
    async def test_task_executes(self):
        call_count = 0

        async def counter():
            nonlocal call_count
            call_count += 1

        scheduler = Scheduler(tick_interval=0.05)
        scheduler.add_task(ScheduledTask(
            name="count", handler=counter, interval_seconds=0.0
        ))
        await scheduler.start()
        await asyncio.sleep(0.3)
        await scheduler.stop()
        assert call_count >= 1

    @pytest.mark.asyncio
    async def test_task_failure_does_not_crash(self):
        async def bad():
            raise RuntimeError("oops")

        scheduler = Scheduler(tick_interval=0.05)
        scheduler.add_task(ScheduledTask(
            name="bad", handler=bad, interval_seconds=0.0
        ))
        await scheduler.start()
        await asyncio.sleep(0.2)
        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_delayed_task(self):
        ran = False

        async def delayed():
            nonlocal ran
            ran = True

        scheduler = Scheduler(tick_interval=0.05)
        task = ScheduledTask(
            name="delayed", handler=delayed,
            interval_seconds=999.0, delay_seconds=0.1,
        )
        scheduler.add_task(task)
        await scheduler.start()
        await asyncio.sleep(0.5)
        await scheduler.stop()
        assert ran is True
