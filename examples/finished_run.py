"""A run that is not allowed to call itself done yet.

Runs entirely in memory - no database, no payment provider, no API key. Two
refunds go out. The first comes back clean; the connection drops on the
second, so nobody knows whether it landed. The agent has nothing left to do
and tries to finish, and the coordinator refuses: a run with an effect in
doubt is not a success. Once the reconciler has asked the provider and got an
answer, the same run completes, and its closing entry counts both refunds.

    python examples/finished_run.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from ledgerloop.adapters.clock import ManualClock
from ledgerloop.adapters.memory import (
    InMemoryApprovalGateway,
    InMemoryIdempotencyStore,
    InMemoryLedgerStore,
    InMemoryRunStore,
    RecordingDispatcher,
)
from ledgerloop.core.enums import ActionKind, Currency, FailureClass, IdempotencyState
from ledgerloop.core.errors import IndeterminateError
from ledgerloop.core.ids import ActionId, IdempotencyKey, TenantId
from ledgerloop.core.models import Action, ActionReceipt, RunSpec
from ledgerloop.core.money import Money
from ledgerloop.policy import ThresholdPolicyEngine
from ledgerloop.runtime import ActionExecutor, Reconciler, RunCoordinator


class ProviderSaysItLanded:
    """Stands in for a PSP's lookup-by-idempotency-key endpoint."""

    def __init__(self, action: Action) -> None:
        self._action = action

    async def lookup(self, key: IdempotencyKey, tenant_id: TenantId) -> ActionReceipt | None:
        return ActionReceipt(
            action_id=self._action.id,
            state=IdempotencyState.SUCCEEDED,
            provider_reference="psp_late_7731",
        )


def build_refund(order: str, amount: str) -> Action:
    """A refund action, with its idempotency key derived from its content."""
    money = Money.from_major(amount, Currency.INR)
    return Action(
        id=ActionId.generate(),
        kind=ActionKind.REFUND,
        description=f"Duplicate charge on order {order}",
        amount=money,
        counterparty="mer_9f21c",
        idempotency_key=IdempotencyKey.derive(
            "refund", order, str(money.minor_units), money.currency
        ),
    )


async def main() -> None:
    clock = ManualClock(datetime(2026, 5, 1, 9, 0, tzinfo=UTC))

    runs = InMemoryRunStore(clock=clock)
    ledger = InMemoryLedgerStore()
    idempotency = InMemoryIdempotencyStore()
    dispatcher = RecordingDispatcher()

    coordinator = RunCoordinator(
        runs=runs,
        policy=ThresholdPolicyEngine(),
        approvals=InMemoryApprovalGateway(),
        executor=ActionExecutor(
            idempotency=idempotency,
            dispatcher=dispatcher,
            ledger=ledger,
            clock=clock,
        ),
        ledger=ledger,
        clock=clock,
    )

    tenant = TenantId.generate()
    run = await runs.create(
        RunSpec(tenant_id=tenant, objective="Clear this morning's duplicate-charge queue")
    )
    run = await coordinator.start(run)
    print(f"start         -> run={run.state.value}")

    # --- the first refund comes back clean --------------------------------
    first = await coordinator.propose(run, build_refund("ord_4001", "1200"))
    print(f"refund 1      -> succeeded={first.outcome.succeeded}")

    # --- the connection drops on the second -------------------------------
    dispatcher.fail_next(FailureClass.INDETERMINATE)
    second_refund = build_refund("ord_4002", "900")
    second = await coordinator.propose(first.run, second_refund)
    print(f"refund 2      -> indeterminate={second.outcome.indeterminate}")

    # --- nothing left to do, so the agent tries to finish -----------------
    try:
        await coordinator.complete(second.run)
    except IndeterminateError as exc:
        print(f"complete      -> refused: {exc.message}")
    print(f"              run={(await runs.get(tenant, run.id)).state.value}")

    # --- the reconciler asks the provider what happened -------------------
    await clock.advance(timedelta(minutes=30))
    report = await Reconciler(
        idempotency=idempotency,
        lookup=ProviderSaysItLanded(second_refund),
        ledger=ledger,
        clock=clock,
    ).sweep()
    print(f"\nreconcile     -> {report}")

    # --- and now it is a success ------------------------------------------
    done = await coordinator.complete(second.run, summary="Two duplicate charges refunded")
    print(f"complete      -> run={done.state.value}")

    # --- the audit trail --------------------------------------------------
    await ledger.verify_chain(tenant, run.id)
    entries = await ledger.read(tenant, run.id)
    print(f"              value moved: {entries[-1].payload['value_moved']}")
    print("\naudit chain verified. entries:")
    for entry in entries:
        print(f"  {entry.sequence:>2}  {entry.event_type.value}")


if __name__ == "__main__":
    asyncio.run(main())
