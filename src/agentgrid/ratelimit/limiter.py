"""High-level rate limiter interface."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from agentgrid.ratelimit.bucket import TokenBucket
from agentgrid.ratelimit.config import RateLimitConfig

logger = logging.getLogger(__name__)


@dataclass
class RateLimitResult:
    """Result of a rate-limit check."""

    allowed: bool
    remaining: float
    limit: int
    retry_after: float | None = None


class RateLimiter:
    """Per-key rate limiter backed by token buckets.

    Creates an isolated bucket for each unique key (e.g. agent id,
    user id, or endpoint name).
    """

    def __init__(self, config: RateLimitConfig | None = None) -> None:
        self._config = config or RateLimitConfig()
        self._buckets: dict[str, TokenBucket] = field(default_factory=dict)
        self._stats: dict[str, dict[str, int]] = {}

    @property
    def config(self) -> RateLimitConfig:
        return self._config

    def _get_bucket(self, key: str) -> TokenBucket:
        if key not in self._buckets:
            capacity = self._config.burst or self._config.max_requests
            refill_rate = self._config.max_requests / self._config.window_seconds
            self._buckets[key] = TokenBucket(capacity=capacity, refill_rate=refill_rate)
            self._stats[key] = {"allowed": 0, "denied": 0}
        return self._buckets[key]

    async def acquire(self, key: str = "default", tokens: int = 1) -> RateLimitResult:
        """Attempt to acquire *tokens* for *key*, blocking if necessary."""
        bucket = self._get_bucket(key)
        allowed = await bucket.consume(tokens)

        if allowed:
            self._stats[key]["allowed"] += 1
        else:
            self._stats[key]["denied"] += 1

        return RateLimitResult(
            allowed=allowed,
            remaining=bucket.tokens,
            limit=self._config.max_requests,
        )

    async def try_acquire(self, key: str = "default", tokens: int = 1) -> RateLimitResult:
        """Non-blocking version of acquire."""
        bucket = self._get_bucket(key)
        allowed = await bucket.try_consume(tokens)

        if allowed:
            self._stats[key]["allowed"] += 1
        else:
            self._stats[key]["denied"] += 1

        return RateLimitResult(
            allowed=allowed,
            remaining=bucket.tokens,
            limit=self._config.max_requests,
            retry_after=(tokens - bucket.tokens) / bucket._refill_rate if not allowed else None,
        )

    def get_stats(self, key: str = "default") -> dict[str, int]:
        """Return allowed/denied counters for *key*."""
        return dict(self._stats.get(key, {"allowed": 0, "denied": 0}))

    def reset(self, key: str | None = None) -> None:
        """Reset one or all buckets."""
        if key:
            if key in self._buckets:
                self._buckets[key].reset()
                self._stats[key] = {"allowed": 0, "denied": 0}
        else:
            for bucket in self._buckets.values():
                bucket.reset()
            self._stats.clear()

    def __repr__(self) -> str:
        return (
            f"RateLimiter(max_requests={self._config.max_requests}, "
            f"window={self._config.window_seconds}s, keys={len(self._buckets)})"
        )
