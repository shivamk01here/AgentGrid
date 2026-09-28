"""Tests for the transactional unit of work.

The claim under test is the one the port makes: a run's state change and its
ledger entries land together, and if they cannot, neither does.
"""

from datetime import UTC, datetime, timedelta

import pytest

from ledgerloop.adapters.memory import (
    InMemoryIdempotencyStore,
    InMemoryLedgerStore,
    InMemoryRunStore,
    InMemoryStepStore,
    InMemoryUnitOfWork,
)
from ledgerloop.core.enums import LedgerEventType, RunState, StepOutcome
from ledgerloop.core.errors import ConcurrencyError
from ledgerloop.core.ids import IdempotencyKey, RunId, StepId, TenantId
from ledgerloop.core.models import RunSpec, Step
from ledgerloop.core.ports import UnitOfWork

AT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def runs() -> InMemoryRunStore:
    return InMemoryRunStore()


@pytest.fixture
def steps() -> InMemoryStepStore:
    return InMemoryStepStore()


@pytest.fixture
def ledger() -> InMemoryLedgerStore:
    return InMemoryLedgerStore()


@pytest.fixture
def idempotency() -> InMemoryIdempotencyStore:
    return InMemoryIdempotencyStore()


@pytest.fixture
def uow(runs, steps, ledger, idempotency) -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork(runs, steps, ledger, idempotency)


@pytest.fixture
def tenant() -> TenantId:
    return TenantId.generate()


async def _step(run_id: RunId, tenant_id: TenantId, index: int) -> Step:
    return Step(
        id=StepId.generate(),
        run_id=run_id,
        tenant_id=tenant_id,
        index=index,
        outcome=StepOutcome.COMPLETED,
        started_at=AT,
        ended_at=AT + timedelta(seconds=1),
    )


class TestShape:
    def test_it_satisfies_the_port(self, uow):
        assert isinstance(uow, UnitOfWork)

    def test_it_exposes_the_four_stores(self, uow, runs, steps, ledger, idempotency):
        assert uow.runs is runs
        assert uow.steps is steps
        assert uow.ledger is ledger
        assert uow.idempotency is idempotency

    def test_repr_says_whether_it_is_open(self, uow):
        assert "in_transaction=False" in repr(uow)


class TestCommit:
    async def test_writes_are_readable_inside_the_block(self, uow, runs, ledger, tenant):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="reconcile"))

        async with uow:
            await uow.ledger.append(
                run.id, tenant, LedgerEventType.RUN_CREATED, {}, occurred_at=AT
            )
            # A session reads its own uncommitted writes, and so does this.
            assert len(await uow.ledger.read(tenant, run.id)) == 1

        assert len(await ledger.read(tenant, run.id)) == 1

    async def test_commit_keeps_every_store(self, uow, runs, steps, ledger, idempotency, tenant):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="reconcile"))

        async with uow:
            await uow.runs.save(run.start(at=AT), expected_version=0)
            await uow.steps.append(await _step(run.id, tenant, 1))
            await uow.ledger.append(
                run.id, tenant, LedgerEventType.RUN_STARTED, {}, occurred_at=AT
            )
            await uow.idempotency.claim(
                IdempotencyKey.generate(), tenant, "fp-1", at=AT, run_id=run.id
            )

        assert (await runs.get(tenant, run.id)).state is RunState.RUNNING
        assert len(await steps.list_for_run(tenant, run.id)) == 1
        assert len(await ledger.read(tenant, run.id)) == 1
        assert len(idempotency.snapshot()) == 1

    async def test_commit_before_the_block_ends(self, uow, runs, ledger, tenant):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="reconcile"))

        async with uow:
            await uow.ledger.append(
                run.id, tenant, LedgerEventType.RUN_CREATED, {}, occurred_at=AT
            )
            await uow.commit()
            # A rollback after an explicit commit must not reach back into it.
            await uow.ledger.append(
                run.id, tenant, LedgerEventType.RUN_STARTED, {}, occurred_at=AT
            )

        assert len(await ledger.read(tenant, run.id)) == 2

    async def test_the_chain_still_verifies_after_a_commit(self, uow, runs, ledger, tenant):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="reconcile"))

        async with uow:
            await uow.ledger.append(
                run.id, tenant, LedgerEventType.RUN_CREATED, {}, occurred_at=AT
            )
            await uow.ledger.append(
                run.id, tenant, LedgerEventType.RUN_STARTED, {"i": 1}, occurred_at=AT
            )

        await ledger.verify_chain(tenant, run.id)


