"""Tests for the policy engine and the approval gateway.

Together these decide whether money moves without a human. The tests are
written around the ways that decision could be wrong in a customer's favour
and against ours.
"""

from datetime import UTC, datetime, timedelta

import pytest

from ledgerloop.adapters.memory import ApproverDirectory, InMemoryApprovalGateway
from ledgerloop.core.enums import (
    ActionKind,
    ApprovalState,
    Currency,
    PolicyEffect,
    RiskTier,
    RunState,
)
from ledgerloop.core.errors import ConfigurationError, PolicyViolationError, StateTransitionError
from ledgerloop.core.ids import ActionId, ApprovalId, IdempotencyKey, RunId, TenantId
from ledgerloop.core.models import Action, PolicyDecision, Run, RunSpec
from ledgerloop.core.money import Money
from ledgerloop.policy import PolicyRule, ThresholdPolicy, ThresholdPolicyEngine

AT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def tenant() -> TenantId:
    return TenantId.generate()


@pytest.fixture
def run(tenant: TenantId) -> Run:
    return Run(
        id=RunId.generate(),
        spec=RunSpec(tenant_id=tenant, objective="Handle exceptions"),
        state=RunState.RUNNING,
        created_at=AT,
    )


def _action(kind: ActionKind, amount: str | None = None) -> Action:
    money = Money.from_major(amount, Currency.INR) if amount is not None else None
    key = (
        IdempotencyKey.derive(kind.value, "ord_1", amount or "0")
        if kind.moves_value
        else None
    )
    return Action(
        id=ActionId.generate(),
        kind=kind,
        description=f"{kind.value} test action",
        amount=money,
        counterparty="mer_1",
        idempotency_key=key,
    )


class TestClassification:
    def test_read_only_actions_carry_no_risk(self):
        policy = ThresholdPolicy.conservative()
        assert ThresholdPolicyEngine.classify(_action(ActionKind.READ), policy) is RiskTier.NONE

    def test_irreversible_value_movement_is_critical_regardless_of_size(self):
        policy = ThresholdPolicy.conservative()
        tiny_payout = _action(ActionKind.PAYOUT, "1")
        assert ThresholdPolicyEngine.classify(tiny_payout, policy) is RiskTier.CRITICAL

    def test_large_reversible_amounts_rank_high(self):
        policy = ThresholdPolicy.conservative()
        assert (
            ThresholdPolicyEngine.classify(_action(ActionKind.CAPTURE, "30000"), policy)
            is RiskTier.HIGH
        )

    def test_small_reversible_amounts_rank_low(self):
        policy = ThresholdPolicy.conservative()
        assert (
            ThresholdPolicyEngine.classify(_action(ActionKind.CAPTURE, "100"), policy)
            is RiskTier.LOW
        )


