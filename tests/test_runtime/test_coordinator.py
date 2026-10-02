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
from ledgerloop.core.enums import (
    ActionKind,
    ApprovalState,
    Currency,
    FailureClass,
    LedgerEventType,
    RiskTier,
    RunState,
    StopReason,
)
from ledgerloop.core.errors import (
    ConcurrencyError,
    IndeterminateError,
    PolicyViolationError,
    StateTransitionError,
)
from ledgerloop.core.ids import ActionId, IdempotencyKey, TenantId
from ledgerloop.core.models import Action, RunBudget, RunSpec
from ledgerloop.core.money import Money
from ledgerloop.policy import ThresholdPolicyEngine
from ledgerloop.runtime import ActionExecutor, Reconciler, RunCoordinator

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


class TestTheRunThatComesBack:
    """A caller drives the next proposal off the run in the last result."""

    async def test_the_result_carries_the_run_as_it_now_stands(
        self, coordinator, runs, tenant
    ):
        run = await _running_run(runs, tenant)
        result = await coordinator.propose(run, _refund("1000"))

        stored = await runs.get(tenant, run.id)
        assert result.run.version == stored.version
        assert result.run.value_moved == stored.value_moved

    async def test_a_second_proposal_is_not_a_lost_concurrency_race(
        self, coordinator, runs, dispatcher, tenant
    ):
        # Executing the first one advanced the stored version. Handing back
        # the run from before it ran means the next save is checked against
        # a version nobody holds any more.
        run = await _running_run(runs, tenant)
        first = await coordinator.propose(run, _refund("1000"))

        second = await coordinator.propose(first.run, _refund("2000"))

        assert second.executed
        assert dispatcher.dispatch_count == 2

    async def test_the_resumed_run_comes_back_at_its_stored_version(
        self, coordinator, runs, gateway, tenant, clock
    ):
        run = await _running_run(runs, tenant)
        action = _refund("50000")
        halted = await coordinator.propose(run, action)
        await gateway.submit(
            tenant, halted.approval_id, approved=True, actor=APPROVER, at=clock.now()
        )

        result = await coordinator.resume(halted.run, action)

        stored = await runs.get(tenant, run.id)
        assert result.run.version == stored.version
        assert result.run.value_moved == stored.value_moved


class TestValueMoved:
    """The running total is the last defence when a policy is misconfigured."""

    async def test_an_executed_action_adds_to_the_total(
        self, coordinator, runs, tenant
    ):
        run = await _running_run(runs, tenant)
        result = await coordinator.propose(run, _refund("1000"))

        assert result.run.value_moved == Money.from_major("1000", Currency.INR)

    async def test_a_replayed_action_is_not_counted_twice(
        self, coordinator, runs, dispatcher, tenant
    ):
        # Same effect, proposed twice. The claim replays the first receipt
        # rather than dispatching, and the money only ever left once.
        run = await _running_run(runs, tenant)
        first = await coordinator.propose(run, _refund("1000"))
        again = await coordinator.propose(first.run, _refund("1000"))

        assert again.outcome.replayed
        assert dispatcher.dispatch_count == 1
        assert again.run.value_moved == Money.from_major("1000", Currency.INR)

    async def test_a_hold_is_not_counted_as_value_moved(
        self, coordinator, runs, gateway, dispatcher, tenant, clock
    ):
        # A hold carries an amount and blocks it. Nothing leaves anywhere, so
        # nothing should land in the total that is meant to say what did.
        run = await _running_run(runs, tenant)
        hold = Action(
            id=ActionId.generate(),
            kind=ActionKind.HOLD,
            description="Hold pending chargeback review",
            amount=Money.from_major("1000", Currency.INR),
            counterparty="acc_44",
        )
        halted = await coordinator.propose(run, hold)
        await gateway.submit(
            tenant, halted.approval_id, approved=True, actor=APPROVER, at=clock.now()
        )

        result = await coordinator.resume(halted.run, hold)

        assert result.outcome.succeeded
        assert dispatcher.dispatch_count == 1
        assert result.run.value_moved is None


