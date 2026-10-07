"""Tests for reading a run's ledger back as an audit.

The failure that matters most is the audit telling a different story from the
chain: a refund reported as settled that failed, an approval credited to the
wrong person, a total that counts money that never moved - or a report
printed from a ledger somebody edited.
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
from ledgerloop.core.enums import ActionKind, Currency, FailureClass
from ledgerloop.core.errors import (
    BudgetExhaustedError,
    IndeterminateError,
    LedgerIntegrityError,
    PolicyViolationError,
)
from ledgerloop.core.ids import ActionId, IdempotencyKey, RunId, TenantId
from ledgerloop.core.models import Action, RunBudget, RunSpec
from ledgerloop.core.money import Money
from ledgerloop.policy import ThresholdPolicyEngine
from ledgerloop.runtime import ActionExecutor, Compensator, RunCoordinator
from ledgerloop.runtime.audit import ActionOutcome, Auditor, build_audit

AT = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
APPROVER = "priya@example.com"


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
def idempotency() -> InMemoryIdempotencyStore:
    return InMemoryIdempotencyStore()


@pytest.fixture
def dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture
def gateway() -> InMemoryApprovalGateway:
    directory = ApproverDirectory()
    directory.grant_role(APPROVER, "payments-approver")
    return InMemoryApprovalGateway(directory=directory)


@pytest.fixture
def coordinator(runs, gateway, dispatcher, ledger, idempotency, clock) -> RunCoordinator:
    return RunCoordinator(
        runs=runs,
        policy=ThresholdPolicyEngine(),
        approvals=gateway,
        executor=ActionExecutor(
            idempotency=idempotency, dispatcher=dispatcher, ledger=ledger, clock=clock
        ),
        ledger=ledger,
        clock=clock,
    )


@pytest.fixture
def auditor(ledger) -> Auditor:
    return Auditor(ledger=ledger)


async def _started(coordinator, runs, tenant, *, budget: RunBudget | None = None):
    spec = RunSpec(
        tenant_id=tenant,
        objective="Clear the duplicate-charge queue",
        budget=budget or RunBudget(),
    )
    return await coordinator.start(await runs.create(spec))


def _refund(order: str, amount: str) -> Action:
    money = Money.from_major(amount, Currency.INR)
    return Action(
        id=ActionId.generate(),
        kind=ActionKind.REFUND,
        description=f"Duplicate charge on order {order}",
        amount=money,
        counterparty="mer_9f21c",
        idempotency_key=IdempotencyKey.derive("refund", order, str(money.minor_units)),
    )


def _only(audit, outcome: ActionOutcome):
    (record,) = [r for r in audit.actions if r.outcome is outcome]
    return record


class TestASettledAction:
    async def test_it_is_reported_with_its_amount_policy_and_reference(
        self, coordinator, runs, auditor, tenant
    ):
        run = await _started(coordinator, runs, tenant)
        action = _refund("ord_1001", "1200")
        await coordinator.propose(run, action)

        audit = await auditor.report(tenant, run.id)

        (record,) = audit.actions
        assert record.action_id == str(action.id)
        assert record.outcome is ActionOutcome.SETTLED
        assert record.kind == "refund"
        assert record.amount == Money.from_major("1200", Currency.INR)
        assert record.counterparty == "mer_9f21c"
        assert record.decision == "allow"
        assert record.rule_id == "allow-small-refund"
        assert record.provider_reference == "rec_1"

    async def test_what_it_moved_is_totalled(self, coordinator, runs, auditor, tenant):
        run = await _started(coordinator, runs, tenant)
        first = await coordinator.propose(run, _refund("ord_1001", "1200"))
        await coordinator.propose(first.run, _refund("ord_1002", "800"))

        audit = await auditor.report(tenant, run.id)

        assert audit.value_moved == {"INR": 200_000}
        assert audit.in_doubt == ()


class TestApprovals:
    async def test_the_approver_is_credited(
        self, coordinator, runs, gateway, auditor, tenant, clock
    ):
        run = await _started(coordinator, runs, tenant)
        action = _refund("ord_2002", "84000")
        halted = await coordinator.propose(run, action)
        await gateway.submit(
            tenant, halted.approval_id, approved=True, actor=APPROVER, at=clock.now()
        )
        await coordinator.resume(halted.run, action)

        record = _only(await auditor.report(tenant, run.id), ActionOutcome.SETTLED)

        assert record.decision == "require_approval"
        assert record.approved_by == APPROVER
        assert record.approval_id == str(halted.approval_id)

    async def test_a_run_still_waiting_says_so(self, coordinator, runs, auditor, tenant):
        run = await _started(coordinator, runs, tenant)
        halted = await coordinator.propose(run, _refund("ord_2002", "84000"))

        audit = await auditor.report(tenant, run.id)

        record = _only(audit, ActionOutcome.AWAITING_APPROVAL)
        assert record.approval_id == str(halted.approval_id)
        assert record.approved_by is None
        assert not audit.closed

    async def test_a_rejection_is_traced_back_to_its_action(
        self, coordinator, runs, gateway, auditor, tenant, clock
    ):
        # The rejection entry names the approval, not the action. The audit
        # has to find the action through the request that raised it.
        run = await _started(coordinator, runs, tenant)
        action = _refund("ord_2002", "84000")
        halted = await coordinator.propose(run, action)
        await gateway.submit(
            tenant, halted.approval_id, approved=False, actor=APPROVER, at=clock.now()
        )
        with pytest.raises(PolicyViolationError):
            await coordinator.resume(halted.run, action)

        audit = await auditor.report(tenant, run.id)

        record = _only(audit, ActionOutcome.NOT_APPROVED)
        assert record.action_id == str(action.id)
        assert record.detail == "approval rejected"
        assert audit.closing_event == "run.failed"
        assert audit.value_moved == {}


class TestActionsThatMovedNothing:
    async def test_a_denied_action(self, coordinator, runs, dispatcher, auditor, tenant):
        run = await _started(coordinator, runs, tenant)
        payout = Action(
            id=ActionId.generate(),
            kind=ActionKind.PAYOUT,
            description="Payout to a new account",
            amount=Money.from_major("500", Currency.INR),
            idempotency_key=IdempotencyKey.derive("payout", "acc_77"),
        )
        with pytest.raises(PolicyViolationError):
            await coordinator.propose(run, payout)

        audit = await auditor.report(tenant, run.id)

        record = _only(audit, ActionOutcome.DENIED)
        assert record.rule_id == "deny-payout"
        assert audit.value_moved == {}

    async def test_an_action_the_ceiling_stopped(self, coordinator, runs, auditor, tenant):
        budget = RunBudget(max_value_moved=Money.from_major("1000", Currency.INR))
        run = await _started(coordinator, runs, tenant, budget=budget)
        action = _refund("ord_1001", "1200")
        with pytest.raises(BudgetExhaustedError):
            await coordinator.propose(run, action)

        audit = await auditor.report(tenant, run.id)

        # The ceiling's run.failed entry names the action - it is about the
        # run and the action both, and neither may be lost.
        record = _only(audit, ActionOutcome.STOPPED)
        assert record.action_id == str(action.id)
        assert audit.closing_event == "run.failed"
        assert audit.value_moved == {}

    async def test_a_provider_refusal(self, coordinator, runs, dispatcher, auditor, tenant):
        run = await _started(coordinator, runs, tenant)
        dispatcher.fail_next(FailureClass.INVALID_REQUEST)
        await coordinator.propose(run, _refund("ord_3003", "900"))

        audit = await auditor.report(tenant, run.id)

        record = _only(audit, ActionOutcome.FAILED)
        assert "invalid_request" in (record.detail or "")
        assert audit.value_moved == {}

    async def test_a_replay_is_not_a_second_payment(
        self, coordinator, runs, dispatcher, auditor, tenant
    ):
        run = await _started(coordinator, runs, tenant)
        first = await coordinator.propose(run, _refund("ord_1001", "1200"))
        await coordinator.propose(first.run, _refund("ord_1001", "1200"))

        audit = await auditor.report(tenant, run.id)

        assert _only(audit, ActionOutcome.REPLAYED)
        assert _only(audit, ActionOutcome.SETTLED)
        assert audit.value_moved == {"INR": 120_000}


class TestDoubtAndReversal:
    async def test_an_unanswered_dispatch_is_in_doubt_not_settled(
        self, coordinator, runs, dispatcher, auditor, tenant
    ):
        run = await _started(coordinator, runs, tenant)
        dispatcher.fail_next(FailureClass.INDETERMINATE)
        action = _refund("ord_1001", "1200")
        await coordinator.propose(run, action)

        audit = await auditor.report(tenant, run.id)

        assert _only(audit, ActionOutcome.IN_DOUBT).action_id == str(action.id)
        assert audit.in_doubt == (str(action.id),)
        # Counted as moved: it may well have landed, and a total that left it
        # out would be a figure for what we heard about.
        assert audit.value_moved == {"INR": 120_000}

    async def test_a_reversed_action(
        self, coordinator, runs, ledger, dispatcher, idempotency, auditor, tenant, clock
    ):
        run = await _started(coordinator, runs, tenant)
        result = await coordinator.propose(run, _refund("ord_1001", "1200"))
        capture = Action(
            id=ActionId.generate(),
            kind=ActionKind.CAPTURE,
            description="Capture on order ord_9",
            amount=Money.from_major("2500", Currency.INR),
            counterparty="mer_9f21c",
            idempotency_key=IdempotencyKey.derive("capture", "ord_9"),
        )
        await ActionExecutor(
            idempotency=idempotency, dispatcher=dispatcher, ledger=ledger, clock=clock
        ).execute(capture, run_id=run.id, tenant_id=tenant)
        await Compensator(
            runs=runs, ledger=ledger, dispatcher=dispatcher, idempotency=idempotency, clock=clock
        ).compensate(result.run)

        audit = await auditor.report(tenant, run.id)

        reversed_record = _only(audit, ActionOutcome.REVERSED)
        assert reversed_record.action_id == str(capture.id)
        assert reversed_record.detail == "reversed by a refund"
        assert audit.closing_event in {"run.compensated", "run.failed"}


    async def test_a_reversal_nobody_heard_back_from_is_in_doubt_not_failed(
        self, runs, ledger, dispatcher, idempotency, auditor, tenant, clock
    ):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="Undo a capture"))
        run = await runs.save(run.start(at=clock.now()), expected_version=0)
        capture = Action(
            id=ActionId.generate(),
            kind=ActionKind.CAPTURE,
            description="Capture on order ord_9",
            amount=Money.from_major("2500", Currency.INR),
            counterparty="mer_9f21c",
            idempotency_key=IdempotencyKey.derive("capture", "ord_9"),
        )
        await ActionExecutor(
            idempotency=idempotency, dispatcher=dispatcher, ledger=ledger, clock=clock
        ).execute(capture, run_id=run.id, tenant_id=tenant)
        await Compensator(
            runs=runs,
            ledger=ledger,
            dispatcher=_TimingOutDispatcher(),
            idempotency=idempotency,
            clock=clock,
        ).compensate(run)

        record = _only(await auditor.report(tenant, run.id), ActionOutcome.SETTLED)

        # The capture still stands, and whether its refund went out is the
        # open question - not a provider that said no.
        assert record.action_id == str(capture.id)
        assert (record.detail or "").startswith("reversal in doubt")


class TestTheRunAsAWhole:
    async def test_how_the_run_opened_and_closed(
        self, coordinator, runs, auditor, tenant, clock
    ):
        run = await _started(coordinator, runs, tenant)
        result = await coordinator.propose(run, _refund("ord_1001", "1200"))
        await clock.advance(timedelta(minutes=30))
        await coordinator.complete(result.run, summary="Queue cleared")

        audit = await auditor.report(tenant, run.id)

        assert audit.objective == "Clear the duplicate-charge queue"
        assert audit.opened_at == AT
        assert audit.closed_at == AT + timedelta(minutes=30)
        assert audit.closing_event == "run.completed"
        assert audit.closing_reason == "Queue cleared"
        assert audit.verified

    async def test_a_run_with_no_ledger_is_an_empty_audit(self, auditor, tenant):
        audit = await auditor.report(tenant, RunId.generate())

        assert audit.entry_count == 0
        assert audit.actions == ()
        assert not audit.closed


class TestTrust:
    async def test_a_tampered_chain_produces_no_report(
        self, coordinator, runs, ledger, auditor, tenant
    ):
        run = await _started(coordinator, runs, tenant)
        await coordinator.propose(run, _refund("ord_1001", "1200"))
        chain = ledger._chains[(tenant.value, run.id.value)]
        dispatched = next(e for e in chain if e.payload.get("amount_minor") is not None)
        dispatched.payload["amount_minor"] = 1  # make a 1200 refund look like 0.01

        with pytest.raises(LedgerIntegrityError):
            await auditor.report(tenant, run.id)

    async def test_an_unverified_fold_says_it_is_unverified(
        self, coordinator, runs, ledger, tenant
    ):
        run = await _started(coordinator, runs, tenant)
        await coordinator.propose(run, _refund("ord_1001", "1200"))

        audit = build_audit(run.id, tenant, await ledger.read(tenant, run.id))

        assert not audit.verified
        assert "chain NOT verified" in audit.render()


class TestRendering:
    async def test_the_text_tells_the_story(
        self, coordinator, runs, gateway, auditor, tenant, clock
    ):
        run = await _started(coordinator, runs, tenant)
        action = _refund("ord_2002", "84000")
        halted = await coordinator.propose(run, action)
        await gateway.submit(
            tenant, halted.approval_id, approved=True, actor=APPROVER, at=clock.now()
        )
        resumed = await coordinator.resume(halted.run, action)
        await coordinator.complete(resumed.run, summary="Queue cleared")

        text = (await auditor.report(tenant, run.id)).render()

        assert "hash chain verified" in text
        assert "run.completed - Queue cleared" in text
        assert "moved       84000.00 INR" in text
        assert "refund 84000.00 INR to mer_9f21c - settled" in text
        assert f"approved by {APPROVER}" in text


class _TimingOutDispatcher(RecordingDispatcher):
    """Takes a reversal and never answers."""

    async def compensate(self, action, receipt, *, at):
        raise IndeterminateError(
            "Connection dropped after the reversal was sent",
            idempotency_key=action.idempotency_key,
            action_id=action.id,
        )
