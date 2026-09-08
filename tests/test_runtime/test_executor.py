"""Tests for the action executor.

Each of these is a way to move money twice, or to lose track of whether it
moved at all.
"""

from datetime import UTC, datetime

import pytest

from ledgerloop.adapters.clock import ManualClock
from ledgerloop.adapters.memory import (
    InMemoryIdempotencyStore,
    InMemoryLedgerStore,
    RecordingDispatcher,
)
from ledgerloop.core.enums import ActionKind, Currency, FailureClass, IdempotencyState
from ledgerloop.core.ids import ActionId, IdempotencyKey, RunId, TenantId
from ledgerloop.core.models import Action
from ledgerloop.core.money import Money
from ledgerloop.runtime import ActionExecutor

AT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(AT)


@pytest.fixture
def idempotency() -> InMemoryIdempotencyStore:
    return InMemoryIdempotencyStore()


@pytest.fixture
def ledger() -> InMemoryLedgerStore:
    return InMemoryLedgerStore()


@pytest.fixture
def dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture
def executor(idempotency, dispatcher, ledger, clock) -> ActionExecutor:
    return ActionExecutor(
        idempotency=idempotency, dispatcher=dispatcher, ledger=ledger, clock=clock
    )


@pytest.fixture
def tenant() -> TenantId:
    return TenantId.generate()


@pytest.fixture
def run_id() -> RunId:
    return RunId.generate()


def refund(amount: str = "1000") -> Action:
    money = Money.from_major(amount, Currency.INR)
    return Action(
        id=ActionId.generate(),
        kind=ActionKind.REFUND,
        description="Duplicate charge",
        amount=money,
        counterparty="mer_1",
        idempotency_key=IdempotencyKey.derive("refund", "ord_1", str(money.minor_units)),
    )


class TestHappyPath:
    async def test_action_is_dispatched_and_settled(
        self, executor, dispatcher, idempotency, tenant, run_id
    ):
        action = refund()
        outcome = await executor.execute(action, run_id=run_id, tenant_id=tenant)

        assert outcome.succeeded
        assert dispatcher.dispatch_count == 1

        record = await idempotency.get(action.idempotency_key, tenant)
        assert record.state is IdempotencyState.SUCCEEDED
        assert record.is_settled

    async def test_claim_records_the_run_and_action(
        self, executor, idempotency, tenant, run_id
    ):
        action = refund()
        await executor.execute(action, run_id=run_id, tenant_id=tenant)

        record = await idempotency.get(action.idempotency_key, tenant)
        assert record.run_id == run_id
        assert record.action_id == action.id

    async def test_the_ledger_records_dispatch_and_settlement(
        self, executor, ledger, tenant, run_id
    ):
        await executor.execute(refund(), run_id=run_id, tenant_id=tenant)

        events = [e.event_type.value for e in await ledger.read(tenant, run_id)]
        assert "action.dispatched" in events
        assert "action.settled" in events
        await ledger.verify_chain(tenant, run_id)

    async def test_the_amount_is_ledgered_in_minor_units(
        self, executor, ledger, tenant, run_id
    ):
        await executor.execute(refund("1000"), run_id=run_id, tenant_id=tenant)

        dispatched = next(
            e for e in await ledger.read(tenant, run_id) if e.event_type.value == "action.dispatched"
        )
        # Machines read this during reconciliation, not humans.
        assert dispatched.payload["amount_minor"] == 100000
        assert dispatched.payload["currency"] == "INR"


class TestNoDoubleSpend:
    async def test_running_the_same_action_twice_dispatches_once(
        self, executor, dispatcher, tenant, run_id
    ):
        action = refund()
        first = await executor.execute(action, run_id=run_id, tenant_id=tenant)
        second = await executor.execute(action, run_id=run_id, tenant_id=tenant)

        assert dispatcher.dispatch_count == 1
        assert first.succeeded
        assert second.replayed
        assert second.receipt.replayed

    async def test_a_new_proposal_of_the_same_effect_also_replays(
        self, executor, dispatcher, tenant, run_id
    ):
        # Different action id, same derived key - a duplicate upstream event.
        await executor.execute(refund("1000"), run_id=run_id, tenant_id=tenant)
        again = await executor.execute(refund("1000"), run_id=run_id, tenant_id=tenant)

        assert dispatcher.dispatch_count == 1
        assert again.replayed

    async def test_a_different_amount_is_a_different_effect(
        self, executor, dispatcher, tenant, run_id
    ):
        await executor.execute(refund("1000"), run_id=run_id, tenant_id=tenant)
        await executor.execute(refund("2000"), run_id=run_id, tenant_id=tenant)

        assert dispatcher.dispatch_count == 2

    async def test_replay_returns_the_original_provider_reference(
        self, executor, tenant, run_id
    ):
        action = refund()
        first = await executor.execute(action, run_id=run_id, tenant_id=tenant)
        second = await executor.execute(action, run_id=run_id, tenant_id=tenant)

        assert second.receipt.provider_reference == first.receipt.provider_reference


