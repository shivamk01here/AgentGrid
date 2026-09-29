"""A run that gets stopped before its next move, not ended.

Runs entirely in memory - no database, no payment provider, no API key. A
batch of refunds is under way when the provider starts having an incident.
An operator puts the run on hold: the next refund the agent proposes is
refused before it is even evaluated, nothing reaches the provider, and when
the incident is over the hold is lifted and the run carries on from where it
was. Cancelling would have ended it; this does not.

    python examples/held_run.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ledgerloop.adapters.clock import ManualClock
from ledgerloop.adapters.memory import (
    InMemoryApprovalGateway,
    InMemoryIdempotencyStore,
    InMemoryLedgerStore,
    InMemoryRunStore,
    RecordingDispatcher,
)
from ledgerloop.core.enums import ActionKind, Currency
from ledgerloop.core.errors import StateTransitionError
from ledgerloop.core.ids import ActionId, IdempotencyKey, TenantId
from ledgerloop.core.models import Action, RunSpec
from ledgerloop.core.money import Money
from ledgerloop.policy import ThresholdPolicyEngine
from ledgerloop.runtime import ActionExecutor, RunCoordinator


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
    dispatcher = RecordingDispatcher()

    coordinator = RunCoordinator(
        runs=runs,
        policy=ThresholdPolicyEngine(),
        approvals=InMemoryApprovalGateway(),
        executor=ActionExecutor(
            idempotency=InMemoryIdempotencyStore(),
            dispatcher=dispatcher,
            ledger=ledger,
            clock=clock,
        ),
        ledger=ledger,
        clock=clock,
    )

    tenant = TenantId.generate()
    run = await runs.create(
        RunSpec(tenant_id=tenant, objective="Clear this week's duplicate-charge queue")
    )
    run = await runs.save(run.start(at=clock.now()), expected_version=0)

    # --- the batch is under way -------------------------------------------
    first = await coordinator.propose(run, build_refund("ord_3001", "1200"))
    print(f"refund 1      -> executed={first.executed} state={first.run.state.value}")

    # --- the provider has an incident; an operator puts the run on hold ---
    held = await coordinator.suspend(first.run, reason="Provider incident, waiting on status")
    print(f"\nhold          -> run={held.state.value}")

    # --- the agent does not know, and proposes its next refund anyway -----
    second_refund = build_refund("ord_3002", "900")
    try:
        await coordinator.propose(held, second_refund)
    except StateTransitionError as exc:
        print(f"refund 2      -> refused: {exc.message}")
    print(f"dispatches    -> {dispatcher.dispatch_count}")

    # --- the incident is over ---------------------------------------------
    running = await coordinator.lift_hold(held, reason="Provider is back")
    print(f"\nlift hold     -> run={running.state.value}")

    second = await coordinator.propose(running, second_refund)
    print(f"refund 2      -> executed={second.executed} state={second.run.state.value}")
    print(f"dispatches    -> {dispatcher.dispatch_count}")

    # --- the audit trail --------------------------------------------------
    await ledger.verify_chain(tenant, run.id)
    print("\naudit chain verified. entries:")
    for entry in await ledger.read(tenant, run.id):
        print(f"  {entry.sequence:>2}  {entry.event_type.value}")


if __name__ == "__main__":
    asyncio.run(main())
