"""In-memory approval gateway.

Reference implementation of `ApprovalGateway`. It holds the requests and
enforces the three checks that make an approval mean something:

* the approver holds the required role,
* the approver is not the party that proposed the action, and
* the grant is bound to the action's fingerprint, so approving a small
  refund cannot later authorize a large one.

Notification is a hook rather than a feature here - a real gateway pushes to
Slack, an ops console, or a queue. What it must not do is decide anything.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime

from ledgerloop.core.enums import ApprovalState, PolicyEffect
from ledgerloop.core.errors import ConfigurationError, PolicyViolationError, StateTransitionError
from ledgerloop.core.ids import ApprovalId, TenantId
from ledgerloop.core.models import Action, ApprovalRequest, PolicyDecision, Run

logger = logging.getLogger(__name__)

__all__ = ["ApproverDirectory", "InMemoryApprovalGateway"]

Notifier = Callable[[ApprovalRequest], Awaitable[None]]
"""Called once when a request is raised. Failures are swallowed - a broken
notification channel must not block a run from halting safely."""


class ApproverDirectory:
    """Who may approve what.

    Deliberately minimal and separate from `auth`: the question "does this
    person hold this role" is one a real deployment answers from its own
    identity provider, and the gateway should depend on the question, not on
    any particular answer.
    """

    def __init__(self, roles: dict[str, set[str]] | None = None) -> None:
        self._roles: dict[str, set[str]] = {k: set(v) for k, v in (roles or {}).items()}

    def grant_role(self, actor: str, role: str) -> None:
        """Give `actor` the named role."""
        self._roles.setdefault(actor, set()).add(role)

    def revoke_role(self, actor: str, role: str) -> None:
        """Remove the role. A no-op when the actor never held it."""
        self._roles.get(actor, set()).discard(role)

    def holds(self, actor: str, role: str) -> bool:
        """True when `actor` holds `role`."""
        return role in self._roles.get(actor, set())


class InMemoryApprovalGateway:
    """Approval requests held in a dictionary."""

    def __init__(
        self,
        *,
        directory: ApproverDirectory | None = None,
        notifier: Notifier | None = None,
        enforce_separation_of_duties: bool = True,
    ) -> None:
        self._requests: dict[tuple[str, str], ApprovalRequest] = {}
        self._lock = asyncio.Lock()
        self._directory = directory or ApproverDirectory()
        self._notifier = notifier
        self._enforce_separation = enforce_separation_of_duties

    @property
    def directory(self) -> ApproverDirectory:
        """The role directory this gateway consults."""
        return self._directory

    async def request(
        self,
        run: Run,
        action: Action,
        decision: PolicyDecision,
        *,
        at: datetime,
        expires_at: datetime | None = None,
    ) -> ApprovalRequest:
        """Raise an approval request and notify the responsible role.

        Raises:
            ConfigurationError: `decision` does not actually require approval.
        """
        if decision.effect is not PolicyEffect.REQUIRE_APPROVAL:
            raise ConfigurationError(
                f"Cannot raise an approval for a {decision.effect.value} decision",
                context={"rule_id": decision.rule_id},
            )

        request = ApprovalRequest(
            id=ApprovalId.generate(),
            run_id=run.id,
            tenant_id=run.tenant_id,
            action=action,
            # Bound at request time. If the action changes afterwards, the
            # grant stops applying rather than silently covering the new one.
            action_fingerprint=action.fingerprint(),
            reason=decision.reason,
            approver_role=decision.approver_role or "payments-approver",
            state=ApprovalState.PENDING,
            requested_at=at,
            risk_tier=decision.risk_tier,
            expires_at=expires_at,
        )

        async with self._lock:
            self._requests[(run.tenant_id.value, request.id.value)] = request

        await self._notify(request)
        return request

    async def get(self, tenant_id: TenantId, approval_id: ApprovalId) -> ApprovalRequest | None:
        """Load a request, or None when it does not exist."""
        return self._requests.get((tenant_id.value, approval_id.value))

    async def submit(
        self,
        tenant_id: TenantId,
        approval_id: ApprovalId,
        *,
        approved: bool,
        actor: str,
        at: datetime,
        note: str | None = None,
    ) -> ApprovalRequest:
        """Record a reviewer's verdict.

        Raises:
            StateTransitionError: The request does not exist, or was already
                decided.
            PolicyViolationError: The actor lacks the role, or proposed the
                action they are trying to approve.
        """
        key = (tenant_id.value, approval_id.value)

        async with self._lock:
            request = self._requests.get(key)
            if request is None:
                raise StateTransitionError("ApprovalRequest", "absent", "decided")
            if request.state is not ApprovalState.PENDING:
                raise StateTransitionError(
                    "ApprovalRequest", request.state.value, "decided"
                )

            # Expiry is checked on submission rather than by a background
            # sweep, so a late decision cannot slip through between the
            # deadline and whenever a reaper happens to run.
            if request.expires_at is not None and at >= request.expires_at:
                expired = request.expire(at=at)
                self._requests[key] = expired
                raise StateTransitionError("ApprovalRequest", "expired", "decided")

            if not self._directory.holds(actor, request.approver_role):
                raise PolicyViolationError(
                    f"{actor} does not hold the role {request.approver_role!r}",
                    rule_id="approver-role",
                )

            if self._enforce_separation and actor == request.action.counterparty:
                raise PolicyViolationError(
                    "An approver cannot approve an action naming them as counterparty",
                    rule_id="separation-of-duties",
                )

            decided = (
                request.grant(at=at, by=actor, note=note)
                if approved
                else request.reject(at=at, by=actor, note=note)
            )
            self._requests[key] = decided
            return decided

    async def expire(
        self, tenant_id: TenantId, approval_id: ApprovalId, *, at: datetime
    ) -> ApprovalRequest | None:
        """Retire a request nobody answered in time.

        Returns:
            The expired request, or None when it does not exist or a
            reviewer had already decided it. A decision that landed before
            the deadline is not something a later sweep gets to overwrite.
        """
        key = (tenant_id.value, approval_id.value)

        async with self._lock:
            request = self._requests.get(key)
            if request is None or request.state is not ApprovalState.PENDING:
                return None
            expired = request.expire(at=at)
            self._requests[key] = expired
            return expired

    async def list_pending(
        self, tenant_id: TenantId, *, limit: int = 100
    ) -> AsyncIterator[ApprovalRequest]:
        """Stream outstanding requests for a tenant, oldest first."""
        pending = sorted(
            (
                request
                for (tenant, _), request in self._requests.items()
                if tenant == tenant_id.value and request.state is ApprovalState.PENDING
            ),
            key=lambda request: request.requested_at,
        )
        for request in pending[:limit]:
            yield request

    async def expire_overdue(self, *, as_of: datetime) -> tuple[ApprovalRequest, ...]:
        """Expire every pending request past its deadline.

        Returns:
            The requests that were expired by this call.
        """
        expired: list[ApprovalRequest] = []
        async with self._lock:
            for key, request in list(self._requests.items()):
                if (
                    request.state is ApprovalState.PENDING
                    and request.expires_at is not None
                    and as_of >= request.expires_at
                ):
                    updated = request.expire(at=as_of)
                    self._requests[key] = updated
                    expired.append(updated)
        return tuple(expired)

    async def _notify(self, request: ApprovalRequest) -> None:
        """Fire the notifier, swallowing its failures.

        A halted run is already safe. Failing the halt because a webhook was
        down would turn a safe outcome into an unsafe one.
        """
        if self._notifier is None:
            return
        try:
            await self._notifier(request)
        except Exception:  # noqa: BLE001 - notification must never break a halt
            logger.exception("Approval notification failed for %s", request.id)