class TestCancel:
    async def test_cancelling_a_running_run(self, coordinator, runs, tenant):
        run = await _running_run(runs, tenant)

        cancelled = await coordinator.cancel(run)

        assert cancelled.state is RunState.CANCELLED
        stored = await runs.get(tenant, run.id)
        assert stored.state is RunState.CANCELLED

    async def test_the_cancellation_is_ledgered(self, coordinator, runs, ledger, tenant):
        run = await _running_run(runs, tenant)

        await coordinator.cancel(run, reason="Duplicate case")

        entry = next(
            e
            for e in await ledger.read(tenant, run.id)
            if e.event_type.value == "run.cancelled"
        )
        assert entry.payload["reason"] == "Duplicate case"
        await ledger.verify_chain(tenant, run.id)

    async def test_cancelling_a_halted_run_withdraws_its_approval(
        self, coordinator, runs, gateway, tenant
    ):
        run = await _running_run(runs, tenant)
        halted = await coordinator.propose(run, _refund("50000"))

        cancelled = await coordinator.cancel(halted.run)

        assert cancelled.state is RunState.CANCELLED
        request = await gateway.get(tenant, halted.approval_id)
        assert request.state is ApprovalState.WITHDRAWN

    async def test_a_granted_approval_is_not_overwritten_by_a_late_cancel(
        self, coordinator, runs, gateway, tenant, clock
    ):
        # The approval already carries a decision. Cancelling the run after
        # the fact must not rewrite that decision into a withdrawal.
        run = await _running_run(runs, tenant)
        halted = await coordinator.propose(run, _refund("50000"))
        await gateway.submit(
            tenant, halted.approval_id, approved=True, actor=APPROVER, at=clock.now()
        )

        await coordinator.cancel(halted.run)

        request = await gateway.get(tenant, halted.approval_id)
        assert request.state is ApprovalState.GRANTED

    async def test_a_cancelled_run_cannot_be_cancelled_again(self, coordinator, runs, tenant):
        run = await _running_run(runs, tenant)
        cancelled = await coordinator.cancel(run)

        with pytest.raises(StateTransitionError):
            await coordinator.cancel(cancelled)

    async def test_a_withdrawn_approval_cannot_resume_the_run(
        self, coordinator, runs, tenant
    ):
        run = await _running_run(runs, tenant)
        action = _refund("50000")
        halted = await coordinator.propose(run, action)

        cancelled = await coordinator.cancel(halted.run)

        with pytest.raises(StateTransitionError):
            await coordinator.resume(cancelled, action)


