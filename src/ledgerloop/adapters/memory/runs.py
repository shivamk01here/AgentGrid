"""In-memory run and step storage.

Reference implementations of `RunStore` and `StepStore`. They enforce the
same guarantees a real database must - tenant isolation, optimistic
concurrency, and exclusive leases - so a test that passes here fails against
a Postgres adapter only if that adapter is wrong.

State lives in a process dictionary, so it does not survive a restart. Use
these for tests and local development, never for real runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ledgerloop.adapters.clock import SystemClock
from ledgerloop.core.enums import RunState
from ledgerloop.core.errors import ConcurrencyError, RunNotFoundError
from ledgerloop.core.ids import RunId, StepId, TenantId
from ledgerloop.core.models import Run, RunSpec, Step

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ledgerloop.core.ports import Clock

__all__ = ["InMemoryRunStore", "InMemoryStepStore"]


@dataclass(slots=True)
class _Lease:
    """Who holds a run, and until when."""

    owner: str
    expires_at: datetime

    def is_live(self, at: datetime) -> bool:
        return at < self.expires_at


class InMemoryRunStore:
    """Run storage backed by a dictionary.

    Every mutating operation holds a single lock. That is coarser than a real
    database would be, but it makes the concurrency semantics exact: a caller
    can never observe a half-applied write.
    """

    def __init__(self, clock: Clock | None = None) -> None:
        # Keyed by (tenant, run) so a cross-tenant read cannot succeed even
        # by accident - the tenant is part of the identity, not a filter.
        self._runs: dict[tuple[str, str], Run] = {}
        self._leases: dict[tuple[str, str], _Lease] = {}
        self._lock = asyncio.Lock()
        self._clock: Clock = clock or SystemClock()

    async def create(self, spec: RunSpec, *, run_id: RunId | None = None) -> Run:
        """Persist a new run in `PENDING`.

        Raises:
            ConcurrencyError: A run already exists under `run_id`.
        """
        identifier = run_id or RunId.generate()
        created_at = self._now()
        run = Run(
            id=identifier,
            spec=spec,
            state=RunState.PENDING,
            created_at=created_at,
            version=0,
        )
        key = self._key(spec.tenant_id, identifier)
        async with self._lock:
            if key in self._runs:
                raise ConcurrencyError("Run", 0, self._runs[key].version)
            self._runs[key] = run
        return run

    async def get(self, tenant_id: TenantId, run_id: RunId) -> Run:
        """Load a run.

        Raises:
            RunNotFoundError: No such run for this tenant.
        """
        run = self._runs.get(self._key(tenant_id, run_id))
        if run is None:
            raise RunNotFoundError(run_id)
        return run

    async def save(self, run: Run, *, expected_version: int) -> Run:
        """Persist a transitioned run under optimistic concurrency.

        Raises:
            RunNotFoundError: The run does not exist.
            ConcurrencyError: Another worker advanced it first.
        """
        key = self._key(run.tenant_id, run.id)
        async with self._lock:
            current = self._runs.get(key)
            if current is None:
                raise RunNotFoundError(run.id)
            if current.version != expected_version:
                raise ConcurrencyError("Run", expected_version, current.version)
            self._runs[key] = run
        return run

    async def acquire_lease(
        self, tenant_id: TenantId, run_id: RunId, *, owner: str, duration_seconds: float
    ) -> bool:
        """Claim exclusive execution rights, or renew a lease already held.

        Returns:
            True when this caller may execute the run.
        """
        if duration_seconds <= 0:
            raise ValueError("Lease duration must be positive")
        key = self._key(tenant_id, run_id)
        now = self._now()
        async with self._lock:
            if key not in self._runs:
                raise RunNotFoundError(run_id)
            existing = self._leases.get(key)
            # A live lease belonging to someone else blocks; our own renews.
            if existing is not None and existing.is_live(now) and existing.owner != owner:
                return False
            self._leases[key] = _Lease(
                owner=owner, expires_at=now + timedelta(seconds=duration_seconds)
            )
            return True

    async def release_lease(self, tenant_id: TenantId, run_id: RunId, *, owner: str) -> None:
        """Release a lease this worker holds. A no-op otherwise."""
        key = self._key(tenant_id, run_id)
        async with self._lock:
            existing = self._leases.get(key)
            # Only the holder may release: a worker whose lease already
            # expired must not evict whoever legitimately took it next.
            if existing is not None and existing.owner == owner:
                del self._leases[key]

    async def list_by_state(
        self, tenant_id: TenantId, state: RunState, *, limit: int = 100
    ) -> AsyncIterator[Run]:
        """Stream runs in a given state, oldest first."""
        matches = sorted(
            (
                run
                for (tenant, _), run in self._runs.items()
                if tenant == tenant_id.value and run.state is state
            ),
            key=lambda run: run.created_at,
        )
        for run in matches[:limit]:
            yield run

    async def find_expired(self, *, as_of: datetime, limit: int = 100) -> AsyncIterator[Run]:
        """Stream halted runs whose deadline has passed, across all tenants."""
        matches = sorted(
            (
                run
                for run in self._runs.values()
                if run.state.is_halted
                and run.spec.deadline is not None
                and run.spec.deadline <= as_of
            ),
            key=lambda run: run.created_at,
        )
        for run in matches[:limit]:
            yield run

    # ---- test helpers -----------------------------------------------------

    def snapshot(self) -> tuple[Run, ...]:
        """Every stored run. For assertions, not for production reads."""
        return tuple(self._runs.values())

    @staticmethod
    def _key(tenant_id: TenantId, run_id: RunId) -> tuple[str, str]:
        return (tenant_id.value, run_id.value)

    def _now(self) -> datetime:
        return self._clock.now()


class InMemoryStepStore:
    """Step storage backed by a dictionary.

    Steps are immutable: appending the same step id twice is an error rather
    than an update, because a step that changed after the fact would make the
    run's ledger disagree with its own history.
    """

    def __init__(self) -> None:
        self._steps: dict[tuple[str, str], Step] = {}
        self._by_run: dict[tuple[str, str], list[StepId]] = {}
        self._lock = asyncio.Lock()

    async def append(self, step: Step) -> Step:
        """Persist a step.

        Raises:
            ConcurrencyError: A step already exists under this id.
        """
        tenant = step.tenant_id.value
        key = (tenant, step.id.value)
        async with self._lock:
            if key in self._steps:
                raise ConcurrencyError("Step", -1, step.index)
            self._steps[key] = step
            self._by_run.setdefault((tenant, step.run_id.value), []).append(step.id)
        return step

    async def get(self, tenant_id: TenantId, step_id: StepId) -> Step | None:
        """Load one step, or None when it does not exist."""
        return self._steps.get((tenant_id.value, step_id.value))

    async def list_for_run(self, tenant_id: TenantId, run_id: RunId) -> Sequence[Step]:
        """Every step of a run, in execution order."""
        ids = self._by_run.get((tenant_id.value, run_id.value), [])
        steps = [self._steps[(tenant_id.value, i.value)] for i in ids]
        return sorted(steps, key=lambda s: s.index)