class TestEvaluation:
    async def test_read_only_actions_are_allowed(self, run):
        engine = ThresholdPolicyEngine()
        decision = await engine.evaluate(_action(ActionKind.READ), run, at=AT)
        assert decision.effect is PolicyEffect.ALLOW

    async def test_small_refunds_are_allowed(self, run):
        engine = ThresholdPolicyEngine()
        decision = await engine.evaluate(_action(ActionKind.REFUND, "1000"), run, at=AT)
        assert decision.effect is PolicyEffect.ALLOW

    async def test_large_refunds_require_approval(self, run):
        engine = ThresholdPolicyEngine()
        decision = await engine.evaluate(_action(ActionKind.REFUND, "50000"), run, at=AT)
        assert decision.effect is PolicyEffect.REQUIRE_APPROVAL
        assert decision.approver_role

    async def test_payouts_are_denied_by_the_conservative_default(self, run):
        engine = ThresholdPolicyEngine()
        decision = await engine.evaluate(_action(ActionKind.PAYOUT, "100"), run, at=AT)
        assert decision.effect is PolicyEffect.DENY

    async def test_the_ceiling_beats_a_permissive_rule(self, run):
        # A rule that allows everything must not be able to defeat the ceiling.
        policy = ThresholdPolicy(
            rules=(
                PolicyRule(
                    effect=PolicyEffect.ALLOW,
                    reason="Allow all refunds",
                    rule_id="allow-everything",
                    kinds=frozenset({ActionKind.REFUND}),
                ),
            ),
            auto_approve_ceiling=Money.from_major("25000", Currency.INR),
        )
        engine = ThresholdPolicyEngine(default_policy=policy)

        decision = await engine.evaluate(_action(ActionKind.REFUND, "100000"), run, at=AT)
        assert decision.effect is PolicyEffect.REQUIRE_APPROVAL
        assert decision.rule_id == "ceiling"

    async def test_unmatched_actions_fall_back_to_requiring_approval(self, run):
        engine = ThresholdPolicyEngine(default_policy=ThresholdPolicy())
        decision = await engine.evaluate(_action(ActionKind.ADJUSTMENT, "10"), run, at=AT)
        assert decision.effect is PolicyEffect.REQUIRE_APPROVAL
        assert decision.rule_id == "default"

    async def test_per_tenant_policies_override_the_default(self, run, tenant):
        engine = ThresholdPolicyEngine()
        engine.set_policy(
            tenant.value,
            ThresholdPolicy(
                rules=(
                    PolicyRule(
                        effect=PolicyEffect.DENY,
                        reason="This tenant permits nothing",
                        rule_id="deny-all",
                    ),
                )
            ),
        )
        decision = await engine.evaluate(_action(ActionKind.READ), run, at=AT)
        assert decision.effect is PolicyEffect.DENY

    async def test_every_decision_carries_a_reason(self, run):
        engine = ThresholdPolicyEngine()
        for kind, amount in (
            (ActionKind.READ, None),
            (ActionKind.REFUND, "100"),
            (ActionKind.PAYOUT, "100"),
        ):
            decision = await engine.evaluate(_action(kind, amount), run, at=AT)
            assert decision.reason


class TestPolicyRule:
    def test_approval_rules_must_name_an_approver(self):
        with pytest.raises(ValueError, match="approver_role"):
            PolicyRule(
                effect=PolicyEffect.REQUIRE_APPROVAL, reason="Needs review", rule_id="r1"
            )

    def test_bounds_cannot_mix_currencies(self):
        with pytest.raises(ValueError, match="mixes currencies"):
            PolicyRule(
                effect=PolicyEffect.ALLOW,
                reason="Mixed",
                rule_id="r1",
                min_amount=Money.from_major("1", Currency.INR),
                max_amount=Money.from_major("2", Currency.USD),
            )

    def test_an_amount_bounded_rule_does_not_match_an_amountless_action(self):
        rule = PolicyRule(
            effect=PolicyEffect.ALLOW,
            reason="Small",
            rule_id="r1",
            max_amount=Money.from_major("100", Currency.INR),
        )
        assert not rule.matches(_action(ActionKind.READ))

    def test_a_rule_does_not_match_a_different_currency(self):
        rule = PolicyRule(
            effect=PolicyEffect.ALLOW,
            reason="Small INR",
            rule_id="r1",
            max_amount=Money.from_major("100", Currency.INR),
        )
        usd = Action(
            id=ActionId.generate(),
            kind=ActionKind.REFUND,
            description="USD refund",
            amount=Money.from_major("1", Currency.USD),
            idempotency_key=IdempotencyKey.derive("refund", "usd"),
        )
        assert not rule.matches(usd)


