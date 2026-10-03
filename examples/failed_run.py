"""A run that cannot go on, ended as a failure rather than a cancellation.

Runs entirely in memory - no database, no payment provider, no API key. A
batch of refunds is under way. The first goes through. The provider refuses
the second outright: the merchant account it belongs to has been closed.
Nothing about that is in doubt, and nothing about it is going to change on a
retry, so the agent ends the run as FAILED with the reason on the record.

The first refund is still out there. fail() does not walk it back - that is
what the compensator is for, and a caller who wants it walked back calls the
compensator instead of fail(), not after it.

    python examples/failed_run.py
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
from ledgerloop.core.enums import ActionKind, Currency, FailureClass
from ledgerloop.core.ids import ActionId, IdempotencyKey, TenantId
from ledgerloop.core.models import Action, RunSpec
from ledgerloop.core.money import Money
from ledgerloop.policy import ThresholdPolicyEngine
from ledgerloop.runtime import ActionExecutor, RunCoordinator, replay_effects


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
        RunSpec(tenant_id=tenant, objective="Clear this merchant's duplicate-charge queue")
    )
    run = await coordinator.start(run)

    # --- the first refund goes through ------------------------------------
    first = await coordinator.propose(run, build_refund("ord_5001", "1200"))
    print(f"refund 1      -> succeeded={first.outcome.succeeded}")

    # --- the provider refuses the second, and means it ---------------------
    dispatcher.fail_next(FailureClass.INVALID_REQUEST)
    second = await coordinator.propose(first.run, build_refund("ord_5002", "900"))
    print(f"refund 2      -> succeeded={second.outcome.succeeded}")
    print(f"              error: {second.outcome.error}")

    # --- nothing left in this batch is going to work: end it --------------
    failed = await coordinator.fail(
        second.run, "Merchant account closed - remaining refunds cannot be issued"
    )
    print(f"\nfail          -> run={failed.state.value} stop_reason={failed.stop_reason.value}")
    print(f"              reason: {failed.failure_reason}")

    # --- what it leaves behind --------------------------------------------
    standing = replay_effects(await ledger.read(tenant, run.id))
    print(f"still applied -> {[str(effect.amount) for effect in standing]}")

    # --- the audit trail --------------------------------------------------
    await ledger.verify_chain(tenant, run.id)
    print("\naudit chain verified. entries:")
    for entry in await ledger.read(tenant, run.id):
        print(f"  {entry.sequence:>2}  {entry.event_type.value}")


if __name__ == "__main__":
    asyncio.run(main())
