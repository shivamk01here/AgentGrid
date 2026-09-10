"""Closed vocabularies for the Ledgerloop domain.

Every enum here is a `StrEnum`: the member's value is the exact token that
gets persisted, logged, and sent over the wire. That equivalence is
deliberate - an audit record written three years ago must still deserialize
against today's code, so the wire format is the enum rather than a mapping
maintained alongside it.

Members are only ever appended. Renaming or removing one invalidates
historical ledger entries, which are immutable by contract.
"""

from __future__ import annotations

from enum import StrEnum, unique

__all__ = [
    "ActionKind",
    "ApprovalDecision",
    "ApprovalState",
    "Currency",
    "Effort",
    "FailureClass",
    "IdempotencyState",
    "LedgerEventType",
    "PolicyEffect",
    "RiskTier",
    "RunState",
    "StepOutcome",
    "StopReason",
]


@unique
class RunState(StrEnum):
    """Lifecycle of a single agent run.

    Legal transitions - anything else is a bug and must raise:

        PENDING     -> RUNNING | CANCELLED
        RUNNING     -> AWAITING_APPROVAL | SUCCEEDED | FAILED
                       | COMPENSATING | CANCELLED | SUSPENDED
        AWAITING_APPROVAL -> RUNNING | CANCELLED | EXPIRED
        SUSPENDED   -> RUNNING | CANCELLED | EXPIRED
        COMPENSATING -> COMPENSATED | FAILED
        SUCCEEDED | FAILED | COMPENSATED | CANCELLED | EXPIRED -> (terminal)
    """

    PENDING = "pending"
    """Accepted and durably recorded, not yet started."""

    RUNNING = "running"
    """A worker holds the lease and is executing steps."""

    AWAITING_APPROVAL = "awaiting_approval"
    """Halted on a policy gate; a human decision is outstanding."""

    SUSPENDED = "suspended"
    """Halted for a non-approval reason - a dependency backoff or an
    operator hold. Resumable without a human decision."""

    COMPENSATING = "compensating"
    """Rolling back effects that were applied before the failure."""

    COMPENSATED = "compensated"
    """Rollback completed; the run left no net effect."""

    SUCCEEDED = "succeeded"
    """Completed and every effect committed."""

    FAILED = "failed"
    """Terminated without completing. Effects may remain - check the ledger."""

    CANCELLED = "cancelled"
    """Stopped by operator or caller before completion."""

    EXPIRED = "expired"
    """Exceeded its deadline while halted, and was never resumed."""

    @property
    def is_terminal(self) -> bool:
        """True when no further transition is permitted."""
        return self in _TERMINAL_RUN_STATES

    @property
    def is_halted(self) -> bool:
        """True when the run is alive but not progressing on its own."""
        return self in (RunState.AWAITING_APPROVAL, RunState.SUSPENDED)

    @property
    def is_resumable(self) -> bool:
        """True when a resume request could legally restart this run."""
        return self in (RunState.PENDING, RunState.AWAITING_APPROVAL, RunState.SUSPENDED)


_TERMINAL_RUN_STATES = frozenset(
    {
        RunState.COMPENSATED,
        RunState.SUCCEEDED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.EXPIRED,
    }
)


