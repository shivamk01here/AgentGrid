"""Rolling a run back.

A run that fails halfway has usually already moved money, and leaving those
effects standing because the run errored afterwards is not an option anybody
would accept from a payments system. This is the piece that walks them back.

The order is the guarantee, and it is the mirror of the executor's:

    read the ledger -> newest effect first -> claim -> reverse -> record

Newest first because effects are rarely independent: a hold placed to cover a
transfer has to outlive the transfer it was covering. The claim is taken
under a key derived from the original one, so running compensation twice
reverses each effect exactly once rather than refunding twice.

Three things it refuses to do, and all three end the run in FAILED rather
than COMPENSATED:

* Reverse an effect whose kind has no reversal. A payout is gone.
* Reverse an effect whose outcome was never determined. Reconcile it first.
* Pretend a provider's refusal to reverse was a successful rollback.

A partial rollback is a worse state than either extreme, so it is reported
loudly instead of being rounded up to success.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ledgerloop.core.enums import LedgerEventType, RunState
from ledgerloop.core.errors import LedgerloopError, StateTransitionError
from ledgerloop.core.ids import IdempotencyKey
from ledgerloop.runtime.effects import replay_effects

if TYPE_CHECKING:
    from ledgerloop.core.ids import TenantId
    from ledgerloop.core.models import ActionReceipt, Run
    from ledgerloop.core.ports import (
        ActionDispatcher,
        Clock,
        IdempotencyStore,
        LedgerStore,
        RunStore,
    )
    from ledgerloop.runtime.effects import AppliedEffect

logger = logging.getLogger(__name__)

__all__ = ["CompensationReport", "Compensator"]


@dataclass(frozen=True, slots=True)
class CompensationReport:
    """What one rollback attempt managed to undo."""

    standing: int = 0
    """Effects found still applied when the rollback started."""
    compensated: int = 0
    """Effects successfully reversed."""
    replayed: int = 0
    """Effects a previous rollback had already reversed. Counted as done."""
    irreversible: int = 0
    """Effects whose kind cannot be undone. Needs human remediation."""
    unresolved: int = 0
    """Effects with no known outcome. Reconcile them, then try again."""
    failed: int = 0
    """Reversals the provider refused."""

    @property
    def complete(self) -> bool:
        """True when nothing was left standing in the world."""
        return self.compensated + self.replayed == self.standing

    @property
    def stranded(self) -> int:
        """Effects still out there after the attempt."""
        return self.irreversible + self.unresolved + self.failed

    def __str__(self) -> str:
        return (
            f"standing={self.standing} compensated={self.compensated} "
            f"replayed={self.replayed} irreversible={self.irreversible} "
            f"unresolved={self.unresolved} failed={self.failed}"
        )


class Compensator:
    """Reverses the effects a run applied, exactly once each.

    Example:
        compensator = Compensator(
            runs=run_store, ledger=ledger, dispatcher=psp,
            idempotency=store, clock=clock,
        )
        report = await compensator.compensate(run)
        if not report.complete:
            ...  # somebody has to go and look at this
    """

    def __init__(
        self,
        *,
        runs: RunStore,
        ledger: LedgerStore,
        dispatcher: ActionDispatcher,
        idempotency: IdempotencyStore,
        clock: Clock,
    ) -> None:
        self._runs = runs
        self._ledger = ledger
        self._dispatcher = dispatcher
        self._idempotency = idempotency
        self._clock = clock

    async def compensate(self, run: Run) -> CompensationReport:
        """Walk back everything `run` applied, newest effect first.

        The run moves to COMPENSATING for the duration, then to COMPENSATED
        if nothing was left behind and FAILED if anything was. It never comes
        back RUNNING - a run that needed rolling back does not get to carry
        on afterwards.

        Args:
            run: A RUNNING run. Its ledger is the source of what to undo.

        Returns:
            What was reversed and what was not.

        Raises:
            StateTransitionError: The run cannot enter COMPENSATING.
        """
        if run.state is not RunState.RUNNING:
            raise StateTransitionError("Run", run.state.value, RunState.COMPENSATING.value)

        rolling_back = await self._save(run.begin_compensation(at=self._clock.now()))
        entries = await self._ledger.read(run.tenant_id, run.id)
        effects = replay_effects(entries)

        report = CompensationReport(standing=len(effects))
        # Newest first: later effects often depend on earlier ones, and
        # undoing the dependency before the dependant strands both.
        for effect in reversed(effects):
            report = await self._reverse(effect, rolling_back, report)

        return await self._finish(rolling_back, report)

    async def _reverse(
        self, effect: AppliedEffect, run: Run, report: CompensationReport
    ) -> CompensationReport:
        """Reverse one effect and fold the result into the report."""
        if effect.indeterminate:
            logger.error(
                "Effect %s has no known outcome; refusing to reverse it on run %s",
                effect.action_id,
                run.id,
            )
            await self._write(run, effect, reversed_ok=False, detail="outcome unknown")
            return replace(report, unresolved=report.unresolved + 1)

        reversal_kind = effect.kind.reversal_kind
        if reversal_kind is None:
            logger.error(
                "Effect %s is a %s and cannot be reversed; run %s needs a human",
                effect.action_id,
                effect.kind.value,
                run.id,
            )
            await self._write(run, effect, reversed_ok=False, detail="kind is not reversible")
            return replace(report, irreversible=report.irreversible + 1)

        claimed = await self._claim(effect, run.tenant_id)
        if claimed is None:
            # Already reversed by an earlier attempt. The world is in the
            # state we wanted, which is the only thing that matters.
            logger.info("Effect %s was already reversed; skipping", effect.action_id)
            await self._write(run, effect, reversed_ok=True, detail="already reversed")
            return replace(report, replayed=report.replayed + 1)

        try:
            receipt = await self._dispatcher.compensate(
                effect.to_action(), effect.to_receipt(), at=self._clock.now()
            )
        except (LedgerloopError, NotImplementedError, ValueError) as exc:
            # The claim stays as it is. An unanswered reversal is exactly as
            # dangerous as an unanswered payment, and gets the same treatment.
            logger.exception("Could not reverse effect %s on run %s", effect.action_id, run.id)
            await self._write(run, effect, reversed_ok=False, detail=str(exc))
            return replace(report, failed=report.failed + 1)

        await self._settle(claimed, run.tenant_id, receipt)
        await self._write(
            run,
            effect,
            reversed_ok=True,
            detail=None,
            reversal_kind=reversal_kind.value,
            provider_reference=receipt.provider_reference,
        )
        return replace(report, compensated=report.compensated + 1)

    async def _claim(self, effect: AppliedEffect, tenant_id: TenantId) -> IdempotencyKey | None:
        """Claim the reversal, or None when it has already been settled.

        The key is derived from the original effect's key, so the same
        reversal proposed by a second rollback attempt collides here rather
        than reaching the provider.
        """
        key = reversal_key(effect)
        record = await self._idempotency.claim(
            key,
            tenant_id,
            _reversal_fingerprint(effect),
            at=self._clock.now(),
            action_id=effect.action_id,
        )
        return None if record.is_settled else key

    async def _settle(
        self, key: IdempotencyKey, tenant_id: TenantId, receipt: ActionReceipt
    ) -> None:
        """Settle the reversal's claim, tolerating a benign double-settle."""
        try:
            await self._idempotency.settle(key, tenant_id, receipt, at=self._clock.now())
        except LedgerloopError:
            logger.info("Reversal claim %s was already settled by someone else", key)

    async def _finish(self, run: Run, report: CompensationReport) -> CompensationReport:
        """Land the run in its final state and say what happened."""
        now = self._clock.now()
        if report.complete:
            await self._save(run.complete_compensation(at=now))
            logger.info("Run %s rolled back cleanly: %s", run.id, report)
            return report

        await self._save(
            run.fail(
                f"Rollback incomplete - {report.stranded} effect(s) still applied",
                at=now,
            )
        )
        logger.error("Run %s could not be fully rolled back: %s", run.id, report)
        return report

    async def _save(self, run: Run) -> Run:
        """Persist a transitioned run at its new version."""
        return await self._runs.save(run, expected_version=run.version - 1)

    async def _write(
        self,
        run: Run,
        effect: AppliedEffect,
        *,
        reversed_ok: bool,
        detail: str | None,
        reversal_kind: str | None = None,
        provider_reference: str | None = None,
    ) -> None:
        """Record what happened to one effect, never failing the rollback."""
        # `compensation` marks the entry as being about the reversal rather
        # than the original effect. Without it a failed reversal reads back
        # as a failed dispatch, and the replay concludes the effect never
        # landed - which is exactly the effect still sitting out there.
        payload: dict[str, object] = {
            "action_id": str(effect.action_id),
            "compensation": True,
            "kind": effect.kind.value,
            "reversal_kind": reversal_kind,
            "amount_minor": None if effect.amount is None else effect.amount.minor_units,
            "currency": None if effect.amount is None else effect.amount.currency.value,
            "provider_reference": provider_reference,
            "detail": detail,
        }
        event = (
            LedgerEventType.ACTION_COMPENSATED if reversed_ok else LedgerEventType.ACTION_FAILED
        )
        try:
            await self._ledger.append(
                run.id, run.tenant_id, event, payload, occurred_at=self._clock.now()
            )
        except Exception:
            logger.exception("Ledger write failed for %s on run %s", event.value, run.id)


def reversal_key(effect: AppliedEffect) -> IdempotencyKey:
    """The idempotency key a reversal of `effect` is claimed under.

    Derived from the original key where there is one, so the reversal is
    stable across attempts and can never collide with the effect it undoes.
    """
    original = (
        str(effect.idempotency_key)
        if effect.idempotency_key is not None
        else str(effect.action_id)
    )
    return IdempotencyKey.derive("reverse", original)


def _reversal_fingerprint(effect: AppliedEffect) -> str:
    """Fingerprint for a reversal claim.

    Distinct from the original action's fingerprint on purpose: the claim
    store rejects a key reused under different content, and a reversal is
    different content.
    """
    return f"reverse:{effect.action_id}:{effect.kind.value}"


