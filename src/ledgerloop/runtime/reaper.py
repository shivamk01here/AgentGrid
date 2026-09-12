"""Runs nobody ever came back to.

A run halted on a policy gate is safe. Nothing was dispatched, and the grant
it is waiting on binds to one action fingerprint, so no amount of waiting
turns it into something dangerous. What it is not is free: it holds a
request in a reviewer's queue, it counts towards whatever a dashboard says
is outstanding, and the deadline the caller set - because the case stops
mattering after Friday - goes by with nothing watching it.

This is the sweep that watches. It reads the runs whose deadline passed
while they were still halted, moves each one to EXPIRED, and retires the
approval behind it so nobody signs off on a case that is already over.

The order is the guarantee:

    find the halted -> expire the run -> retire its approval

The run moves first, under its own version check. A worker that resumed it a
moment ago wins that check and the sweep leaves both the run and its
approval alone - which is the outcome you want, because the other order
retires a live grant and fails a run that was about to succeed.

Nothing here reverses anything. A run that halted may well have moved money
before it did, and walking that back is the compensator's job, not this
one's. Expiring a run says the decision is never coming; it does not say the
world is unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ledgerloop.core.enums import LedgerEventType
from ledgerloop.core.errors import ConcurrencyError, LedgerloopError, StateTransitionError

if TYPE_CHECKING:
    from datetime import datetime

    from ledgerloop.core.ids import ApprovalId
    from ledgerloop.core.models import Run
    from ledgerloop.core.ports import ApprovalGateway, Clock, LedgerStore, RunStore

logger = logging.getLogger(__name__)

__all__ = ["Reaper", "ReaperReport"]


@dataclass(frozen=True, slots=True)
class ReaperReport:
    """What one expiry sweep found, and what it did about it."""

    checked: int = 0
    """Halted runs past their deadline that the sweep looked at."""
    expired: int = 0
    """Runs moved to EXPIRED."""
    approvals_retired: int = 0
    """Pending requests taken out of a reviewer's queue behind them."""
    skipped: int = 0
    """Runs that moved on their own between the query and the write. Somebody
    else got there first, which is exactly what the version check is for."""

    def __str__(self) -> str:
        return (
            f"checked={self.checked} expired={self.expired} "
            f"approvals_retired={self.approvals_retired} skipped={self.skipped}"
        )


class Reaper:
    """Expires halted runs whose deadline has gone by.

    Run it on a schedule, next to the reconciler. It is cross-tenant by
    design - a deadline is an operator concern, and a per-tenant sweep
    quietly skips the tenant nobody remembered to schedule.

    Example:
        reaper = Reaper(
            runs=run_store, approvals=gateway, ledger=ledger, clock=clock,
        )
        report = await reaper.sweep()
    """

    def __init__(
        self,
        *,
        runs: RunStore,
        approvals: ApprovalGateway,
        ledger: LedgerStore,
        clock: Clock,
    ) -> None:
        self._runs = runs
        self._approvals = approvals
        self._ledger = ledger
        self._clock = clock

    async def sweep(self, *, limit: int = 100) -> ReaperReport:
        """Expire every halted run whose deadline has passed.

        Args:
            limit: Most runs to take in one pass. Whatever this pass does not
                reach, the next one does - the sweep is safe to repeat.

        Returns:
            What it found. `expired` is the number worth looking at: each one
            is a decision somebody was asked for and never gave.
        """
        now = self._clock.now()
        report = ReaperReport()

        async for run in self._runs.find_expired(as_of=now, limit=limit):
            report = replace(report, checked=report.checked + 1)
            report = await self._expire(run, report, at=now)

        if report.checked:
            logger.info("Expiry sweep complete: %s", report)
        return report

    async def _expire(self, run: Run, report: ReaperReport, *, at: datetime) -> ReaperReport:
        """End one run and retire whatever it was waiting on."""
        if not run.state.is_halted:
            # It is running again, or already terminal. Either way it is not
            # this sweep's to end.
            return replace(report, skipped=report.skipped + 1)

        waiting_on = run.pending_approval_id
        try:
            ended = await self._save(run.expire(at=at))
        except (ConcurrencyError, StateTransitionError):
            logger.info("Run %s moved on before the sweep reached it", run.id)
            return replace(report, skipped=report.skipped + 1)

        await self._write(
            ended,
            LedgerEventType.RUN_EXPIRED,
            {
                "halted_in": run.state.value,
                "deadline": None
                if run.spec.deadline is None
                else run.spec.deadline.isoformat(),
                "approval_id": None if waiting_on is None else str(waiting_on),
            },
        )

        retired = await self._retire(ended, waiting_on, at=at)
        return replace(
            report,
            expired=report.expired + 1,
            approvals_retired=report.approvals_retired + int(retired),
        )

    async def _retire(self, run: Run, approval_id: ApprovalId | None, *, at: datetime) -> bool:
        """Take the run's pending request out of the queue behind it.

        Returns:
            True when there was one to retire. A run halted for a
            non-approval reason has nothing here, and neither does one whose
            reviewer decided just inside the deadline.
        """
        if approval_id is None:
            return False

        try:
            retired = await self._approvals.expire(run.tenant_id, approval_id, at=at)
        except LedgerloopError:
            # The run is already EXPIRED and cannot resume, so a request left
            # standing here is stale paperwork rather than a way back in.
            logger.exception("Could not retire approval %s on run %s", approval_id, run.id)
            return False

        if retired is None:
            return False

        await self._write(
            run,
            LedgerEventType.APPROVAL_EXPIRED,
            {"approval_id": str(approval_id), "state": retired.state.value},
        )
        return True

    async def _save(self, run: Run) -> Run:
        """Persist a transitioned run at its new version."""
        return await self._runs.save(run, expected_version=run.version - 1)

    async def _write(
        self, run: Run, event: LedgerEventType, payload: dict[str, object]
    ) -> None:
        """Append to the run's audit chain, never failing the sweep."""
        try:
            await self._ledger.append(
                run.id, run.tenant_id, event, dict(payload), occurred_at=self._clock.now()
            )
        except Exception:
            logger.exception("Ledger write failed for %s on run %s", event.value, run.id)
