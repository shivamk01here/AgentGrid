"""Tests for rolling a run back.

The expensive mistakes here are refunding twice and thinking you rolled back
when you didn't, so most of these are about one of those two.
"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from ledgerloop.adapters.clock import ManualClock
from ledgerloop.adapters.memory import (
    InMemoryIdempotencyStore,
    InMemoryLedgerStore,
    InMemoryRunStore,
    RecordingDispatcher,
)
from ledgerloop.core.enums import (
    ActionKind,
    Currency,
    FailureClass,
    IdempotencyState,
    RunState,
)
from ledgerloop.core.ids import ActionId, IdempotencyKey, TenantId
from ledgerloop.core.models import Action, ActionReceipt, RunSpec
from ledgerloop.core.money import Money
from ledgerloop.runtime import ActionExecutor
from ledgerloop.runtime.compensator import Compensator, reversal_key
from ledgerloop.runtime.effects import replay_effects

AT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(AT)


@pytest.fixture
def ledger() -> InMemoryLedgerStore:
    return InMemoryLedgerStore()


@pytest.fixture
def idempotency() -> InMemoryIdempotencyStore:
    return InMemoryIdempotencyStore()


@pytest.fixture
def dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture
def runs(clock) -> InMemoryRunStore:
    return InMemoryRunStore(clock=clock)


@pytest.fixture
def tenant() -> TenantId:
    return TenantId.generate()


@pytest.fixture
def executor(idempotency, dispatcher, ledger, clock) -> ActionExecutor:
    return ActionExecutor(
        idempotency=idempotency, dispatcher=dispatcher, ledger=ledger, clock=clock
    )


@pytest.fixture
def compensator(runs, ledger, dispatcher, idempotency, clock) -> Compensator:
    return Compensator(
        runs=runs,
        ledger=ledger,
        dispatcher=dispatcher,
        idempotency=idempotency,
        clock=clock,
    )


def capture(order: str, amount: str = "2500") -> Action:
    money = Money.from_major(amount, Currency.INR)
    return Action(
        id=ActionId.generate(),
        kind=ActionKind.CAPTURE,
        description=f"Capture on order {order}",
        amount=money,
        counterparty="mer_9f21c",
        idempotency_key=IdempotencyKey.derive("capture", order, str(money.minor_units)),
    )


async def started_run(runs, tenant, clock):
    """A run in RUNNING, ready to have effects applied to it."""
    run = await runs.create(RunSpec(tenant_id=tenant, objective="Settle the batch"))
    return await runs.save(run.start(at=clock.now()), expected_version=0)


async def apply(executor, run, action):
    """Put one effect into the world through the real executor."""
    return await executor.execute(action, run_id=run.id, tenant_id=run.tenant_id)


class TestRollingBack:
    async def test_a_single_capture_is_reversed(
        self, compensator, executor, runs, ledger, dispatcher, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        action = capture("ord_1")
        await apply(executor, run, action)

        run = await runs.get(tenant, run.id)
        report = await compensator.compensate(run)

        assert report.standing == 1
        assert report.compensated == 1
        assert report.complete
        assert [a.id for a in dispatcher.compensated] == [action.id]

    async def test_the_run_ends_compensated(
        self, compensator, executor, runs, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        await apply(executor, run, capture("ord_1"))

        await compensator.compensate(await runs.get(tenant, run.id))

        assert (await runs.get(tenant, run.id)).state is RunState.COMPENSATED

    async def test_effects_are_reversed_newest_first(
        self, compensator, executor, runs, dispatcher, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        first, second, third = capture("ord_1"), capture("ord_2"), capture("ord_3")
        for action in (first, second, third):
            await apply(executor, run, action)

        await compensator.compensate(await runs.get(tenant, run.id))

        assert [a.id for a in dispatcher.compensated] == [third.id, second.id, first.id]

    async def test_a_run_with_nothing_applied_rolls_back_trivially(
        self, compensator, runs, dispatcher, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)

        report = await compensator.compensate(run)

        assert report.standing == 0
        assert report.complete
        assert dispatcher.compensated == []
        assert (await runs.get(tenant, run.id)).state is RunState.COMPENSATED

    async def test_a_failed_dispatch_is_not_reversed(
        self, compensator, executor, runs, dispatcher, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        dispatcher.fail_next(FailureClass.INVALID_REQUEST)
        await apply(executor, run, capture("ord_1"))

        report = await compensator.compensate(await runs.get(tenant, run.id))

        # Nothing landed, so there is nothing out there to undo.
        assert report.standing == 0
        assert dispatcher.compensated == []


class TestExactlyOnce:
    async def test_rolling_back_twice_reverses_once(
        self, compensator, executor, runs, ledger, dispatcher, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        await apply(executor, run, capture("ord_1"))
        await compensator.compensate(await runs.get(tenant, run.id))

        # The run is terminal now, so a second attempt has to start from a
        # run that thinks it is still going - which is exactly what a crashed
        # and restarted worker would hand us.
        replayed_run = (await runs.get(tenant, run.id))
        second = await compensator.compensate(_pretend_running(replayed_run))

        assert dispatcher.compensated and len(dispatcher.compensated) == 1
        assert second.standing == 0

    async def test_the_reversal_takes_its_own_idempotency_claim(
        self, compensator, executor, runs, ledger, idempotency, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        action = capture("ord_1")
        await apply(executor, run, action)

        await compensator.compensate(await runs.get(tenant, run.id))

        effects = replay_effects(await ledger.read(tenant, run.id))
        # The chain now shows the reversal, so nothing is left standing.
        assert effects == ()

        original = await idempotency.get(action.idempotency_key, tenant)
        assert original.state is IdempotencyState.SUCCEEDED

    async def test_the_reversal_key_is_not_the_original_key(
        self, executor, runs, ledger, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        action = capture("ord_1")
        await apply(executor, run, action)

        effect = replay_effects(await ledger.read(tenant, run.id))[0]

        assert reversal_key(effect) != action.idempotency_key

    async def test_an_already_settled_reversal_claim_is_not_dispatched_again(
        self, compensator, executor, runs, ledger, idempotency, dispatcher, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        action = capture("ord_1")
        await apply(executor, run, action)

        # Somebody - an earlier attempt, a manual fix - already reversed it.
        effect = replay_effects(await ledger.read(tenant, run.id))[0]
        key = reversal_key(effect)
        await idempotency.claim(key, tenant, f"reverse:{action.id}:capture", at=clock.now())
        await idempotency.settle(
            key,
            tenant,
            ActionReceipt(action_id=action.id, state=IdempotencyState.SUCCEEDED),
            at=clock.now(),
        )

        report = await compensator.compensate(await runs.get(tenant, run.id))

        assert report.replayed == 1
        assert report.compensated == 0
        assert report.complete
        assert dispatcher.compensated == []


def _pretend_running(run):
    """A run as a restarted worker would find it: alive, mid-rollback.

    Reaching into the aggregate rather than transitioning to it, because the
    state machine correctly refuses to walk backwards out of a terminal
    state.
    """
    return replace(run, state=RunState.RUNNING, ended_at=None, stop_reason=None)
