"""Tests for rebuilding a run's applied effects from its ledger.

Every one of these is a way to get the rollback list wrong: forgetting an
effect that is still out there, or reversing one that never happened.
"""

from datetime import UTC, datetime, timedelta

import pytest

from ledgerloop.adapters.memory import InMemoryLedgerStore
from ledgerloop.core.enums import ActionKind, Currency, LedgerEventType
from ledgerloop.core.ids import ActionId, IdempotencyKey, RunId, TenantId
from ledgerloop.core.money import Money
from ledgerloop.runtime.effects import replay_effects

AT = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def ledger() -> InMemoryLedgerStore:
    return InMemoryLedgerStore()


@pytest.fixture
def tenant() -> TenantId:
    return TenantId.generate()


@pytest.fixture
def run_id() -> RunId:
    return RunId.generate()


def dispatch_payload(
    action_id: ActionId,
    *,
    kind: ActionKind = ActionKind.CAPTURE,
    minor: int | None = 250000,
    currency: str | None = "INR",
) -> dict[str, object]:
    """The payload the executor writes when it dispatches an action."""
    return {
        "action_id": str(action_id),
        "kind": kind.value,
        "description": "Capture on order ord_1",
        "counterparty": "mer_9f21c",
        "amount_minor": minor,
        "currency": currency,
        "idempotency_key": str(IdempotencyKey.derive("capture", "ord_1")),
        "fingerprint": "abc123",
    }


async def write(ledger, run_id, tenant, event, payload, *, at=AT):
    return await ledger.append(run_id, tenant, event, payload, occurred_at=at)


class TestStandingEffects:
    async def test_dispatched_and_settled_effect_stands(self, ledger, run_id, tenant):
        action_id = ActionId.generate()
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_DISPATCHED,
            dispatch_payload(action_id),
        )
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_SETTLED,
            {"action_id": str(action_id), "provider_reference": "psp_77", "replayed": False},
            at=AT + timedelta(seconds=2),
        )

        effects = replay_effects(await ledger.read(tenant, run_id))

        assert len(effects) == 1
        effect = effects[0]
        assert effect.action_id == action_id
        assert effect.kind is ActionKind.CAPTURE
        assert effect.amount == Money(250000, Currency.INR)
        assert effect.provider_reference == "psp_77"
        assert effect.settled_at == AT + timedelta(seconds=2)
        assert effect.is_reversible

    async def test_effects_come_back_in_the_order_they_were_applied(
        self, ledger, run_id, tenant
    ):
        first, second = ActionId.generate(), ActionId.generate()
        for action_id in (first, second):
            await write(
                ledger, run_id, tenant, LedgerEventType.ACTION_DISPATCHED,
                dispatch_payload(action_id),
            )
            await write(
                ledger, run_id, tenant, LedgerEventType.ACTION_SETTLED,
                {"action_id": str(action_id)},
            )

        effects = replay_effects(await ledger.read(tenant, run_id))

        assert [e.action_id for e in effects] == [first, second]

    async def test_the_rebuilt_action_can_be_handed_back_to_a_dispatcher(
        self, ledger, run_id, tenant
    ):
        action_id = ActionId.generate()
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_DISPATCHED,
            dispatch_payload(action_id),
        )

        effect = replay_effects(await ledger.read(tenant, run_id))[0]
        action = effect.to_action()

        assert action.id == action_id
        assert action.kind is ActionKind.CAPTURE
        assert action.amount == Money(250000, Currency.INR)
        assert action.counterparty == "mer_9f21c"
        assert action.idempotency_key is not None