@unique
class ActionKind(StrEnum):
    """What an action does to the outside world.

    The classification drives policy, idempotency, and compensation. It is
    the single most safety-relevant field on an action: `READ` may be
    replayed freely, while every `MOVES_VALUE` member below must be claimed
    against the idempotency store before it is dispatched.
    """

    READ = "read"
    """Retrieves data. No external state changes. Freely replayable."""

    CAPTURE = "capture"
    """Captures a previously authorized amount."""

    REFUND = "refund"
    """Returns funds to a payer."""

    PAYOUT = "payout"
    """Disburses funds to a recipient."""

    TRANSFER = "transfer"
    """Moves funds between two accounts under the operator's control."""

    VOID = "void"
    """Cancels an authorization before capture."""

    ADJUSTMENT = "adjustment"
    """Posts a correcting ledger entry, e.g. after reconciliation."""

    HOLD = "hold"
    """Places a block on funds or an account."""

    RELEASE = "release"
    """Removes a previously placed hold."""

    DISPUTE_RESPONSE = "dispute_response"
    """Submits evidence to an acquirer or scheme. Irreversible once filed."""

    NOTIFY = "notify"
    """Sends an outbound message. Irreversible, but moves no value."""

    ANNOTATE = "annotate"
    """Writes an internal note or tag. No external effect."""

    @property
    def moves_value(self) -> bool:
        """True when executing this action moves money.

        Every such action requires an idempotency claim and is subject to
        amount-based policy.
        """
        return self in _VALUE_MOVING_ACTIONS

    @property
    def is_reversible(self) -> bool:
        """True when a compensating action can undo this one.

        False does not mean "harmless" - it means a mistake cannot be
        walked back by the system and needs human remediation.
        """
        return self in _REVERSAL_KINDS

    @property
    def reversal_kind(self) -> ActionKind | None:
        """The kind of action that undoes this one, or None if nothing does.

        A reversal is a new effect in its own right, not an erasure: undoing
        a capture means issuing a refund, and the refund is what the payer
        and the ledger both see. Naming the kind here keeps that visible in
        the audit trail instead of hiding a reversal behind the kind it
        reversed.
        """
        return _REVERSAL_KINDS.get(self)

    @property
    def is_read_only(self) -> bool:
        """True when the action leaves no external trace."""
        return self in (ActionKind.READ, ActionKind.ANNOTATE)


_VALUE_MOVING_ACTIONS = frozenset(
    {
        ActionKind.CAPTURE,
        ActionKind.REFUND,
        ActionKind.PAYOUT,
        ActionKind.TRANSFER,
        ActionKind.ADJUSTMENT,
    }
)

_REVERSAL_KINDS: dict[ActionKind, ActionKind] = {
    ActionKind.CAPTURE: ActionKind.REFUND,
    ActionKind.TRANSFER: ActionKind.TRANSFER,
    ActionKind.ADJUSTMENT: ActionKind.ADJUSTMENT,
    ActionKind.HOLD: ActionKind.RELEASE,
}
"""What undoes what. Membership here is what makes a kind reversible - the
two facts cannot drift apart because there is only one of them."""


@unique
class RiskTier(StrEnum):
    """How much scrutiny an action warrants.

    Assigned by the policy engine from action kind, amount, counterparty
    history, and tenant configuration - never by the model.
    """

    NONE = "none"
    """Read-only or internal. No gate."""

    LOW = "low"
    """Small value, reversible, well-understood counterparty."""

    MEDIUM = "medium"
    """Material value or a partially reversible effect."""

    HIGH = "high"
    """Large value or irreversible. Human approval expected."""

    CRITICAL = "critical"
    """Never auto-executes under any policy configuration."""

    @property
    def rank(self) -> int:
        """Ordinal for comparison. Higher means riskier."""
        return _RISK_RANKS[self]

    def __lt__(self, other: RiskTier) -> bool:  # type: ignore[override]
        if not isinstance(other, RiskTier):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: RiskTier) -> bool:  # type: ignore[override]
        if not isinstance(other, RiskTier):
            return NotImplemented
        return self.rank <= other.rank

    def __gt__(self, other: RiskTier) -> bool:  # type: ignore[override]
        if not isinstance(other, RiskTier):
            return NotImplemented
        return self.rank > other.rank

    def __ge__(self, other: RiskTier) -> bool:  # type: ignore[override]
        if not isinstance(other, RiskTier):
            return NotImplemented
        return self.rank >= other.rank


