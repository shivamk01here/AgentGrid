"""In-memory unit of work - the transaction boundary over the stores.

`core.ports.UnitOfWork` has been part of the interface since the first
commit, with nothing behind it. This is the reference implementation: a run
advancing without its ledger entries, or the other way round, is the failure
the port exists to make impossible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, Self

if TYPE_CHECKING:
    from types import TracebackType

    from ledgerloop.core.ports import IdempotencyStore, LedgerStore, RunStore, StepStore

__all__ = ["InMemoryUnitOfWork"]


class _UndoableStore(Protocol):
    """What a unit of work needs from a store to be able to undo it.

    An adapter opts in by offering these two. A durable adapter implements
    them with `BEGIN` and `ROLLBACK` instead; nothing above this file knows
    which it is talking to.
    """

    def capture_state(self) -> Any:
        """Return a snapshot of everything this store holds."""
        ...

    def restore_state(self, state: Any) -> None:
        """Discard everything written since `state` was captured."""
        ...


class InMemoryUnitOfWork:
    """Commits a run's state and its ledger entries together, or neither.

    The stores are the caller's, not this object's. They are long-lived and
    hold everything written so far, so they are passed in rather than
    created here: a transaction that committed into stores nobody else could
    read would be a transaction that threw its own work away.

    Writes apply as they happen and read back immediately, the way a database
    session behaves inside an open transaction, so the code in the block sees
    its own writes. Committing is therefore just dropping the snapshot, and
    rolling back is putting it back.

    One transaction at a time, and not nestable: a nested entry would capture
    a snapshot of the outer transaction's own uncommitted writes, and the
    inner rollback would throw those away too.

    Example:
        uow = InMemoryUnitOfWork(runs, steps, ledger, idempotency)
        async with uow:
            await uow.runs.save(run, expected_version=version)
            await uow.ledger.append(
                run.id, tenant_id, LedgerEventType.RUN_STARTED, {}, occurred_at=now
            )
    """

    def __init__(
        self,
        runs: RunStore,
        steps: StepStore,
        ledger: LedgerStore,
        idempotency: IdempotencyStore,
    ) -> None:
        self._runs: _UndoableStore = runs  # type: ignore[assignment]
        self._steps: _UndoableStore = steps  # type: ignore[assignment]
        self._ledger: _UndoableStore = ledger  # type: ignore[assignment]
        self._idempotency: _UndoableStore = idempotency  # type: ignore[assignment]
        self._snapshot: tuple[Any, Any, Any, Any] | None = None

    @property
    def runs(self) -> RunStore:
        return self._runs  # type: ignore[return-value]

    @property
    def steps(self) -> StepStore:
        return self._steps  # type: ignore[return-value]

    @property
    def ledger(self) -> LedgerStore:
        return self._ledger  # type: ignore[return-value]

    @property
    def idempotency(self) -> IdempotencyStore:
        return self._idempotency  # type: ignore[return-value]

    @property
    def in_transaction(self) -> bool:
        """True between `__aenter__` and the matching commit or rollback."""
        return self._snapshot is not None

    async def __aenter__(self) -> Self:
        if self._snapshot is not None:
            raise RuntimeError("this unit of work is already in a transaction")
        self._snapshot = (
            self._runs.capture_state(),
            self._steps.capture_state(),
            self._ledger.capture_state(),
            self._idempotency.capture_state(),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Nothing is suppressed: a failure inside the block leaves the stores
        # as they were and the exception keeps going, which is the behaviour
        # a caller wrapping this in try/except will assume.
        if not self.in_transaction:
            # An explicit commit() or rollback() inside the block already
            # ended it, and that decision stands.
            return
        if exc_type is None:
            await self.commit()
        else:
            await self.rollback()

    async def commit(self) -> None:
        """Commit early, before the context exits.

        The writes are already in the stores, so this only discards the
        snapshot - which is what makes a later rollback unable to reach back
        into a transaction that already committed.

        Raises:
            RuntimeError: No transaction is open.
        """
        self._require_transaction()
        self._snapshot = None

    async def rollback(self) -> None:
        """Discard every write made in this transaction.

        Raises:
            RuntimeError: No transaction is open.
        """
        snapshot = self._require_transaction()
        runs, steps, ledger, idempotency = snapshot
        self._runs.restore_state(runs)
        self._steps.restore_state(steps)
        self._ledger.restore_state(ledger)
        self._idempotency.restore_state(idempotency)
        self._snapshot = None

    def _require_transaction(self) -> tuple[Any, Any, Any, Any]:
        if self._snapshot is None:
            raise RuntimeError("no transaction is open")
        return self._snapshot

    def __repr__(self) -> str:
        return f"InMemoryUnitOfWork(in_transaction={self.in_transaction})"
