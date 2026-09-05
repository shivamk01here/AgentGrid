"""Domain entities.

Every model here is frozen. State changes produce a new instance through an
explicit transition method that validates the move, so an illegal state is
unrepresentable rather than merely discouraged. Aggregates carry a `version`
for optimistic concurrency - stores compare it on write and raise
`ConcurrencyError` rather than clobbering another worker's progress.

Nothing in this module performs I/O, imports a vendor SDK, or reads a clock.
Timestamps arrive as arguments so that a replayed run reconstructs exactly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Self

from ledgerloop.core.enums import (
    ActionKind,
    ApprovalState,
    Effort,
    IdempotencyState,
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
    CorrelationId,
    EntryId,
    IdempotencyKey,
    RunId,
    StepId,
    TenantId,
)
from ledgerloop.core.money import Money

__all__ = [
    "Action",
    "ActionReceipt",
    "ApprovalRequest",
    "IdempotencyRecord",
    "LedgerEntry",
    "PolicyDecision",
    "Run",
    "RunBudget",
    "RunSpec",
    "Step",
    "TokenSpend",
]

GENESIS_HASH = "0" * 64
"""Predecessor hash of the first entry in every run's chain."""


# ---------------------------------------------------------------------------
# Budgets and spend
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenSpend:
    """Tokens consumed by a run, split by billing category."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: TokenSpend) -> TokenSpend:
        return TokenSpend(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )

    @property
    def total(self) -> int:
        """Every token that crossed the wire, cached or not."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass(frozen=True, slots=True)
