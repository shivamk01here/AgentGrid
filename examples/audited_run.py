"""A run, read back the way an auditor would want to read it.

Runs entirely in memory - no database, no payment provider, no API key. A
morning's refund queue: one small refund goes straight through, a large one
waits an hour for a reviewer, a payout is refused by policy, and the provider
turns one refund down. Then the run's ledger is verified and read back as an
audit - every action with its amount, the rule that decided it, who approved
it, and what the provider said - instead of as forty JSON payloads.

The same audit is then printed as data, the shape a dashboard or an export
file would take it in: money as minor units beside its currency, never a
formatted figure.

    python examples/audited_run.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime

from ledgerloop import Auditor
from ledgerloop.adapters.clock import ManualClock
from ledgerloop.adapters.memory import (
    ApproverDirectory,
    InMemoryApprovalGateway,
    InMemoryIdempotencyStore,
    InMemoryLedgerStore,
    InMemoryRunStore,
    RecordingDispatcher,
)
from ledgerloop.core.enums import ActionKind, Currency, FailureClass
from ledgerloop.core.errors import PolicyViolationError
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

    runs = InMemoryRunStore(clock=clock)
    ledger = InMemoryLedgerStore()
    dispatcher = RecordingDispatcher()
    directory = ApproverDirectory()
    directory.grant_role(APPROVER, "payments-approver")
    gateway = InMemoryApprovalGateway(directory=directory)

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
    run = await coordinator.start(
        await runs.create(
            RunSpec(tenant_id=tenant, objective="Clear this morning's duplicate-charge queue")
        )
    )

    # --- a small refund: straight through ---------------------------------
    first = await coordinator.propose(run, build_refund("ord_1001", "1200"))

    # --- a large one: waits an hour for a reviewer -------------------------
    large = build_refund("ord_2002", "84000")
    halted = await coordinator.propose(first.run, large)
    await clock.advance_seconds(60 * 60)
    await gateway.submit(
        tenant, halted.approval_id, approved=True, actor=APPROVER, at=clock.now()
    )
    resumed = await coordinator.resume(halted.run, large)

    # --- a payout: policy will not let an agent do it ----------------------
    payout = Action(
        id=ActionId.generate(),
        kind=ActionKind.PAYOUT,
        description="Payout to a newly added bank account",
        amount=Money.from_major("500", Currency.INR),
        counterparty="acc_77",
        idempotency_key=IdempotencyKey.derive("payout", "acc_77", "50000"),
    )
    with contextlib.suppress(PolicyViolationError):
        await coordinator.propose(resumed.run, payout)

    # --- the provider turns one down ---------------------------------------
    dispatcher.fail_next(FailureClass.INVALID_REQUEST)
    declined = await coordinator.propose(resumed.run, build_refund("ord_3003", "900"))

    await coordinator.complete(declined.run, summary="Queue cleared")

    # --- and the audit -------------------------------------------------------
    audit = await Auditor(ledger=ledger).report(tenant, run.id)
    print(audit.render())

    # --- and the same audit as data, for whatever stores or serves it --------
    data = audit.to_dict()
    summary = {key: data[key] for key in ("run_id", "verified", "closing_event", "value_moved")}
    approved = next(record for record in data["actions"] if record["approved_by"])
    print()
    print(json.dumps(summary, indent=2))
    print(json.dumps(approved, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
