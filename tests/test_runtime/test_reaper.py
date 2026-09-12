"""Tests for the expiry sweep.

The failure this is about is a quiet one. A run halts for a signature,
nobody signs, and it sits in AWAITING_APPROVAL with a live request in a
reviewer's queue long after the case it was about stopped mattering.
"""

from datetime import UTC, datetime, timedelta

import pytest

from ledgerloop.adapters.clock import ManualClock
from ledgerloop.adapters.memory import (
    ApproverDirectory,
    InMemoryApprovalGateway,
    InMemoryIdempotencyStore,
    InMemoryLedgerStore,
    InMemoryRunStore,
    RecordingDispatcher,
)
from ledgerloop.core.enums import (
    ActionKind,
    ApprovalState,
    Currency,
    RunState,
    StopReason,
)
from ledgerloop.core.errors import StateTransitionError
from ledgerloop.core.ids import ActionId, IdempotencyKey, RunId, TenantId
from ledgerloop.core.models import Action, Run, RunSpec
from ledgerloop.core.money import Money
from ledgerloop.policy import ThresholdPolicyEngine
from ledgerloop.runtime import ActionExecutor, RunCoordinator
from ledgerloop.runtime.reaper import Reaper, ReaperReport

AT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
DEADLINE = AT + timedelta(days=3)
APPROVER = "ops@example.com"


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(AT)


@pytest.fixture
def tenant() -> TenantId:
    return TenantId.generate()


@pytest.fixture
def runs(clock) -> InMemoryRunStore:
    return InMemoryRunStore(clock=clock)


@pytest.fixture
def ledger() -> InMemoryLedgerStore:
    return InMemoryLedgerStore()


@pytest.fixture
def gateway() -> InMemoryApprovalGateway:
    directory = ApproverDirectory()
    directory.grant_role(APPROVER, "payments-approver")
    return InMemoryApprovalGateway(directory=directory)


@pytest.fixture
def coordinator(runs, gateway, ledger, clock) -> RunCoordinator:
    return RunCoordinator(
        runs=runs,
        policy=ThresholdPolicyEngine(),
        approvals=gateway,
        executor=ActionExecutor(
            idempotency=InMemoryIdempotencyStore(),
            dispatcher=RecordingDispatcher(),
            ledger=ledger,
            clock=clock,
        ),
        ledger=ledger,
        clock=clock,
    )


@pytest.fixture
def reaper(runs, gateway, ledger, clock) -> Reaper:
    return Reaper(runs=runs, approvals=gateway, ledger=ledger, clock=clock)


def _refund(amount: str) -> Action:
    money = Money.from_major(amount, Currency.INR)
    return Action(
        id=ActionId.generate(),
        kind=ActionKind.REFUND,
        description=f"Refund {amount}",
        amount=money,
        counterparty="mer_1",
        idempotency_key=IdempotencyKey.derive("refund", "ord_1", str(money.minor_units)),
    )


async def _halted(coordinator, runs, tenant, *, deadline=DEADLINE):
    """A run parked on an approval, and the action it stopped on."""
    action = _refund("50000")
    run = await runs.create(
        RunSpec(
            tenant_id=tenant,
            objective="Clear the duplicate-charge queue",
            deadline=deadline,
        )
    )
    run = await runs.save(run.start(at=AT), expected_version=0)
    return await coordinator.propose(run, action), action


class _StaleRunStore:
    """Hands the sweep a run that somebody else has already moved on.

    The real store cannot produce this on demand - find_expired only yields
    runs that are halted right now - but two workers a millisecond apart can,
    and that race is what the version check is there for.
    """

    def __init__(self, inner: InMemoryRunStore, stale: Run) -> None:
        self._inner = inner
        self._stale = stale

    async def find_expired(self, *, as_of: datetime, limit: int = 100):
        yield self._stale

    async def save(self, run: Run, *, expected_version: int) -> Run:
        return await self._inner.save(run, expected_version=expected_version)

    async def get(self, tenant_id: TenantId, run_id: RunId) -> Run:
        return await self._inner.get(tenant_id, run_id)


