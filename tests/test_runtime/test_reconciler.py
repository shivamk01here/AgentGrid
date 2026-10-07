"""Tests for the reconciler."""

from datetime import UTC, datetime, timedelta

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
    LedgerEventType,
)
from ledgerloop.core.errors import IndeterminateError
from ledgerloop.core.ids import ActionId, IdempotencyKey, RunId, TenantId
from ledgerloop.core.models import Action, ActionReceipt, RunSpec
from ledgerloop.core.money import Money
from ledgerloop.runtime import ActionExecutor, Compensator, Reconciler, replay_effects

AT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


class StubLookup:
    """A provider that answers however the test tells it to."""

    def __init__(self, *, receipt: ActionReceipt | None = None, raises: bool = False) -> None:
        self._receipt = receipt
        self._raises = raises
        self.calls = 0

    async def lookup(self, key, tenant_id):
        self.calls += 1
        if self._raises:
            raise ConnectionError("provider unreachable")
        return self._receipt


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
def tenant() -> TenantId:
    return TenantId.generate()


@pytest.fixture
def run_id() -> RunId:
    return RunId.generate()


async def _stranded_claim(idempotency, ledger, clock, tenant, run_id) -> Action:
    """Leave one action stuck in flight, the way a dropped connection would."""
    dispatcher = RecordingDispatcher()
    dispatcher.fail_next(FailureClass.INDETERMINATE)
    executor = ActionExecutor(
        idempotency=idempotency, dispatcher=dispatcher, ledger=ledger, clock=clock
    )
    money = Money.from_major("1000", Currency.INR)
    action = Action(
        id=ActionId.generate(),
        kind=ActionKind.REFUND,
        description="Stranded refund",
        amount=money,
        counterparty="mer_1",
        idempotency_key=IdempotencyKey.derive("refund", "ord_1", str(money.minor_units)),
    )
    await executor.execute(action, run_id=run_id, tenant_id=tenant)
    return action


def _reconciler(idempotency, ledger, clock, lookup, **kwargs) -> Reconciler:
    return Reconciler(
        idempotency=idempotency, lookup=lookup, ledger=ledger, clock=clock, **kwargs
    )


