"""What happened on a run, told to someone who was not there.

The case for the whole project is an auditor asking why a refund went out.
The answer is in the run's hash-chained ledger, entry by entry - and an
auditor does not want forty JSON payloads, they want the story: what the
agent proposed and for how much, what policy said about it and under which
rule, who signed off, what the provider answered, and whether the money is
still out there.

This folds a chain into exactly that: one record per action, in the order
the actions were first seen, plus how the run opened and closed and what it
moved. It reads the chain only after the chain verifies. A report built from
a ledger somebody edited would be a well-formatted lie, and the reason the
ledger is hash-chained is so that it cannot be one.

Everything here is read-only. Nothing is settled, reconciled or reversed;
an action still in doubt is reported as in doubt, not resolved for the
report's convenience.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Any

from ledgerloop.core.enums import Currency, LedgerEventType, StopReason
from ledgerloop.core.money import Money
from ledgerloop.runtime.effects import replay_effects, value_by_currency

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from ledgerloop.core.ids import RunId, TenantId
    from ledgerloop.core.models import LedgerEntry
    from ledgerloop.core.ports import LedgerStore

__all__ = ["ActionOutcome", "ActionRecord", "Auditor", "RunAudit", "build_audit"]


@unique
class ActionOutcome(StrEnum):
    """Where an action ended up, as far as the run's ledger knows."""

    PROPOSED = "proposed"
    """Evaluated and then went no further - the run ended or moved on."""

    DENIED = "denied"
    """Policy refused it. Nothing was dispatched."""

    STOPPED = "stopped at ceiling"
    """It would have carried the run past its value ceiling. Nothing was
    dispatched and nobody was asked."""

    AWAITING_APPROVAL = "awaiting approval"
    """A reviewer has been asked and has not answered."""

    NOT_APPROVED = "not approved"
    """The request came back rejected, expired or withdrawn."""

    IN_DOUBT = "in doubt"
    """Dispatched with no answer yet. It may or may not have landed."""

    SETTLED = "settled"
    """The provider confirmed it."""

    REPLAYED = "replayed"
    """The same effect had already settled; this proposal moved nothing new."""

    FAILED = "failed"
    """The provider definitively refused it. Nothing landed."""

    REVERSED = "reversed"
    """It landed, and a rollback has since undone it."""


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """Everything the ledger says about one action, folded into one line."""

    action_id: str
    outcome: ActionOutcome
    kind: str | None = None
    description: str | None = None
    amount: Money | None = None
    counterparty: str | None = None
    decision: str | None = None
    """The policy effect: allow, require_approval or deny."""
    rule_id: str | None = None
    policy_reason: str | None = None
    approval_id: str | None = None
    approved_by: str | None = None
    provider_reference: str | None = None
    detail: str | None = None
    """The provider's error, why an approval closed, or what a failed reversal
    left behind - whatever the outcome alone does not say."""


@dataclass(frozen=True, slots=True)
class RunAudit:
    """A run's ledger, read back as the account of what it did."""

    run_id: RunId
    tenant_id: TenantId
    entry_count: int
    verified: bool
    """True only when the chain was checked before it was read. `Auditor`
    always checks; `build_audit` on its own cannot know."""
    actions: tuple[ActionRecord, ...] = ()
    objective: str | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    closing_event: str | None = None
    """The entry that ended the run - run.completed, run.failed and so on -
    or None while the run is still going."""
    closing_reason: str | None = None
    value_moved: dict[str, int] = field(default_factory=dict)
    """Minor units per ISO currency code, from the effects still standing."""
    in_doubt: tuple[str, ...] = ()
    """Action ids dispatched with no known outcome. Reconcile these."""

    @property
    def closed(self) -> bool:
        return self.closing_event is not None

    def render(self) -> str:
        """The audit as plain text, for a person rather than a parser."""
        moved = ", ".join(
            _format_minor(minor, code) for code, minor in sorted(self.value_moved.items())
        )
        lines = [f"Run {self.run_id}"]
        if self.objective:
            lines.append(f"  objective   {self.objective}")
        chain = "hash chain verified" if self.verified else "chain NOT verified"
        lines.append(f"  ledger      {self.entry_count} entries, {chain}")
        if self.opened_at is not None:
            lines.append(f"  opened      {_format_time(self.opened_at)}")
        if self.closed_at is not None and self.closing_event is not None:
            ending = self.closing_event
            if self.closing_reason:
                ending = f"{ending} - {self.closing_reason}"
            lines.append(f"  closed      {_format_time(self.closed_at)}  {ending}")
        else:
            lines.append("  closed      not yet")
        lines.append(f"  moved       {moved or 'nothing'}")
        lines.append(f"  in doubt    {', '.join(self.in_doubt) or 'nothing'}")

        for number, record in enumerate(self.actions, start=1):
            lines.append("")
            lines.extend(_render_record(number, record))
        return "\n".join(lines)