_RISK_RANKS: dict[RiskTier, int] = {
    RiskTier.NONE: 0,
    RiskTier.LOW: 1,
    RiskTier.MEDIUM: 2,
    RiskTier.HIGH: 3,
    RiskTier.CRITICAL: 4,
}


@unique
class PolicyEffect(StrEnum):
    """What the policy engine decided about a proposed action."""

    ALLOW = "allow"
    """Execute immediately."""

    REQUIRE_APPROVAL = "require_approval"
    """Halt the run and raise an approval request."""

    DENY = "deny"
    """Refuse. The model is told why and may propose something else."""

    @property
    def permits_execution(self) -> bool:
        """True only for ALLOW - approval is a separate, later grant."""
        return self is PolicyEffect.ALLOW


@unique
class ApprovalState(StrEnum):
    """Lifecycle of a human approval request."""

    PENDING = "pending"
    """Raised and waiting on a reviewer."""

    GRANTED = "granted"
    """Approved. The run may execute the exact action that was reviewed."""

    REJECTED = "rejected"
    """Declined by a reviewer."""

    EXPIRED = "expired"
    """Nobody responded before the deadline."""

    WITHDRAWN = "withdrawn"
    """Retracted before a decision - the run was cancelled or superseded."""

    @property
    def is_terminal(self) -> bool:
        """True when the request will not change again."""
        return self is not ApprovalState.PENDING


@unique
class ApprovalDecision(StrEnum):
    """A reviewer's verdict, as submitted."""

    APPROVE = "approve"
    REJECT = "reject"


@unique
class IdempotencyState(StrEnum):
    """Lifecycle of an idempotency claim.

    The claim is taken *before* the effect is dispatched and settled after,
    so a crash between the two leaves an `IN_FLIGHT` record. Recovery must
    reconcile that record against the provider rather than assume either
    outcome - the whole point is that a retry can never double-execute.
    """

    IN_FLIGHT = "in_flight"
    """Claimed; the effect may or may not have reached the provider."""

    SUCCEEDED = "succeeded"
    """Settled. The stored result is replayed for any later attempt."""

    FAILED = "failed"
    """Settled as a definitive provider-side failure. Safe to retry under a
    new key only if the caller decides to."""


@unique
class StepOutcome(StrEnum):
    """How one loop iteration ended."""

    COMPLETED = "completed"
    """Model turn and any tool calls finished normally."""

    TOOLS_REQUESTED = "tools_requested"
    """The model asked for tools; results were returned to it."""

    GATED = "gated"
    """A proposed action hit a policy gate and the run halted."""

    FAILED = "failed"
    """The step errored and did not produce a usable result."""

    SKIPPED = "skipped"
    """Replayed from the ledger rather than re-executed."""


@unique
class StopReason(StrEnum):
    """Why a run's loop stopped iterating."""

    COMPLETED = "completed"
    """The model produced a final answer with no further tool calls."""

    MAX_ITERATIONS = "max_iterations"
    """The iteration ceiling was reached first."""

    AWAITING_APPROVAL = "awaiting_approval"
    """Halted on a gate. Resumable."""

    BUDGET_EXHAUSTED = "budget_exhausted"
    """The run's token or cost budget ran out."""

    DEADLINE_EXCEEDED = "deadline_exceeded"
    """The run's wall-clock deadline passed."""

    REFUSED = "refused"
    """The model declined the request on policy grounds."""

    ERROR = "error"
    """An unrecoverable failure ended the run."""

    CANCELLED = "cancelled"
    """An operator or caller stopped the run."""