class TestFailures:
    async def test_provider_failure_settles_the_claim_as_failed(
        self, executor, dispatcher, idempotency, tenant, run_id
    ):
        dispatcher.fail_next(FailureClass.INVALID_REQUEST)
        action = refund()
        outcome = await executor.execute(action, run_id=run_id, tenant_id=tenant)

        assert not outcome.succeeded
        assert not outcome.indeterminate
        record = await idempotency.get(action.idempotency_key, tenant)
        assert record.state is IdempotencyState.FAILED

    async def test_a_settled_failure_is_not_retried(
        self, executor, dispatcher, tenant, run_id
    ):
        dispatcher.fail_next(FailureClass.INVALID_REQUEST)
        action = refund()
        await executor.execute(action, run_id=run_id, tenant_id=tenant)

        second = await executor.execute(action, run_id=run_id, tenant_id=tenant)
        assert second.replayed
        assert dispatcher.dispatch_count == 0

    async def test_failure_is_ledgered(self, executor, dispatcher, ledger, tenant, run_id):
        dispatcher.fail_next(FailureClass.TRANSIENT)
        await executor.execute(refund(), run_id=run_id, tenant_id=tenant)

        events = [e.event_type.value for e in await ledger.read(tenant, run_id)]
        assert "action.failed" in events


class TestIndeterminate:
    async def test_the_claim_is_left_in_flight(
        self, executor, dispatcher, idempotency, tenant, run_id
    ):
        # The dangerous case: the request went out, the answer did not come back.
        dispatcher.fail_next(FailureClass.INDETERMINATE)
        action = refund()
        outcome = await executor.execute(action, run_id=run_id, tenant_id=tenant)

        assert outcome.indeterminate
        assert outcome.needs_reconciliation
        record = await idempotency.get(action.idempotency_key, tenant)
        assert record.state is IdempotencyState.IN_FLIGHT
        assert not record.is_settled

    async def test_a_retry_after_indeterminate_does_not_dispatch_again(
        self, executor, dispatcher, tenant, run_id
    ):
        dispatcher.fail_next(FailureClass.INDETERMINATE)
        action = refund()
        await executor.execute(action, run_id=run_id, tenant_id=tenant)
        before = dispatcher.dispatch_count

        # Naively retrying is exactly how you pay twice. The claim is still
        # in flight, so the second call must not reach the provider again.
        await executor.execute(action, run_id=run_id, tenant_id=tenant)
        assert dispatcher.dispatch_count == before

    async def test_it_shows_up_for_reconciliation(
        self, executor, dispatcher, idempotency, tenant, run_id, clock
    ):
        from datetime import timedelta

        dispatcher.fail_next(FailureClass.INDETERMINATE)
        await executor.execute(refund(), run_id=run_id, tenant_id=tenant)

        cutoff = clock.now() + timedelta(hours=1)
        stale = [r async for r in idempotency.find_in_flight(older_than=cutoff)]
        assert len(stale) == 1


class TestReadOnlyActions:
    async def test_no_claim_is_taken(self, executor, idempotency, tenant, run_id):
        action = Action(
            id=ActionId.generate(), kind=ActionKind.READ, description="Fetch the batch"
        )
        outcome = await executor.execute(action, run_id=run_id, tenant_id=tenant)

        assert outcome.succeeded
        assert idempotency.snapshot() == ()

    async def test_reads_can_run_repeatedly(self, executor, dispatcher, tenant, run_id):
        action = Action(
            id=ActionId.generate(), kind=ActionKind.READ, description="Fetch the batch"
        )
        await executor.execute(action, run_id=run_id, tenant_id=tenant)
        await executor.execute(action, run_id=run_id, tenant_id=tenant)

        assert dispatcher.dispatch_count == 2