class TestRollback:
    async def test_a_failure_undoes_the_ledger(self, uow, runs, ledger, tenant):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="reconcile"))

        with pytest.raises(RuntimeError, match="provider exploded"):
            async with uow:
                await uow.ledger.append(
                    run.id, tenant, LedgerEventType.RUN_STARTED, {}, occurred_at=AT
                )
                raise RuntimeError("provider exploded")

        assert await ledger.read(tenant, run.id) == ()

    async def test_a_failure_undoes_every_store_together(self, uow, runs, steps, ledger, idempotency, tenant):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="reconcile"))

        with pytest.raises(RuntimeError):
            async with uow:
                await uow.runs.save(run.start(at=AT), expected_version=0)
                await uow.steps.append(await _step(run.id, tenant, 1))
                await uow.ledger.append(
                    run.id, tenant, LedgerEventType.RUN_STARTED, {}, occurred_at=AT
                )
                await uow.idempotency.claim(
                    IdempotencyKey.generate(), tenant, "fp-1", at=AT, run_id=run.id
                )
                raise RuntimeError("half the work landed")

        # The run did not advance, and the ledger does not claim it did.
        assert (await runs.get(tenant, run.id)).state is RunState.PENDING
        assert await steps.list_for_run(tenant, run.id) == []
        assert await ledger.read(tenant, run.id) == ()
        assert idempotency.snapshot() == ()

    async def test_a_rolled_back_run_can_be_written_again(self, uow, runs, tenant):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="reconcile"))

        with pytest.raises(RuntimeError):
            async with uow:
                await uow.runs.save(run.start(at=AT), expected_version=0)
                raise RuntimeError("abandoned")

        # The version is back where it was, so the same transition is legal
        # again rather than failing on a version nothing actually reached.
        await runs.save(run.start(at=AT), expected_version=0)
        assert (await runs.get(tenant, run.id)).state is RunState.RUNNING

    async def test_a_rolled_back_run_is_never_seen_by_another_reader(self, uow, runs, tenant):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="reconcile"))

        with pytest.raises(RuntimeError):
            async with uow:
                await uow.runs.save(run.start(at=AT), expected_version=0)
                # A concurrent reader inside the transaction sees the write...
                assert (await runs.get(tenant, run.id)).state is RunState.RUNNING
                raise RuntimeError("abandoned")

        # ...and after the rollback it does not.
        assert (await runs.get(tenant, run.id)).state is RunState.PENDING

    async def test_a_rolled_back_ledger_entry_leaves_no_gap(self, uow, runs, ledger, tenant):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="reconcile"))
        await ledger.append(run.id, tenant, LedgerEventType.RUN_CREATED, {}, occurred_at=AT)

        with pytest.raises(RuntimeError):
            async with uow:
                await uow.ledger.append(
                    run.id, tenant, LedgerEventType.RUN_STARTED, {}, occurred_at=AT
                )
                raise RuntimeError("abandoned")

        # Sequence 1 was never used, so the next entry takes it. A dense chain
        # is what verify_chain insists on, and a hole would be indistinguishable
        # from a deleted row.
        next_entry = await ledger.append(
            run.id, tenant, LedgerEventType.RUN_STARTED, {}, occurred_at=AT
        )
        assert next_entry.sequence == 1
        await ledger.verify_chain(tenant, run.id)

    async def test_a_rolled_back_claim_frees_the_key(self, uow, idempotency, tenant):
        key = IdempotencyKey.generate()

        with pytest.raises(RuntimeError):
            async with uow:
                await uow.idempotency.claim(key, tenant, "fp-1", at=AT)
                raise RuntimeError("abandoned")

        # A claim is not a "no" that outlives its transaction: an in-flight
        # record nobody dispatched against would be an unpaid payment as far
        # as the reconciler is concerned.
        claim = await idempotency.claim(key, tenant, "fp-1", at=AT)
        assert claim.newly_claimed is True

    async def test_an_explicit_rollback_undoes_the_block(self, uow, runs, ledger, tenant):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="reconcile"))

        async with uow:
            await uow.ledger.append(
                run.id, tenant, LedgerEventType.RUN_CREATED, {}, occurred_at=AT
            )
            await uow.rollback()

        assert await ledger.read(tenant, run.id) == ()

    async def test_rollback_restores_only_this_transaction(self, uow, runs, ledger, tenant):
        first = await runs.create(RunSpec(tenant_id=tenant, objective="first"))
        second = await runs.create(RunSpec(tenant_id=tenant, objective="second"))
        await ledger.append(first.id, tenant, LedgerEventType.RUN_CREATED, {}, occurred_at=AT)

        with pytest.raises(RuntimeError):
            async with uow:
                await uow.ledger.append(
                    first.id, tenant, LedgerEventType.RUN_STARTED, {}, occurred_at=AT
                )
                await uow.ledger.append(
                    second.id, tenant, LedgerEventType.RUN_STARTED, {}, occurred_at=AT
                )
                raise RuntimeError("abandoned")

        assert len(await ledger.read(tenant, first.id)) == 1
        assert await ledger.read(tenant, second.id) == ()


