"""Threshold policy engine.

Classifies a proposed action by risk and rules on it. The rules are ordered
and the first match wins, so a tenant can layer specific carve-outs above
broad defaults without the outcome depending on dictionary ordering.

Two invariants hold regardless of configuration:

* An action the model proposes is never trusted to classify itself. Risk is
  computed from the action's kind and amount, both of which are validated
  domain values rather than model output.
* `ActionKind.CRITICAL` risk never resolves to ALLOW. A misconfigured
  threshold can make the system ask for approval too often; it must not be
  able to make it stop asking.

The engine is pure and deterministic. The same action under the same
configuration always produces the same decision, which is what makes replay
and audit meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ledgerloop.core.enums import ActionKind, Currency, PolicyEffect, RiskTier
from ledgerloop.core.models import Action, PolicyDecision
from ledgerloop.core.money import Money

if TYPE_CHECKING:
    from datetime import datetime

    from ledgerloop.core.models import Run

__all__ = ["PolicyRule", "ThresholdPolicy", "ThresholdPolicyEngine"]


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """One ordered rule.

    A rule matches when every condition it specifies is satisfied. Omitted
    conditions match anything, so a rule with no conditions is a catch-all
    and belongs last.
    """

    effect: PolicyEffect
    reason: str
    rule_id: str
    kinds: frozenset[ActionKind] | None = None
    """Action kinds this rule applies to. None matches every kind."""
    min_amount: Money | None = None
    """Inclusive lower bound. The rule matches at or above this amount."""
    max_amount: Money | None = None
    """Exclusive upper bound. The rule matches strictly below this amount."""
    approver_role: str | None = None
    """Required for REQUIRE_APPROVAL rules."""

    def __post_init__(self) -> None:
        if self.effect is PolicyEffect.REQUIRE_APPROVAL and not self.approver_role:
            raise ValueError(
                f"Rule {self.rule_id!r} requires approval but names no approver_role"
            )
        if (
            self.min_amount is not None
            and self.max_amount is not None
            and self.min_amount.currency is not self.max_amount.currency
        ):
            raise ValueError(f"Rule {self.rule_id!r} mixes currencies in its bounds")

    def matches(self, action: Action) -> bool:
        """True when this rule applies to `action`."""
        if self.kinds is not None and action.kind not in self.kinds:
            return False

        bound = self.min_amount or self.max_amount
        if bound is None:
            return True

        # An amount bound cannot be evaluated against an action that has no
        # amount, and a rule about money must not silently catch one.
        if action.amount is None:
            return False
        if action.amount.currency is not bound.currency:
            return False

        if self.min_amount is not None and action.amount < self.min_amount:
            return False
        return not (self.max_amount is not None and action.amount >= self.max_amount)


@dataclass(frozen=True, slots=True)
class ThresholdPolicy:
    """A tenant's rule set.

    `rules` are evaluated in order. `default_effect` applies when none match,
    and defaults to requiring approval - an action nobody wrote a rule for is
    an action nobody has thought about.
    """

    rules: tuple[PolicyRule, ...] = ()
    default_effect: PolicyEffect = PolicyEffect.REQUIRE_APPROVAL
    default_approver_role: str = "payments-approver"
    auto_approve_ceiling: Money | None = None
    """Hard ceiling. Any amount at or above this requires approval no matter
    what the rules say - the backstop for a misconfigured rule set."""

    def __post_init__(self) -> None:
        if self.default_effect is PolicyEffect.REQUIRE_APPROVAL and not self.default_approver_role:
            raise ValueError("default_approver_role is required")

    @classmethod
    def conservative(cls, currency: Currency = Currency.INR) -> ThresholdPolicy:
        """A sane starting configuration.

        Reads are free, annotations are free, small reversible refunds go
        through, everything that moves real money asks a human. Tenants are
        expected to loosen this deliberately rather than tighten it later.
        """
        return cls(
            rules=(
                PolicyRule(
                    effect=PolicyEffect.ALLOW,
                    reason="Read-only action with no external effect",
                    rule_id="allow-read-only",
                    kinds=frozenset({ActionKind.READ, ActionKind.ANNOTATE}),
                ),
                PolicyRule(
                    effect=PolicyEffect.ALLOW,
                    reason="Small refund below the review threshold",
                    rule_id="allow-small-refund",
                    kinds=frozenset({ActionKind.REFUND}),
                    max_amount=Money.from_major("5000", currency),
                ),
                PolicyRule(
                    effect=PolicyEffect.DENY,
                    reason="Payouts are not eligible for agent execution",
                    rule_id="deny-payout",
                    kinds=frozenset({ActionKind.PAYOUT}),
                ),
            ),
            auto_approve_ceiling=Money.from_major("25000", currency),
        )


class ThresholdPolicyEngine:
    """Evaluates proposed actions against a per-tenant rule set.

    Example:
        engine = ThresholdPolicyEngine(default_policy=ThresholdPolicy.conservative())
        decision = await engine.evaluate(action, run, at=clock.now())
        if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
            ...
    """

    def __init__(
        self,
        *,
        default_policy: ThresholdPolicy | None = None,
        per_tenant: dict[str, ThresholdPolicy] | None = None,
    ) -> None:
        self._default = default_policy or ThresholdPolicy.conservative()
        self._per_tenant = dict(per_tenant or {})

    def set_policy(self, tenant: str, policy: ThresholdPolicy) -> None:
        """Install or replace a tenant's rule set."""
        self._per_tenant[tenant] = policy

    async def evaluate(self, action: Action, run: Run, *, at: datetime) -> PolicyDecision:
        """Classify and rule on one proposed action.

        Args:
            action: The proposal. Never trusted to classify itself.
            run: The run proposing it, for tenant lookup and cumulative
                value checks.
            at: Evaluation time. Unused today, but part of the port so that
                time-of-day and velocity rules do not change the signature.

        Returns:
            The decision, always carrying a reason.
        """
        del at  # reserved for velocity and time-window rules

        policy = self._per_tenant.get(run.tenant_id.value, self._default)
        risk = self.classify(action, policy)

        # The backstop runs before the rules, not after: a rule that would
        # allow a large amount must not be able to override the ceiling.
        if (
            policy.auto_approve_ceiling is not None
            and action.amount is not None
            and action.amount.currency is policy.auto_approve_ceiling.currency
            and action.amount >= policy.auto_approve_ceiling
        ):
            return PolicyDecision(
                effect=PolicyEffect.REQUIRE_APPROVAL,
                risk_tier=max(risk, RiskTier.HIGH),
                reason=(
                    f"{action.amount} is at or above the auto-approval ceiling "
                    f"of {policy.auto_approve_ceiling}"
                ),
                rule_id="ceiling",
                approver_role=policy.default_approver_role,
            )

        for rule in policy.rules:
            if not rule.matches(action):
                continue
            effect = rule.effect
            # CRITICAL never resolves to ALLOW, whatever a rule says.
            if risk is RiskTier.CRITICAL and effect is PolicyEffect.ALLOW:
                effect = PolicyEffect.REQUIRE_APPROVAL
            return PolicyDecision(
                effect=effect,
                risk_tier=risk,
                reason=rule.reason,
                rule_id=rule.rule_id,
                approver_role=(
                    rule.approver_role or policy.default_approver_role
                    if effect is PolicyEffect.REQUIRE_APPROVAL
                    else None
                ),
            )

        return PolicyDecision(
            effect=policy.default_effect,
            risk_tier=risk,
            reason="No rule matched; falling back to the tenant default",
            rule_id="default",
            approver_role=(
                policy.default_approver_role
                if policy.default_effect is PolicyEffect.REQUIRE_APPROVAL
                else None
            ),
        )

    @staticmethod
    def classify(action: Action, policy: ThresholdPolicy) -> RiskTier:
        """Assign a risk tier from the action's own properties.

        Computed, never taken from the model. Value movement nothing can
        walk back is CRITICAL regardless of size - a payout is gone the
        moment it lands. A reversal is the exception: nothing undoes a refund
        either, but a refund is what undoing looks like, and a tier that by
        definition never auto-executes would put the remedy further out of
        reach than the mistake.
        """
        if action.kind.is_read_only:
            return RiskTier.NONE

        if (
            action.kind.moves_value
            and not action.kind.is_reversible
            and not action.kind.is_reversal
        ):
            return RiskTier.CRITICAL

        if not action.kind.moves_value:
            # Irreversible but moves no value - filing dispute evidence, or
            # sending an outbound message.
            return RiskTier.MEDIUM if not action.kind.is_reversible else RiskTier.LOW

        ceiling = policy.auto_approve_ceiling
        if action.amount is None or ceiling is None:
            return RiskTier.MEDIUM
        if action.amount.currency is not ceiling.currency:
            return RiskTier.HIGH

        # Graduated against the tenant's own ceiling rather than a hardcoded
        # figure, so the tiers mean the same thing in every currency.
        if action.amount >= ceiling:
            return RiskTier.HIGH
        if action.amount.minor_units * 2 >= ceiling.minor_units:
            return RiskTier.MEDIUM
        return RiskTier.LOW

