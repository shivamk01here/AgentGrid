"""Threshold policy engine.

Classifies a proposed action by risk and rules on it. The rules are ordered
and the first match wins, so a tenant can layer specific carve-outs above
broad defaults without the outcome depending on dictionary ordering.

Three invariants hold regardless of configuration:

* An action the model proposes is never trusted to classify itself. Risk is
  computed from the action's kind and amount, both of which are validated
  domain values rather than model output.
* `RiskTier.CRITICAL` never resolves to ALLOW, whether the ALLOW came from a
  rule or from the tenant default. A misconfigured threshold can make the
  system ask for approval too often; it must not be able to make it stop
  asking.
* The backstops only tighten. The ceiling and the CRITICAL guard can turn an
  ALLOW into a request for approval, and nothing can turn a DENY into one.

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
    """Hard ceiling. Any amount at or above this needs at least a human's
    approval, whatever the rules say - the backstop for a misconfigured rule
    set. It only ever tightens: a rule that denies still denies. An amount in
    another currency counts as above it, since there is nothing here to
    convert it with."""

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
        rule = next((r for r in policy.rules if r.matches(action)), None)
        effect = policy.default_effect if rule is None else rule.effect

        # A refusal is final. Everything below this line can only make a
        # decision stricter: a backstop that turned a DENY into a question for
        # a human would be loosening the very policy it exists to protect.
        if effect is PolicyEffect.DENY:
            return self._decide(effect, risk, rule, policy)

        breach = self._ceiling_breach(action, policy)
        if breach is not None:
            risk = max(risk, RiskTier.HIGH)

        # The ceiling outranks a rule that would let a large amount through,
        # and names itself when it does, so the reviewer sees the real reason.
        # A rule that already asks for approval keeps its own approver - that
        # role may well be stricter than the tenant default.
        if breach is not None and (rule is None or rule.effect is PolicyEffect.ALLOW):
            return PolicyDecision(
                effect=PolicyEffect.REQUIRE_APPROVAL,
                risk_tier=risk,
                reason=breach,
                rule_id="ceiling",
                approver_role=policy.default_approver_role,
            )

        # CRITICAL never resolves to ALLOW - not from a rule, and not from a
        # tenant default either.
        if risk is RiskTier.CRITICAL and effect is PolicyEffect.ALLOW:
            effect = PolicyEffect.REQUIRE_APPROVAL

        return self._decide(effect, risk, rule, policy)

    @staticmethod
    def _decide(
        effect: PolicyEffect,
        risk: RiskTier,
        rule: PolicyRule | None,
        policy: ThresholdPolicy,
    ) -> PolicyDecision:
        """The decision a matched rule, or the tenant default, arrived at."""
        if rule is None:
            reason = "No rule matched; falling back to the tenant default"
            rule_id = "default"
            approver_role = policy.default_approver_role
        else:
            reason = rule.reason
            rule_id = rule.rule_id
            approver_role = rule.approver_role or policy.default_approver_role

        return PolicyDecision(
            effect=effect,
            risk_tier=risk,
            reason=reason,
            rule_id=rule_id,
            approver_role=approver_role if effect is PolicyEffect.REQUIRE_APPROVAL else None,
        )

    @staticmethod
    def _ceiling_breach(action: Action, policy: ThresholdPolicy) -> str | None:
        """Why the tenant's hard ceiling wants a human for this amount, if it does.

        An amount in a different currency from the ceiling counts as over it.
        There is no rate in here to compare the two at, and reading "cannot
        tell" as "below" is how a rule set that allows every refund lets a
        large USD one straight through an INR ceiling.
        """
        ceiling = policy.auto_approve_ceiling
        if ceiling is None or action.amount is None:
            return None
        if action.amount.currency is not ceiling.currency:
            return (
                f"{action.amount} cannot be measured against the auto-approval "
                f"ceiling of {ceiling}"
            )
        if action.amount >= ceiling:
            return f"{action.amount} is at or above the auto-approval ceiling of {ceiling}"
        return None

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