class TestSweep:
    async def test_confirmed_by_provider_settles_as_succeeded(
        self, idempotency, ledger, clock, tenant, run_id
    ):
        action = await _stranded_claim(idempotency, ledger, clock, tenant, run_id)
        lookup = StubLookup(
            receipt=ActionReceipt(
                action_id=action.id,
                state=IdempotencyState.SUCCEEDED,
                provider_reference="psp_real_1",
                settled_at=AT,
            )
        )
        await clock.advance(timedelta(hours=1))

        report = _reconciler(idempotency, ledger, clock, lookup)
        result = await report.sweep()

        assert result.confirmed == 1
        record = await idempotency.get(action.idempotency_key, tenant)
        assert record.state is IdempotencyState.SUCCEEDED
        assert record.receipt.provider_reference == "psp_real_1"

    async def test_never_seen_by_provider_settles_as_failed(
        self, idempotency, ledger, clock, tenant, run_id
    ):
        action = await _stranded_claim(idempotency, ledger, clock, tenant, run_id)
        await clock.advance(timedelta(hours=1))

        result = await _reconciler(
            idempotency, ledger, clock, StubLookup(receipt=None)
        ).sweep()

        assert result.not_found == 1
        record = await idempotency.get(action.idempotency_key, tenant)
        assert record.state is IdempotencyState.FAILED

    async def test_a_provider_reported_failure_is_not_a_confirmation(
        self, idempotency, ledger, clock, tenant, run_id
    ):
        action = await _stranded_claim(idempotency, ledger, clock, tenant, run_id)
        lookup = StubLookup(
            receipt=ActionReceipt(
                action_id=action.id,
                state=IdempotencyState.FAILED,
                provider_reference="psp_real_2",
                failure_reason="issuer declined",
                settled_at=AT,
            )
        )
        await clock.advance(timedelta(hours=1))

        result = await _reconciler(idempotency, ledger, clock, lookup).sweep()

        # The provider answered, so the claim resolves - but it resolves to
        # "this did not happen", and the counts have to say so.
        assert result.confirmed == 0
        assert result.failed == 1
        assert result.not_found == 0
        assert result.resolved == 1
        record = await idempotency.get(action.idempotency_key, tenant)
        assert record.state is IdempotencyState.FAILED

        entries = await ledger.read(tenant, run_id)
        assert entries[-1].event_type is LedgerEventType.ACTION_FAILED

    async def test_a_failing_lookup_leaves_the_claim_open(
        self, idempotency, ledger, clock, tenant, run_id
    ):
        action = await _stranded_claim(idempotency, ledger, clock, tenant, run_id)
        await clock.advance(timedelta(hours=1))

        result = await _reconciler(
            idempotency, ledger, clock, StubLookup(raises=True)
        ).sweep()

        # An unanswered question must not become an answer.
        assert result.unresolved == 1
        record = await idempotency.get(action.idempotency_key, tenant)
        assert record.state is IdempotencyState.IN_FLIGHT

    async def test_a_provider_still_working_on_it_is_not_a_failure(
        self, idempotency, ledger, clock, tenant, run_id
    ):
        action = await _stranded_claim(idempotency, ledger, clock, tenant, run_id)
        lookup = StubLookup(
            receipt=ActionReceipt(
                action_id=action.id,
                state=IdempotencyState.IN_FLIGHT,
                provider_reference="psp_real_3",
            )
        )
        await clock.advance(timedelta(hours=1))

        result = await _reconciler(idempotency, ledger, clock, lookup).sweep()

        # "Still processing" is not an outcome. The claim stays open with no
        # receipt on it, and nothing is ledgered as resolved.
        assert result.unresolved == 1
        assert result.failed == 0
        assert result.resolved == 0
        record = await idempotency.get(action.idempotency_key, tenant)
        assert record.state is IdempotencyState.IN_FLIGHT
        assert record.receipt is None
        entries = await ledger.read(tenant, run_id)
        assert not [e for e in entries if e.payload.get("reconciled")]

    async def test_a_pending_refund_is_still_standing_in_the_chain(
        self, idempotency, ledger, clock, tenant, run_id
    ):
        action = await _stranded_claim(idempotency, ledger, clock, tenant, run_id)
        lookup = StubLookup(
            receipt=ActionReceipt(action_id=action.id, state=IdempotencyState.IN_FLIGHT)
        )
        await clock.advance(timedelta(hours=1))

        await _reconciler(idempotency, ledger, clock, lookup).sweep()

        # A failure entry would have dropped it, and the run's value ceiling
        # would have stopped counting money that may yet go out.
        (effect,) = replay_effects(await ledger.read(tenant, run_id))
        assert effect.action_id == action.id
        assert effect.indeterminate

    async def test_claims_inside_the_grace_period_are_left_alone(
        self, idempotency, ledger, clock, tenant, run_id
    ):
        await _stranded_claim(idempotency, ledger, clock, tenant, run_id)
        lookup = StubLookup(receipt=None)

        # Only a minute old; the provider may still be working on it.
        await clock.advance(timedelta(minutes=1))
        result = await _reconciler(idempotency, ledger, clock, lookup).sweep()

        assert result.checked == 0
        assert lookup.calls == 0

    async def test_an_empty_sweep_is_fine(self, idempotency, ledger, clock):
        result = await _reconciler(
            idempotency, ledger, clock, StubLookup(receipt=None)
        ).sweep()
        assert result.checked == 0
        assert result.resolved == 0

    async def test_settled_claims_are_never_swept(
        self, idempotency, ledger, clock, tenant, run_id
    ):
        # A normal, successful action leaves nothing to reconcile.
        executor = ActionExecutor(
            idempotency=idempotency,
            dispatcher=RecordingDispatcher(),
            ledger=ledger,
            clock=clock,
        )
        money = Money.from_major("500", Currency.INR)
        await executor.execute(
            Action(
                id=ActionId.generate(),
                kind=ActionKind.REFUND,
                description="Clean refund",
                amount=money,
                idempotency_key=IdempotencyKey.derive("refund", "clean"),
            ),
            run_id=run_id,
            tenant_id=tenant,
        )
        await clock.advance(timedelta(hours=1))

        result = await _reconciler(
            idempotency, ledger, clock, StubLookup(receipt=None)
        ).sweep()
        assert result.checked == 0


class SettlesFirstLookup:
    """A provider whose own callback lands while the sweep is still asking.

    By the time the lookup returns, something else has settled the claim as
    failed - and the answer the sweep is handed says it succeeded.
    """

    def __init__(self, idempotency, action: Action) -> None:
        self._idempotency = idempotency
        self._action = action

    async def lookup(self, key, tenant_id):
        await self._idempotency.settle(
            key,
            tenant_id,
            ActionReceipt(
                action_id=self._action.id,
                state=IdempotencyState.FAILED,
                failure_reason="issuer declined",
            ),
            at=AT,
        )
        return ActionReceipt(action_id=self._action.id, state=IdempotencyState.SUCCEEDED)


