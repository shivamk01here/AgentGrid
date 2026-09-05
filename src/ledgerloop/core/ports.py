"""Ports: the interfaces the runtime depends on.

Everything the core needs from the outside world is declared here as an
async `Protocol`. The runtime imports these; it never imports an adapter.
Postgres, Redis, an approval queue, a PSP client - all of them are details
that satisfy a port, and all of them are replaceable in a test by an
in-memory object with the same shape.

Two conventions hold throughout:

* Every method is `async`. There are no sync escape hatches, because a
  blocking call inside an agent loop stalls every other run on the worker.
* Every method that touches stored state takes a `TenantId`. Isolation is
  enforced at the port boundary, not left to a caller's discipline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self, runtime_checkable

from ledgerloop.core.enums import LedgerEventType, RunState
from ledgerloop.core.ids import (
    ApprovalId,
    CorrelationId,
    IdempotencyKey,
    RunId,
    StepId,
    TenantId,
)
from ledgerloop.core.models import (
    Action,
    ActionReceipt,
    ApprovalRequest,
    IdempotencyRecord,
    LedgerEntry,
    PolicyDecision,
    Run,
    RunSpec,
    Step,
)

__all__ = [
    "ActionDispatcher",
    "ApprovalGateway",
    "Clock",
    "EventPublisher",
    "IdempotencyStore",
    "LedgerStore",
    "PolicyEngine",
    "RunStore",
    "StepStore",
    "UnitOfWork",
]


@runtime_checkable
class Clock(Protocol):
    """The only source of time in the system.

    Injected rather than called directly so that runs are deterministic under
    test and reconstructible under replay. Nothing in the core calls
    `datetime.now()`.
    """

    def now(self) -> datetime:
        """Current time, always timezone-aware and always UTC."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Yield for `seconds`. Virtualized in tests to run instantly."""
        ...


@runtime_checkable
class RunStore(Protocol):
    """Durable storage for run aggregates."""

    async def create(self, spec: RunSpec, *, run_id: RunId | None = None) -> Run:
        """Persist a new run in `PENDING`.

        Args:
            spec: The immutable request.
            run_id: Caller-supplied id, for callers that need to know the id
                before the run exists. Generated when omitted.

        Returns:
            The stored run at version 0.
        """
        ...

    async def get(self, tenant_id: TenantId, run_id: RunId) -> Run:
        """Load a run.

        Raises:
            RunNotFoundError: No such run for this tenant.
        """
        ...

    async def save(self, run: Run, *, expected_version: int) -> Run:
        """Persist a transitioned run under optimistic concurrency.

        Args:
            run: The new state, already at `expected_version + 1`.
            expected_version: The version the caller read.

        Returns:
            The stored run.

        Raises:
            ConcurrencyError: Another worker advanced the run first. Reload
                and re-evaluate - never force the write.
        """
        ...

    async def acquire_lease(
        self, tenant_id: TenantId, run_id: RunId, *, owner: str, duration_seconds: float
    ) -> bool:
        """Claim exclusive execution rights over a run.

        Returns:
            True when the lease was granted. False means another worker holds
            it and this caller must not execute the run.
        """
        ...

    async def release_lease(self, tenant_id: TenantId, run_id: RunId, *, owner: str) -> None:
        """Release a lease this worker holds. Safe to call if already expired."""
        ...

    def list_by_state(
        self,
        tenant_id: TenantId,
        state: RunState,
        *,
        limit: int = 100,
    ) -> AsyncIterator[Run]:
        """Stream runs in a given state, oldest first.

        Not `async def`: the implementation is an async generator, so callers
        write `async for run in store.list_by_state(...)`.
        """
        ...

    def find_expired(self, *, as_of: datetime, limit: int = 100) -> AsyncIterator[Run]:
        """Stream halted runs whose deadline has passed, across all tenants.

        Used by the reaper. The one intentionally cross-tenant read in the
        interface, because expiry is an operator concern.
        """
        ...


@runtime_checkable
class StepStore(Protocol):
    """Durable storage for individual loop iterations."""

    async def append(self, step: Step) -> Step:
        """Persist a step. Steps are immutable once written."""
        ...

    async def get(self, tenant_id: TenantId, step_id: StepId) -> Step | None:
        """Load one step, or None when it does not exist."""
        ...

    async def list_for_run(self, tenant_id: TenantId, run_id: RunId) -> Sequence[Step]:
        """Every step of a run, in execution order."""
        ...


@runtime_checkable
class LedgerStore(Protocol):
    """Append-only audit storage.

    Implementations must reject updates and deletes at the storage layer, not
    merely decline to offer them here. If an operator with database access can
    edit a row, the chain is the only remaining evidence - so verify it.
    """

    async def append(
        self,
        run_id: RunId,
        tenant_id: TenantId,
        event_type: LedgerEventType,
        payload: dict[str, Any],
        *,
        occurred_at: datetime,
    ) -> LedgerEntry:
        """Seal and append one entry, chaining it to the run's current head.

        The implementation assigns the sequence number and the previous hash
        atomically, so concurrent appends cannot fork the chain.

        Returns:
            The sealed, stored entry.
        """
        ...

    async def read(self, tenant_id: TenantId, run_id: RunId) -> Sequence[LedgerEntry]:
        """Every entry for a run, in sequence order."""
        ...

    async def verify_chain(self, tenant_id: TenantId, run_id: RunId) -> None:
        """Re-hash the run's entire chain and check every link.

        Raises:
            LedgerIntegrityError: An entry was altered, removed, or reordered.
        """
        ...