class Auditor:
    """Reads a run's ledger back as an account of what the run did.

    Example:
        auditor = Auditor(ledger=ledger)
        audit = await auditor.report(tenant_id, run_id)
        print(audit.render())
    """

    def __init__(self, *, ledger: LedgerStore) -> None:
        self._ledger = ledger

    async def report(self, tenant_id: TenantId, run_id: RunId) -> RunAudit:
        """Verify a run's chain, then fold it into an audit.

        Raises:
            LedgerIntegrityError: The chain does not verify. No report is
                produced from it - not even a partial one.
        """
        await self._ledger.verify_chain(tenant_id, run_id)
        entries = await self._ledger.read(tenant_id, run_id)
        return build_audit(run_id, tenant_id, entries, verified=True)


def build_audit(
    run_id: RunId,
    tenant_id: TenantId,
    entries: Sequence[LedgerEntry],
    *,
    verified: bool = False,
) -> RunAudit:
    """Fold a run's entries, in sequence order, into an audit.

    Pure: it trusts what it is given. Use `Auditor.report` when the answer is
    going to be shown to anyone, so the chain is checked first.
    """
    records: dict[str, ActionRecord] = {}
    approvals: dict[str, str] = {}
    objective: str | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    closing_event: str | None = None
    closing_reason: str | None = None

    for entry in entries:
        event = entry.event_type
        payload = entry.payload

        if event is LedgerEventType.RUN_STARTED:
            objective = _text(payload.get("objective"))
            opened_at = entry.occurred_at
            continue

        if event in _CLOSING_EVENTS:
            closed_at = entry.occurred_at
            closing_event = event.value
            closing_reason = _closing_reason(event, payload)
            stopped = _text(payload.get("action_id"))
            if (
                event is LedgerEventType.RUN_FAILED
                and payload.get("stop_reason") == StopReason.BUDGET_EXHAUSTED.value
                and stopped is not None
            ):
                record = _ensure(records, stopped, payload)
                records[stopped] = replace(
                    record, outcome=ActionOutcome.STOPPED, detail=closing_reason
                )
            continue

        if event in _CLOSED_APPROVALS:
            approval_id = _text(payload.get("approval_id"))
            target = None if approval_id is None else approvals.get(approval_id)
            if target is not None and target in records:
                state = _text(payload.get("state")) or event.value.split(".")[-1]
                records[target] = replace(
                    records[target],
                    outcome=ActionOutcome.NOT_APPROVED,
                    detail=f"approval {state}",
                )
            continue

        action_id = _text(payload.get("action_id"))
        if action_id is None:
            continue

        if event is LedgerEventType.ACTION_PROPOSED:
            records[action_id] = replace(
                _ensure(records, action_id, payload), outcome=ActionOutcome.PROPOSED
            )

        elif event is LedgerEventType.ACTION_EVALUATED:
            effect = _text(payload.get("effect"))
            record = replace(
                _ensure(records, action_id, payload),
                decision=effect,
                rule_id=_text(payload.get("rule_id")),
                policy_reason=_text(payload.get("reason")),
            )
            if effect == "deny":
                record = replace(record, outcome=ActionOutcome.DENIED)
            records[action_id] = record

        elif event is LedgerEventType.APPROVAL_REQUESTED:
            approval_id = _text(payload.get("approval_id"))
            if approval_id is not None:
                approvals[approval_id] = action_id
            records[action_id] = replace(
                _ensure(records, action_id, payload),
                outcome=ActionOutcome.AWAITING_APPROVAL,
                approval_id=approval_id,
            )

        elif event is LedgerEventType.APPROVAL_GRANTED:
            records[action_id] = replace(
                _ensure(records, action_id, payload),
                approval_id=_text(payload.get("approval_id")),
                approved_by=_text(payload.get("decided_by")),
            )

        elif event is LedgerEventType.ACTION_DISPATCHED:
            records[action_id] = replace(
                _ensure(records, action_id, payload), outcome=ActionOutcome.IN_DOUBT
            )

        elif event is LedgerEventType.ACTION_SETTLED:
            record = _ensure(records, action_id, payload)
            records[action_id] = replace(
                record,
                outcome=ActionOutcome.REPLAYED
                if payload.get("replayed") is True
                else ActionOutcome.SETTLED,
                provider_reference=_text(payload.get("provider_reference"))
                or record.provider_reference,
            )

        elif event is LedgerEventType.ACTION_FAILED:
            records[action_id] = _fold_failure(_ensure(records, action_id, payload), payload)

        elif event is LedgerEventType.ACTION_COMPENSATED:
            reversal = _text(payload.get("reversal_kind"))
            records[action_id] = replace(
                _ensure(records, action_id, payload),
                outcome=ActionOutcome.REVERSED,
                detail=None if reversal is None else f"reversed by a {reversal}",
            )

    standing = replay_effects(entries)
    return RunAudit(
        run_id=run_id,
        tenant_id=tenant_id,
        entry_count=len(entries),
        verified=verified,
        actions=tuple(records.values()),
        objective=objective,
        opened_at=opened_at,
        closed_at=closed_at,
        closing_event=closing_event,
        closing_reason=closing_reason,
        value_moved=value_by_currency(standing),
        in_doubt=tuple(str(effect.action_id) for effect in standing if effect.indeterminate),
    )