class TestHold:
    async def test_suspending_a_running_run(self, coordinator, runs, tenant):
        run = await _running_run(runs, tenant)

        held = await coordinator.suspend(run)

        assert held.state is RunState.SUSPENDED
        assert (await runs.get(tenant, run.id)).state is RunState.SUSPENDED

    async def test_the_hold_is_ledgered_with_its_reason(
        self, coordinator, runs, ledger, tenant
    ):
        run = await _running_run(runs, tenant)

        await coordinator.suspend(run, reason="Provider incident, waiting on their status page")

        entry = next(
            e
            for e in await ledger.read(tenant, run.id)
            if e.event_type.value == "run.suspended"
        )
        assert entry.payload["reason"] == "Provider incident, waiting on their status page"
        await ledger.verify_chain(tenant, run.id)

    async def test_a_hold_with_no_reason_still_says_something(
        self, coordinator, runs, ledger, tenant
    ):
        run = await _running_run(runs, tenant)

        await coordinator.suspend(run)

        entry = next(
            e
            for e in await ledger.read(tenant, run.id)
            if e.event_type.value == "run.suspended"
        )
        assert entry.payload["reason"]

    async def test_a_held_run_cannot_act(
        self, coordinator, runs, dispatcher, ledger, tenant
    ):
        run = await _running_run(runs, tenant)
        held = await coordinator.suspend(run)

        with pytest.raises(StateTransitionError):
            await coordinator.propose(held, _refund("1000"))

        assert dispatcher.dispatch_count == 0
        # Refused before it is even written down as a proposal.
        events = [e.event_type.value for e in await ledger.read(tenant, run.id)]
        assert "action.proposed" not in events

    async def test_lifting_the_hold_returns_the_run_to_running(
        self, coordinator, runs, tenant
    ):
        run = await _running_run(runs, tenant)
        held = await coordinator.suspend(run)

        lifted = await coordinator.lift_hold(held)

        assert lifted.state is RunState.RUNNING
        assert (await runs.get(tenant, run.id)).state is RunState.RUNNING

    async def test_lifting_the_hold_is_ledgered(self, coordinator, runs, ledger, tenant):
        run = await _running_run(runs, tenant)
        held = await coordinator.suspend(run)

        await coordinator.lift_hold(held, reason="Provider is back")

        entry = next(
            e
            for e in await ledger.read(tenant, run.id)
            if e.event_type.value == "run.resumed"
        )
        assert entry.payload["reason"] == "Provider is back"
        assert entry.payload["resumed_from"] == "suspended"
        await ledger.verify_chain(tenant, run.id)

    async def test_a_run_whose_hold_was_lifted_can_act_again(
        self, coordinator, runs, dispatcher, tenant
    ):
        run = await _running_run(runs, tenant)
        held = await coordinator.suspend(run)
        lifted = await coordinator.lift_hold(held)

        result = await coordinator.propose(lifted, _refund("1000"))

        assert result.executed
        assert dispatcher.dispatch_count == 1

    async def test_a_run_that_is_not_held_has_no_hold_to_lift(
        self, coordinator, runs, tenant
    ):
        run = await _running_run(runs, tenant)

        with pytest.raises(StateTransitionError):
            await coordinator.lift_hold(run)

    async def test_lifting_a_hold_never_starts_a_pending_run(
        self, coordinator, runs, tenant
    ):
        # PENDING -> RUNNING is a legal transition, which is exactly why this
        # has to be refused explicitly rather than left to the state machine.
        pending = await runs.create(RunSpec(tenant_id=tenant, objective="Not started"))

        with pytest.raises(StateTransitionError):
            await coordinator.lift_hold(pending)

        assert (await runs.get(tenant, pending.id)).state is RunState.PENDING

    async def test_a_run_waiting_on_approval_cannot_be_suspended(
        self, coordinator, runs, gateway, tenant
    ):
        run = await _running_run(runs, tenant)
        halted = await coordinator.propose(run, _refund("50000"))

        with pytest.raises(StateTransitionError):
            await coordinator.suspend(halted.run)

        request = await gateway.get(tenant, halted.approval_id)
        assert request.state is ApprovalState.PENDING
        assert (await runs.get(tenant, run.id)).state is RunState.AWAITING_APPROVAL

    async def test_a_held_run_is_not_an_approval_resume(self, coordinator, runs, tenant):
        run = await _running_run(runs, tenant)
        held = await coordinator.suspend(run)

        with pytest.raises(StateTransitionError):
            await coordinator.resume(held, _refund("1000"))

    async def test_a_held_run_can_still_be_cancelled(self, coordinator, runs, tenant):
        run = await _running_run(runs, tenant)
        held = await coordinator.suspend(run)

        cancelled = await coordinator.cancel(held)

        assert cancelled.state is RunState.CANCELLED