class TestMisuse:
    async def test_commit_outside_a_transaction_is_an_error(self, uow):
        with pytest.raises(RuntimeError, match="no transaction"):
            await uow.commit()

    async def test_rollback_outside_a_transaction_is_an_error(self, uow):
        with pytest.raises(RuntimeError, match="no transaction"):
            await uow.rollback()

    async def test_rollback_after_commit_is_an_error(self, uow):
        async with uow:
            await uow.commit()
        with pytest.raises(RuntimeError, match="no transaction"):
            await uow.rollback()

    async def test_the_same_object_can_run_a_second_transaction(self, uow, runs, ledger, tenant):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="reconcile"))

        async with uow:
            await uow.ledger.append(
                run.id, tenant, LedgerEventType.RUN_CREATED, {}, occurred_at=AT
            )
        async with uow:
            await uow.ledger.append(
                run.id, tenant, LedgerEventType.RUN_STARTED, {}, occurred_at=AT
            )

        assert len(await ledger.read(tenant, run.id)) == 2

    async def test_nesting_is_refused(self, uow):
        async with uow:
            with pytest.raises(RuntimeError, match="already in a transaction"):
                async with uow:
                    pass

    async def test_a_refused_nesting_does_not_poison_the_outer_transaction(self, uow, runs, ledger, tenant):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="reconcile"))

        async with uow:
            with pytest.raises(RuntimeError):
                async with uow:
                    pass
            await uow.ledger.append(
                run.id, tenant, LedgerEventType.RUN_CREATED, {}, occurred_at=AT
            )

        assert len(await ledger.read(tenant, run.id)) == 1

    async def test_stale_write_inside_a_transaction_still_raises(self, uow, runs, tenant):
        run = await runs.create(RunSpec(tenant_id=tenant, objective="reconcile"))

        async with uow:
            await uow.runs.save(run.start(at=AT), expected_version=0)
            # The unit of work is not a licence to skip optimistic
            # concurrency: two workers inside one transaction are still two
            # writers.
            with pytest.raises(ConcurrencyError):
                await uow.runs.save(run.start(at=AT), expected_version=0)

        # And the write that did win is still the only one.
        assert (await runs.get(tenant, run.id)).version == 1
