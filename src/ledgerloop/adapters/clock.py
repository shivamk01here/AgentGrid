"""Clock implementations.

`SystemClock` is what production runs on. `ManualClock` is what tests run
on, and it is the reason time is a port at all: a run that halts for a
three-day approval window has to be testable in milliseconds, and a replayed
run has to reconstruct the same timestamps it had the first time.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from datetime import UTC, datetime, timedelta

__all__ = ["ManualClock", "SystemClock"]


class SystemClock:
    """Wall-clock time, always UTC.

    The only place in Ledgerloop permitted to read the real clock.
    """

    __slots__ = ()

    def now(self) -> datetime:
        """Current UTC time, timezone-aware."""
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        """Suspend this task for `seconds`."""
        if seconds > 0:
            await asyncio.sleep(seconds)


class ManualClock:
    """A clock that only moves when told to.

    Sleeping tasks are parked and released by `advance`, so a test can step a
    run through hours of scheduled behaviour without waiting for any of it.
    Waiters are released in deadline order, and each is given the chance to
    run before the next is woken - otherwise a task that sleeps again would
    see a clock that had already jumped past its own wake-up time.

    Example:
        clock = ManualClock(datetime(2026, 1, 1, tzinfo=UTC))
        task = asyncio.create_task(worker(clock))
        await clock.advance(timedelta(hours=3))
    """

    def __init__(self, start: datetime | None = None) -> None:
        moment = start or datetime(2026, 1, 1, tzinfo=UTC)
        if moment.tzinfo is None:
            raise ValueError("ManualClock requires a timezone-aware start time")
        self._now = moment.astimezone(UTC)
        # (deadline, tiebreak, future) - the counter keeps heapq from ever
        # comparing futures, which are not orderable.
        self._waiters: list[tuple[datetime, int, asyncio.Future[None]]] = []
        self._counter = itertools.count()

    def now(self) -> datetime:
        """The current simulated time."""
        return self._now

    async def sleep(self, seconds: float) -> None:
        """Park until the clock is advanced past `seconds` from now."""
        if seconds <= 0:
            return
        deadline = self._now + timedelta(seconds=seconds)
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (deadline, next(self._counter), future))
        await future

    async def advance(self, delta: timedelta) -> None:
        """Move time forward, releasing every waiter whose deadline passed.

        Args:
            delta: How far to move. Must not be negative - time in a ledgered
                system never goes backwards.

        Raises:
            ValueError: `delta` was negative.
        """
        if delta < timedelta(0):
            raise ValueError("ManualClock cannot move backwards")

        target = self._now + delta
        while self._waiters and self._waiters[0][0] <= target:
            deadline, _, future = heapq.heappop(self._waiters)
            # Step the clock to each waiter's deadline rather than jumping
            # straight to the target, so a task that wakes and sleeps again
            # measures its next interval from the right moment.
            self._now = deadline
            if not future.done():
                future.set_result(None)
            # Let the woken task run before deciding what else is due.
            await asyncio.sleep(0)

        self._now = target

    async def advance_seconds(self, seconds: float) -> None:
        """Convenience wrapper over `advance`."""
        await self.advance(timedelta(seconds=seconds))

    def set(self, moment: datetime) -> None:
        """Jump straight to `moment` without releasing waiters.

        For arranging a starting state before a test begins. Prefer `advance`
        once tasks are running.

        Raises:
            ValueError: `moment` is naive or earlier than the current time.
        """
        if moment.tzinfo is None:
            raise ValueError("ManualClock requires a timezone-aware time")
        moment = moment.astimezone(UTC)
        if moment < self._now:
            raise ValueError("ManualClock cannot move backwards")
        self._now = moment

    @property
    def pending_sleepers(self) -> int:
        """How many tasks are parked waiting for time to move."""
        return len(self._waiters)