_CLOSING_EVENTS = frozenset(
    {
        LedgerEventType.RUN_COMPLETED,
        LedgerEventType.RUN_FAILED,
        LedgerEventType.RUN_CANCELLED,
        LedgerEventType.RUN_EXPIRED,
        LedgerEventType.RUN_COMPENSATED,
    }
)
"""Entries that end a run. Checked before anything keyed on action_id: the
ceiling's RUN_FAILED names the action it stopped, and is still about the run."""

_CLOSED_APPROVALS = frozenset(
    {
        LedgerEventType.APPROVAL_REJECTED,
        LedgerEventType.APPROVAL_EXPIRED,
        LedgerEventType.APPROVAL_WITHDRAWN,
    }
)
"""Entries that close a request without a grant. They name the approval, not
the action, so they are matched back through the request that raised it."""


def _ensure(records: dict[str, ActionRecord], action_id: str, payload: dict[str, Any]) -> ActionRecord:
    """The record for `action_id`, filling in whatever this entry describes.

    The first entry about an action is usually its proposal, but an action
    run straight through the executor starts at its dispatch, and either one
    carries the description. Later entries never overwrite what is known.
    """
    record = records.get(action_id) or ActionRecord(
        action_id=action_id, outcome=ActionOutcome.PROPOSED
    )
    return replace(
        record,
        kind=record.kind or _text(payload.get("kind")),
        description=record.description or _text(payload.get("description")),
        amount=record.amount or _money(payload),
        counterparty=record.counterparty or _text(payload.get("counterparty")),
    )


def _fold_failure(record: ActionRecord, payload: dict[str, Any]) -> ActionRecord:
    """Fold an ACTION_FAILED entry, which means three different things."""
    error = _text(payload.get("error")) or _text(payload.get("detail"))
    if payload.get("compensation") is True:
        # A reversal failed, or went out and never came back. Either way the
        # action itself still stands as it was.
        if payload.get("indeterminate") is True:
            return replace(record, detail=f"reversal in doubt: {error or 'no answer'}")
        return replace(record, detail=f"reversal failed: {error or 'no reason given'}")
    if payload.get("indeterminate") is True:
        return replace(record, outcome=ActionOutcome.IN_DOUBT, detail=error)
    return replace(record, outcome=ActionOutcome.FAILED, detail=error)


def _closing_reason(event: LedgerEventType, payload: dict[str, Any]) -> str | None:
    """The words a closing entry gives for why the run ended."""
    if event is LedgerEventType.RUN_COMPLETED:
        return _text(payload.get("summary"))
    if event is LedgerEventType.RUN_EXPIRED:
        return "deadline passed"
    if event is LedgerEventType.RUN_COMPENSATED:
        return "rolled back"
    return _text(payload.get("reason"))


def _render_record(number: int, record: ActionRecord) -> list[str]:
    """One action as a few indented lines."""
    headline = f"  {number}. {record.kind or 'action'}"
    if record.amount is not None:
        headline += f" {record.amount}"
    if record.counterparty:
        headline += f" to {record.counterparty}"
    headline += f" - {record.outcome.value}"
    if record.provider_reference:
        headline += f" (ref {record.provider_reference})"

    lines = [headline]
    if record.description:
        lines.append(f"     {record.description}")
    if record.decision:
        policy = f"     policy: {record.decision}"
        if record.rule_id:
            policy += f" by {record.rule_id}"
        if record.policy_reason:
            policy += f" - {record.policy_reason}"
        lines.append(policy)
    if record.approved_by:
        lines.append(f"     approved by {record.approved_by} ({record.approval_id})")
    if record.detail:
        lines.append(f"     {record.detail}")
    return lines


def _money(payload: dict[str, Any]) -> Money | None:
    """Minor units plus currency, both or neither."""
    minor = payload.get("amount_minor")
    code = payload.get("currency")
    if not isinstance(minor, int) or isinstance(minor, bool) or not isinstance(code, str):
        return None
    try:
        return Money(minor, Currency(code))
    except ValueError:
        return None


def _format_minor(minor: int, code: str) -> str:
    """A per-currency total as it reads on a statement."""
    try:
        return Money(minor, Currency(code)).format()
    except ValueError:
        return f"{minor} {code} (minor units)"


def _format_time(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S %Z").strip()


def _text(raw: Any) -> str | None:
    """A payload field as a string, treating anything else as absent."""
    return raw if isinstance(raw, str) and raw else None
