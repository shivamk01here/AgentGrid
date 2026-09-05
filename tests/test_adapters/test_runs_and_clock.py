"""Tests for run/step storage and the clock implementations."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from ledgerloop.adapters.clock import ManualClock, SystemClock
from ledgerloop.adapters.memory import InMemoryRunStore, InMemoryStepStore
from ledgerloop.core.enums import RunState, StepOutcome
from ledgerloop.core.errors import ConcurrencyError, RunNotFoundError
from ledgerloop.core.ids import RunId, StepId, TenantId
from ledgerloop.core.models import RunSpec, Step

AT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(AT)


@pytest.fixture
def store(clock: ManualClock) -> InMemoryRunStore:
    return InMemoryRunStore(clock=clock)


@pytest.fixture
def tenant() -> TenantId:
    return TenantId.generate()


@pytest.fixture
def spec(tenant: TenantId) -> RunSpec:
    return RunSpec(tenant_id=tenant, objective="Reconcile")


class TestRunStore:
    async def test_create_starts_pending_at_version_zero(self, store, spec):
        run = await store.create(spec)
        assert run.state is RunState.PENDING
        assert run.version == 0

    async def test_get_round_trips(self, store, spec, tenant):
        created = await store.create(spec)
        assert (await store.get(tenant, created.id)).id == created.id

    async def test_missing_run_raises(self, store, tenant):
        with pytest.raises(RunNotFoundError):
            await store.get(tenant, RunId.generate())

    async def test_cross_tenant_read_is_refused(self, store, spec):
        created = await store.create(spec)
        with pytest.raises(RunNotFoundError):
            await store.get(TenantId.generate(), created.id)

    async def test_save_advances_the_stored_version(self, store, spec, tenant):
        run = await store.create(spec)
        started = run.start(at=AT)
        await store.save(started, expected_version=0)
        assert (await store.get(tenant, run.id)).state is RunState.RUNNING

    async def test_stale_write_is_rejected(self, store, spec):
        run = await store.create(spec)
        await store.save(run.start(at=AT), expected_version=0)
        # A second worker holding the version-0 read must lose.
        with pytest.raises(ConcurrencyError):
            await store.save(run.cancel(at=AT), expected_version=0)

    async def test_concurrent_saves_produce_exactly_one_winner(self, store, spec):
        run = await store.create(spec)
        attempts = [store.save(run.start(at=AT), expected_version=0) for _ in range(20)]
        results = await asyncio.gather(*attempts, return_exceptions=True)
        succeeded = [r for r in results if not isinstance(r, Exception)]
        assert len(succeeded) == 1

    async def test_list_by_state_is_tenant_scoped(self, store, spec, tenant):
        await store.create(spec)
        await store.create(RunSpec(tenant_id=TenantId.generate(), objective="Other"))
        mine = [r async for r in store.list_by_state(tenant, RunState.PENDING)]
        assert len(mine) == 1

    async def test_find_expired_returns_halted_runs_past_deadline(self, store, tenant):
        spec = RunSpec(
            tenant_id=tenant, objective="Halted", deadline=AT + timedelta(hours=1)
        )
        run = await store.create(spec)
        halted = run.start(at=AT).suspend(at=AT)
        await store.save(halted, expected_version=1)

        found = [r async for r in store.find_expired(as_of=AT + timedelta(hours=2))]
        assert [r.id for r in found] == [run.id]

    async def test_find_expired_ignores_running_runs(self, store, tenant):
        spec = RunSpec(tenant_id=tenant, objective="Live", deadline=AT - timedelta(hours=1))
        run = await store.create(spec)
        await store.save(run.start(at=AT), expected_version=0)

        found = [r async for r in store.find_expired(as_of=AT)]
        assert found == []


class TestLeases:
    async def test_first_claim_wins(self, store, spec, tenant):
        run = await store.create(spec)
        assert await store.acquire_lease(tenant, run.id, owner="w1", duration_seconds=30)

    async def test_second_worker_is_blocked(self, store, spec, tenant):
        run = await store.create(spec)
        await store.acquire_lease(tenant, run.id, owner="w1", duration_seconds=30)
        assert not await store.acquire_lease(tenant, run.id, owner="w2", duration_seconds=30)

    async def test_holder_can_renew(self, store, spec, tenant):
        run = await store.create(spec)
        await store.acquire_lease(tenant, run.id, owner="w1", duration_seconds=30)
        assert await store.acquire_lease(tenant, run.id, owner="w1", duration_seconds=30)

    async def test_expired_lease_can_be_taken(self, store, spec, tenant, clock):
        run = await store.create(spec)
        await store.acquire_lease(tenant, run.id, owner="w1", duration_seconds=10)
        await clock.advance(timedelta(seconds=11))
        assert await store.acquire_lease(tenant, run.id, owner="w2", duration_seconds=10)

    async def test_only_the_holder_may_release(self, store, spec, tenant):
        run = await store.create(spec)
        await store.acquire_lease(tenant, run.id, owner="w1", duration_seconds=30)
        # A worker whose lease expired must not evict whoever took it next.
        await store.release_lease(tenant, run.id, owner="stale-worker")
        assert not await store.acquire_lease(tenant, run.id, owner="w2", duration_seconds=30)

    async def test_release_frees_the_run(self, store, spec, tenant):
        run = await store.create(spec)
        await store.acquire_lease(tenant, run.id, owner="w1", duration_seconds=30)
        await store.release_lease(tenant, run.id, owner="w1")
        assert await store.acquire_lease(tenant, run.id, owner="w2", duration_seconds=30)

    async def test_non_positive_duration_rejected(self, store, spec, tenant):
        run = await store.create(spec)
        with pytest.raises(ValueError, match="positive"):
            await store.acquire_lease(tenant, run.id, owner="w1", duration_seconds=0)


class TestStepStore:
    def _step(self, tenant: TenantId, run_id: RunId, index: int) -> Step:
        return Step(
            id=StepId.generate(),
            run_id=run_id,
            tenant_id=tenant,
            index=index,
            outcome=StepOutcome.COMPLETED,
            started_at=AT,
        )

    async def test_append_and_list_in_order(self, tenant):
        store = InMemoryStepStore()
        run_id = RunId.generate()
        for index in (2, 0, 1):
            await store.append(self._step(tenant, run_id, index))

        steps = await store.list_for_run(tenant, run_id)
        assert [s.index for s in steps] == [0, 1, 2]

    async def test_steps_are_immutable(self, tenant):
        store = InMemoryStepStore()
        step = self._step(tenant, RunId.generate(), 0)
        await store.append(step)
        with pytest.raises(ConcurrencyError):
            await store.append(step)

    async def test_get_returns_none_when_absent(self, tenant):
        store = InMemoryStepStore()
        assert await store.get(tenant, StepId.generate()) is None


class TestClocks:
    def test_system_clock_is_utc_aware(self):
        assert SystemClock().now().tzinfo is not None

    def test_manual_clock_requires_an_aware_start(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            ManualClock(datetime(2026, 1, 1))

    async def test_manual_clock_only_moves_when_advanced(self, clock):
        assert clock.now() == AT
        await clock.advance(timedelta(hours=3))
        assert clock.now() == AT + timedelta(hours=3)

    async def test_manual_clock_refuses_to_move_backwards(self, clock):
        with pytest.raises(ValueError, match="backwards"):
            await clock.advance(timedelta(seconds=-1))

    async def test_sleepers_are_released_by_advancing(self, clock):
        woke: list[str] = []

        async def worker() -> None:
            await clock.sleep(60)
            woke.append("done")

        task = asyncio.create_task(worker())
        await asyncio.sleep(0)
        assert woke == []

        await clock.advance(timedelta(seconds=61))
        await task
        assert woke == ["done"]

    async def test_a_task_that_sleeps_twice_measures_from_its_wake_time(self, clock):
        marks: list[datetime] = []

        async def worker() -> None:
            await clock.sleep(10)
            marks.append(clock.now())
            await clock.sleep(10)
            marks.append(clock.now())

        task = asyncio.create_task(worker())
        await asyncio.sleep(0)
        await clock.advance(timedelta(seconds=25))
        await task

        assert marks == [AT + timedelta(seconds=10), AT + timedelta(seconds=20)]

    async def test_zero_length_sleep_returns_immediately(self, clock):
        await clock.sleep(0)
        assert clock.pending_sleepers == 0
