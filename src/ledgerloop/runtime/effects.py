"""What a run actually did to the outside world.

Rolling a run back means knowing what it applied, and the only record of that
which cannot have been quietly edited is the run's own hash-chained ledger.
So this module reads the chain rather than any mutable table: dispatches
pair up with their settlements, failures cancel out, and what is left
standing is the set of effects still out there in the world.

The one case that does not resolve is an effect the executor left
indeterminate. It is neither applied nor not applied - nobody knows - and it
is reported as exactly that. Reversing it would risk refunding a capture that
never happened; dropping it would risk leaving one behind. Both are wrong, so
the caller is told and a human decides.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from ledgerloop.core.enums import ActionKind, Currency, IdempotencyState, LedgerEventType
from ledgerloop.core.ids import ActionId, IdempotencyKey
from ledgerloop.core.models import Action, ActionReceipt
from ledgerloop.core.money import Money

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from ledgerloop.core.models import LedgerEntry

logger = logging.getLogger(__name__)

__all__ = ["AppliedEffect", "replay_effects"]


@dataclass(frozen=True, slots=True)
class AppliedEffect:
    """One effect a run put into the world, rebuilt from its ledger entries.

    Carries enough to reconstruct the original action, because that is what a
    dispatcher needs in order to reverse it - a provider reference on its own
    tells you nothing about how much to give back.
    """

    action_id: ActionId
    kind: ActionKind
    description: str
    dispatched_at: datetime
    amount: Money | None = None
    counterparty: str | None = None
    idempotency_key: IdempotencyKey | None = None
    provider_reference: str | None = None
    settled_at: datetime | None = None
    indeterminate: bool = False
    """True when the dispatch never came back with an answer. Such an effect
    must not be reversed and must not be forgotten."""

    @property
    def is_reversible(self) -> bool:
        """True when this effect's kind has a reversal and its fate is known."""
        return self.kind.is_reversible and not self.indeterminate

    def to_action(self) -> Action:
        """Rebuild the action that was dispatched.

        The id is preserved, so the reversal lands in the audit trail beside
        the effect it undoes rather than under a fresh id nobody can tie back.
        """
        return Action(
            id=self.action_id,
            kind=self.kind,
            description=self.description,
            amount=self.amount,
            counterparty=self.counterparty,
            idempotency_key=self.idempotency_key,
        )

    def to_receipt(self) -> ActionReceipt:
        """Rebuild the receipt the dispatch returned."""
        return ActionReceipt(
            action_id=self.action_id,
            state=IdempotencyState.SUCCEEDED,
            provider_reference=self.provider_reference,
            settled_at=self.settled_at,
        )


def replay_effects(entries: Sequence[LedgerEntry]) -> tuple[AppliedEffect, ...]:
    """Fold a run's ledger into the effects still standing, oldest first.

    Args:
        entries: The run's chain, in sequence order. Verify it first if the
            answer is going to be acted on - this function trusts what it is
            given.

    Returns:
        Every effect this run dispatched that has not since been reversed or
        settled as failed, in the order they were applied.
    """
    standing: dict[str, AppliedEffect] = {}

    for entry in entries:
        action_id = entry.payload.get("action_id")
        if not isinstance(action_id, str):
            continue

        if entry.event_type is LedgerEventType.ACTION_DISPATCHED:
            effect = _from_dispatch(entry, action_id)
            if effect is not None:
                standing[action_id] = effect

        elif entry.event_type is LedgerEventType.ACTION_SETTLED:
            existing = standing.get(action_id)
            if existing is not None:
                standing[action_id] = _settled(existing, entry)

        elif entry.event_type is LedgerEventType.ACTION_FAILED:
            existing = standing.get(action_id)
            if existing is None:
                continue
            if entry.payload.get("indeterminate") is True:
                # The request left the process and never came back. Keep it,
                # flagged - this is the one nobody may quietly resolve.
                standing[action_id] = _replace_indeterminate(existing)
            else:
                # A definitive provider-side refusal. Nothing landed, so
                # there is nothing to undo.
                del standing[action_id]

        elif entry.event_type is LedgerEventType.ACTION_COMPENSATED:
            standing.pop(action_id, None)

    return tuple(standing.values())


def _from_dispatch(entry: LedgerEntry, action_id: str) -> AppliedEffect | None:
    """Build an effect from an ACTION_DISPATCHED entry, or None if unusable."""
    payload = entry.payload
    kind = _kind(payload.get("kind"))
    if kind is None:
        logger.warning(
            "Ledger entry %s dispatched an unknown action kind %r; skipping it",
            entry.sequence,
            payload.get("kind"),
        )
        return None

    return AppliedEffect(
        action_id=ActionId.parse(action_id),
        kind=kind,
        description=str(payload.get("description") or f"{kind.value} action"),
        dispatched_at=entry.occurred_at,
        amount=_amount(payload),
        counterparty=_optional_str(payload.get("counterparty")),
        idempotency_key=_key(payload.get("idempotency_key")),
    )


def _settled(effect: AppliedEffect, entry: LedgerEntry) -> AppliedEffect:
    """Fold a settlement into an effect."""
    return replace(
        effect,
        provider_reference=_optional_str(entry.payload.get("provider_reference"))
        or effect.provider_reference,
        settled_at=entry.occurred_at,
        indeterminate=False,
    )


def _replace_indeterminate(effect: AppliedEffect) -> AppliedEffect:
    """Mark an effect as having no known outcome."""
    return replace(effect, indeterminate=True)


def _kind(raw: Any) -> ActionKind | None:
    """Parse an action kind written by an older release, tolerantly."""
    if not isinstance(raw, str):
        return None
    try:
        return ActionKind(raw)
    except ValueError:
        return None


def _amount(payload: dict[str, Any]) -> Money | None:
    """Rebuild the amount from minor units plus currency.

    Both or neither. A minor-unit figure without its currency is not an
    amount, it is a number, and guessing which currency it was is how you
    refund 84,000 of the wrong thing.
    """
    minor = payload.get("amount_minor")
    currency = payload.get("currency")
    if minor is None or currency is None:
        return None
    if not isinstance(minor, int) or isinstance(minor, bool):
        return None
    try:
        return Money(minor, Currency(currency))
    except ValueError:
        return None


def _key(raw: Any) -> IdempotencyKey | None:
    """Parse a stored idempotency key."""
    if not isinstance(raw, str) or not raw:
        return None
    return IdempotencyKey.parse(raw)


def _optional_str(raw: Any) -> str | None:
    """Coerce a payload field to a string, treating anything odd as absent."""
    return raw if isinstance(raw, str) and raw else None
