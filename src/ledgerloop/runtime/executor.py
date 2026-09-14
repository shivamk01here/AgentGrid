"""Action execution.

This is the piece that makes "exactly once" true rather than aspirational.
Every value-moving action goes through here, and the order of operations is
the entire guarantee:

    claim -> (replay if settled, stop if held) -> dispatch -> settle

The claim is durable before the dispatch leaves the process. If we die
between the two, the claim survives as `IN_FLIGHT`, which is a standing
instruction to reconcile - not to retry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ledgerloop.core.enums import IdempotencyState, LedgerEventType
from ledgerloop.core.errors import IndeterminateError, LedgerloopError, ProviderError
from ledgerloop.core.models import Action, ActionReceipt

if TYPE_CHECKING:
    from ledgerloop.core.ids import RunId, TenantId
    from ledgerloop.core.ports import ActionDispatcher, Clock, IdempotencyStore, LedgerStore

logger = logging.getLogger(__name__)

__all__ = ["ActionExecutor", "ExecutionOutcome"]


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """What happened when an action was executed."""

    receipt: ActionReceipt | None
    replayed: bool = False
    """True when an existing settled claim was returned instead of dispatching."""
    indeterminate: bool = False
    """True when the effect may or may not have landed. Needs reconciliation."""
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.receipt is not None and self.receipt.succeeded

    @property
    def needs_reconciliation(self) -> bool:
        return self.indeterminate


class ActionExecutor:
    """Executes actions exactly once, and writes down everything it did.

    Example:
        executor = ActionExecutor(
            idempotency=store, dispatcher=psp, ledger=ledger, clock=clock,
        )
        outcome = await executor.execute(action, run_id=run.id, tenant_id=tenant)
    """

    def __init__(
        self,
        *,
        idempotency: IdempotencyStore,
        dispatcher: ActionDispatcher,
        ledger: LedgerStore,
        clock: Clock,
    ) -> None:
        self._idempotency = idempotency
        self._dispatcher = dispatcher
        self._ledger = ledger
        self._clock = clock

    async def execute(
        self, action: Action, *, run_id: RunId, tenant_id: TenantId
    ) -> ExecutionOutcome:
        """Run one action, exactly once.

        Read-only actions skip the claim entirely - there is nothing to be
        idempotent about, and burning a claim record on every lookup would
        bloat the store for no benefit.

        Args:
            action: The action to execute. Already approved by policy.
            run_id: The run this belongs to, for the ledger.
            tenant_id: Owning tenant.

        Returns:
            The outcome. Check `needs_reconciliation` before doing anything
            else with a failed one.
        """
        if not action.kind.moves_value:
            return await self._execute_unclaimed(action, run_id=run_id, tenant_id=tenant_id)

        if action.idempotency_key is None:  # pragma: no cover - Action forbids this
            raise LedgerloopError(
                f"Value-moving action {action.id} has no idempotency key",
                context={"action_id": str(action.id)},
            )

        fingerprint = action.fingerprint()

        # 1. Claim. Durable before anything leaves the process.
        record = await self._idempotency.claim(
            action.idempotency_key,
            tenant_id,
            fingerprint,
            at=self._clock.now(),
            action_id=action.id,
            run_id=run_id,
        )

        # 2. Already settled? Replay the original outcome. This is what makes
        #    a retried request safe rather than a second payment.
        if record.is_settled:
            logger.info(
                "Replaying settled action key=%s state=%s",
                action.idempotency_key,
                record.state.value,
            )
            await self._write(
                run_id,
                tenant_id,
                LedgerEventType.ACTION_SETTLED,
                {
                    "action_id": str(action.id),
                    "replayed": True,
                    "state": record.state.value,
                },
            )
            receipt = record.receipt
            return ExecutionOutcome(
                receipt=None if receipt is None else _mark_replayed(receipt),
                replayed=True,
                error=None if record.state is IdempotencyState.SUCCEEDED else "previously failed",
            )

        # 3. Held, but not by us. Another worker took this claim, or an earlier
        #    attempt at the same effect dispatched and never heard back. The
        #    outcome is unknown either way, and dispatching again is precisely
        #    the second payment the claim was taken to prevent.
        if not record.newly_claimed:
            logger.error(
                "Action %s is already in flight under key=%s - not dispatching it again",
                action.id,
                action.idempotency_key,
            )
            await self._write(
                run_id,
                tenant_id,
                LedgerEventType.ACTION_FAILED,
                {
                    "action_id": str(action.id),
                    "indeterminate": True,
                    "in_flight": True,
                    "error": "already in flight under this idempotency key",
                },
            )
            return ExecutionOutcome(
                receipt=None,
                indeterminate=True,
                error="Already in flight under this idempotency key - reconcile it first",
            )

        await self._write(
            run_id,
            tenant_id,
            LedgerEventType.ACTION_DISPATCHED,
            _describe(action),
        )

        # 4. Dispatch.
        try:
            receipt = await self._dispatcher.dispatch(action, at=self._clock.now())
        except IndeterminateError as exc:
            # Deliberately leave the claim IN_FLIGHT. Settling it either way
            # would be a guess, and both guesses are wrong half the time.
            logger.error(
                "Action %s is indeterminate - leaving claim in flight for reconciliation",
                action.id,
            )
            await self._write(
                run_id,
                tenant_id,
                LedgerEventType.ACTION_FAILED,
                {
                    "action_id": str(action.id),
                    "indeterminate": True,
                    "error": str(exc),
                },
            )
            return ExecutionOutcome(receipt=None, indeterminate=True, error=str(exc))
        except ProviderError as exc:
            settled = await self._settle_failure(action, tenant_id, str(exc))
            await self._write(
                run_id,
                tenant_id,
                LedgerEventType.ACTION_FAILED,
                {
                    "action_id": str(action.id),
                    "error": str(exc),
                    "failure_class": exc.failure_class.value,
                },
            )
            return ExecutionOutcome(receipt=settled, error=str(exc))

        # 5. Settle.
        await self._idempotency.settle(
            action.idempotency_key, tenant_id, receipt, at=self._clock.now()
        )
        await self._write(
            run_id,
            tenant_id,
            LedgerEventType.ACTION_SETTLED,
            {
                "action_id": str(action.id),
                "provider_reference": receipt.provider_reference,
                "replayed": False,
            },
        )
        return ExecutionOutcome(receipt=receipt)

    async def _execute_unclaimed(
        self, action: Action, *, run_id: RunId, tenant_id: TenantId
    ) -> ExecutionOutcome:
        """Dispatch an action that moves no value, without a claim."""
        await self._write(
            run_id,
            tenant_id,
            LedgerEventType.ACTION_DISPATCHED,
            _describe(action),
        )
        try:
            receipt = await self._dispatcher.dispatch(action, at=self._clock.now())
        except LedgerloopError as exc:
            await self._write(
                run_id,
                tenant_id,
                LedgerEventType.ACTION_FAILED,
                {"action_id": str(action.id), "error": str(exc)},
            )
            return ExecutionOutcome(receipt=None, error=str(exc))

        await self._write(
            run_id,
            tenant_id,
            LedgerEventType.ACTION_SETTLED,
            {"action_id": str(action.id), "provider_reference": receipt.provider_reference},
        )
        return ExecutionOutcome(receipt=receipt)

    async def _settle_failure(
        self, action: Action, tenant_id: TenantId, reason: str
    ) -> ActionReceipt:
        """Record a definitive provider failure against the claim."""
        receipt = ActionReceipt(
            action_id=action.id,
            state=IdempotencyState.FAILED,
            failure_reason=reason,
            settled_at=self._clock.now(),
        )
        assert action.idempotency_key is not None  # guaranteed by the caller
        await self._idempotency.settle(
            action.idempotency_key, tenant_id, receipt, at=self._clock.now()
        )
        return receipt

    async def _write(
        self,
        run_id: RunId,
        tenant_id: TenantId,
        event: LedgerEventType,
        payload: dict[str, object],
    ) -> None:
        """Append to the audit ledger.

        A ledger write failing is serious, but it must not mask the outcome
        of the action itself - the caller needs to know what happened to the
        money before it needs to know the audit trail is broken.
        """
        try:
            await self._ledger.append(
                run_id, tenant_id, event, dict(payload), occurred_at=self._clock.now()
            )
        except Exception:
            logger.exception("Ledger write failed for %s on run %s", event.value, run_id)


def _describe(action: Action) -> dict[str, object]:
    """Summarize an action for the ledger.

    Amount is recorded in minor units plus currency, never as a formatted
    string - the ledger is read by machines during reconciliation.
    """
    return {
        "action_id": str(action.id),
        "kind": action.kind.value,
        "description": action.description,
        "counterparty": action.counterparty,
        "amount_minor": None if action.amount is None else action.amount.minor_units,
        "currency": None if action.amount is None else action.amount.currency.value,
        "idempotency_key": None
        if action.idempotency_key is None
        else str(action.idempotency_key),
        "fingerprint": action.fingerprint(),
    }


def _mark_replayed(receipt: ActionReceipt) -> ActionReceipt:
    """Flag a receipt as served from an existing claim."""
    return replace(receipt, replayed=True)
