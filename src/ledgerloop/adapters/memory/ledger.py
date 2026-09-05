"""In-memory audit ledger.

Reference implementation of `LedgerStore`. The interesting part is not the
storage - it is that appends are serialized per run, so two concurrent
writers cannot both read the same head and fork the chain into two entries
claiming the same predecessor.

There is deliberately no update and no delete. The chain is the evidence.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ledgerloop.core.errors import LedgerIntegrityError
from ledgerloop.core.ids import EntryId, RunId, TenantId
from ledgerloop.core.models import GENESIS_HASH, LedgerEntry

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from ledgerloop.core.enums import LedgerEventType

__all__ = ["InMemoryLedgerStore"]


class InMemoryLedgerStore:
    """Append-only ledger backed by per-run lists.

    Each run gets its own lock. Appends to different runs proceed in
    parallel; appends to the same run are serialized, which is what keeps
    sequence numbers dense and the chain single-threaded.
    """

    def __init__(self) -> None:
        self._chains: dict[tuple[str, str], list[LedgerEntry]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

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

        Sequence assignment and hashing happen under the run's lock, so the
        entry that is written is the entry that read the head.

        Returns:
            The sealed, stored entry.
        """
        key = (tenant_id.value, run_id.value)
        lock = await self._lock_for(key)

        async with lock:
            chain = self._chains.setdefault(key, [])
            previous_hash = chain[-1].entry_hash if chain else GENESIS_HASH
            entry = LedgerEntry(
                id=EntryId.generate(),
                run_id=run_id,
                tenant_id=tenant_id,
                sequence=len(chain),
                event_type=event_type,
                occurred_at=occurred_at,
                payload=payload,
                previous_hash=previous_hash,
            ).sealed()
            chain.append(entry)
            return entry

    async def read(self, tenant_id: TenantId, run_id: RunId) -> Sequence[LedgerEntry]:
        """Every entry for a run, in sequence order.

        Returns a copy: a caller must not be able to mutate the chain by
        holding a reference to it.
        """
        return tuple(self._chains.get((tenant_id.value, run_id.value), ()))

    async def verify_chain(self, tenant_id: TenantId, run_id: RunId) -> None:
        """Re-hash the run's entire chain and check every link.

        Verifies three separate properties, because a tampered chain can
        break any one of them independently: each entry's own hash matches
        its content, each entry names its predecessor's hash, and the
        sequence numbers are dense and ordered.

        Raises:
            LedgerIntegrityError: The chain does not verify.
        """
        chain = self._chains.get((tenant_id.value, run_id.value), [])
        expected_previous = GENESIS_HASH

        for position, entry in enumerate(chain):
            entry.verify()

            if entry.sequence != position:
                raise LedgerIntegrityError(
                    "Ledger sequence is not dense - an entry was removed or reordered",
                    context={
                        "run_id": str(run_id),
                        "position": position,
                        "sequence": entry.sequence,
                    },
                )

            if entry.previous_hash != expected_previous:
                raise LedgerIntegrityError(
                    "Ledger chain is broken - entry does not follow its predecessor",
                    context={
                        "run_id": str(run_id),
                        "sequence": entry.sequence,
                        "expected_previous": expected_previous,
                        "stored_previous": entry.previous_hash,
                    },
                )

            expected_previous = entry.entry_hash

    async def head(self, tenant_id: TenantId, run_id: RunId) -> LedgerEntry | None:
        """The most recent entry for a run, or None when the chain is empty."""
        chain = self._chains.get((tenant_id.value, run_id.value))
        return chain[-1] if chain else None

    async def _lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        """Fetch or create the per-run lock.

        Guarded by its own lock: two coroutines racing to append the first
        entry for a run must not each create a different lock object and
        then both proceed.
        """
        async with self._registry_lock:
            return self._locks.setdefault(key, asyncio.Lock())