class TestLosingTheRace:
    async def test_a_claim_somebody_else_settled_is_not_counted_as_confirmed(
        self, idempotency, ledger, clock, tenant, run_id
    ):
        action = await _stranded_claim(idempotency, ledger, clock, tenant, run_id)
        await clock.advance(timedelta(hours=1))

        result = await _reconciler(
            idempotency, ledger, clock, SettlesFirstLookup(idempotency, action)
        ).sweep()

        # The claim on record is FAILED. A report saying one was confirmed
        # would be describing a settlement that never happened.
        assert result.confirmed == 0
        assert result.resolved == 0
        assert result.skipped == 1
        record = await idempotency.get(action.idempotency_key, tenant)
        assert record.state is IdempotencyState.FAILED

    async def test_the_answer_that_lost_is_not_ledgered(
        self, idempotency, ledger, clock, tenant, run_id
    ):
        action = await _stranded_claim(idempotency, ledger, clock, tenant, run_id)
        await clock.advance(timedelta(hours=1))

        await _reconciler(
            idempotency, ledger, clock, SettlesFirstLookup(idempotency, action)
        ).sweep()

        entries = await ledger.read(tenant, run_id)
        assert not [e for e in entries if e.payload.get("reconciled")]

    async def test_every_claim_checked_lands_in_exactly_one_count(
        self, idempotency, ledger, clock, tenant, run_id
    ):
        action = await _stranded_claim(idempotency, ledger, clock, tenant, run_id)
        await clock.advance(timedelta(hours=1))

        result = await _reconciler(
            idempotency, ledger, clock, SettlesFirstLookup(idempotency, action)
        ).sweep()

        assert result.checked == result.resolved + result.unresolved + result.skipped


class DroppedReversalDispatcher(RecordingDispatcher):
    """Dispatches normally, and loses the connection on every reversal."""

    async def compensate(self, action, receipt, *, at):
        raise IndeterminateError(
            "Connection dropped after the reversal was sent", action_id=action.id
        )


async def _stranded_reversal(idempotency, ledger, clock, tenant):
    """Capture, then roll back with a reversal nobody heard back from."""
    runs = InMemoryRunStore(clock=clock)
    dispatcher = DroppedReversalDispatcher()
    run = await runs.create(RunSpec(tenant_id=tenant, objective="Settle the batch"))
    run = await runs.save(run.start(at=clock.now()), expected_version=0)

    money = Money.from_major("2500", Currency.INR)
    action = Action(
        id=ActionId.generate(),
        kind=ActionKind.CAPTURE,
        description="Capture on order ord_1",
        amount=money,
        counterparty="mer_1",
        idempotency_key=IdempotencyKey.derive("capture", "ord_1", str(money.minor_units)),
    )
    executor = ActionExecutor(
        idempotency=idempotency, dispatcher=dispatcher, ledger=ledger, clock=clock
    )
    await executor.execute(action, run_id=run.id, tenant_id=tenant)

    await Compensator(
        runs=runs, ledger=ledger, dispatcher=dispatcher, idempotency=idempotency, clock=clock
    ).compensate(run)
    return run, action


class TestReversalClaims:
    """A reversal's claim carries the id of the action it was undoing."""

    async def test_a_reversal_that_never_landed_leaves_the_capture_standing(
        self, idempotency, ledger, clock, tenant
    ):
        run, action = await _stranded_reversal(idempotency, ledger, clock, tenant)
        await clock.advance(timedelta(hours=1))

        result = await _reconciler(
            idempotency, ledger, clock, StubLookup(receipt=None)
        ).sweep()

        # The refund did not happen. The capture did, and the chain must not
        # come out of reconciliation saying otherwise.
        assert result.not_found == 1
        (effect,) = replay_effects(await ledger.read(tenant, run.id))
        assert effect.action_id == action.id
        assert not effect.indeterminate

    async def test_the_failure_is_ledgered_against_the_reversal(
        self, idempotency, ledger, clock, tenant
    ):
        run, _ = await _stranded_reversal(idempotency, ledger, clock, tenant)
        await clock.advance(timedelta(hours=1))

        await _reconciler(idempotency, ledger, clock, StubLookup(receipt=None)).sweep()

        entry = (await ledger.read(tenant, run.id))[-1]
        assert entry.event_type is LedgerEventType.ACTION_FAILED
        assert entry.payload["compensation"] is True
        assert entry.payload["reconciled"] is True

    async def test_a_reversal_that_did_land_takes_the_capture_down(
        self, idempotency, ledger, clock, tenant
    ):
        run, action = await _stranded_reversal(idempotency, ledger, clock, tenant)
        lookup = StubLookup(
            receipt=ActionReceipt(
                action_id=action.id,
                state=IdempotencyState.SUCCEEDED,
                provider_reference="psp_refund_1",
            )
        )
        await clock.advance(timedelta(hours=1))

        result = await _reconciler(idempotency, ledger, clock, lookup).sweep()

        assert result.confirmed == 1
        entries = await ledger.read(tenant, run.id)
        assert entries[-1].event_type is LedgerEventType.ACTION_COMPENSATED
        assert entries[-1].payload["provider_reference"] == "psp_refund_1"
        assert replay_effects(entries) == ()
        await ledger.verify_chain(tenant, run.id)

    async def test_an_ordinary_claim_is_ledgered_as_before(
        self, idempotency, ledger, clock, tenant, run_id
    ):
        action = await _stranded_claim(idempotency, ledger, clock, tenant, run_id)
        lookup = StubLookup(
            receipt=ActionReceipt(action_id=action.id, state=IdempotencyState.SUCCEEDED)
        )
        await clock.advance(timedelta(hours=1))

        await _reconciler(idempotency, ledger, clock, lookup).sweep()

        entry = (await ledger.read(tenant, run_id))[-1]
        assert entry.event_type is LedgerEventType.ACTION_SETTLED
        assert "compensation" not in entry.payload