@runtime_checkable
class IdempotencyStore(Protocol):
    """The exactly-once boundary for value-moving effects.

    The contract is a two-phase claim: `claim` before dispatch, `settle`
    after. A record left `IN_FLIGHT` by a crash means the outcome is unknown
    and must be reconciled - it never means "safe to retry".
    """

    async def claim(
        self,
        key: IdempotencyKey,
        tenant_id: TenantId,
        action_fingerprint: str,
        *,
        at: datetime,
    ) -> IdempotencyRecord:
        """Atomically claim `key`, or return the existing claim.

        A returned record in state `SUCCEEDED` carries the original receipt
        and the caller must replay it rather than dispatch again.

        Returns:
            The freshly created claim, or the pre-existing record.

        Raises:
            IdempotencyConflictError: The key exists under a different
                fingerprint - two different effects derived the same key,
                which is a correctness bug.
        """
        ...

    async def settle(
        self, key: IdempotencyKey, tenant_id: TenantId, receipt: ActionReceipt, *, at: datetime
    ) -> IdempotencyRecord:
        """Record the outcome of a dispatched effect.

        Raises:
            StateTransitionError: The claim was already settled.
        """
        ...

    async def get(self, key: IdempotencyKey, tenant_id: TenantId) -> IdempotencyRecord | None:
        """Look up a claim, or None when the key was never claimed."""
        ...

    def find_in_flight(
        self, *, older_than: datetime, limit: int = 100
    ) -> AsyncIterator[IdempotencyRecord]:
        """Stream stale claims awaiting reconciliation.

        Anything still `IN_FLIGHT` past a provider's settlement window is an
        effect whose fate nobody knows. Feeding this to a reconciler is
        mandatory, not optional.
        """
        ...


@runtime_checkable
class PolicyEngine(Protocol):
    """Decides whether a proposed action may execute.

    Deterministic and side-effect free: the same action under the same
    tenant configuration must always produce the same decision, or replay and
    audit both become meaningless.
    """

    async def evaluate(
        self, action: Action, run: Run, *, at: datetime
    ) -> PolicyDecision:
        """Classify and rule on one proposed action."""
        ...


@runtime_checkable
class ApprovalGateway(Protocol):
    """Where human decisions are requested and collected."""

    async def request(
        self,
        run: Run,
        action: Action,
        decision: PolicyDecision,
        *,
        at: datetime,
        expires_at: datetime | None = None,
    ) -> ApprovalRequest:
        """Raise an approval request and notify the responsible role."""
        ...

    async def get(self, tenant_id: TenantId, approval_id: ApprovalId) -> ApprovalRequest | None:
        """Load a request, or None when it does not exist."""
        ...

    async def submit(
        self,
        tenant_id: TenantId,
        approval_id: ApprovalId,
        *,
        approved: bool,
        actor: str,
        at: datetime,
        note: str | None = None,
    ) -> ApprovalRequest:
        """Record a reviewer's verdict.

        Implementations must verify that `actor` holds the required role and
        is not the party that requested the action.

        Raises:
            StateTransitionError: The request was already decided.
        """
        ...

    def list_pending(
        self, tenant_id: TenantId, *, limit: int = 100
    ) -> AsyncIterator[ApprovalRequest]:
        """Stream outstanding requests for a tenant, oldest first."""
        ...


@runtime_checkable
class ActionDispatcher(Protocol):
    """Executes an approved action against an external system.

    Implementations must pass the action's idempotency key through to the
    provider wherever the provider supports one, so exactly-once holds even
    if this process dies between claim and settle.
    """

    def handles(self, action: Action) -> bool:
        """True when this dispatcher can execute `action`."""
        ...

    async def dispatch(self, action: Action, *, at: datetime) -> ActionReceipt:
        """Execute the action.

        Raises:
            IndeterminateError: The request left the process but the outcome
                is unknown. The caller must reconcile, never retry.
            ProviderError: The provider rejected or failed the request
                definitively.
        """
        ...

    async def compensate(self, action: Action, receipt: ActionReceipt, *, at: datetime) -> ActionReceipt:
        """Reverse a previously executed action.

        Raises:
            NotImplementedError: `action.kind` is not reversible - the caller
                must check `ActionKind.is_reversible` first.
        """
        ...


@runtime_checkable
class EventPublisher(Protocol):
    """Emits domain events to the outside world.

    Best-effort and strictly non-blocking to the run: a webhook endpoint being
    down must never stall or fail a run. Durability belongs to the ledger,
    not here.
    """

    async def publish(
        self,
        event_type: LedgerEventType,
        tenant_id: TenantId,
        payload: dict[str, Any],
        *,
        correlation_id: CorrelationId | None = None,
    ) -> None:
        """Publish one event. Never raises into the caller."""
        ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Transactional boundary spanning the stores.

    A run's state change and its ledger entries must commit together. If the
    run advances but the ledger does not, the audit trail is a lie - which is
    worse than the run failing.

    Example:
        async with unit_of_work() as uow:
            await uow.runs.save(run, expected_version=version)
            await uow.ledger.append(...)
    """

    @property
    def runs(self) -> RunStore: ...

    @property
    def steps(self) -> StepStore: ...

    @property
    def ledger(self) -> LedgerStore: ...

    @property
    def idempotency(self) -> IdempotencyStore: ...

    async def __aenter__(self) -> Self:
        """Begin the transaction."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Commit on clean exit, roll back on exception."""
        ...

    async def commit(self) -> None:
        """Commit early, before the context exits."""
        ...

    async def rollback(self) -> None:
        """Discard every write made in this transaction."""
        ...
