"""Typed identifiers.

Every identifier in the domain is its own type. A `RunId` cannot be passed
where a `TenantId` is expected, and neither can a bare `str`. The cost is a
constructor call at the boundary; the benefit is that the class of bug where
a step id reaches a ledger lookup is a type error rather than an empty
result set at 3am.

Identifiers are prefixed on the wire (`run_a1b2...`), which makes them
self-describing in logs, support tickets, and provider dashboards.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import ClassVar, Final, Self

__all__ = [
    "ActionId",
    "ApprovalId",
    "CorrelationId",
    "EntryId",
    "Identifier",
    "IdempotencyKey",
    "RunId",
    "StepId",
    "TenantId",
]

_TOKEN_BYTES: Final = 16
_IDENTIFIER_PATTERN: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True, slots=True, order=True)
class Identifier:
    """Base class for prefixed, immutable identifiers.

    Subclasses set `prefix`. Construction validates the shape but does not
    require the prefix - identifiers minted by external systems are accepted
    as-is so that a provider's own reference can be carried without being
    mangled.
    """

    value: str

    prefix: ClassVar[str] = "id"

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError(f"{type(self).__name__} cannot be empty")
        if not _IDENTIFIER_PATTERN.match(self.value):
            raise ValueError(
                f"{type(self).__name__} must be 1-128 characters of "
                f"[A-Za-z0-9_.:-], got {self.value!r}"
            )

    @classmethod
    def generate(cls) -> Self:
        """Mint a new random identifier carrying this type's prefix."""
        return cls(f"{cls.prefix}_{secrets.token_hex(_TOKEN_BYTES)}")

    @classmethod
    def parse(cls, raw: str) -> Self:
        """Rehydrate an identifier read from storage or a request.

        Distinct from the constructor only in intent: `parse` marks a trust
        boundary crossing at the call site.
        """
        return cls(raw)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class TenantId(Identifier):
    """The customer organization a run belongs to.

    Present on every stored record. Every store implementation must scope
    its queries by this field - it is the isolation boundary.
    """

    prefix: ClassVar[str] = "ten"


@dataclass(frozen=True, slots=True, order=True)
class RunId(Identifier):
    """One agent execution against one case."""

    prefix: ClassVar[str] = "run"


@dataclass(frozen=True, slots=True, order=True)
class StepId(Identifier):
    """One iteration within a run."""

    prefix: ClassVar[str] = "stp"


@dataclass(frozen=True, slots=True, order=True)
class ActionId(Identifier):
    """One proposed or executed effect on the outside world."""

    prefix: ClassVar[str] = "act"


@dataclass(frozen=True, slots=True, order=True)
class ApprovalId(Identifier):
    """One human approval request."""

    prefix: ClassVar[str] = "apr"


@dataclass(frozen=True, slots=True, order=True)
class EntryId(Identifier):
    """One append-only ledger entry."""

    prefix: ClassVar[str] = "led"


@dataclass(frozen=True, slots=True, order=True)
class CorrelationId(Identifier):
    """Ties a run to the upstream request or case that triggered it.

    Propagated into every outbound call and log line so a single incident can
    be traced across Ledgerloop, the provider, and the caller's own systems.
    """

    prefix: ClassVar[str] = "cor"


@dataclass(frozen=True, slots=True, order=True)
class IdempotencyKey(Identifier):
    """The key under which an effect is claimed exactly once.

    Never generated randomly for a value-moving action: it must be *derived*
    from the action's semantic content, so that the same logical effect
    proposed twice - by a retry, a replay, or a duplicate upstream event -
    computes the same key and collides on the second attempt.

    Use `derive` rather than the constructor.
    """

    prefix: ClassVar[str] = "idk"

    @classmethod
    def derive(cls, *parts: str) -> IdempotencyKey:
        """Build a deterministic key from the parts that define an effect.

        Args:
            *parts: The semantic components - typically run id, action kind,
                counterparty, and amount. Order matters and must be stable
                across releases.

        Returns:
            The key. Identical inputs always produce an identical key.

        Raises:
            ValueError: No parts were supplied, or one was empty.
        """
        if not parts:
            raise ValueError("IdempotencyKey.derive requires at least one part")
        if any(not part for part in parts):
            raise ValueError("IdempotencyKey parts cannot be empty")

        # Hashed rather than concatenated: parts may contain separators or
        # PII, and the key ends up in provider-side logs.
        import hashlib

        digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
        return cls(f"{cls.prefix}_{digest[:32]}")
