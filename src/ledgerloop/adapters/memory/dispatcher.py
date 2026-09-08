"""Fake action dispatchers.

Stand-ins for a real PSP or bank client. `RecordingDispatcher` remembers what
it was asked to do so a test can assert on it, and can be told to fail in the
specific ways a payment provider actually fails - including the nasty one,
where the request left the process and nobody knows what happened to it.
"""

from __future__ import annotations

from datetime import datetime

from ledgerloop.core.enums import ActionKind, FailureClass, IdempotencyState
from ledgerloop.core.errors import IndeterminateError, ProviderError
from ledgerloop.core.models import Action, ActionReceipt

__all__ = ["RecordingDispatcher"]


class RecordingDispatcher:
    """Records dispatches instead of performing them.

    Example:
        dispatcher = RecordingDispatcher()
        dispatcher.fail_next(FailureClass.TRANSIENT)
        receipt = await dispatcher.dispatch(action, at=clock.now())
    """

    def __init__(self, *, handles_kinds: frozenset[ActionKind] | None = None) -> None:
        self.dispatched: list[Action] = []
        self.compensated: list[Action] = []
        self._handles = handles_kinds
        self._next_failure: FailureClass | None = None
        self._reference_counter = 0

    def handles(self, action: Action) -> bool:
        """True when this dispatcher can execute `action`."""
        return self._handles is None or action.kind in self._handles

    def fail_next(self, failure: FailureClass) -> None:
        """Make the next dispatch fail in the given way, then reset."""
        self._next_failure = failure

    async def dispatch(self, action: Action, *, at: datetime) -> ActionReceipt:
        """Pretend to execute the action.

        Raises:
            IndeterminateError: When primed with `FailureClass.INDETERMINATE`.
                The action IS recorded first, because that is what makes this
                case dangerous - the effect happened, the caller just doesn't
                know it.
            ProviderError: For any other primed failure.
        """
        failure, self._next_failure = self._next_failure, None

        if failure is FailureClass.INDETERMINATE:
            # Recorded on purpose: the request reached the provider.
            self.dispatched.append(action)
            raise IndeterminateError(
                "Connection dropped after the request was sent",
                idempotency_key=action.idempotency_key,
                action_id=action.id,
            )

        if failure is not None:
            raise ProviderError(
                f"Simulated {failure.value} failure",
                provider="recording",
                failure_class=failure,
            )

        self.dispatched.append(action)
        self._reference_counter += 1
        return ActionReceipt(
            action_id=action.id,
            state=IdempotencyState.SUCCEEDED,
            provider_reference=f"rec_{self._reference_counter}",
            settled_at=at,
        )

    async def compensate(
        self, action: Action, receipt: ActionReceipt, *, at: datetime
    ) -> ActionReceipt:
        """Reverse a previously executed action.

        Raises:
            NotImplementedError: The action kind cannot be reversed.
        """
        if not action.kind.is_reversible:
            raise NotImplementedError(f"{action.kind.value} cannot be compensated")

        self.compensated.append(action)
        return ActionReceipt(
            action_id=action.id,
            state=IdempotencyState.SUCCEEDED,
            provider_reference=f"{receipt.provider_reference}_reversed",
            settled_at=at,
        )

    @property
    def dispatch_count(self) -> int:
        """How many times a real dispatch was attempted."""
        return len(self.dispatched)
