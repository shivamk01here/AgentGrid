"""An approval nobody answers, and the run that has to end anyway.

Runs entirely in memory - no database, no payment provider, no API key. A
refund parks itself for a signature, the signature never comes, and the
deadline the caller set goes by. The sweep is the thing that notices.

    python examples/expired_approval.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

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
from ledgerloop.core.errors import LedgerloopError
from ledgerloop.core.ids import ActionId, IdempotencyKey, TenantId
from ledgerloop.core.models import Action, RunSpec
from ledgerloop.core.money import Money
from ledgerloop.policy import ThresholdPolicyEngine
from ledgerloop.runtime import ActionExecutor, Reaper, RunCoordinator

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
    reaper = Reaper(runs=runs, approvals=gateway, ledger=ledger, clock=clock)

    tenant = TenantId.generate()
    # The caller says when this stops being worth doing. Nothing expires
    # without one of these.
    run = await runs.create(
        RunSpec(
            tenant_id=tenant,
            objective="Clear this week's duplicate-charge queue",
            deadline=clock.now() + timedelta(days=5),
        )
    )
    run = await runs.save(run.start(at=clock.now()), expected_version=0)

    # --- it stops for a human ---------------------------------------------
    refund = build_refund("ord_2002", "84000")
    halted = await coordinator.propose(run, refund)
    print(f"large refund  -> halted={halted.halted} state={halted.run.state.value}")
    print(f"              waiting on: {halted.approval_id}")
    queue = [request async for request in gateway.list_pending(tenant)]
    print(f"              reviewer queue: {len(queue)}")

    # --- ...and the human never comes -------------------------------------
    await clock.advance(timedelta(days=6))
    report = await reaper.sweep()
    print(f"\nsweep         -> {report}")

    stored = await runs.get(tenant, run.id)
    print(f"run           -> {stored.state.value}, stopped because {stored.stop_reason}")
    request = await gateway.get(tenant, halted.approval_id)
    print(f"approval      -> {request.state.value}")
    queue = [pending async for pending in gateway.list_pending(tenant)]
    print(f"reviewer queue-> {len(queue)}")
    print(f"dispatches    -> {dispatcher.dispatch_count}")

    # --- a late signature is not a way back in ----------------------------
    try:
        await gateway.submit(
            tenant, halted.approval_id, approved=True, actor=APPROVER, at=clock.now()
        )
    except LedgerloopError as exc:
        print(f"\nlate approval -> refused: {exc.message}")

    # --- the audit trail --------------------------------------------------
    await ledger.verify_chain(tenant, run.id)
    print("\naudit chain verified. entries:")
    for entry in await ledger.read(tenant, run.id):
        print(f"  {entry.sequence:>2}  {entry.event_type.value}")


if __name__ == "__main__":
    asyncio.run(main())
