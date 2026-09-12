"""Tests for the domain entities.

Covers the three properties the money-safety story depends on: run state
transitions are validated, ledger entries are tamper-evident, and approvals
bind to the exact action that was reviewed.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ledgerloop.core.enums import (
    ActionKind,
    ApprovalState,
    Currency,
    LedgerEventType,
    PolicyEffect,
    RiskTier,
    RunState,
    StepOutcome,
    StopReason,
)
from ledgerloop.core.errors import LedgerIntegrityError, StateTransitionError
from ledgerloop.core.ids import (
    ActionId,
    ApprovalId,
    EntryId,
    IdempotencyKey,
    RunId,
    StepId,
    TenantId,
)
from ledgerloop.core.models import (
    GENESIS_HASH,
    Action,
    ApprovalRequest,
    LedgerEntry,
    PolicyDecision,
    Run,
    RunBudget,
    RunSpec,
    Step,
    TokenSpend,
)
from ledgerloop.core.money import Money

AT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def tenant() -> TenantId:
    return TenantId.generate()


@pytest.fixture
def spec(tenant: TenantId) -> RunSpec:
    return RunSpec(tenant_id=tenant, objective="Reconcile yesterday's batch")


@pytest.fixture
def run(spec: RunSpec) -> Run:
    return Run(id=RunId.generate(), spec=spec, state=RunState.PENDING, created_at=AT)


def _refund(amount: str = "1000") -> Action:
    money = Money.from_major(amount, Currency.INR)
    return Action(
        id=ActionId.generate(),
        kind=ActionKind.REFUND,
        description="Duplicate charge",
        amount=money,
        counterparty="mer_1",
        idempotency_key=IdempotencyKey.derive("refund", "ord_1", str(money.minor_units)),
    )


class TestAction:
    def test_value_moving_action_requires_an_idempotency_key(self):
        with pytest.raises(ValueError, match="idempotency_key"):
            Action(
                id=ActionId.generate(),
                kind=ActionKind.REFUND,
                description="No key",
                amount=Money.from_major("100", Currency.INR),
            )

    def test_value_moving_action_requires_an_amount(self):
        with pytest.raises(ValueError, match="requires an amount"):
            Action(id=ActionId.generate(), kind=ActionKind.PAYOUT, description="No amount")

    def test_value_moving_action_requires_a_positive_amount(self):
        money = Money.from_major("0", Currency.INR)
        with pytest.raises(ValueError, match="positive amount"):
            Action(
                id=ActionId.generate(),
                kind=ActionKind.REFUND,
                description="Zero",
                amount=money,
                idempotency_key=IdempotencyKey.derive("refund", "zero"),
            )

    def test_read_only_action_needs_neither(self):
        action = Action(id=ActionId.generate(), kind=ActionKind.READ, description="Fetch")
        assert action.amount is None

    def test_description_is_required(self):
        with pytest.raises(ValueError, match="description"):
            Action(id=ActionId.generate(), kind=ActionKind.READ, description="")

    def test_fingerprint_is_stable_across_identical_content(self):
        assert _refund().fingerprint() == _refund().fingerprint()

    def test_fingerprint_changes_with_the_amount(self):
        assert _refund("1000").fingerprint() != _refund("2000").fingerprint()

    def test_fingerprint_ignores_the_action_id(self):
        # Two proposals of the same effect are the same effect.
        a, b = _refund(), _refund()
        assert a.id != b.id
        assert a.fingerprint() == b.fingerprint()


class TestActionKindSemantics:
    def test_value_moving_kinds(self):
        assert ActionKind.REFUND.moves_value
        assert ActionKind.PAYOUT.moves_value
        assert not ActionKind.READ.moves_value
        assert not ActionKind.NOTIFY.moves_value

    def test_reversibility(self):
        assert ActionKind.CAPTURE.is_reversible
        assert not ActionKind.PAYOUT.is_reversible
        assert not ActionKind.DISPUTE_RESPONSE.is_reversible

    def test_a_reversal_is_not_reversible_but_is_still_a_reversal(self):
        assert ActionKind.REFUND.is_reversal
        assert ActionKind.RELEASE.is_reversal
        assert not ActionKind.REFUND.is_reversible
        assert not ActionKind.CAPTURE.is_reversal
        assert not ActionKind.PAYOUT.is_reversal

    def test_read_only_kinds(self):
        assert ActionKind.READ.is_read_only
        assert ActionKind.ANNOTATE.is_read_only
        assert not ActionKind.REFUND.is_read_only


class TestRunTransitions:
    def test_start_moves_pending_to_running_and_bumps_version(self, run: Run):
        started = run.start(at=AT)
        assert started.state is RunState.RUNNING
        assert started.version == run.version + 1
        assert started.started_at == AT

    def test_illegal_transition_raises(self, run: Run):
        with pytest.raises(StateTransitionError):
            run.succeed(at=AT)

    def test_terminal_states_accept_nothing(self, run: Run):
        finished = run.start(at=AT).succeed(at=AT)
        assert finished.is_terminal
        with pytest.raises(StateTransitionError):
            finished.cancel(at=AT)

    def test_approval_halt_and_resume(self, run: Run):
        approval = ApprovalId.generate()
        halted = run.start(at=AT).await_approval(approval, at=AT)
        assert halted.state is RunState.AWAITING_APPROVAL
        assert halted.pending_approval_id == approval
        assert halted.stop_reason is StopReason.AWAITING_APPROVAL

        resumed = halted.resume(at=AT)
        assert resumed.state is RunState.RUNNING
        assert resumed.pending_approval_id is None
        assert resumed.stop_reason is None

    def test_compensation_path(self, run: Run):
        compensated = run.start(at=AT).begin_compensation(at=AT).complete_compensation(at=AT)
        assert compensated.state is RunState.COMPENSATED
        assert compensated.is_terminal

    def test_failure_records_a_reason(self, run: Run):
        failed = run.start(at=AT).fail("provider unreachable", at=AT)
        assert failed.state is RunState.FAILED
        assert failed.failure_reason == "provider unreachable"

    def test_record_step_folds_spend_and_count(self, run: Run):
        step = Step(
            id=StepId.generate(),
            run_id=run.id,
            tenant_id=run.tenant_id,
            index=1,
            outcome=StepOutcome.COMPLETED,
            started_at=AT,
            spend=TokenSpend(input_tokens=100, output_tokens=50),
        )
        updated = run.record_step(step)
        assert updated.step_count == 1
        assert updated.spend.input_tokens == 100
        assert updated.spend.total == 150

    def test_record_value_moved_accumulates(self, run: Run):
        first = run.record_value_moved(Money.from_major("100", Currency.INR))
        second = first.record_value_moved(Money.from_major("50", Currency.INR))
        assert second.value_moved == Money.from_major("150", Currency.INR)

    def test_record_value_moved_refuses_a_currency_mix(self, run: Run):
        first = run.record_value_moved(Money.from_major("100", Currency.INR))
        with pytest.raises(ValueError, match="Cannot combine"):
            first.record_value_moved(Money.from_major("50", Currency.USD))


class TestRunStateSemantics:
    def test_terminal_flags(self):
        assert RunState.SUCCEEDED.is_terminal
        assert not RunState.RUNNING.is_terminal

    def test_halted_flags(self):
        assert RunState.AWAITING_APPROVAL.is_halted
        assert RunState.SUSPENDED.is_halted
        assert not RunState.RUNNING.is_halted

    def test_resumable_flags(self):
        assert RunState.PENDING.is_resumable
        assert RunState.AWAITING_APPROVAL.is_resumable
        assert not RunState.SUCCEEDED.is_resumable


class TestRunBudget:
    def test_rejects_non_positive_iterations(self):
        with pytest.raises(ValueError, match="max_iterations"):
            RunBudget(max_iterations=0)

    def test_rejects_negative_cost_ceiling(self):
        with pytest.raises(ValueError, match="max_cost"):
            RunBudget(max_cost=Money(-1, Currency.INR))


class TestLedgerEntry:
    def _entry(self, tenant: TenantId, run_id: RunId) -> LedgerEntry:
        return LedgerEntry(
            id=EntryId.generate(),
            run_id=run_id,
            tenant_id=tenant,
            sequence=0,
            event_type=LedgerEventType.RUN_STARTED,
            occurred_at=AT,
            payload={"objective": "reconcile"},
            previous_hash=GENESIS_HASH,
        ).sealed()

    def test_sealed_entry_verifies(self, tenant: TenantId):
        self._entry(tenant, RunId.generate()).verify()

    def test_unsealed_entry_fails_verification(self, tenant: TenantId):
        entry = LedgerEntry(
            id=EntryId.generate(),
            run_id=RunId.generate(),
            tenant_id=tenant,
            sequence=0,
            event_type=LedgerEventType.RUN_STARTED,
            occurred_at=AT,
        )
        with pytest.raises(LedgerIntegrityError, match="never sealed"):
            entry.verify()

    def test_tampering_with_the_payload_breaks_the_hash(self, tenant: TenantId):
        entry = self._entry(tenant, RunId.generate())
        tampered = replace(entry, payload={"objective": "something else"})
        with pytest.raises(LedgerIntegrityError, match="does not match"):
            tampered.verify()

    def test_tampering_with_the_predecessor_breaks_the_hash(self, tenant: TenantId):
        entry = self._entry(tenant, RunId.generate())
        tampered = replace(entry, previous_hash="f" * 64)
        with pytest.raises(LedgerIntegrityError):
            tampered.verify()

    def test_hash_is_deterministic(self, tenant: TenantId):
        run_id = RunId.generate()
        payload = {"a": 1, "b": 2}
        common = {
            "run_id": run_id,
            "tenant_id": tenant,
            "sequence": 3,
            "event_type": LedgerEventType.ACTION_DISPATCHED,
            "occurred_at": AT,
            "payload": payload,
            "previous_hash": "a" * 64,
        }
        first = LedgerEntry(id=EntryId.generate(), **common)
        second = LedgerEntry(id=EntryId.generate(), **common)
        # The entry id is not part of the digest - content is.
        assert first.compute_hash() == second.compute_hash()

    def test_negative_sequence_rejected(self, tenant: TenantId):
        with pytest.raises(ValueError, match="non-negative"):
            LedgerEntry(
                id=EntryId.generate(),
                run_id=RunId.generate(),
                tenant_id=tenant,
                sequence=-1,
                event_type=LedgerEventType.RUN_STARTED,
                occurred_at=AT,
            )


class TestApprovalRequest:
    def _request(self, tenant: TenantId, action: Action) -> ApprovalRequest:
        return ApprovalRequest(
            id=ApprovalId.generate(),
            run_id=RunId.generate(),
            tenant_id=tenant,
            action=action,
            action_fingerprint=action.fingerprint(),
            reason="Above threshold",
            approver_role="payments-approver",
            state=ApprovalState.PENDING,
            requested_at=AT,
        )

    def test_grant_records_the_decider(self, tenant: TenantId):
        granted = self._request(tenant, _refund()).grant(at=AT, by="ops@example.com")
        assert granted.state is ApprovalState.GRANTED
        assert granted.decided_by == "ops@example.com"
        assert granted.state.is_terminal

    def test_cannot_decide_twice(self, tenant: TenantId):
        granted = self._request(tenant, _refund()).grant(at=AT, by="ops@example.com")
        with pytest.raises(StateTransitionError):
            granted.reject(at=AT, by="other@example.com")

    def test_expiry_is_a_terminal_decision(self, tenant: TenantId):
        expired = self._request(tenant, _refund()).expire(at=AT + timedelta(days=1))
        assert expired.state is ApprovalState.EXPIRED
        with pytest.raises(StateTransitionError):
            expired.grant(at=AT, by="late@example.com")

    def test_grant_authorizes_only_the_exact_action(self, tenant: TenantId):
        action = _refund("1000")
        granted = self._request(tenant, action).grant(at=AT, by="ops@example.com")

        assert granted.authorizes(action)
        # This is the whole point: approving 1,000 must not authorize 5,00,000.
        assert not granted.authorizes(_refund("500000"))

    def test_pending_request_authorizes_nothing(self, tenant: TenantId):
        action = _refund()
        assert not self._request(tenant, action).authorizes(action)


class TestPolicyDecision:
    def test_reason_is_required(self):
        with pytest.raises(ValueError, match="reason"):
            PolicyDecision(effect=PolicyEffect.ALLOW, risk_tier=RiskTier.LOW, reason="")

    def test_approval_decisions_must_name_a_role(self):
        with pytest.raises(ValueError, match="approver_role"):
            PolicyDecision(
                effect=PolicyEffect.REQUIRE_APPROVAL,
                risk_tier=RiskTier.HIGH,
                reason="Large amount",
            )

    def test_risk_tiers_are_ordered(self):
        assert RiskTier.LOW < RiskTier.HIGH
        assert RiskTier.CRITICAL > RiskTier.MEDIUM
        assert max(RiskTier.LOW, RiskTier.HIGH) is RiskTier.HIGH
