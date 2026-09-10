"""Exception hierarchy.

Every failure carries a `FailureClass`, and that class - not the exception
type, not a string match - is what callers branch on to decide whether to
retry. The types exist to carry context; the classification exists to drive
behavior.

The distinction that matters most is `IndeterminateError`: a failure where
the effect may or may not have landed. It is deliberately not a subclass of
anything retryable, because retrying it is how you pay twice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ledgerloop.core.enums import FailureClass

if TYPE_CHECKING:
    from ledgerloop.core.ids import ActionId, IdempotencyKey, RunId

__all__ = [
    "ApprovalRequired",
    "BudgetExhaustedError",
    "CompensationError",
    "ConcurrencyError",
    "ConfigurationError",
    "DeadlineExceededError",
    "IdempotencyConflictError",
    "IndeterminateError",
    "LedgerIntegrityError",
    "LedgerloopError",
    "PolicyViolationError",
    "ProviderError",
    "RunNotFoundError",
    "StateTransitionError",
    "ToolExecutionError",
]


class LedgerloopError(Exception):
    """Base class for every error raised by Ledgerloop.

    Attributes:
        failure_class: What kind of failure this is, and therefore what a
            caller may do about it.
        context: Structured detail for logs and ledger entries. Must never
            contain credentials or full instrument numbers.
        retry_after: Seconds to wait before retrying, when upstream said so.
    """

    default_failure_class: FailureClass = FailureClass.INTERNAL

    def __init__(
        self,
        message: str,
        *,
        failure_class: FailureClass | None = None,
        context: dict[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.failure_class = failure_class or self.default_failure_class
        self.context: dict[str, Any] = context or {}
        self.retry_after = retry_after

    @property
    def is_retryable(self) -> bool:
        """True when an identical retry could plausibly succeed."""
        return self.failure_class.is_retryable

    @property
    def needs_reconciliation(self) -> bool:
        """True when the real-world outcome must be confirmed before acting."""
        return self.failure_class.needs_reconciliation

    def to_dict(self) -> dict[str, Any]:
        """Serialize for a ledger entry or an API response."""
        return {
            "type": type(self).__name__,
            "message": self.message,
            "failure_class": self.failure_class.value,
            "retryable": self.is_retryable,
            "context": self.context,
        }

    def __str__(self) -> str:
        if not self.context:
            return self.message
        detail = " ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} ({detail})"


# ---- configuration and programming errors --------------------------------


class ConfigurationError(LedgerloopError):
    """The system is misconfigured. Fails fast at startup, not at runtime."""

    default_failure_class = FailureClass.INVALID_REQUEST


class StateTransitionError(LedgerloopError):
    """An illegal state transition was attempted.

    Always a defect. The legal transitions are documented on `RunState`.
    """

    default_failure_class = FailureClass.CONFLICT

    def __init__(self, entity: str, from_state: str, to_state: str) -> None:
        super().__init__(
            f"Illegal {entity} transition: {from_state} -> {to_state}",
            context={"entity": entity, "from_state": from_state, "to_state": to_state},
        )
        self.from_state = from_state
        self.to_state = to_state


# ---- lookup and concurrency ----------------------------------------------


class RunNotFoundError(LedgerloopError):
    """No run with that id exists for this tenant."""

    default_failure_class = FailureClass.NOT_FOUND

    def __init__(self, run_id: RunId) -> None:
        super().__init__(f"Run not found: {run_id}", context={"run_id": str(run_id)})
        self.run_id = run_id


class ConcurrencyError(LedgerloopError):
    """Lost an optimistic-concurrency race.

    Another worker advanced the run first. The correct response is to reload
    and re-evaluate, never to force the write.
    """

    default_failure_class = FailureClass.CONFLICT

    def __init__(self, entity: str, expected_version: int, actual_version: int) -> None:
        super().__init__(
            f"{entity} was modified concurrently: "
            f"expected version {expected_version}, found {actual_version}",
            context={
                "entity": entity,
                "expected_version": expected_version,
                "actual_version": actual_version,
            },
        )


# ---- money safety --------------------------------------------------------


class IdempotencyConflictError(LedgerloopError):
    """An idempotency key was reused with different action content.

    The key is derived from what the action does, so a collision under
    different content means two genuinely different effects computed the same
    key. That is a correctness bug and must never be papered over.
    """

    default_failure_class = FailureClass.CONFLICT

    def __init__(self, key: IdempotencyKey, existing_fingerprint: str, new_fingerprint: str) -> None:
        super().__init__(
            f"Idempotency key {key} was claimed for different action content",
            context={
                "idempotency_key": str(key),
                "existing_fingerprint": existing_fingerprint,
                "new_fingerprint": new_fingerprint,
            },
        )
        self.key = key


class IndeterminateError(LedgerloopError):
    """An effect was dispatched but its outcome is unknown.

    Raised when a value-moving call times out or the connection drops after
    the request left the process. The effect may have landed. Never retry -
    reconcile against the provider using the idempotency key, then settle
    the claim accordingly.
    """

    default_failure_class = FailureClass.INDETERMINATE

    def __init__(
        self,
        message: str,
        *,
        idempotency_key: IdempotencyKey | None = None,
        action_id: ActionId | None = None,
    ) -> None:
        context: dict[str, Any] = {}
        if idempotency_key is not None:
            context["idempotency_key"] = str(idempotency_key)
        if action_id is not None:
            context["action_id"] = str(action_id)
        super().__init__(message, context=context)
        self.idempotency_key = idempotency_key
        self.action_id = action_id


class CompensationError(LedgerloopError):
    """An applied effect could not be reversed.

    Raised when rollback is asked for and cannot be delivered - the kind has
    no reversal, or the provider refused one. The effect is still out there:
    this is a hand-off to a human, not a retry signal.
    """

    default_failure_class = FailureClass.INTERNAL

    def __init__(
        self,
        message: str,
        *,
        action_id: ActionId | None = None,
        kind: str | None = None,
    ) -> None:
        context: dict[str, Any] = {}
        if action_id is not None:
            context["action_id"] = str(action_id)
        if kind is not None:
            context["kind"] = kind
        super().__init__(message, context=context)
        self.action_id = action_id
        self.kind = kind


class LedgerIntegrityError(LedgerloopError):
    """The audit ledger's hash chain does not verify.

    Either the chain was written incorrectly or a record was altered after
    the fact. Both are severe: the ledger is the evidence.
    """

    default_failure_class = FailureClass.INTERNAL


# ---- policy and approval -------------------------------------------------


class PolicyViolationError(LedgerloopError):
    """A proposed action was denied by policy.

    Not a defect - policy denying an action is the system working. The model
    is told why and may propose an alternative.
    """

    default_failure_class = FailureClass.POLICY

    def __init__(self, reason: str, *, rule_id: str | None = None) -> None:
        super().__init__(
            f"Action denied by policy: {reason}",
            context={"rule_id": rule_id} if rule_id else {},
        )
        self.reason = reason
        self.rule_id = rule_id


class ApprovalRequired(LedgerloopError):
    """Control-flow signal: the run must halt for a human decision.

    An exception rather than a return value so that it cannot be ignored by
    a caller that forgot to check a result. Callers are expected to catch
    this, persist the run, and return - not to log and continue.
    """

    default_failure_class = FailureClass.POLICY

    def __init__(self, reason: str, *, action_id: ActionId | None = None) -> None:
        super().__init__(
            f"Approval required: {reason}",
            context={"action_id": str(action_id)} if action_id else {},
        )
        self.reason = reason
        self.action_id = action_id


# ---- budgets and deadlines -----------------------------------------------


class BudgetExhaustedError(LedgerloopError):
    """The run consumed its token or cost budget."""

    default_failure_class = FailureClass.POLICY


class DeadlineExceededError(LedgerloopError):
    """The run passed its wall-clock deadline."""

    default_failure_class = FailureClass.POLICY


# ---- external boundaries -------------------------------------------------


class ProviderError(LedgerloopError):
    """An external system failed - a model API, a PSP, a bank.

    Carries the upstream status and provider name so that the retry decision
    and the ledger entry are both made from real information.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        failure_class: FailureClass = FailureClass.TRANSIENT,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        context: dict[str, Any] = {"provider": provider}
        if status_code is not None:
            context["status_code"] = status_code
        super().__init__(
            message,
            failure_class=failure_class,
            context=context,
            retry_after=retry_after,
        )
        self.provider = provider
        self.status_code = status_code


class ToolExecutionError(LedgerloopError):
    """A tool raised while executing.

    Surfaced to the model as a failed tool result rather than propagated:
    the model can correct itself, a crashed run cannot.
    """

    default_failure_class = FailureClass.INTERNAL

    def __init__(self, tool_name: str, cause: str) -> None:
        super().__init__(
            f"Tool {tool_name!r} raised: {cause}",
            context={"tool": tool_name},
        )
        self.tool_name = tool_name