@unique
class FailureClass(StrEnum):
    """Why something failed, in terms of what to do about it.

    This is the retry contract. `TRANSIENT` and `RATE_LIMITED` are the only
    classes a runtime may retry on its own; `INDETERMINATE` must never be
    retried blindly because the effect may already have landed.
    """

    TRANSIENT = "transient"
    """Network fault, timeout, or 5xx. Retry with backoff."""

    RATE_LIMITED = "rate_limited"
    """Throttled upstream. Retry after the advertised delay."""

    INVALID_REQUEST = "invalid_request"
    """Malformed or rejected input. Retrying changes nothing."""

    UNAUTHORIZED = "unauthorized"
    """Missing or rejected credentials. Needs operator action."""

    NOT_FOUND = "not_found"
    """The referenced entity does not exist."""

    CONFLICT = "conflict"
    """Lost an optimistic-concurrency race, or violated a unique constraint."""

    POLICY = "policy"
    """Refused by policy - the model's, or the tenant's."""

    INSUFFICIENT_FUNDS = "insufficient_funds"
    """The source could not cover the amount."""

    INDETERMINATE = "indeterminate"
    """The outcome is genuinely unknown - a timeout after dispatch. Requires
    reconciliation against the provider, never a blind retry."""

    INTERNAL = "internal"
    """A defect in Ledgerloop itself."""

    @property
    def is_retryable(self) -> bool:
        """True when an identical retry could plausibly succeed."""
        return self in (FailureClass.TRANSIENT, FailureClass.RATE_LIMITED)

    @property
    def needs_reconciliation(self) -> bool:
        """True when the real-world outcome must be confirmed before acting."""
        return self is FailureClass.INDETERMINATE


@unique
class LedgerEventType(StrEnum):
    """The kinds of record that appear in a run's audit ledger.

    The ledger is append-only and hash-chained; these are its verbs.
    """

    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_RESUMED = "run.resumed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"

    STEP_STARTED = "step.started"
    STEP_COMPLETED = "step.completed"

    MODEL_CALLED = "model.called"
    MODEL_RESPONDED = "model.responded"

    ACTION_PROPOSED = "action.proposed"
    ACTION_EVALUATED = "action.evaluated"
    ACTION_DISPATCHED = "action.dispatched"
    ACTION_SETTLED = "action.settled"
    ACTION_FAILED = "action.failed"
    ACTION_COMPENSATED = "action.compensated"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_REJECTED = "approval.rejected"
    APPROVAL_EXPIRED = "approval.expired"


@unique
class Effort(StrEnum):
    """Reasoning depth requested from the model.

    Replaces sampling parameters, which the current model family rejects.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


@unique
class Currency(StrEnum):
    """ISO 4217 currencies, with their minor-unit exponents.

    The exponent is what makes `Money` safe: every amount is stored as an
    integer count of minor units, and the exponent is the only thing that
    turns it back into a human-readable figure.
    """

    AED = "AED"
    AUD = "AUD"
    BHD = "BHD"
    CAD = "CAD"
    CHF = "CHF"
    CNY = "CNY"
    EUR = "EUR"
    GBP = "GBP"
    HKD = "HKD"
    IDR = "IDR"
    INR = "INR"
    JPY = "JPY"
    KRW = "KRW"
    KWD = "KWD"
    MYR = "MYR"
    NZD = "NZD"
    OMR = "OMR"
    PHP = "PHP"
    SAR = "SAR"
    SEK = "SEK"
    SGD = "SGD"
    THB = "THB"
    USD = "USD"
    VND = "VND"
    ZAR = "ZAR"

    @property
    def exponent(self) -> int:
        """Number of decimal places this currency subdivides into.

        Most currencies use 2. The zero- and three-decimal exceptions are
        the classic source of 100x and 1000x production incidents.
        """
        return _CURRENCY_EXPONENTS.get(self, 2)

    @property
    def minor_units_per_major(self) -> int:
        """How many minor units make one major unit - 100 for USD, 1 for JPY."""
        return 10**self.exponent


_CURRENCY_EXPONENTS: dict[Currency, int] = {
    Currency.BHD: 3,
    Currency.IDR: 0,
    Currency.JPY: 0,
    Currency.KRW: 0,
    Currency.KWD: 3,
    Currency.OMR: 3,
    Currency.VND: 0,
}