class TestLedgering:
    async def test_the_resolution_lands_in_the_runs_chain(
        self, idempotency, ledger, clock, tenant, run_id
    ):
        action = await _stranded_claim(idempotency, ledger, clock, tenant, run_id)
        await clock.advance(timedelta(hours=1))

        await _reconciler(
            idempotency,
            ledger,
            clock,
            StubLookup(
                receipt=ActionReceipt(
                    action_id=action.id,
                    state=IdempotencyState.SUCCEEDED,
                    provider_reference="psp_real_1",
                )
            ),
        ).sweep()

        entries = await ledger.read(tenant, run_id)
        reconciled = [e for e in entries if e.payload.get("reconciled")]
        assert len(reconciled) == 1
        await ledger.verify_chain(tenant, run_id)

    async def test_a_failure_is_ledgered_with_the_providers_reason(
        self, idempotency, ledger, clock, tenant, run_id
    ):
        action = await _stranded_claim(idempotency, ledger, clock, tenant, run_id)
        lookup = StubLookup(
            receipt=ActionReceipt(
                action_id=action.id,
                state=IdempotencyState.FAILED,
                failure_reason="issuer declined",
            )
        )
        await clock.advance(timedelta(hours=1))

        await _reconciler(idempotency, ledger, clock, lookup).sweep()

        # The claim has the reason. A chain without it says the refund
        # failed and leaves whoever reads it to go and ask the provider why.
        entry = (await ledger.read(tenant, run_id))[-1]
        assert entry.event_type is LedgerEventType.ACTION_FAILED
        assert entry.payload["error"] == "issuer declined"

    async def test_a_request_the_provider_never_saw_says_so_in_the_chain(
        self, idempotency, ledger, clock, tenant, run_id
    ):
        await _stranded_claim(idempotency, ledger, clock, tenant, run_id)
        await clock.advance(timedelta(hours=1))

        await _reconciler(idempotency, ledger, clock, StubLookup(receipt=None)).sweep()

        entry = (await ledger.read(tenant, run_id))[-1]
        assert entry.payload["error"] == "Provider has no record of this request"

    async def test_a_confirmation_carries_no_error(
        self, idempotency, ledger, clock, tenant, run_id
    ):
        action = await _stranded_claim(idempotency, ledger, clock, tenant, run_id)
        lookup = StubLookup(
            receipt=ActionReceipt(action_id=action.id, state=IdempotencyState.SUCCEEDED)
        )
        await clock.advance(timedelta(hours=1))

        await _reconciler(idempotency, ledger, clock, lookup).sweep()

        assert "error" not in (await ledger.read(tenant, run_id))[-1].payload


class TestConfiguration:
    def test_negative_grace_is_rejected(self, idempotency, ledger, clock):
        with pytest.raises(ValueError, match="grace"):
            _reconciler(
                idempotency,
                ledger,
                clock,
                StubLookup(receipt=None),
                grace=timedelta(seconds=-1),
            )

    def test_report_renders_readably(self):
        from ledgerloop.runtime import ReconciliationReport

        report = ReconciliationReport(checked=3, confirmed=1, not_found=1, unresolved=1)
        assert report.resolved == 2
        assert "checked=3" in str(report)
        assert "skipped=0" in str(report)
