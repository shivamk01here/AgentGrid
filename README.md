<div align="center">

<img src="logo1.png" alt="Ledgerloop" width="420" />

### The runtime for AI agents that move money.

Agents are good at the judgment work buried in payment operations. They are
catastrophic at it without idempotency, an audit trail, and a human in the loop.
Ledgerloop is the layer that makes the difference.

[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Typed](https://img.shields.io/badge/mypy-strict-blue)](https://mypy-lang.org/)

</div>

---

## The problem

Payments mostly work. The cost lives in the 1–5% that doesn't.

A settlement file that won't tie out. A chargeback with a seven-day deadline. A
payout that failed with a bank narration nobody can parse. A refund that needs a
policy check and a signature. This work is high-volume, semi-structured, and
requires reading messy human artifacts — which is exactly what deterministic code
is bad at and language models are good at.

So why isn't every payments team running agents already? Because the moment an
agent touches money, a general-purpose agent framework becomes a liability:

| What generic frameworks do | What that costs you |
|---|---|
| Retry a failed call blindly | You pay twice |
| Keep a conversation in memory | The process dies, the run is gone |
| Log free text | An auditor asks why ₹2,00,000 moved, and you have a chat transcript |
| Execute whatever the model decides | No threshold, no approval, no ceiling |
| Timeout and move on | The effect may have landed. Nobody knows |

Ledgerloop exists for the last column.

## What it is

A Python runtime for agents whose actions have financial consequences. The agent
loop is the small part. The guarantees around it are the product.

**Exactly-once effects.** Every value-moving action is claimed against an
idempotency store *before* dispatch and settled after. A key is derived from the
action's content, so the same logical effect proposed twice collides on the
second attempt. A crash between claim and settle produces an `IN_FLIGHT` record
that must be reconciled — never blindly retried.

**A hash-chained audit ledger.** Every model turn, tool call, argument, policy
decision, and approval is appended to a per-run chain, each entry carrying the
digest of its predecessor. Alter a historical record and every entry after it
fails verification. This is evidence, not logging.

**Approval gates that actually gate.** Policy classifies each proposed action by
risk tier and returns `ALLOW`, `REQUIRE_APPROVAL`, or `DENY`. A gated run halts,
persists, and resumes days later on a reviewer's decision. Approvals bind to the
action's *fingerprint* — approving a ₹5,000 refund can never authorize a
₹5,00,000 one.

**Durable, resumable runs.** Runs are persisted aggregates with explicit state
transitions and optimistic concurrency. Crash at step 7, resume at step 7. Steps
already committed are never re-executed.

**Money that behaves like money.** Integer minor units and an ISO currency, with
no constructor that accepts a float, no arithmetic across currencies, and an
`allocate` that conserves every last paisa when splitting.

## Design principles

1. **Illegal states are unrepresentable.** Frozen entities, validated
   transitions, closed enums. A `Run` cannot be in a state it never legally
   reached.
2. **Classification drives behavior, not string matching.** Every failure
   carries a `FailureClass`. `TRANSIENT` retries. `INDETERMINATE` never does.
3. **The core does no I/O.** `ledgerloop.core` depends on the standard library
   and nothing else. Postgres, Redis, PSPs, and model APIs satisfy ports.
4. **Time is injected.** Nothing calls `datetime.now()`. Runs replay exactly.
5. **Async at every boundary.** No sync escape hatches — one blocking call
   stalls every run on the worker.
6. **Tenancy at the port boundary.** Every stored read takes a `TenantId`;
   isolation is not left to a caller's discipline.

## Install

```bash
git clone https://github.com/shivamk01here/ledgerloop.git
cd ledgerloop
pip install -e .
```

The core has no third-party dependencies. Install a model provider's SDK only
if you use that provider.

## A first agent

```python
import asyncio

from ledgerloop import Agent, AgentConfig, AnthropicProvider
from ledgerloop.tools.builtins import DateTimeTool


async def main() -> None:
    agent = Agent(
        AgentConfig(
            name="settlement-analyst",
            system_prompt="You reconcile settlement files. Cite every figure.",
            effort="high",
        )
    )
    agent.attach_provider(AnthropicProvider())
    agent.attach_tool(DateTimeTool())

    result = await agent.run_loop("Which settlement batches are still open?")

    print(result.output)
    for invocation in result.invocations:
        print(f"  {invocation.name}({invocation.arguments}) -> {invocation.success}")


asyncio.run(main())
```

`run_loop` returns the full step ledger — every turn, its reasoning summary, its
tool calls, and its token spend. `run()` returns just the final text when that's
all you need.

## Modelling an action

Actions are proposals. Constructing one is always safe; only dispatch has
consequences.

```python
from ledgerloop.core import (
    Action, ActionId, ActionKind, Currency, IdempotencyKey, Money,
)

amount = Money.from_major("4310.50", Currency.INR)

refund = Action(
    id=ActionId.generate(),
    kind=ActionKind.REFUND,
    description="Duplicate charge on order #88213",
    amount=amount,
    counterparty="mer_9f21c",
    # Derived, never random - the same effect must compute the same key.
    idempotency_key=IdempotencyKey.derive(
        "refund", "ord_88213", str(amount.minor_units), amount.currency
    ),
)
```

Omit the idempotency key on a value-moving action and construction fails. That
is the point.

## A refund that stops for a human

The coordinator puts every proposal through policy. Small ones go through;
large ones park the run until somebody signs off, and pick up exactly where
they stopped.

```python
result = await coordinator.propose(run, build_refund("ord_1001", "1200"))
# -> executed immediately, below the review threshold

halted = await coordinator.propose(result.run, build_refund("ord_2002", "84000"))
# -> halted, run is AWAITING_APPROVAL, nothing reached the provider

# ...a day later, once a reviewer has decided...
await gateway.submit(tenant, halted.approval_id, approved=True, actor=approver, at=now)
resumed = await coordinator.resume(halted.run, large)
# -> executed exactly once, against the fingerprint that was approved
```

A grant covers one action fingerprint. Come back with a different amount and
the resume is refused rather than riding on the old signature.

Run it end to end, no database and no API key required:

```bash
python examples/gated_refund.py
```

## A batch that has to be walked back

A run that fails halfway has usually already moved money. The compensator
reads the run's own ledger to work out what is still standing, then reverses
it newest-first — each reversal under its own idempotency claim, so running
the rollback twice does not refund twice.

```python
report = await compensator.compensate(run)
# -> standing=3 compensated=2 irreversible=1

report.complete   # False - something is still out there
report.stranded   # 1 - the payout, which has no reversal
```

It refuses to reverse three things, and each one leaves the run `FAILED`
rather than `COMPENSATED`: a kind with no reversal, an effect whose outcome
was never determined, and a reversal the provider declined. Half a rollback
is worse than either end of it, so it is reported rather than rounded up.

A rollback is also one-way: the run never goes back to `RUNNING`. So you can
ask first. `plan` reads the same chain and claims, decides the same way, and
then claims nothing, sends nothing and leaves the run where it is:

```python
plan = await compensator.plan(run)
# payout   9000.00 INR  irreversible
# capture  1800.00 INR  reverse by refund
# capture  2500.00 INR  reverse by refund

plan.complete     # False - a rollback now would leave the payout standing
plan.stranded     # the steps it would leave, each with its verdict
```

Every step gets one of six verdicts: reverse, already reversed,
irreversible, in doubt, reversal in flight, or previously rejected (with the
provider's reason). A plan is a reading, not a reservation, and `compensate`
decides again from whatever it finds when it runs.

```bash
python examples/rolled_back_batch.py
```

## A run that never lies about what it did

The ledger is the audit trail, so the entry explaining a state change has to
land in the same transaction as the change itself. A run that reaches
`SUCCEEDED` while its ledger has no entry saying so is worse than a run that
failed: the first one gets believed.

```python
uow = InMemoryUnitOfWork(runs, steps, ledger, idempotency)

async with uow:
    await uow.runs.save(run.succeed(at=now), expected_version=version)
    await uow.ledger.append(
        run.id, tenant_id, LedgerEventType.RUN_COMPLETED, {}, occurred_at=now
    )
    await uow.idempotency.settle(key, tenant_id, receipt, at=now)
```

Writes are visible inside the block, the way they are in a database session,
so the code reads normally. Leaving the block cleanly is what makes them real:
if any of it raises, none of it is in the stores, the run is still where it
was, and the chain has no entry claiming otherwise.

Two details that are easy to get wrong. A rolled-back ledger append leaves no
gap in the sequence numbers, because a hole in the chain is indistinguishable
from a deleted row. And a rolled-back claim frees the key rather than leaving
an `IN_FLIGHT` record behind — the reconciler reads that as a payment whose
fate is unknown, and goes looking for a receipt nobody has.

## A run that knows when to stop

Policy decides whether one action may go. A run's budget decides how much the
whole run may move, and it is there for the day the policy is wrong.

```python
spec = RunSpec(
    tenant_id=tenant,
    objective="Clear the duplicate-charge queue",
    budget=RunBudget(max_value_moved=Money.from_major("10000", Currency.INR)),
)

await coordinator.propose(run, build_refund("ord_1", "4000"))   # executed
await coordinator.propose(run, build_refund("ord_2", "4000"))   # executed
await coordinator.propose(run, build_refund("ord_3", "4000"))
# -> BudgetExhaustedError: 4000.00 INR would take this run to 12000.00 INR,
#    past its ceiling of 10000.00 INR. The run is FAILED; nothing was sent.
```

The check happens before anything is dispatched and before any approval is
raised, and again on resume — an approval authorizes one action, not a bigger
budget. It is measured against the run's own ledger, so an effect that timed
out counts as though it landed. Proposing an effect that is already standing
does not count twice. An amount in another currency fails closed.

## An approval that never comes

A halted run is safe: nothing was dispatched, and the grant it is waiting on
binds to one action fingerprint. It is not free. It holds a request in a
reviewer's queue, and the deadline the caller set — because the case stops
mattering after Friday — goes by with nothing watching it.

The reaper is what watches. It sweeps the runs whose deadline passed while
they were still halted, ends each one, and retires the request behind it.

```python
report = await reaper.sweep()
# -> checked=4 expired=4 approvals_retired=3 skipped=0

# the run is EXPIRED with DEADLINE_EXCEEDED on it, and its reviewer's queue
# is that much shorter. nobody can sign off on it now.
```

The run moves first and the approval second, both under the run's version
check. Do it the other way round and a worker that resumed a run a moment
earlier has a perfectly good grant pulled out from under it.

Expiry is opt-in: a run whose `RunSpec` carries no deadline is never swept.
And nothing here reverses anything — a run may well have moved money before
it halted, and walking that back is the compensator's job.

```bash
python examples/expired_approval.py
```

## A case that gets called off

Not every halted run is waiting on a deadline that quietly passes — a case
can get resolved another way, a duplicate can turn up, an operator can just
say stop. The coordinator can cancel a run directly, and if it was sitting
on an approval, that request is retired in the same call rather than left
PENDING for a reviewer to find later.

```python
cancelled = await coordinator.cancel(halted.run, reason="Duplicate case")
# -> run is CANCELLED, and the approval it was waiting on is WITHDRAWN
```

The run moves first, under its own version check, the same order the reaper
uses and for the same reason: a worker that had just resumed the run onto a
fresh grant must not have that grant pulled out from under it because a
cancel request arrived a moment later and lost the race cleanly instead.

Withdrawal is not expiry wearing a different label. Expiry means a deadline
passed with nobody answering; withdrawal means the question stopped applying
before anyone had to answer it. Either way a decision that already landed
stands — cancelling a run after its approval was granted retires nothing,
because the grant is the true record of what happened and a cancellation
does not get to rewrite it.

```bash
python examples/cancelled_run.py
```

## A run you want to stop, not end

Cancelling is final. Sometimes what you want is a brake: the provider is
having an incident, or someone wants to look at what the agent is about to do
before it does the next thing. The coordinator can put a running run on hold
and lift the hold later.

```python
held = await coordinator.suspend(run, reason="Provider incident")
await coordinator.propose(held, refund)
# -> StateTransitionError: nothing was evaluated, nothing was dispatched

running = await coordinator.lift_hold(held, reason="Provider is back")
await coordinator.propose(running, refund)
# -> executed, exactly as if the pause had never happened
```

A hold raises no approval and waits on no one, so there is nothing to retire
and nothing for a reviewer to find. What it does share with a halted run is
the deadline: a run left suspended past its deadline is expired by the
reaper like any other, so a hold somebody forgot about cannot outlive the
case it was placed on.

`lift_hold` only takes a suspended run. `resume` is still the way back from
an approval, and it will not take a run that is merely held.

```bash
python examples/held_run.py
```

## A run that is not done until it knows

A run begins and ends through the coordinator too, and both ends are written
down. `start` opens the chain with what the run was for and the limits it was
given; `complete` closes it. A chain that simply stops after its last
settlement looks the same whether the run finished or its worker died.

```python
run = await coordinator.start(run)
# -> RUNNING, and run.started is the first entry in its ledger

second = await coordinator.propose(first.run, build_refund("ord_4002", "900"))
# -> the connection dropped. nobody knows whether this one landed.

await coordinator.complete(second.run)
# -> IndeterminateError: 1 effect(s) with no known outcome. still RUNNING.

await reconciler.sweep()
# -> checked=1 confirmed=1 failed=0 not_found=0 unresolved=0 skipped=0

done = await coordinator.complete(second.run, summary="Two duplicate charges refunded")
# -> SUCCEEDED, and run.completed says 2100.00 INR moved
```

`SUCCEEDED` means every effect committed, so a run with an effect still in
doubt cannot get there. The agent that was just told "indeterminate" is the
party most likely to shrug and call it finished, which is why the check is
not left to it. A provider that definitely said no is different: that is an
answer, and a run can finish having been refused something.

The total in the closing entry is added up from the run's own ledger, in
minor units per currency. A refund the reconciler confirmed an hour later
counts, though the run never heard back about it directly.

`start` only takes a `PENDING` run. A halted one comes back through `resume`
or `lift_hold`, and starting it again is not a way round either.

```bash
python examples/finished_run.py
```

## A run that cannot go on

Some runs end because something they needed is gone: the merchant account
was closed, the tool the agent relies on is returning nonsense, the agent
has used up its own retries. That is not a cancellation - nobody stopped a
run that could have finished. It is a failure, and the chain should say so.

```python
second = await coordinator.propose(first.run, build_refund("ord_5002", "900"))
# -> the provider refused it outright: the merchant account is closed

failed = await coordinator.fail(
    second.run, "Merchant account closed - remaining refunds cannot be issued"
)
# -> FAILED, stop_reason error, and run.failed is the last entry in its ledger
```

`fail` only takes a running run and writes `run.failed` with the reason and
a stop reason, `error` unless the caller names a better one. Every other way
the coordinator fails a run, the value ceiling and a turned-down approval,
writes the same entry through the same code.

It leaves what the run did in place. A refund that went out before the
failure is still out there afterwards, and a failed run is terminal, so the
compensator will not take it later. If you want it walked back, call the
compensator instead of `fail`, while the run is still running. It ends the
run itself, `COMPENSATED` or `FAILED`.

```bash
python examples/failed_run.py
```

## Why did that money move?

The question this whole project exists to answer. The answer is in the run's
hash-chained ledger, but as a chain of JSON payloads, with one action's story
spread across six or seven entries. The auditor verifies the chain and reads
it back as one record per action.

```python
audit = await Auditor(ledger=ledger).report(tenant, run.id)
print(audit.render())
```

```
Run run_269c5f3df8a81cb427a0f0aee366c56e
  objective   Clear this morning's duplicate-charge queue
  ledger      18 entries, hash chain verified
  opened      2026-05-01 09:00:00 UTC
  closed      2026-05-01 10:00:00 UTC  run.completed - Queue cleared
  moved       85200.00 INR
  in doubt    nothing

  1. refund 1200.00 INR to mer_9f21c - settled (ref rec_1)
     Duplicate charge on order ord_1001
     policy: allow by allow-small-refund - Small refund below the review threshold

  2. refund 84000.00 INR to mer_9f21c - settled (ref rec_2)
     Duplicate charge on order ord_2002
     policy: require_approval by ceiling - 84000.00 INR is at or above the auto-approval ceiling of 25000.00 INR
     approved by priya@example.com (apr_d95105d96f7196ce2eba4987535ec753)

  3. payout 500.00 INR to acc_77 - denied
     Payout to a newly added bank account
     policy: deny by deny-payout - Payouts are not eligible for agent execution

  4. refund 900.00 INR to mer_9f21c - failed
     Duplicate charge on order ord_3003
     policy: allow by allow-small-refund - Small refund below the review threshold
     Simulated invalid_request failure (provider='recording')
```

The chain is verified before it is read. If anyone has edited an entry, there
is no report at all, not even a partial one. A report built from a tampered
ledger would be a well-formatted lie.

Every action ends in one of a fixed set of outcomes: settled, denied, stopped
at the ceiling, not approved, in doubt, failed, replayed or reversed. An
action still in doubt is reported as in doubt, never resolved to make the
report tidier. The total counts what is still standing, so the refund the
provider declined is not in it.

The report is also data. `audit.to_dict()` gives the same audit in a form
`json.dumps` takes as it is, for a dashboard, an API, or an export file. Money
comes out the way the ledger stores it, as integer minor units with the
currency next to them, never as a formatted figure that someone has to parse
back:

```python
record = audit.to_dict()["actions"][1]
```

```json
{
  "outcome": "settled",
  "kind": "refund",
  "amount_minor": 8400000,
  "currency": "INR",
  "decision": "require_approval",
  "rule_id": "ceiling",
  "approved_by": "priya@example.com",
  "provider_reference": "rec_2"
}
```

That excerpt leaves out a few fields; the full record also has the action and
approval ids, the description, the counterparty and the policy reason. The
`verified` flag is passed through unchanged, so whatever consumes the data can
reject an audit whose chain was never checked. There is deliberately no way
to turn that data back into an audit, because nothing would have verified it.

```bash
python examples/audited_run.py
```

## Architecture

```
ledgerloop/
├── core/           Domain: enums, ids, money, models, errors, ports. No I/O.
│   ├── enums.py        Closed vocabularies - the wire format
│   ├── ids.py          Typed, prefixed identifiers
│   ├── money.py        Integer-minor-unit monetary arithmetic
│   ├── models.py       Frozen entities with validated transitions
│   ├── errors.py       Failure hierarchy carrying retry semantics
│   └── ports.py        Async protocols for every external dependency
├── runtime/        Coordinator, executor, reconciler, compensator, reaper, auditor
├── adapters/       Concrete ports: clocks, in-memory stores, approval gateway
├── policy/         Risk classification and approval rules
├── agent/          The loop: config, execution, retries, lifecycle
├── llm/            Model boundary - provider protocol + Anthropic adapter
├── tools/          Tool contract, registry, built-ins
├── memory/         Namespaced agent memory with TTL
├── cache/          Namespaced caching
├── events/         Async pub/sub
├── workflow/       Multi-step workflows with dependency resolution
├── scheduler/      Periodic and delayed execution
├── ratelimit/      Token-bucket limiting
├── auth/           API-key identity and permissions
└── observability/  Structured logging and metrics
```

## Status

**Pre-alpha, and honest about it.**

| Component | State |
|---|---|
| Domain layer (`core/`) | Implemented |
| Agent loop + Anthropic provider | Implemented |
| Policy engine | Implemented — ordered rules, risk classification, hard ceiling |
| Approval gateway | Implemented — role checks, separation of duties, fingerprint binding |
| Run coordinator — start → propose → gate → halt → resume → complete or fail, plus cancel and operator hold | Implemented |
| Action executor — exactly-once dispatch | Implemented |
| Reconciler for in-flight claims | Implemented — reversals included, and a provider still processing is left open |
| Compensator — rollback of applied effects | Implemented — exactly-once reversals, refuses to guess, can plan a rollback without running it |
| Reaper — expiry of halted runs | Implemented — deadline-driven, retires the request behind it |
| Auditor — a run's ledger read back as what it did | Implemented — verifies the chain first, one record per action, as text or as JSON-ready data |
| Run value ceiling | Implemented — checked before dispatch and approval, counts unanswered effects |
| Idempotency, ledger, run, step stores | Implemented **in memory only** |
| Unit of work — a run's state and its ledger entries commit together | Implemented **in memory only** |
| Durable (Postgres) adapters | Not started |
| Action dispatchers (PSP, bank) | Not started — port defined, fake for tests only |
| Dashboard | Not started |

The in-memory adapters are correct, not durable: they enforce the same
atomicity, isolation, and concurrency guarantees a database must, so a
Postgres adapter has a reference to agree with. They do not survive a restart.

Nothing here has executed against a real payment provider.
**Do not point this at production money yet.**

## Development

```bash
pip install -e ".[dev]"
ruff check src/ tests/
mypy src/ledgerloop/
pytest
```

## License

MIT — see [LICENSE](LICENSE).
