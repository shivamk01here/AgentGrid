"""Reconciliation of in-flight claims.

An `IN_FLIGHT` idempotency record is an open question: the request reached
the provider, and then we lost the answer. Somebody has to go and ask.

This is the part most systems skip, and it is why they double-pay. The
executor deliberately refuses to guess; the reconciler is where the guess is
replaced with a lookup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ledgerloop.core.enums import IdempotencyState, LedgerEventType
from ledgerloop.core.errors import LedgerloopError
from ledgerloop.core.models import ActionReceipt

if TYPE_CHECKING:
    from ledgerloop.core.ids import IdempotencyKey, TenantId
    from ledgerloop.core.models import IdempotencyRecord
    from ledgerloop.core.ports import Clock, IdempotencyStore, LedgerStore

logger = logging.getLogger(__name__)

__all__ = ["ProviderLookup", "ReconciliationReport", "Reconciler"]


@runtime_checkable
class ProviderLookup(Protocol):
    """Asks a provider what actually happened to an idempotency key.

    Every payment provider worth using exposes this - a lookup by the
    idempotency key you sent them. If one does not, you cannot safely run
    agents against it, and that is a procurement problem rather than a
    software one.
    """

    async def lookup(self, key: IdempotencyKey, tenant_id: TenantId) -> ActionReceipt | None:
        """Return the effect's outcome, or None if the provider never saw it.

        None is a definitive answer, not an error: it means the request never
        landed and the effect did not happen.
        """
        ...


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """What one reconciliation sweep resolved."""

    checked: int = 0
    confirmed: int = 0
    """Provider says it happened. Claim settled as succeeded."""
    failed: int = 0
    """Provider says it failed. Claim settled as failed."""
    not_found: int = 0
    """Provider never saw it. Claim settled as failed."""
    unresolved: int = 0
    """Lookup itself failed. Left in flight, will be retried next sweep."""

    @property
    def resolved(self) -> int:
        return self.confirmed + self.failed + self.not_found

    def __str__(self) -> str:
        return (
            f"checked={self.checked} confirmed={self.confirmed} "
            f"failed={self.failed} not_found={self.not_found} "
            f"unresolved={self.unresolved}"
        )


class Reconciler:
    """Resolves stale in-flight claims against the provider.

    Run it on a schedule. `grace` should exceed the provider's own settlement
    window - sweeping too eagerly means asking about requests that are simply
    still in progress, and getting told "not found" for something that lands
    a second later.

    Example:
        reconciler = Reconciler(
            idempotency=store, lookup=psp_lookup, ledger=ledger, clock=clock,
        )
        report = await reconciler.sweep()
    """

    def __init__(
        self,
        *,
        idempotency: IdempotencyStore,
        lookup: ProviderLookup,
        ledger: LedgerStore,
        clock: Clock,
        grace: timedelta = timedelta(minutes=15),
    ) -> None:
        if grace < timedelta(0):
            raise ValueError("grace cannot be negative")
        self._idempotency = idempotency
        self._lookup = lookup
        self._ledger = ledger
        self._clock = clock
        self._grace = grace

    async def sweep(self, *, limit: int = 100) -> ReconciliationReport:
        """Resolve every claim that has been in flight longer than the grace.

        Returns:
            Counts of what was resolved. An `unresolved` count that never
            falls is an alert, not a statistic.
        """
        cutoff = self._clock.now() - self._grace
        checked = confirmed = failed = not_found = unresolved = 0

        async for record in self._idempotency.find_in_flight(older_than=cutoff, limit=limit):
            checked += 1
            outcome = await self._resolve(record)
            if outcome == "unresolved":
                unresolved += 1
            elif outcome == "confirmed":
                confirmed += 1
            elif outcome == "failed":
                failed += 1
            elif outcome == "not_found":
                not_found += 1

        report = ReconciliationReport(
            checked=checked,
            confirmed=confirmed,
            failed=failed,
            not_found=not_found,
            unresolved=unresolved,
        )
        if checked:
            logger.info("Reconciliation sweep complete: %s", report)
        return report

    async def _resolve(self, record: IdempotencyRecord) -> str:
        """Resolve one record.

        Returns:
            A string indicating the resolution: "confirmed", "failed", 
            "not_found", or "unresolved".
        """
        try:
            receipt = await self._lookup.lookup(record.key, record.tenant_id)
        except Exception as exc:
            logger.warning("Lookup failed for %s: %s", record.key, exc)
            return "unresolved"

        if receipt is None:
            if record.action_id is None:  # pragma: no cover - claims carry it
                logger.warning("Claim %s has no action id; cannot settle it", record.key)
                return "unresolved"
            settled = ActionReceipt(
                action_id=record.action_id,
                state=IdempotencyState.FAILED,
                failure_reason="Provider has no record of this request",
                settled_at=self._clock.now(),
            )
            await self._settle(record, settled, LedgerEventType.ACTION_FAILED)
            return "not_found"

        if receipt.succeeded:
            await self._settle(record, receipt, LedgerEventType.ACTION_SETTLED)
            return "confirmed"

        # The provider found the request and told us it failed. That is still
        # a resolution - the claim settles - but it is not a confirmation,
        # and it must not be ledgered as one.
        await self._settle(record, receipt, LedgerEventType.ACTION_FAILED)
        return "failed"

    async def _settle(
        self,
        record: IdempotencyRecord,
        receipt: ActionReceipt,
        event: LedgerEventType,
    ) -> None:
        """Settle the claim and write the resolution to the ledger."""
        try:
            await self._idempotency.settle(
                record.key, record.tenant_id, receipt, at=self._clock.now()
            )
        except LedgerloopError:
            # Another sweep, or a late callback, settled it first. That is a
            # benign race - the claim is resolved either way.
            logger.info("Claim %s was already settled by someone else", record.key)
            return

        if record.run_id is None:
            # Nothing to attach the entry to. The claim is still correctly
            # settled - we just cannot file the paperwork.
            logger.warning("Claim %s has no run id; reconciliation not ledgered", record.key)
            return

        try:
            await self._ledger.append(
                record.run_id,
                record.tenant_id,
                event,
                {
                    "idempotency_key": str(record.key),
                    "action_id": None if record.action_id is None else str(record.action_id),
                    "reconciled": True,
                    "state": receipt.state.value,
                    "provider_reference": receipt.provider_reference,
                },
                occurred_at=self._clock.now(),
            )
        except Exception:
            logger.exception("Failed to ledger reconciliation of %s", record.key)