class RunBudget:
    """Hard ceilings on what a single run may consume.

    Exceeding any of these stops the run rather than throttling it: an agent
    that has burned its budget is usually stuck, and letting it continue
    turns a bounded cost into an unbounded one.
    """

    max_iterations: int = 10
    max_tokens: int | None = None
    max_cost: Money | None = None
    max_value_moved: Money | None = None
    """Ceiling on the total value this run may move, across all actions.
    The last line of defence when policy rules are misconfigured."""

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("max_tokens must be at least 1 when set")
        if self.max_cost is not None and self.max_cost.is_negative:
            raise ValueError("max_cost cannot be negative")
        if self.max_value_moved is not None and self.max_value_moved.is_negative:
            raise ValueError("max_value_moved cannot be negative")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Action:
    """A proposed effect on the outside world.

    An `Action` is a *proposal* until policy has evaluated it and the
    dispatcher has executed it. Constructing one is always safe; only
    dispatch has consequences.

    `idempotency_key` is required for any value-moving kind and must be
    derived from this action's content - see `IdempotencyKey.derive`.
    """

    id: ActionId
    kind: ActionKind
    description: str
    amount: Money | None = None
    counterparty: str | None = None
    """Opaque reference to the other side - a merchant, payer, or account.
    Never a full instrument number."""
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: IdempotencyKey | None = None
    proposed_by_step: StepId | None = None

    def __post_init__(self) -> None:
        if not self.description:
            raise ValueError("Action.description is required - it appears in approval UIs")
        if self.kind.moves_value:
            if self.amount is None:
                raise ValueError(f"Action of kind {self.kind.value} requires an amount")
            if self.idempotency_key is None:
                raise ValueError(
                    f"Action of kind {self.kind.value} requires an idempotency_key. "
                    "Derive it from the action's content with IdempotencyKey.derive()."
                )
            if not self.amount.is_positive:
                raise ValueError(
                    f"Action of kind {self.kind.value} requires a positive amount, "
                    f"got {self.amount}"
                )

    def fingerprint(self) -> str:
        """Stable digest of what this action *does*.

        Two actions with the same fingerprint are the same effect. Used to
        detect an idempotency key reused for different content.
        """
        material = json.dumps(
            {
                "kind": self.kind.value,
                "amount": None if self.amount is None else self.amount.minor_units,
                "currency": None if self.amount is None else self.amount.currency.value,
                "counterparty": self.counterparty,
                "payload": self.payload,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The policy engine's verdict on one proposed action."""

    effect: PolicyEffect
    risk_tier: RiskTier
    reason: str
    rule_id: str | None = None
    approver_role: str | None = None
    """Role required to approve, when `effect` is REQUIRE_APPROVAL."""

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("PolicyDecision.reason is required - it is shown to reviewers")
        if self.effect is PolicyEffect.REQUIRE_APPROVAL and not self.approver_role:
            raise ValueError("REQUIRE_APPROVAL decisions must name an approver_role")


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    """Proof of what happened when an action was dispatched."""

    action_id: ActionId
    state: IdempotencyState
    provider_reference: str | None = None
    """The provider's own id for the effect - the join key for reconciliation."""
    settled_at: datetime | None = None
    failure_reason: str | None = None
    replayed: bool = False
    """True when this receipt was served from an existing idempotency claim
    rather than a fresh dispatch."""

    @property
    def succeeded(self) -> bool:
        return self.state is IdempotencyState.SUCCEEDED


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """The durable claim that makes an effect exactly-once.

    Written before dispatch and settled after. A record left `IN_FLIGHT` by a
    crash is the signal to reconcile, never to retry.
    """

    key: IdempotencyKey
    tenant_id: TenantId
    action_fingerprint: str
    state: IdempotencyState
    claimed_at: datetime
    settled_at: datetime | None = None
    receipt: ActionReceipt | None = None

    @property
    def is_settled(self) -> bool:
        return self.state is not IdempotencyState.IN_FLIGHT


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A human decision blocking a run.

    The approval binds to `action_fingerprint`, not just the action id: if the
    action's content changes after approval was granted, the grant no longer
    applies. Approving a ₹5,000 refund must never authorize a ₹5,00,000 one.
    """

    id: ApprovalId
    run_id: RunId
    tenant_id: TenantId
    action: Action
    action_fingerprint: str
    reason: str
    approver_role: str
    state: ApprovalState
    requested_at: datetime
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_note: str | None = None

    def grant(self, *, at: datetime, by: str, note: str | None = None) -> Self:
        """Record an approval.

        Raises:
            StateTransitionError: The request is no longer pending.
        """
        self._assert_pending(ApprovalState.GRANTED)
        return replace(
            self,
            state=ApprovalState.GRANTED,
            decided_at=at,
            decided_by=by,
            decision_note=note,
        )

    def reject(self, *, at: datetime, by: str, note: str | None = None) -> Self:
        """Record a rejection.

        Raises:
            StateTransitionError: The request is no longer pending.
        """
        self._assert_pending(ApprovalState.REJECTED)
        return replace(
            self,
            state=ApprovalState.REJECTED,
            decided_at=at,
            decided_by=by,
            decision_note=note,
        )

    def expire(self, *, at: datetime) -> Self:
        """Expire an unanswered request.

        Raises:
            StateTransitionError: The request is no longer pending.
        """
        self._assert_pending(ApprovalState.EXPIRED)
        return replace(self, state=ApprovalState.EXPIRED, decided_at=at)

    def authorizes(self, action: Action) -> bool:
        """True when this grant covers `action` exactly."""
        return (
            self.state is ApprovalState.GRANTED
            and action.fingerprint() == self.action_fingerprint
        )

    def _assert_pending(self, target: ApprovalState) -> None:
        if self.state is not ApprovalState.PENDING:
            raise StateTransitionError("ApprovalRequest", self.state.value, target.value)


# ---------------------------------------------------------------------------
# Audit ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One immutable record in a run's audit chain.

    Entries are hash-chained: each carries the digest of its predecessor, so
    altering or removing a historical entry invalidates every entry after it.
    That property is what makes the ledger evidence rather than a log.
    """

    id: EntryId
    run_id: RunId
    tenant_id: TenantId
    sequence: int
    event_type: LedgerEventType
    occurred_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    previous_hash: str = GENESIS_HASH
    entry_hash: str = ""

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("LedgerEntry.sequence must be non-negative")

    def sealed(self) -> LedgerEntry:
        """Return this entry with its hash computed.

        Called once, immediately before append. Sealing twice is harmless but
        pointless; the digest is deterministic.
        """
        return replace(self, entry_hash=self.compute_hash())

    def compute_hash(self) -> str:
        """Digest of this entry's content plus its predecessor's hash."""
        material = json.dumps(
            {
                "run_id": str(self.run_id),
                "tenant_id": str(self.tenant_id),
                "sequence": self.sequence,
                "event_type": self.event_type.value,
                "occurred_at": self.occurred_at.isoformat(),
                "payload": self.payload,
                "previous_hash": self.previous_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def verify(self) -> None:
        """Check this entry's own hash.

        Raises:
            LedgerIntegrityError: The stored hash does not match the content.
        """
        if not self.entry_hash:
            raise LedgerIntegrityError(
                "Ledger entry was never sealed",
                context={"run_id": str(self.run_id), "sequence": self.sequence},
            )
        expected = self.compute_hash()
        if expected != self.entry_hash:
            raise LedgerIntegrityError(
                "Ledger entry hash does not match its content",
                context={
                    "run_id": str(self.run_id),
                    "sequence": self.sequence,
                    "expected": expected,
                    "stored": self.entry_hash,
                },
            )


# ---------------------------------------------------------------------------
# Steps and runs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Step:
    """One iteration of the agent loop."""

    id: StepId
    run_id: RunId
    tenant_id: TenantId
    index: int
    outcome: StepOutcome
    started_at: datetime
    ended_at: datetime | None = None
    output_text: str = ""
    reasoning_summary: str = ""
    spend: TokenSpend = field(default_factory=TokenSpend)
    action_ids: tuple[ActionId, ...] = ()
    failure_reason: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        """Wall-clock duration, or None while the step is still open."""
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at).total_seconds()


@dataclass(frozen=True, slots=True)
class RunSpec:
    """Everything needed to start a run, and nothing that changes during one.

    Separated from `Run` so that the immutable request can be persisted,
    audited, and replayed independently of the mutable execution state.
    """

    tenant_id: TenantId
    objective: str
    system_prompt: str = ""
    model: str = "claude-opus-5"
    effort: Effort = Effort.HIGH
    budget: RunBudget = field(default_factory=RunBudget)
    correlation_id: CorrelationId | None = None
    deadline: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.objective:
            raise ValueError("RunSpec.objective is required")


@dataclass(frozen=True, slots=True)
class Run:
    """The aggregate root: one agent execution and its current state.

    Transitions are methods that validate the move and return a new `Run`.
    `version` is incremented on every transition and checked by the store, so
    two workers cannot both advance the same run.
    """

    id: RunId
    spec: RunSpec
    state: RunState
    created_at: datetime
    version: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    stop_reason: StopReason | None = None
    step_count: int = 0
    spend: TokenSpend = field(default_factory=TokenSpend)
    value_moved: Money | None = None
    pending_approval_id: ApprovalId | None = None
    failure_reason: str | None = None

    @property
    def tenant_id(self) -> TenantId:
        return self.spec.tenant_id

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    def start(self, *, at: datetime) -> Run:
        """Move PENDING -> RUNNING."""
        return self._transition(RunState.RUNNING, at=at, started_at=at)

    def await_approval(self, approval_id: ApprovalId, *, at: datetime) -> Run:
        """Halt on a policy gate."""
        return self._transition(
            RunState.AWAITING_APPROVAL,
            at=at,
            pending_approval_id=approval_id,
            stop_reason=StopReason.AWAITING_APPROVAL,
        )

    def resume(self, *, at: datetime) -> Run:
        """Return a halted run to RUNNING and clear the gate."""
        return self._transition(
            RunState.RUNNING,
            at=at,
            pending_approval_id=None,
            stop_reason=None,
        )

    def suspend(self, *, at: datetime) -> Run:
        """Halt for a non-approval reason."""
        return self._transition(RunState.SUSPENDED, at=at)

    def succeed(self, *, at: datetime, stop_reason: StopReason = StopReason.COMPLETED) -> Run:
        """Complete successfully."""
        return self._transition(
            RunState.SUCCEEDED, at=at, ended_at=at, stop_reason=stop_reason
        )

    def fail(self, reason: str, *, at: datetime, stop_reason: StopReason = StopReason.ERROR) -> Run:
        """Terminate without completing."""
        return self._transition(
            RunState.FAILED,
            at=at,
            ended_at=at,
            stop_reason=stop_reason,
            failure_reason=reason,
        )

    def cancel(self, *, at: datetime) -> Run:
        """Stop at an operator's or caller's request."""
        return self._transition(
            RunState.CANCELLED, at=at, ended_at=at, stop_reason=StopReason.CANCELLED
        )

    def expire(self, *, at: datetime) -> Run:
        """Expire a halted run that was never resumed."""
        return self._transition(
            RunState.EXPIRED, at=at, ended_at=at, stop_reason=StopReason.DEADLINE_EXCEEDED
        )

    def begin_compensation(self, *, at: datetime) -> Run:
        """Start rolling back applied effects."""
        return self._transition(RunState.COMPENSATING, at=at)

    def complete_compensation(self, *, at: datetime) -> Run:
        """Finish rollback with no net effect remaining."""
        return self._transition(RunState.COMPENSATED, at=at, ended_at=at)

    def record_step(self, step: Step) -> Run:
        """Fold a completed step's spend and count into the run.

        Does not change state - a step completing is not, by itself, a
        transition.
        """
        return replace(
            self,
            step_count=self.step_count + 1,
            spend=self.spend + step.spend,
            version=self.version + 1,
        )

    def record_value_moved(self, amount: Money) -> Run:
        """Add to the total value this run has moved.

        Raises:
            ValueError: The amount's currency differs from what the run has
                already moved.
        """
        moved = amount if self.value_moved is None else self.value_moved + amount
        return replace(self, value_moved=moved, version=self.version + 1)

    def _transition(self, target: RunState, *, at: datetime, **changes: Any) -> Run:
        if target not in _LEGAL_TRANSITIONS.get(self.state, frozenset()):
            raise StateTransitionError("Run", self.state.value, target.value)
        _ = at  # transitions stamp their own timestamps via `changes`
        return replace(self, state=target, version=self.version + 1, **changes)


_LEGAL_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.PENDING: frozenset({RunState.RUNNING, RunState.CANCELLED}),
    RunState.RUNNING: frozenset(
        {
            RunState.AWAITING_APPROVAL,
            RunState.SUSPENDED,
            RunState.COMPENSATING,
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.AWAITING_APPROVAL: frozenset(
        {RunState.RUNNING, RunState.CANCELLED, RunState.EXPIRED, RunState.FAILED}
    ),
    RunState.SUSPENDED: frozenset(
        {RunState.RUNNING, RunState.CANCELLED, RunState.EXPIRED, RunState.FAILED}
    ),
    RunState.COMPENSATING: frozenset({RunState.COMPENSATED, RunState.FAILED}),
    RunState.SUCCEEDED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.COMPENSATED: frozenset(),
    RunState.CANCELLED: frozenset(),
    RunState.EXPIRED: frozenset(),
}
