"""A refund that stops for a human, then finishes.

Runs entirely in memory - no database, no payment provider, no API key. It
shows the shape of the thing: a small refund goes straight through, a large
one parks itself until somebody signs off, and neither one can execute twice.

    python examples/gated_refund.py
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
        RunSpec(tenant_id=tenant, objective="Clear today's duplicate-charge queue")
    )
    run = await runs.save(run.start(at=clock.now()), expected_version=0)

    # --- a small one: straight through -----------------------------------
    small = build_refund("ord_1001", "1200")
    result = await coordinator.propose(run, small)
    print(f"small refund  -> executed={result.executed} dispatches={dispatcher.dispatch_count}")

    # --- a large one: stops for a human ----------------------------------
    large = build_refund("ord_2002", "84000")
    halted = await coordinator.propose(result.run, large)
    print(f"large refund  -> halted={halted.halted} state={halted.run.state.value}")
    print(f"              reason: {halted.decision.reason}")
    print(f"              dispatches so far: {dispatcher.dispatch_count}")

    # --- a day passes, someone signs off ---------------------------------
    await clock.advance_seconds(60 * 60 * 24)
    await gateway.submit(
        tenant, halted.approval_id, approved=True, actor=APPROVER, at=clock.now()
    )
    resumed = await coordinator.resume(halted.run, large)
    print(f"after approval-> executed={resumed.executed} by={APPROVER}")

    # --- the same refund again: replayed, not repaid ---------------------
    again = await coordinator.propose(resumed.run, build_refund("ord_1001", "1200"))
    print(f"duplicate     -> replayed={again.outcome.replayed} dispatches={dispatcher.dispatch_count}")

    # --- the audit trail --------------------------------------------------
    await ledger.verify_chain(tenant, run.id)
    print("\naudit chain verified. entries:")
    for entry in await ledger.read(tenant, run.id):
        print(f"  {entry.sequence:>2}  {entry.event_type.value}")


if __name__ == "__main__":
    asyncio.run(main())