class TestExpiringAHaltedRun:
    async def test_a_run_past_its_deadline_is_expired(
        self, reaper, coordinator, runs, tenant, clock
    ):
        halted, _ = await _halted(coordinator, runs, tenant)
        await clock.advance(timedelta(days=7))

        report = await reaper.sweep()

        assert report.checked == 1
        assert report.expired == 1
        stored = await runs.get(tenant, halted.run.id)
        assert stored.state is RunState.EXPIRED
        assert stored.stop_reason is StopReason.DEADLINE_EXCEEDED

    async def test_the_request_behind_it_is_retired(
        self, reaper, coordinator, runs, gateway, tenant, clock
    ):
        halted, _ = await _halted(coordinator, runs, tenant)
        await clock.advance(timedelta(days=7))

        report = await reaper.sweep()

        assert report.approvals_retired == 1
        request = await gateway.get(tenant, halted.approval_id)
        assert request.state is ApprovalState.EXPIRED

    async def test_an_expired_run_cannot_be_resumed(
        self, reaper, coordinator, runs, tenant, clock
    ):
        halted, action = await _halted(coordinator, runs, tenant)
        await clock.advance(timedelta(days=7))
        await reaper.sweep()

        stored = await runs.get(tenant, halted.run.id)
        with pytest.raises(StateTransitionError):
            await coordinator.resume(stored, action)

    async def test_the_expiry_lands_in_the_runs_own_chain(
        self, reaper, coordinator, runs, ledger, tenant, clock
    ):
        halted, _ = await _halted(coordinator, runs, tenant)
        await clock.advance(timedelta(days=7))

        await reaper.sweep()

        events = [e.event_type.value for e in await ledger.read(tenant, halted.run.id)]
        assert "run.expired" in events
        assert "approval.expired" in events
        await ledger.verify_chain(tenant, halted.run.id)

    async def test_the_entry_says_what_the_run_was_waiting_on(
        self, reaper, coordinator, runs, ledger, tenant, clock
    ):
        halted, _ = await _halted(coordinator, runs, tenant)
        await clock.advance(timedelta(days=7))

        await reaper.sweep()

        entry = next(
            e
            for e in await ledger.read(tenant, halted.run.id)
            if e.event_type.value == "run.expired"
        )
        assert entry.payload["approval_id"] == str(halted.approval_id)
        assert entry.payload["halted_in"] == RunState.AWAITING_APPROVAL.value


class TestRunsItLeavesAlone:
    async def test_a_run_inside_its_deadline_is_not_touched(
        self, reaper, coordinator, runs, tenant, clock
    ):
        halted, _ = await _halted(coordinator, runs, tenant)
        await clock.advance(timedelta(days=1))

        report = await reaper.sweep()

        assert report.checked == 0
        stored = await runs.get(tenant, halted.run.id)
        assert stored.state is RunState.AWAITING_APPROVAL

    async def test_a_run_with_no_deadline_is_never_swept(
        self, reaper, coordinator, runs, tenant, clock
    ):
        # Expiry is opt-in. A run whose caller set no deadline has not said
        # when it stops caring, and the sweep does not get to decide that.
        halted, _ = await _halted(coordinator, runs, tenant, deadline=None)
        await clock.advance(timedelta(days=400))

        report = await reaper.sweep()

        assert report.checked == 0
        stored = await runs.get(tenant, halted.run.id)
        assert stored.state is RunState.AWAITING_APPROVAL

    async def test_a_run_somebody_else_advanced_first_is_skipped(
        self, coordinator, runs, gateway, ledger, tenant, clock
    ):
        halted, action = await _halted(coordinator, runs, tenant)
        await gateway.submit(
            tenant, halted.approval_id, approved=True, actor=APPROVER, at=clock.now()
        )
        await coordinator.resume(halted.run, action)

        racing = Reaper(
            runs=_StaleRunStore(runs, halted.run),
            approvals=gateway,
            ledger=ledger,
            clock=clock,
        )
        await clock.advance(timedelta(days=7))
        report = await racing.sweep()

        assert report.checked == 1
        assert report.skipped == 1
        assert report.expired == 0
        assert (await runs.get(tenant, halted.run.id)).state is RunState.RUNNING


class TestTheSweep:
    async def test_an_empty_sweep_is_fine(self, reaper):
        report = await reaper.sweep()

        assert report.checked == 0
        assert report.expired == 0

    async def test_it_crosses_tenants(self, reaper, coordinator, runs, clock):
        # Expiry is an operator concern. A per-tenant sweep silently skips
        # whichever tenant nobody remembered to schedule.
        await _halted(coordinator, runs, TenantId.generate())
        await _halted(coordinator, runs, TenantId.generate())
        await clock.advance(timedelta(days=7))

        report = await reaper.sweep()

        assert report.checked == 2
        assert report.expired == 2

    def test_the_report_renders_readably(self):
        report = ReaperReport(checked=3, expired=2, approvals_retired=2, skipped=1)

        assert "checked=3" in str(report)
        assert "expired=2" in str(report)
        assert "skipped=1" in str(report)
