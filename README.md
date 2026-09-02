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

**Pre-alpha, and honest about it.** The domain layer, agent loop, and Anthropic
provider are implemented. Durable stores, the policy engine, the approval
gateway, and the dispatchers are defined as ports in
[`core/ports.py`](src/ledgerloop/core/ports.py) and not yet implemented — the
interfaces are stable, the adapters are next.

Do not point this at production money yet.

## Development

```bash
pip install -e ".[dev,anthropic]"
ruff check src/ tests/
mypy src/ledgerloop/
pytest
```

## License

MIT — see [LICENSE](LICENSE).
