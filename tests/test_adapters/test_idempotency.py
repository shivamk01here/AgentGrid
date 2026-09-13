"""Tests for the idempotency store.

Every test here describes a way to pay twice.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from ledgerloop.adapters.memory import InMemoryIdempotencyStore
from ledgerloop.core.enums import IdempotencyState
from ledgerloop.core.errors import IdempotencyConflictError, StateTransitionError
from ledgerloop.core.ids import ActionId, IdempotencyKey, TenantId
from ledgerloop.core.models import ActionReceipt

AT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def store() -> InMemoryIdempotencyStore:
    return InMemoryIdempotencyStore()


@pytest.fixture
def tenant() -> TenantId:
    return TenantId.generate()


@pytest.fixture
def key() -> IdempotencyKey:
    return IdempotencyKey.derive("refund", "ord_1", "125050")


def _receipt(state: IdempotencyState = IdempotencyState.SUCCEEDED) -> ActionReceipt:
    return ActionReceipt(
        action_id=ActionId.generate(),
        state=state,
        provider_reference="psp_ref_1",
        settled_at=AT,
    )


class TestClaim:
    async def test_first_claim_is_in_flight(self, store, tenant, key):
        record = await store.claim(key, tenant, "fp-1", at=AT)
        assert record.state is IdempotencyState.IN_FLIGHT
        assert not record.is_settled

    async def test_second_claim_returns_the_same_record(self, store, tenant, key):
        first = await store.claim(key, tenant, "fp-1", at=AT)
        second = await store.claim(key, tenant, "fp-1", at=AT + timedelta(seconds=5))
        assert second.claimed_at == first.claimed_at

    async def test_the_claim_that_creates_the_record_is_told_so(self, store, tenant, key):
        record = await store.claim(key, tenant, "fp-1", at=AT)
        assert record.newly_claimed

    async def test_a_second_claim_is_not_told_it_took_the_first(self, store, tenant, key):
        # Same key, same fingerprint, same state. Without the flag this looks
        # exactly like a claim the caller just took, and gets dispatched.
        await store.claim(key, tenant, "fp-1", at=AT)
        second = await store.claim(key, tenant, "fp-1", at=AT)
        assert second.state is IdempotencyState.IN_FLIGHT
        assert not second.newly_claimed

    async def test_nothing_read_back_later_claims_to_be_new(self, store, tenant, key):
        await store.claim(key, tenant, "fp-1", at=AT)

        fetched = await store.get(key, tenant)
        swept = [r async for r in store.find_in_flight(older_than=AT + timedelta(days=1))]

        assert not fetched.newly_claimed
        assert not any(r.newly_claimed for r in swept)

    async def test_settled_claim_replays_the_original_receipt(self, store, tenant, key):
        await store.claim(key, tenant, "fp-1", at=AT)
        receipt = _receipt()
        await store.settle(key, tenant, receipt, at=AT)

        replayed = await store.claim(key, tenant, "fp-1", at=AT + timedelta(minutes=1))
        assert replayed.state is IdempotencyState.SUCCEEDED
        assert replayed.receipt is not None
        assert replayed.receipt.provider_reference == "psp_ref_1"

    async def test_different_fingerprint_under_the_same_key_raises(self, store, tenant, key):
        await store.claim(key, tenant, "fp-1", at=AT)
        with pytest.raises(IdempotencyConflictError):
            await store.claim(key, tenant, "fp-DIFFERENT", at=AT)

    async def test_concurrent_claims_produce_exactly_one_winner(self, store, tenant, key):
        # The whole product rests on this being atomic.
        results = await asyncio.gather(
            *(store.claim(key, tenant, "fp-1", at=AT) for _ in range(50))
        )
        assert len({r.claimed_at for r in results}) == 1
        # ...and exactly one of the fifty was told it was the one.
        assert sum(r.newly_claimed for r in results) == 1
        assert len(store.snapshot()) == 1

    async def test_tenants_are_isolated(self, store, key):
        a, b = TenantId.generate(), TenantId.generate()
        await store.claim(key, a, "fp-a", at=AT)
        # The same key under another tenant is a different claim entirely,
        # and must not raise a conflict.
        record = await store.claim(key, b, "fp-b", at=AT)
        assert record.tenant_id == b
        assert len(store.snapshot()) == 2


class TestSettle:
    async def test_settle_records_the_receipt(self, store, tenant, key):
        await store.claim(key, tenant, "fp-1", at=AT)
        settled = await store.settle(key, tenant, _receipt(), at=AT)
        assert settled.state is IdempotencyState.SUCCEEDED
        assert settled.is_settled
        assert settled.settled_at == AT

    async def test_settling_an_unclaimed_key_raises(self, store, tenant, key):
        with pytest.raises(StateTransitionError):
            await store.settle(key, tenant, _receipt(), at=AT)

    async def test_settling_twice_raises(self, store, tenant, key):
        # The first outcome is the true one; overwriting it erases history.
        await store.claim(key, tenant, "fp-1", at=AT)
        await store.settle(key, tenant, _receipt(), at=AT)
        with pytest.raises(StateTransitionError):
            await store.settle(key, tenant, _receipt(IdempotencyState.FAILED), at=AT)

    async def test_failure_is_a_legitimate_settlement(self, store, tenant, key):
        await store.claim(key, tenant, "fp-1", at=AT)
        settled = await store.settle(key, tenant, _receipt(IdempotencyState.FAILED), at=AT)
        assert settled.state is IdempotencyState.FAILED
        assert settled.is_settled


class TestReconciliation:
    async def test_get_returns_none_for_an_unknown_key(self, store, tenant, key):
        assert await store.get(key, tenant) is None

    async def test_stale_in_flight_records_are_surfaced(self, store, tenant):
        old = IdempotencyKey.derive("refund", "old")
        recent = IdempotencyKey.derive("refund", "recent")
        await store.claim(old, tenant, "fp-old", at=AT)
        await store.claim(recent, tenant, "fp-recent", at=AT + timedelta(hours=2))

        cutoff = AT + timedelta(hours=1)
        found = [r async for r in store.find_in_flight(older_than=cutoff)]
        assert [r.key for r in found] == [old]

    async def test_settled_records_are_not_surfaced_for_reconciliation(self, store, tenant, key):
        await store.claim(key, tenant, "fp-1", at=AT)
        await store.settle(key, tenant, _receipt(), at=AT)

        found = [r async for r in store.find_in_flight(older_than=AT + timedelta(days=1))]
        assert found == []

    async def test_reconciliation_sweep_is_cross_tenant(self, store):
        a, b = TenantId.generate(), TenantId.generate()
        await store.claim(IdempotencyKey.derive("x"), a, "fp", at=AT)
        await store.claim(IdempotencyKey.derive("y"), b, "fp", at=AT)

        found = [r async for r in store.find_in_flight(older_than=AT + timedelta(days=1))]
        assert len(found) == 2
