"""Tests for the reconciler."""

from datetime import UTC, datetime, timedelta

import pytest

from ledgerloop.adapters.clock import ManualClock
from ledgerloop.adapters.memory import (
    InMemoryIdempotencyStore,
    InMemoryLedgerStore,
    RecordingDispatcher,
)
from ledgerloop.core.enums import (
    ActionKind,
    Currency,
    FailureClass,
    IdempotencyState,
    LedgerEventType,
)
from ledgerloop.core.ids import ActionId, IdempotencyKey, RunId, TenantId
from ledgerloop.core.models import Action, ActionReceipt
from ledgerloop.core.money import Money
from ledgerloop.runtime import ActionExecutor, Reconciler

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
        assert result.not_found == 1
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