class TestStart:
    async def test_starting_a_pending_run(self, coordinator, runs, tenant, clock):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="Handle exceptions"))

        running = await coordinator.start(run)

        assert running.state is RunState.RUNNING
        assert running.started_at == clock.now()
        assert (await runs.get(tenant, run.id)).state is RunState.RUNNING

    async def test_the_start_is_the_first_thing_in_the_chain(
        self, coordinator, runs, ledger, tenant
    ):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="Handle exceptions"))

        running = await coordinator.start(run)
        await coordinator.propose(running, _refund("1000"))

        entries = await ledger.read(tenant, run.id)
        assert entries[0].event_type is LedgerEventType.RUN_STARTED
        assert entries[0].payload["objective"] == "Handle exceptions"
        await ledger.verify_chain(tenant, run.id)

    async def test_the_start_records_the_limits_the_run_was_given(
        self, coordinator, runs, ledger, tenant
    ):
        deadline = AT + timedelta(days=2)
        run = await runs.create(
            RunSpec(
                tenant_id=tenant,
                objective="Clear the queue",
                deadline=deadline,
                budget=RunBudget(max_value_moved=Money.from_major("10000", Currency.INR)),
            )
        )

        await coordinator.start(run)

        (entry,) = await ledger.read(tenant, run.id)
        assert entry.payload["deadline"] == deadline.isoformat()
        assert entry.payload["ceiling_minor"] == 1_000_000
        assert entry.payload["currency"] == "INR"

    async def test_a_started_run_can_act(self, coordinator, runs, dispatcher, tenant):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="Handle exceptions"))

        running = await coordinator.start(run)
        result = await coordinator.propose(running, _refund("1000"))

        assert result.executed
        assert dispatcher.dispatch_count == 1

    async def test_a_run_cannot_be_started_twice(self, coordinator, runs, ledger, tenant):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="Handle exceptions"))
        running = await coordinator.start(run)

        with pytest.raises(StateTransitionError):
            await coordinator.start(running)

        assert len(await ledger.read(tenant, run.id)) == 1

    async def test_a_held_run_is_not_started_again(self, coordinator, runs, tenant):
        run = await _running_run(runs, tenant)
        held = await coordinator.suspend(run)

        with pytest.raises(StateTransitionError):
            await coordinator.start(held)

        assert (await runs.get(tenant, run.id)).state is RunState.SUSPENDED

    async def test_starting_is_not_a_way_round_an_approval(
        self, coordinator, runs, dispatcher, tenant
    ):
        run = await _running_run(runs, tenant)
        halted = await coordinator.propose(run, _refund("84000"))

        with pytest.raises(StateTransitionError):
            await coordinator.start(halted.run)

        stored = await runs.get(tenant, run.id)
        assert stored.state is RunState.AWAITING_APPROVAL
        assert stored.pending_approval_id == halted.approval_id
        assert dispatcher.dispatch_count == 0

    async def test_two_workers_starting_the_same_run(self, coordinator, runs, tenant):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="Handle exceptions"))
        await coordinator.start(run)

        # The second worker still holds the PENDING copy it read.
        with pytest.raises(ConcurrencyError):
            await coordinator.start(run)


class _NeverSawIt:
    """A provider with no record of whatever it is asked about."""

    async def lookup(self, key, tenant_id):
        return None


