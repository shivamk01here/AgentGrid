"""In-memory idempotency store.

Reference implementation of `IdempotencyStore` - the exactly-once boundary.

The contract is two-phase. `claim` takes the key before the effect is
dispatched; `settle` records the outcome after. The window between them is
the dangerous one, and it is deliberately visible in the data: a record left
`IN_FLIGHT` by a crash means nobody knows whether the money moved, and the
only correct response is to ask the provider. Retrying is how you pay twice.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime

from ledgerloop.core.enums import IdempotencyState
from ledgerloop.core.errors import IdempotencyConflictError, StateTransitionError
from ledgerloop.core.ids import IdempotencyKey, TenantId
from ledgerloop.core.models import ActionReceipt, IdempotencyRecord

__all__ = ["InMemoryIdempotencyStore"]


class InMemoryIdempotencyStore:
    """Idempotency records backed by a dictionary.

    A single lock serializes every claim. In a real adapter this is an
    `INSERT ... ON CONFLICT DO NOTHING` returning the existing row - the
    atomicity is the entire feature, so the reference implementation makes it
    unmissable rather than clever.
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], IdempotencyRecord] = {}
        self._lock = asyncio.Lock()

    async def claim(
        self,
        key: IdempotencyKey,
        tenant_id: TenantId,
        action_fingerprint: str,
        *,
        at: datetime,
    ) -> IdempotencyRecord:
        """Atomically claim `key`, or return the claim that already exists.

        A returned record in state `SUCCEEDED` carries the original receipt.
        The caller must replay that receipt rather than dispatch again - that
        replay is what makes a retried request safe.

        Returns:
            The new claim, or the pre-existing record.

        Raises:
            IdempotencyConflictError: The key exists under a different
                fingerprint. Two different effects derived the same key,
                which is a correctness bug and must not be papered over.
        """
        storage_key = (tenant_id.value, key.value)

        async with self._lock:
            existing = self._records.get(storage_key)
            if existing is not None:
                if existing.action_fingerprint != action_fingerprint:
                    raise IdempotencyConflictError(
                        key, existing.action_fingerprint, action_fingerprint
                    )
                return existing

            record = IdempotencyRecord(
                key=key,
                tenant_id=tenant_id,
                action_fingerprint=action_fingerprint,
                state=IdempotencyState.IN_FLIGHT,
                claimed_at=at,
            )
            self._records[storage_key] = record
            return record

    async def settle(
        self,
        key: IdempotencyKey,
        tenant_id: TenantId,
        receipt: ActionReceipt,
        *,
        at: datetime,
    ) -> IdempotencyRecord:
        """Record the outcome of a dispatched effect.

        Raises:
            StateTransitionError: The key was never claimed, or was already
                settled. Settling twice would silently overwrite the first
                outcome, and the first outcome is the true one.
        """
        storage_key = (tenant_id.value, key.value)

        async with self._lock:
            existing = self._records.get(storage_key)
            if existing is None:
                raise StateTransitionError("IdempotencyRecord", "absent", receipt.state.value)
            if existing.is_settled:
                raise StateTransitionError(
                    "IdempotencyRecord", existing.state.value, receipt.state.value
                )

            settled = replace(
                existing,
                state=receipt.state,
                settled_at=at,
                receipt=receipt,
            )
            self._records[storage_key] = settled
            return settled

    async def get(self, key: IdempotencyKey, tenant_id: TenantId) -> IdempotencyRecord | None:
        """Look up a claim, or None when the key was never claimed."""
        return self._records.get((tenant_id.value, key.value))

    async def find_in_flight(
        self, *, older_than: datetime, limit: int = 100
    ) -> AsyncIterator[IdempotencyRecord]:
        """Stream stale claims awaiting reconciliation, oldest first.

        Anything still `IN_FLIGHT` past a provider's settlement window is an
        effect whose fate nobody knows. Feeding this to a reconciler is
        mandatory, not optional - an unattended in-flight record is either a
        payment that silently did not happen or one that happened twice.

        Intentionally cross-tenant: reconciliation is an operator concern.
        """
        stale = sorted(
            (
                record
                for record in self._records.values()
                if record.state is IdempotencyState.IN_FLIGHT
                and record.claimed_at < older_than
            ),
            key=lambda record: record.claimed_at,
        )
        for record in stale[:limit]:
            yield record

    # ---- test helpers -----------------------------------------------------

    def snapshot(self) -> tuple[IdempotencyRecord, ...]:
        """Every stored record. For assertions, not for production reads."""
        return tuple(self._records.values())
