"""A batch that goes wrong halfway, and gets walked back out.

Runs entirely in memory - no database, no payment provider, no API key. Three
captures land, the fourth effect is a payout that cannot be undone, and the
rollback has to tell you the difference between what it reversed and what it
left behind.

    python examples/rolled_back_batch.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ledgerloop.adapters.clock import ManualClock
from ledgerloop.adapters.memory import (
    InMemoryIdempotencyStore,
    InMemoryLedgerStore,
    InMemoryRunStore,
    RecordingDispatcher,
)
from ledgerloop.core.enums import ActionKind, Currency
from ledgerloop.core.ids import ActionId, IdempotencyKey, TenantId
from ledgerloop.core.models import Action, RunSpec
from ledgerloop.core.money import Money
from ledgerloop.runtime import ActionExecutor, Compensator, replay_effects


def build(kind: ActionKind, order: str, amount: str) -> Action:
    """One effect, with its idempotency key derived from its content."""
    money = Money.from_major(amount, Currency.INR)
    return Action(
        id=ActionId.generate(),
        kind=kind,
        description=f"{kind.value} on {order}",
        amount=money,
        counterparty="mer_9f21c",
        idempotency_key=IdempotencyKey.derive(kind.value, order, str(money.minor_units)),
    )


async def main() -> None:
    clock = ManualClock(datetime(2026, 5, 1, 9, 0, tzinfo=UTC))

    runs = InMemoryRunStore(clock=clock)
    ledger = InMemoryLedgerStore()
    idempotency = InMemoryIdempotencyStore()
    dispatcher = RecordingDispatcher()

    executor = ActionExecutor(
        idempotency=idempotency, dispatcher=dispatcher, ledger=ledger, clock=clock
    )
    compensator = Compensator(
        runs=runs,
        ledger=ledger,
        dispatcher=dispatcher,
        idempotency=idempotency,
        clock=clock,
    )

    tenant = TenantId.generate()
    run = await runs.create(
        RunSpec(tenant_id=tenant, objective="Settle this morning's batch")
    )
    run = await runs.save(run.start(at=clock.now()), expected_version=0)

    # --- the batch goes out ----------------------------------------------
    applied = [
        build(ActionKind.CAPTURE, "ord_1001", "2500"),
        build(ActionKind.CAPTURE, "ord_1002", "1800"),
        build(ActionKind.PAYOUT, "acc_44", "9000"),
    ]
    for action in applied:
        await executor.execute(action, run_id=run.id, tenant_id=tenant)
        await clock.advance_seconds(1)

    standing = replay_effects(await ledger.read(tenant, run.id))
    print(f"applied       -> {len(standing)} effects out in the world")
    for effect in standing:
        print(f"                {effect.kind.value:<8} {effect.amount}")

    # --- ...and then something downstream goes wrong ----------------------
    print("\nsomething breaks, roll the whole thing back\n")
    report = await compensator.compensate(await runs.get(tenant, run.id))

    print(f"rollback      -> {report}")
    print(f"              complete={report.complete} stranded={report.stranded}")
    print(f"              reversed: {[a.kind.value for a in dispatcher.compensated]}")

    stored = await runs.get(tenant, run.id)
    print(f"run           -> {stored.state.value}: {stored.failure_reason}")

    # The payout is still out there. That is the honest answer - it has no
    # reversal, and nothing here is going to pretend otherwise.
    left = replay_effects(await ledger.read(tenant, run.id))
    print(f"\nstill applied -> {[e.kind.value for e in left]}")

    # --- the audit trail --------------------------------------------------
    await ledger.verify_chain(tenant, run.id)
    print("\naudit chain verified. entries:")
    for entry in await ledger.read(tenant, run.id):
        print(f"  {entry.sequence:>2}  {entry.event_type.value}")


if __name__ == "__main__":
    asyncio.run(main())
