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
from ledgerloop.core.errors import ProviderError, StateTransitionError
from ledgerloop.core.ids import ActionId, ApprovalId, IdempotencyKey, TenantId
from ledgerloop.core.models import Action, ActionReceipt, RunSpec
from ledgerloop.core.money import Money
from ledgerloop.runtime import ActionExecutor, Compensator
from ledgerloop.runtime.compensator import reversal_key
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

    async def test_the_reversal_claim_carries_the_run_id(
        self, compensator, executor, runs, ledger, idempotency, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        action = capture("ord_1")
        await apply(executor, run, action)

        # Capture the reversal key before compensating; replay_effects returns
        # an empty tuple after ACTION_COMPENSATED is written to the ledger.
        effect = replay_effects(await ledger.read(tenant, run.id))[0]
        rev_key = reversal_key(effect)

        await compensator.compensate(await runs.get(tenant, run.id))

        record = await idempotency.get(rev_key, tenant)
        assert record is not None
        assert record.run_id == run.id

    async def test_a_reversal_in_flight_from_another_worker_is_not_dispatched_again(
        self, compensator, executor, runs, idempotency, dispatcher, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        action = capture("ord_1")
        await apply(executor, run, action)

        # Another worker took the reversal claim and has not yet settled it.
        key = IdempotencyKey.derive("reverse", str(action.idempotency_key))
        await idempotency.claim(
            key, tenant, f"reverse:{action.id}:capture", at=clock.now()
        )

        report = await compensator.compensate(await runs.get(tenant, run.id))

        # Dispatching again would be a second refund. Report it as unresolved,
        # not as compensated.
        assert report.unresolved == 1
        assert report.compensated == 0
        assert not report.complete
        assert dispatcher.compensated == []

    async def test_a_previously_rejected_reversal_is_not_counted_as_success(
        self, compensator, executor, runs, idempotency, dispatcher, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        action = capture("ord_1")
        await apply(executor, run, action)

        # A previous attempt reached the provider and was definitively rejected.
        key = IdempotencyKey.derive("reverse", str(action.idempotency_key))
        await idempotency.claim(
            key, tenant, f"reverse:{action.id}:capture", at=clock.now()
        )
        await idempotency.settle(
            key,
            tenant,
            ActionReceipt(
                action_id=action.id,
                state=IdempotencyState.FAILED,
                failure_reason="Reversal window has closed",
            ),
            at=clock.now(),
        )

        report = await compensator.compensate(await runs.get(tenant, run.id))

        # The effect is still out there. Counting this as replayed would make
        # report.complete lie and write ACTION_COMPENSATED to the ledger,
        # hiding the effect from every future replay_effects call.
        assert report.failed == 1
        assert report.replayed == 0
        assert not report.complete
        assert report.stranded == 1
        assert dispatcher.compensated == []


def _pretend_running(run):
    """A run as a restarted worker would find it: alive, mid-rollback.

    Reaching into the aggregate rather than transitioning to it, because the
    state machine correctly refuses to walk backwards out of a terminal
    state.
    """
    return replace(run, state=RunState.RUNNING, ended_at=None, stop_reason=None)


def payout(order: str, amount: str = "9000") -> Action:
    """An effect with no reversal. Once it is gone, it is gone."""
    money = Money.from_major(amount, Currency.INR)
    return Action(
        id=ActionId.generate(),
        kind=ActionKind.PAYOUT,
        description=f"Payout for {order}",
        amount=money,
        counterparty="acc_44",
        idempotency_key=IdempotencyKey.derive("payout", order, str(money.minor_units)),
    )


class TestThingsItRefusesToDo:
    async def test_an_irreversible_effect_is_reported_not_reversed(
        self, compensator, executor, runs, dispatcher, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        await apply(executor, run, payout("ord_9"))

        report = await compensator.compensate(await runs.get(tenant, run.id))

        assert report.standing == 1
        assert report.irreversible == 1
        assert not report.complete
        assert dispatcher.compensated == []

    async def test_an_indeterminate_effect_is_left_alone(
        self, compensator, executor, runs, dispatcher, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        dispatcher.fail_next(FailureClass.INDETERMINATE)
        await apply(executor, run, capture("ord_1"))

        report = await compensator.compensate(await runs.get(tenant, run.id))

        # Nobody knows whether that capture landed. Refunding it might hand
        # back money that was never taken.
        assert report.unresolved == 1
        assert report.compensated == 0
        assert not report.complete
        assert dispatcher.compensated == []

    async def test_a_provider_refusing_the_reversal_is_not_success(
        self, compensator, executor, runs, ledger, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        await apply(executor, run, capture("ord_1"))

        compensator = _with_dispatcher(compensator, _RefusingDispatcher())
        report = await compensator.compensate(await runs.get(tenant, run.id))

        assert report.failed == 1
        assert not report.complete
        assert report.stranded == 1

    async def test_an_effect_a_reversal_failed_on_is_still_standing(
        self, compensator, executor, runs, ledger, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        await apply(executor, run, capture("ord_1"))

        compensator = _with_dispatcher(compensator, _RefusingDispatcher())
        await compensator.compensate(await runs.get(tenant, run.id))

        # The failed reversal must not read back as a failed dispatch. If it
        # did, the next pass would decide the capture never happened and stop
        # trying to undo it - while the money sits with the provider.
        effects = replay_effects(await ledger.read(tenant, run.id))
        assert len(effects) == 1
        assert effects[0].kind is ActionKind.CAPTURE


class TestPartialRollback:
    async def test_a_run_that_could_not_be_fully_rolled_back_fails(
        self, compensator, executor, runs, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        await apply(executor, run, capture("ord_1"))
        await apply(executor, run, payout("ord_9"))

        report = await compensator.compensate(await runs.get(tenant, run.id))

        assert report.compensated == 1
        assert report.irreversible == 1
        assert not report.complete

        stored = await runs.get(tenant, run.id)
        assert stored.state is RunState.FAILED
        assert "still applied" in (stored.failure_reason or "")

    async def test_the_reversible_ones_are_still_reversed(
        self, compensator, executor, runs, dispatcher, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        reversible = capture("ord_1")
        await apply(executor, run, reversible)
        await apply(executor, run, payout("ord_9"))

        await compensator.compensate(await runs.get(tenant, run.id))

        # One bad effect does not excuse leaving the others out there.
        assert [a.id for a in dispatcher.compensated] == [reversible.id]


class TestGuards:
    async def test_a_terminal_run_cannot_be_rolled_back(
        self, compensator, runs, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        run = await runs.save(run.succeed(at=clock.now()), expected_version=run.version)

        with pytest.raises(StateTransitionError):
            await compensator.compensate(run)

    async def test_a_halted_run_cannot_be_rolled_back_until_it_resumes(
        self, compensator, runs, tenant, clock
    ):
        run = await started_run(runs, tenant, clock)
        halted = await runs.save(
            run.await_approval(ApprovalId.generate(), at=clock.now()),
            expected_version=run.version,
        )

        with pytest.raises(StateTransitionError):
            await compensator.compensate(halted)


class _RefusingDispatcher(RecordingDispatcher):
    """A provider that will not reverse anything.

    Real ones do this: past the settlement window, or a scheme that simply
    does not take reversals for that instrument.
    """

    async def compensate(self, action, receipt, *, at):
        raise ProviderError(
            "Reversal window has closed",
            provider="recording",
            failure_class=FailureClass.INVALID_REQUEST,
        )


def _with_dispatcher(compensator: Compensator, dispatcher) -> Compensator:
    """The same compensator, pointed at a different provider.

    Used to apply effects through a working dispatcher and then roll back
    against one that refuses, which is the sequence that actually happens.
    """
    return Compensator(
        runs=compensator._runs,
        ledger=compensator._ledger,
        dispatcher=dispatcher,
        idempotency=compensator._idempotency,
        clock=compensator._clock,
    )
