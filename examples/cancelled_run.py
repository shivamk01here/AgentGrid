"""A case that gets called off before anyone signs off on it.

Runs entirely in memory - no database, no payment provider, no API key. A
refund parks itself for a signature, and before a reviewer gets to it the
case is closed some other way. Cancelling the run retires the request
behind it in the same call, rather than leaving it PENDING for a reviewer
to find later.

    python examples/cancelled_run.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ledgerloop.adapters.clock import ManualClock
from ledgerloop.adapters.memory import (
    ApproverDirectory,
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

APPROVER = "priya@example.com"


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

    directory = ApproverDirectory()
    directory.grant_role(APPROVER, "payments-approver")

    runs = InMemoryRunStore(clock=clock)
    ledger = InMemoryLedgerStore()
    gateway = InMemoryApprovalGateway(directory=directory)
    dispatcher = RecordingDispatcher()

    coordinator = RunCoordinator(
        runs=runs,
        policy=ThresholdPolicyEngine(),
        approvals=gateway,
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

    # --- it stops for a human ---------------------------------------------
    refund = build_refund("ord_2002", "84000")
    halted = await coordinator.propose(run, refund)
    print(f"large refund  -> halted={halted.halted} state={halted.run.state.value}")
    queue = [request async for request in gateway.list_pending(tenant)]
    print(f"              reviewer queue: {len(queue)}")

    # --- ...and the case is closed some other way before anyone gets to it -
    cancelled = await coordinator.cancel(halted.run, reason="Chargeback already reversed it")
    print(f"\ncancel        -> run={cancelled.state.value}")

    request = await gateway.get(tenant, halted.approval_id)
    print(f"approval      -> {request.state.value}")
    queue = [pending async for pending in gateway.list_pending(tenant)]
    print(f"reviewer queue-> {len(queue)}")
    print(f"dispatches    -> {dispatcher.dispatch_count}")

    # --- a cancelled run is not a way back in -------------------------------
    try:
        await coordinator.resume(cancelled, refund)
    except StateTransitionError as exc:
        print(f"\nresume attempt -> refused: {exc.message}")

    # --- the audit trail ----------------------------------------------------
    await ledger.verify_chain(tenant, run.id)
    print("\naudit chain verified. entries:")
    for entry in await ledger.read(tenant, run.id):
        print(f"  {entry.sequence:>2}  {entry.event_type.value}")


if __name__ == "__main__":
    asyncio.run(main())
