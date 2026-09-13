<div align="center">

<img src="logo1.png" alt="Ledgerloop" width="420" />

### The runtime for AI agents that move money.

Agents are good at the judgment work buried in payment operations. They are
catastrophic at it without idempotency, an audit trail, and a human in the loop.
Ledgerloop is the layer that makes the difference.

[![CI](https://github.com/shivamk01here/AgentGrid/actions/workflows/ci.yml/badge.svg)](https://github.com/shivamk01here/AgentGrid/actions/workflows/ci.yml)
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
pip install "ledgerloop[anthropic]"
```

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

```bash
python examples/rolled_back_batch.py
```

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
├── runtime/        Coordinator, executor, reconciler, compensator, reaper
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
| Run coordinator — propose → gate → halt → resume | Implemented |
| Action executor — exactly-once dispatch | Implemented |
| Reconciler for in-flight claims | Implemented |
| Compensator — rollback of applied effects | Implemented — exactly-once reversals, refuses to guess |
| Reaper — expiry of halted runs | Implemented — deadline-driven, retires the request behind it |
| Run value ceiling | Implemented — checked before dispatch and approval, counts unanswered effects |
| Idempotency, ledger, run, step stores | Implemented **in memory only** |
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
pip install -e ".[dev,anthropic]"
ruff check src/ tests/
mypy src/ledgerloop/
pytest
```

## License

MIT — see [LICENSE](LICENSE).
