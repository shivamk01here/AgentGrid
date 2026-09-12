"""Run coordination.

Ties the pieces together: an action is proposed, policy rules on it, and one
of three things happens.

    ALLOW            -> execute it now
    REQUIRE_APPROVAL -> halt the run, raise an approval, return
    DENY             -> refuse, tell the caller why

The halt is the interesting case. A halted run is durable: it can sit for
days waiting on a human and then resume on the same action it stopped at,
without re-running anything it already did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from ledgerloop.core.enums import (
    ApprovalState,
    LedgerEventType,
    PolicyEffect,
    RunState,
    StopReason,
)
from ledgerloop.core.errors import (
    ConcurrencyError,
    LedgerloopError,
    PolicyViolationError,
    StateTransitionError,
)
from ledgerloop.core.models import Action, PolicyDecision, Run

if TYPE_CHECKING:
    from datetime import datetime

    from ledgerloop.core.ids import ApprovalId
    from ledgerloop.core.ports import (
        ApprovalGateway,
        Clock,
        LedgerStore,
        PolicyEngine,
        RunStore,
    )
    from ledgerloop.runtime.executor import ActionExecutor, ExecutionOutcome

logger = logging.getLogger(__name__)

__all__ = ["ActionResult", "RunCoordinator"]


@dataclass(frozen=True, slots=True)
class ActionResult:
    """What the coordinator did with a proposed action."""

    decision: PolicyDecision
    run: Run
    """The run's state after handling the proposal."""
    outcome: ExecutionOutcome | None = None
    """Present only when the action was actually executed."""
    approval_id: ApprovalId | None = None
    """Present when the run halted for a human decision."""

    @property
    def executed(self) -> bool:
        return self.outcome is not None

    @property
    def halted(self) -> bool:
        return self.approval_id is not None

    @property
    def denied(self) -> bool:
        return self.decision.effect is PolicyEffect.DENY