class TestEffectsThatDoNotStand:
    async def test_a_failed_dispatch_left_nothing_to_undo(self, ledger, run_id, tenant):
        action_id = ActionId.generate()
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_DISPATCHED,
            dispatch_payload(action_id),
        )
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_FAILED,
            {"action_id": str(action_id), "error": "card declined",
             "failure_class": "invalid_request"},
        )

        assert replay_effects(await ledger.read(tenant, run_id)) == ()

    async def test_an_already_compensated_effect_is_not_reversed_twice(
        self, ledger, run_id, tenant
    ):
        action_id = ActionId.generate()
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_DISPATCHED,
            dispatch_payload(action_id),
        )
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_SETTLED,
            {"action_id": str(action_id)},
        )
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_COMPENSATED,
            {"action_id": str(action_id), "reversal_kind": "refund"},
        )

        assert replay_effects(await ledger.read(tenant, run_id)) == ()

    async def test_entries_that_are_not_about_actions_are_ignored(
        self, ledger, run_id, tenant
    ):
        await write(ledger, run_id, tenant, LedgerEventType.RUN_STARTED, {})
        await write(
            ledger, run_id, tenant, LedgerEventType.APPROVAL_REQUESTED,
            {"approval_id": "apr_1"},
        )

        assert replay_effects(await ledger.read(tenant, run_id)) == ()

    async def test_a_settle_with_no_dispatch_is_not_an_effect_this_run_owns(
        self, ledger, run_id, tenant
    ):
        # A replayed claim: the money moved under some earlier run, and this
        # run only observed it. Reversing it here would undo someone else's
        # work on the strength of an entry that carries none of the detail.
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_SETTLED,
            {"action_id": str(ActionId.generate()), "replayed": True, "state": "succeeded"},
        )

        assert replay_effects(await ledger.read(tenant, run_id)) == ()


class TestIndeterminateEffects:
    async def test_an_indeterminate_effect_stands_but_is_flagged(
        self, ledger, run_id, tenant
    ):
        action_id = ActionId.generate()
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_DISPATCHED,
            dispatch_payload(action_id),
        )
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_FAILED,
            {"action_id": str(action_id), "indeterminate": True, "error": "timeout"},
        )

        effects = replay_effects(await ledger.read(tenant, run_id))

        assert len(effects) == 1
        assert effects[0].indeterminate
        assert not effects[0].is_reversible

    async def test_reconciliation_clears_the_flag(self, ledger, run_id, tenant):
        action_id = ActionId.generate()
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_DISPATCHED,
            dispatch_payload(action_id),
        )
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_FAILED,
            {"action_id": str(action_id), "indeterminate": True, "error": "timeout"},
        )
        # ...the reconciler asks the provider and gets an answer.
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_SETTLED,
            {"action_id": str(action_id), "reconciled": True, "provider_reference": "psp_88"},
        )

        effect = replay_effects(await ledger.read(tenant, run_id))[0]

        assert not effect.indeterminate
        assert effect.is_reversible
        assert effect.provider_reference == "psp_88"


class TestMalformedPayloads:
    async def test_an_unknown_action_kind_is_skipped_rather_than_guessed(
        self, ledger, run_id, tenant
    ):
        payload = dispatch_payload(ActionId.generate())
        payload["kind"] = "teleport"
        await write(ledger, run_id, tenant, LedgerEventType.ACTION_DISPATCHED, payload)

        assert replay_effects(await ledger.read(tenant, run_id)) == ()

    async def test_minor_units_without_a_currency_is_not_an_amount(
        self, ledger, run_id, tenant
    ):
        action_id = ActionId.generate()
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_DISPATCHED,
            dispatch_payload(action_id, kind=ActionKind.HOLD, currency=None),
        )

        effect = replay_effects(await ledger.read(tenant, run_id))[0]

        assert effect.amount is None


class TestUnansweredDispatches:
    async def test_a_dispatch_with_no_settlement_is_not_assumed_to_have_landed(
        self, ledger, run_id, tenant
    ):
        # The process died between writing the dispatch and settling the
        # claim. From out here that is indistinguishable from a timeout, and
        # it deserves the same answer: nobody knows.
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_DISPATCHED,
            dispatch_payload(ActionId.generate()),
        )

        effect = replay_effects(await ledger.read(tenant, run_id))[0]

        assert effect.indeterminate
        assert not effect.is_reversible

    async def test_a_failed_reversal_does_not_make_the_effect_unknown_again(
        self, ledger, run_id, tenant
    ):
        action_id = ActionId.generate()
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_DISPATCHED,
            dispatch_payload(action_id),
        )
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_SETTLED,
            {"action_id": str(action_id), "provider_reference": "psp_77"},
        )
        await write(
            ledger, run_id, tenant, LedgerEventType.ACTION_FAILED,
            {"action_id": str(action_id), "compensation": True,
             "detail": "reversal window closed"},
        )

        effect = replay_effects(await ledger.read(tenant, run_id))[0]

        # It is still applied and still reversible - the reversal is what
        # failed, and the next attempt has to try again.
        assert not effect.indeterminate
        assert effect.is_reversible