class TestComplete:
    async def test_completing_a_running_run(self, coordinator, runs, tenant, clock):
        run = await _running_run(runs, tenant)

        done = await coordinator.complete(run)

        assert done.state is RunState.SUCCEEDED
        assert done.stop_reason is StopReason.COMPLETED
        assert done.ended_at == clock.now()
        assert (await runs.get(tenant, run.id)).state is RunState.SUCCEEDED

    async def test_the_completion_closes_the_chain(self, coordinator, runs, ledger, tenant):
        run = await _running_run(runs, tenant)
        result = await coordinator.propose(run, _refund("1000"))

        await coordinator.complete(result.run, summary="One duplicate charge refunded")

        entry = (await ledger.read(tenant, run.id))[-1]
        assert entry.event_type is LedgerEventType.RUN_COMPLETED
        assert entry.payload["summary"] == "One duplicate charge refunded"
        assert entry.payload["value_moved_minor"] == 100_000
        assert entry.payload["currency"] == "INR"
        assert entry.payload["effects_standing"] == 1
        await ledger.verify_chain(tenant, run.id)

    async def test_a_run_that_moved_nothing_says_so(self, coordinator, runs, ledger, tenant):
        run = await _running_run(runs, tenant)

        await coordinator.complete(run)

        entry = (await ledger.read(tenant, run.id))[-1]
        assert entry.payload["value_moved_minor"] is None
        assert entry.payload["effects_standing"] == 0

    async def test_a_completed_run_cannot_act(self, coordinator, runs, dispatcher, tenant):
        run = await _running_run(runs, tenant)
        done = await coordinator.complete(run)

        with pytest.raises(StateTransitionError):
            await coordinator.propose(done, _refund("1000"))

        assert dispatcher.dispatch_count == 0

    async def test_a_run_that_never_started_cannot_be_completed(
        self, coordinator, runs, tenant
    ):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="Handle exceptions"))

        with pytest.raises(StateTransitionError):
            await coordinator.complete(run)

    async def test_a_run_waiting_on_a_human_cannot_be_completed(
        self, coordinator, runs, gateway, tenant
    ):
        run = await _running_run(runs, tenant)
        halted = await coordinator.propose(run, _refund("84000"))

        with pytest.raises(StateTransitionError):
            await coordinator.complete(halted.run)

        # The question is still open, and still somebody's to answer.
        request = await gateway.get(tenant, halted.approval_id)
        assert request.state is ApprovalState.PENDING

    async def test_a_run_with_an_effect_in_doubt_is_not_a_success(
        self, coordinator, runs, dispatcher, ledger, tenant
    ):
        run = await _running_run(runs, tenant)
        dispatcher.fail_next(FailureClass.INDETERMINATE)
        result = await coordinator.propose(run, _refund("1000"))
        assert result.outcome.indeterminate

        with pytest.raises(IndeterminateError):
            await coordinator.complete(result.run)

        # Still RUNNING, and the chain has no entry claiming otherwise.
        assert (await runs.get(tenant, run.id)).state is RunState.RUNNING
        events = [e.event_type for e in await ledger.read(tenant, run.id)]
        assert LedgerEventType.RUN_COMPLETED not in events

    async def test_the_refusal_names_the_effect_that_is_in_doubt(
        self, coordinator, runs, dispatcher, tenant
    ):
        run = await _running_run(runs, tenant)
        dispatcher.fail_next(FailureClass.INDETERMINATE)
        refund = _refund("1000")
        result = await coordinator.propose(run, refund)

        with pytest.raises(IndeterminateError) as caught:
            await coordinator.complete(result.run)

        assert caught.value.action_id == refund.id
        assert caught.value.idempotency_key == refund.idempotency_key

    async def test_it_completes_once_the_doubt_is_reconciled(
        self, coordinator, runs, dispatcher, ledger, clock, tenant
    ):
        run = await _running_run(runs, tenant)
        dispatcher.fail_next(FailureClass.INDETERMINATE)
        result = await coordinator.propose(run, _refund("1000"))

        await clock.advance(timedelta(hours=1))
        report = await Reconciler(
            idempotency=coordinator._executor._idempotency,
            lookup=_NeverSawIt(),
            ledger=ledger,
            clock=clock,
        ).sweep()
        assert report.not_found == 1

        done = await coordinator.complete(result.run)

        assert done.state is RunState.SUCCEEDED

    async def test_a_definite_failure_does_not_block_completion(
        self, coordinator, runs, dispatcher, tenant
    ):
        # The provider said no. That is an answer, and a run can finish
        # having been told no about one of the things it tried.
        run = await _running_run(runs, tenant)
        dispatcher.fail_next(FailureClass.INVALID_REQUEST)
        result = await coordinator.propose(run, _refund("1000"))
        assert not result.outcome.succeeded

        done = await coordinator.complete(result.run)

        assert done.state is RunState.SUCCEEDED

    async def test_a_run_cannot_be_completed_twice(self, coordinator, runs, ledger, tenant):
        run = await _running_run(runs, tenant)
        done = await coordinator.complete(run)

        with pytest.raises(StateTransitionError):
            await coordinator.complete(done)

        completions = [
            e
            for e in await ledger.read(tenant, run.id)
            if e.event_type is LedgerEventType.RUN_COMPLETED
        ]
        assert len(completions) == 1


class TestTheWholeLifeOfARun:
    async def test_start_to_finish_reads_as_one_verified_chain(
        self, coordinator, runs, ledger, tenant
    ):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="Handle exceptions"))

        running = await coordinator.start(run)
        result = await coordinator.propose(running, _refund("1000"))
        await coordinator.complete(result.run)

        events = [e.event_type.value for e in await ledger.read(tenant, run.id)]
        assert events == [
            "run.started",
            "action.proposed",
            "action.evaluated",
            "action.dispatched",
            "action.settled",
            "run.completed",
        ]
        await ledger.verify_chain(tenant, run.id)