class RunCoordinator:
    """Drives a run's proposed actions through policy, gates, and execution.

    Example:
        coordinator = RunCoordinator(
            runs=run_store, policy=engine, approvals=gateway,
            executor=executor, ledger=ledger, clock=clock,
        )
        result = await coordinator.propose(run, action)
        if result.halted:
            ...  # come back when a human has decided
    """

    def __init__(
        self,
        *,
        runs: RunStore,
        policy: PolicyEngine,
        approvals: ApprovalGateway,
        executor: ActionExecutor,
        ledger: LedgerStore,
        clock: Clock,
        approval_window: timedelta = timedelta(days=3),
    ) -> None:
        self._runs = runs
        self._policy = policy
        self._approvals = approvals
        self._executor = executor
        self._ledger = ledger
        self._clock = clock
        self._approval_window = approval_window

    async def propose(self, run: Run, action: Action) -> ActionResult:
        """Put one action through policy and act on the verdict.

        Args:
            run: The run proposing the action. Must be RUNNING.
            action: The proposal.

        Returns:
            What happened, including the run's new state.

        Raises:
            StateTransitionError: The run is not in a state that can act.
            PolicyViolationError: Policy denied the action.
        """
        if run.state is not RunState.RUNNING:
            raise StateTransitionError("Run", run.state.value, "acting")

        now = self._clock.now()
        await self._write(run, LedgerEventType.ACTION_PROPOSED, _summarize(action))

        decision = await self._policy.evaluate(action, run, at=now)
        await self._write(
            run,
            LedgerEventType.ACTION_EVALUATED,
            {
                "action_id": str(action.id),
                "effect": decision.effect.value,
                "risk_tier": decision.risk_tier.value,
                "reason": decision.reason,
                "rule_id": decision.rule_id,
            },
        )

        if decision.effect is PolicyEffect.DENY:
            logger.info("Action %s denied: %s", action.id, decision.reason)
            raise PolicyViolationError(decision.reason, rule_id=decision.rule_id)

        if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
            return await self._halt_for_approval(run, action, decision, at=now)

        executed, outcome = await self._execute(run, action)
        return ActionResult(decision=decision, run=executed, outcome=outcome)

    async def resume(self, run: Run, action: Action) -> ActionResult:
        """Resume a halted run once its approval has been decided.

        The action is re-checked against the grant rather than trusted: the
        approval authorizes one specific action fingerprint, and a run that
        comes back with a different action must not ride on the old grant.

        Args:
            run: The halted run.
            action: The action the run stopped on.

        Returns:
            The result of executing it, if the approval was granted.

        Raises:
            StateTransitionError: The run is not awaiting approval, or the
                approval is still pending.
            PolicyViolationError: The approval was rejected or expired, or it
                does not authorize this action.
        """
        if run.state is not RunState.AWAITING_APPROVAL:
            raise StateTransitionError("Run", run.state.value, "resuming")
        if run.pending_approval_id is None:  # pragma: no cover - set on halt
            raise LedgerloopError("Halted run has no pending approval", context={})

        request = await self._approvals.get(run.tenant_id, run.pending_approval_id)
        if request is None:
            raise LedgerloopError(
                "Pending approval no longer exists",
                context={"approval_id": str(run.pending_approval_id)},
            )

        if request.state is ApprovalState.PENDING:
            raise StateTransitionError("ApprovalRequest", "pending", "decided")

        if request.state is not ApprovalState.GRANTED:
            resumed = await self._save(run.resume(at=self._clock.now()))
            failed = await self._save(
                resumed.fail(
                    f"Approval {request.state.value}",
                    at=self._clock.now(),
                    stop_reason=StopReason.CANCELLED,
                )
            )
            await self._write(
                failed,
                LedgerEventType.APPROVAL_REJECTED
                if request.state is ApprovalState.REJECTED
                else LedgerEventType.APPROVAL_EXPIRED,
                {"approval_id": str(request.id), "state": request.state.value},
            )
            raise PolicyViolationError(f"Approval was {request.state.value}")

        # Granted - but only for the exact action that was reviewed.
        if not request.authorizes(action):
            raise PolicyViolationError(
                "The approved action does not match the action being resumed",
                rule_id="fingerprint-mismatch",
            )

        running = await self._save(run.resume(at=self._clock.now()))
        await self._write(
            running,
            LedgerEventType.APPROVAL_GRANTED,
            {
                "approval_id": str(request.id),
                "decided_by": request.decided_by,
                "action_id": str(action.id),
            },
        )

        executed, outcome = await self._execute(running, action)
        decision = PolicyDecision(
            effect=PolicyEffect.ALLOW,
            risk_tier=request.risk_tier,
            reason=f"Approved by {request.decided_by}",
            rule_id="approved",
        )
        return ActionResult(
            decision=decision, run=executed, outcome=outcome, approval_id=request.id
        )

    async def _halt_for_approval(
        self, run: Run, action: Action, decision: PolicyDecision, *, at: datetime
    ) -> ActionResult:
        """Raise an approval and park the run against it."""
        request = await self._approvals.request(
            run,
            action,
            decision,
            at=at,
            expires_at=at + self._approval_window,
        )
        halted = await self._save(run.await_approval(request.id, at=at))
        await self._write(
            halted,
            LedgerEventType.APPROVAL_REQUESTED,
            {
                "approval_id": str(request.id),
                "action_id": str(action.id),
                "reason": decision.reason,
                "approver_role": request.approver_role,
                "expires_at": None if request.expires_at is None else request.expires_at.isoformat(),
            },
        )
        logger.info("Run %s halted awaiting approval %s", run.id, request.id)
        return ActionResult(decision=decision, run=halted, approval_id=request.id)

    async def _execute(self, run: Run, action: Action) -> tuple[Run, ExecutionOutcome]:
        """Execute an approved action and fold the result into the run.

        Returns:
            The run as it now stands, and what the executor did. The run has
            to come back: recording the value moved advances its version, and
            a caller still holding the one it passed in would lose the next
            concurrency check it made.
        """
        outcome = await self._executor.execute(
            action, run_id=run.id, tenant_id=run.tenant_id
        )
        if outcome.succeeded and action.amount is not None:
            try:
                return await self._save(run.record_value_moved(action.amount)), outcome
            except (ConcurrencyError, ValueError):
                # The money moved regardless. Losing the running total is a
                # reporting problem; pretending the action failed would be a
                # correctness one.
                logger.exception("Could not record value moved for run %s", run.id)
        return run, outcome

    async def _save(self, run: Run) -> Run:
        """Persist a transitioned run at its new version."""
        return await self._runs.save(run, expected_version=run.version - 1)

    async def _write(
        self, run: Run, event: LedgerEventType, payload: dict[str, object]
    ) -> None:
        """Append to the run's audit chain, never failing the caller."""
        try:
            await self._ledger.append(
                run.id, run.tenant_id, event, dict(payload), occurred_at=self._clock.now()
            )
        except Exception:
            logger.exception("Ledger write failed for %s on run %s", event.value, run.id)


def _summarize(action: Action) -> dict[str, object]:
    """Describe a proposal for the ledger."""
    return {
        "action_id": str(action.id),
        "kind": action.kind.value,
        "description": action.description,
        "amount_minor": None if action.amount is None else action.amount.minor_units,
        "currency": None if action.amount is None else action.amount.currency.value,
        "counterparty": action.counterparty,
    }
