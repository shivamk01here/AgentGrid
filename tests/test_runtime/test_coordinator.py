"""Tests for the run coordinator.

The halt-and-resume path is the one that has to be right. A run that sits for
three days waiting on a human and then comes back must execute exactly the
action that was approved, exactly once.
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
from ledgerloop.core.enums import ActionKind, Currency, RiskTier, RunState
from ledgerloop.core.errors import PolicyViolationError, StateTransitionError
from ledgerloop.core.ids import ActionId, IdempotencyKey, TenantId
from ledgerloop.core.models import Action, RunSpec
from ledgerloop.core.money import Money
from ledgerloop.policy import ThresholdPolicyEngine
from ledgerloop.runtime import ActionExecutor, RunCoordinator

AT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
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
def dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture
def gateway() -> InMemoryApprovalGateway:
    directory = ApproverDirectory()
    directory.grant_role(APPROVER, "payments-approver")
    return InMemoryApprovalGateway(directory=directory)


@pytest.fixture
def coordinator(runs, gateway, dispatcher, ledger, clock) -> RunCoordinator:
    executor = ActionExecutor(
        idempotency=InMemoryIdempotencyStore(),
        dispatcher=dispatcher,
        ledger=ledger,
        clock=clock,
    )
    return RunCoordinator(
        runs=runs,
        policy=ThresholdPolicyEngine(),
        approvals=gateway,
        executor=executor,
        ledger=ledger,
        clock=clock,
    )


async def _running_run(runs, tenant):
    run = await runs.create(RunSpec(tenant_id=tenant, objective="Handle exceptions"))
    started = run.start(at=AT)
    return await runs.save(started, expected_version=0)


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


class TestAllow:
    async def test_a_small_refund_executes_immediately(
        self, coordinator, runs, dispatcher, tenant
    ):
        run = await _running_run(runs, tenant)
        result = await coordinator.propose(run, _refund("1000"))

        assert result.executed
        assert not result.halted
        assert result.outcome.succeeded
        assert dispatcher.dispatch_count == 1

    async def test_proposal_and_verdict_are_both_ledgered(
        self, coordinator, runs, ledger, tenant
    ):
        run = await _running_run(runs, tenant)
        await coordinator.propose(run, _refund("1000"))

        events = [e.event_type.value for e in await ledger.read(tenant, run.id)]
        assert "action.proposed" in events
        assert "action.evaluated" in events
        await ledger.verify_chain(tenant, run.id)


class TestDeny:
    async def test_a_denied_action_raises_and_never_dispatches(
        self, coordinator, runs, dispatcher, tenant
    ):
        run = await _running_run(runs, tenant)
        payout = Action(
            id=ActionId.generate(),
            kind=ActionKind.PAYOUT,
            description="Payout",
            amount=Money.from_major("100", Currency.INR),
            idempotency_key=IdempotencyKey.derive("payout", "1"),
        )
        with pytest.raises(PolicyViolationError):
            await coordinator.propose(run, payout)

        assert dispatcher.dispatch_count == 0


class TestHalt:
    async def test_a_large_refund_halts_the_run(
        self, coordinator, runs, dispatcher, tenant
    ):
        run = await _running_run(runs, tenant)
        result = await coordinator.propose(run, _refund("50000"))

        assert result.halted
        assert not result.executed
        assert result.run.state is RunState.AWAITING_APPROVAL
        # Nothing reached the provider while a human is still deciding.
        assert dispatcher.dispatch_count == 0

    async def test_the_halt_is_persisted(self, coordinator, runs, tenant):
        run = await _running_run(runs, tenant)
        result = await coordinator.propose(run, _refund("50000"))

        stored = await runs.get(tenant, run.id)
        assert stored.state is RunState.AWAITING_APPROVAL
        assert stored.pending_approval_id == result.approval_id

    async def test_the_approval_carries_the_risk_tier(
        self, coordinator, runs, gateway, tenant
    ):
        run = await _running_run(runs, tenant)
        result = await coordinator.propose(run, _refund("50000"))

        request = await gateway.get(tenant, result.approval_id)
        assert request.risk_tier is not RiskTier.NONE

    async def test_a_halted_run_cannot_propose_again(self, coordinator, runs, tenant):
        run = await _running_run(runs, tenant)
        result = await coordinator.propose(run, _refund("50000"))

        with pytest.raises(StateTransitionError):
            await coordinator.propose(result.run, _refund("1000"))


class TestResume:
    async def test_resuming_after_a_grant_executes_the_action(
        self, coordinator, runs, gateway, dispatcher, tenant, clock
    ):
        run = await _running_run(runs, tenant)
        action = _refund("50000")
        halted = await coordinator.propose(run, action)

        # Three days pass, then someone signs off.
        await clock.advance(timedelta(days=1))
        await gateway.submit(
            tenant, halted.approval_id, approved=True, actor=APPROVER, at=clock.now()
        )

        result = await coordinator.resume(halted.run, action)
        assert result.executed
        assert result.outcome.succeeded
        assert dispatcher.dispatch_count == 1
        assert result.run.state is RunState.RUNNING

    async def test_resume_reports_the_tier_it_was_approved_at(
        self, coordinator, runs, gateway, tenant, clock
    ):
        run = await _running_run(runs, tenant)
        action = _refund("50000")
        halted = await coordinator.propose(run, action)
        await gateway.submit(
            tenant, halted.approval_id, approved=True, actor=APPROVER, at=clock.now()
        )

        result = await coordinator.resume(halted.run, action)
        assert result.decision.risk_tier is halted.decision.risk_tier

    async def test_resuming_while_still_pending_is_refused(
        self, coordinator, runs, tenant
    ):
        run = await _running_run(runs, tenant)
        action = _refund("50000")
        halted = await coordinator.propose(run, action)

        with pytest.raises(StateTransitionError):
            await coordinator.resume(halted.run, action)

    async def test_a_rejected_approval_fails_the_run(
        self, coordinator, runs, gateway, dispatcher, tenant, clock
    ):
        run = await _running_run(runs, tenant)
        action = _refund("50000")
        halted = await coordinator.propose(run, action)
        await gateway.submit(
            tenant, halted.approval_id, approved=False, actor=APPROVER, at=clock.now()
        )

        with pytest.raises(PolicyViolationError):
            await coordinator.resume(halted.run, action)

        assert dispatcher.dispatch_count == 0
        assert (await runs.get(tenant, run.id)).state is RunState.FAILED

    async def test_a_grant_does_not_cover_a_different_action(
        self, coordinator, runs, gateway, dispatcher, tenant, clock
    ):
        run = await _running_run(runs, tenant)
        approved = _refund("50000")
        halted = await coordinator.propose(run, approved)
        await gateway.submit(
            tenant, halted.approval_id, approved=True, actor=APPROVER, at=clock.now()
        )

        # Someone approved 50,000. Coming back with 5,00,000 must not ride
        # on that signature.
        with pytest.raises(PolicyViolationError, match="does not match"):
            await coordinator.resume(halted.run, _refund("500000"))

        assert dispatcher.dispatch_count == 0

    async def test_resuming_a_run_that_never_halted_is_refused(
        self, coordinator, runs, tenant
    ):
        run = await _running_run(runs, tenant)
        with pytest.raises(StateTransitionError):
            await coordinator.resume(run, _refund("1000"))


class TestEndToEnd:
    async def test_the_whole_chain_verifies_after_a_halt_and_resume(
        self, coordinator, runs, gateway, ledger, tenant, clock
    ):
        run = await _running_run(runs, tenant)
        action = _refund("50000")

        halted = await coordinator.propose(run, action)
        await gateway.submit(
            tenant, halted.approval_id, approved=True, actor=APPROVER, at=clock.now()
        )
        await coordinator.resume(halted.run, action)

        await ledger.verify_chain(tenant, run.id)
        events = [e.event_type.value for e in await ledger.read(tenant, run.id)]
        for expected in (
            "action.proposed",
            "action.evaluated",
            "approval.requested",
            "approval.granted",
            "action.dispatched",
            "action.settled",
        ):
            assert expected in events

    async def test_the_ledger_names_who_approved_it(
        self, coordinator, runs, gateway, ledger, tenant, clock
    ):
        run = await _running_run(runs, tenant)
        action = _refund("50000")
        halted = await coordinator.propose(run, action)
        await gateway.submit(
            tenant, halted.approval_id, approved=True, actor=APPROVER, at=clock.now()
        )
        await coordinator.resume(halted.run, action)

        granted = next(
            e for e in await ledger.read(tenant, run.id) if e.event_type.value == "approval.granted"
        )
        assert granted.payload["decided_by"] == APPROVER
