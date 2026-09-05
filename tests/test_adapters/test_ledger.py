"""Tests for the append-only audit ledger."""

import asyncio
from datetime import UTC, datetime

import pytest

from ledgerloop.adapters.memory import InMemoryLedgerStore
from ledgerloop.core.enums import LedgerEventType
from ledgerloop.core.errors import LedgerIntegrityError
from ledgerloop.core.ids import RunId, TenantId
from ledgerloop.core.models import GENESIS_HASH

AT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def store() -> InMemoryLedgerStore:
    return InMemoryLedgerStore()


@pytest.fixture
def tenant() -> TenantId:
    return TenantId.generate()


@pytest.fixture
def run_id() -> RunId:
    return RunId.generate()


class TestAppend:
    async def test_first_entry_chains_to_genesis(self, store, tenant, run_id):
        entry = await store.append(
            run_id, tenant, LedgerEventType.RUN_CREATED, {}, occurred_at=AT
        )
        assert entry.sequence == 0
        assert entry.previous_hash == GENESIS_HASH
        assert entry.entry_hash

    async def test_entries_chain_to_their_predecessor(self, store, tenant, run_id):
        first = await store.append(
            run_id, tenant, LedgerEventType.RUN_CREATED, {}, occurred_at=AT
        )
        second = await store.append(
            run_id, tenant, LedgerEventType.RUN_STARTED, {}, occurred_at=AT
        )
        assert second.sequence == 1
        assert second.previous_hash == first.entry_hash

    async def test_concurrent_appends_do_not_fork_the_chain(self, store, tenant, run_id):
        await asyncio.gather(
            *(
                store.append(
                    run_id, tenant, LedgerEventType.STEP_STARTED, {"i": i}, occurred_at=AT
                )
                for i in range(50)
            )
        )
        entries = await store.read(tenant, run_id)
        # Dense, unique sequence numbers and a single unbroken chain.
        assert [e.sequence for e in entries] == list(range(50))
        await store.verify_chain(tenant, run_id)

    async def test_runs_have_independent_chains(self, store, tenant):
        a, b = RunId.generate(), RunId.generate()
        await store.append(a, tenant, LedgerEventType.RUN_CREATED, {}, occurred_at=AT)
        entry_b = await store.append(b, tenant, LedgerEventType.RUN_CREATED, {}, occurred_at=AT)
        assert entry_b.sequence == 0
        assert entry_b.previous_hash == GENESIS_HASH


class TestRead:
    async def test_unknown_run_reads_empty(self, store, tenant, run_id):
        assert await store.read(tenant, run_id) == ()

    async def test_read_returns_a_copy(self, store, tenant, run_id):
        await store.append(run_id, tenant, LedgerEventType.RUN_CREATED, {}, occurred_at=AT)
        first = await store.read(tenant, run_id)
        # A caller must not be able to mutate the chain through its reference.
        assert isinstance(first, tuple)

    async def test_head_returns_the_latest_entry(self, store, tenant, run_id):
        await store.append(run_id, tenant, LedgerEventType.RUN_CREATED, {}, occurred_at=AT)
        latest = await store.append(
            run_id, tenant, LedgerEventType.RUN_STARTED, {}, occurred_at=AT
        )
        assert (await store.head(tenant, run_id)) == latest

    async def test_head_of_an_empty_chain_is_none(self, store, tenant, run_id):
        assert await store.head(tenant, run_id) is None

    async def test_tenants_are_isolated(self, store, run_id):
        a, b = TenantId.generate(), TenantId.generate()
        await store.append(run_id, a, LedgerEventType.RUN_CREATED, {}, occurred_at=AT)
        assert await store.read(b, run_id) == ()


class TestVerification:
    async def test_a_clean_chain_verifies(self, store, tenant, run_id):
        for event in (
            LedgerEventType.RUN_CREATED,
            LedgerEventType.RUN_STARTED,
            LedgerEventType.ACTION_DISPATCHED,
        ):
            await store.append(run_id, tenant, event, {"x": 1}, occurred_at=AT)
        await store.verify_chain(tenant, run_id)

    async def test_an_empty_chain_verifies(self, store, tenant, run_id):
        await store.verify_chain(tenant, run_id)

    async def test_a_removed_entry_is_detected(self, store, tenant, run_id):
        for i in range(3):
            await store.append(
                run_id, tenant, LedgerEventType.STEP_STARTED, {"i": i}, occurred_at=AT
            )
        # Reach past the interface, the way someone with database access would.
        store._chains[(tenant.value, run_id.value)].pop(1)

        with pytest.raises(LedgerIntegrityError):
            await store.verify_chain(tenant, run_id)

    async def test_a_reordered_chain_is_detected(self, store, tenant, run_id):
        for i in range(3):
            await store.append(
                run_id, tenant, LedgerEventType.STEP_STARTED, {"i": i}, occurred_at=AT
            )
        chain = store._chains[(tenant.value, run_id.value)]
        chain[1], chain[2] = chain[2], chain[1]

        with pytest.raises(LedgerIntegrityError):
            await store.verify_chain(tenant, run_id)

    async def test_an_edited_payload_is_detected(self, store, tenant, run_id):
        from dataclasses import replace

        await store.append(
            run_id, tenant, LedgerEventType.ACTION_DISPATCHED, {"amount": 100}, occurred_at=AT
        )
        chain = store._chains[(tenant.value, run_id.value)]
        chain[0] = replace(chain[0], payload={"amount": 1_000_000})

        with pytest.raises(LedgerIntegrityError, match="does not match"):
            await store.verify_chain(tenant, run_id)