class TestApprovalGateway:
    @pytest.fixture
    def directory(self) -> ApproverDirectory:
        directory = ApproverDirectory()
        directory.grant_role("ops@example.com", "payments-approver")
        return directory

    @pytest.fixture
    def gateway(self, directory) -> InMemoryApprovalGateway:
        return InMemoryApprovalGateway(directory=directory)

    @pytest.fixture
    def decision(self) -> PolicyDecision:
        return PolicyDecision(
            effect=PolicyEffect.REQUIRE_APPROVAL,
            risk_tier=RiskTier.HIGH,
            reason="Above the ceiling",
            rule_id="ceiling",
            approver_role="payments-approver",
        )

    async def test_request_is_raised_pending(self, gateway, run, decision):
        action = _action(ActionKind.REFUND, "50000")
        request = await gateway.request(run, action, decision, at=AT)
        assert request.state is ApprovalState.PENDING
        assert request.action_fingerprint == action.fingerprint()

    async def test_cannot_raise_an_approval_for_an_allow_decision(self, gateway, run):
        allow = PolicyDecision(
            effect=PolicyEffect.ALLOW, risk_tier=RiskTier.LOW, reason="Fine"
        )
        with pytest.raises(ConfigurationError):
            await gateway.request(run, _action(ActionKind.READ), allow, at=AT)

    async def test_grant_by_an_authorized_approver(self, gateway, run, decision, tenant):
        request = await gateway.request(run, _action(ActionKind.REFUND, "50000"), decision, at=AT)
        decided = await gateway.submit(
            tenant, request.id, approved=True, actor="ops@example.com", at=AT
        )
        assert decided.state is ApprovalState.GRANTED
        assert decided.decided_by == "ops@example.com"

    async def test_actor_without_the_role_is_refused(self, gateway, run, decision, tenant):
        request = await gateway.request(run, _action(ActionKind.REFUND, "50000"), decision, at=AT)
        with pytest.raises(PolicyViolationError, match="does not hold the role"):
            await gateway.submit(
                tenant, request.id, approved=True, actor="intern@example.com", at=AT
            )

    async def test_counterparty_cannot_approve_their_own_action(
        self, gateway, run, decision, tenant, directory
    ):
        directory.grant_role("mer_1", "payments-approver")
        request = await gateway.request(run, _action(ActionKind.REFUND, "50000"), decision, at=AT)
        with pytest.raises(PolicyViolationError, match="separation-of-duties|counterparty"):
            await gateway.submit(tenant, request.id, approved=True, actor="mer_1", at=AT)

    async def test_cannot_decide_twice(self, gateway, run, decision, tenant):
        request = await gateway.request(run, _action(ActionKind.REFUND, "50000"), decision, at=AT)
        await gateway.submit(tenant, request.id, approved=True, actor="ops@example.com", at=AT)
        with pytest.raises(StateTransitionError):
            await gateway.submit(
                tenant, request.id, approved=False, actor="ops@example.com", at=AT
            )

    async def test_a_late_decision_is_refused_on_submission(self, gateway, run, decision, tenant):
        request = await gateway.request(
            run,
            _action(ActionKind.REFUND, "50000"),
            decision,
            at=AT,
            expires_at=AT + timedelta(hours=1),
        )
        with pytest.raises(StateTransitionError):
            await gateway.submit(
                tenant,
                request.id,
                approved=True,
                actor="ops@example.com",
                at=AT + timedelta(hours=2),
            )
        stored = await gateway.get(tenant, request.id)
        assert stored.state is ApprovalState.EXPIRED

    async def test_unknown_request_raises(self, gateway, tenant):
        with pytest.raises(StateTransitionError):
            await gateway.submit(
                tenant, ApprovalId.generate(), approved=True, actor="ops@example.com", at=AT
            )

    async def test_pending_listing_is_tenant_scoped(self, gateway, run, decision, tenant):
        await gateway.request(run, _action(ActionKind.REFUND, "50000"), decision, at=AT)
        mine = [r async for r in gateway.list_pending(tenant)]
        theirs = [r async for r in gateway.list_pending(TenantId.generate())]
        assert len(mine) == 1
        assert theirs == []

    async def test_expire_overdue_sweeps_only_past_deadlines(self, gateway, run, decision, tenant):
        await gateway.request(
            run,
            _action(ActionKind.REFUND, "50000"),
            decision,
            at=AT,
            expires_at=AT + timedelta(hours=1),
        )
        await gateway.request(
            run,
            _action(ActionKind.REFUND, "60000"),
            decision,
            at=AT,
            expires_at=AT + timedelta(days=7),
        )
        expired = await gateway.expire_overdue(as_of=AT + timedelta(hours=2))
        assert len(expired) == 1

    async def test_a_failing_notifier_does_not_break_the_halt(self, run, decision, directory):
        async def broken(_request):
            raise RuntimeError("webhook down")

        gateway = InMemoryApprovalGateway(directory=directory, notifier=broken)
        request = await gateway.request(run, _action(ActionKind.REFUND, "50000"), decision, at=AT)
        assert request.state is ApprovalState.PENDING
