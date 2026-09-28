"""High-level rate limiter interface."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ledgerloop.ratelimit.bucket import TokenBucket
from ledgerloop.ratelimit.config import RateLimitConfig

logger = logging.getLogger(__name__)


@dataclass
class RateLimitResult:
    """Result of a rate-limit check.

    `limit` and `remaining` describe the bucket that actually enforced the
    request, which is not always the configured `max_requests`: `burst` raises
    the capacity. `retry_after` is the wait, in seconds, for a caller that was
    refused; it is None when the caller was let through.
    """

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
        self._buckets: dict[str, TokenBucket] = {}
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
        """Acquire *tokens* for *key*, blocking until they are available.

        The bucket's consume() waits rather than refusing, so this cannot deny
        and never reports a retry_after - the caller has its tokens by the
        time it returns.
        """
        bucket = self._get_bucket(key)
        allowed = await bucket.consume(tokens)
        self._stats[key]["allowed"] += 1

        return RateLimitResult(
            allowed=allowed,
            remaining=bucket.tokens,
            limit=bucket.capacity,
        )

    async def try_acquire(self, key: str = "default", tokens: int = 1) -> RateLimitResult:
        """Non-blocking version of acquire."""
        bucket = self._get_bucket(key)
        allowed = await bucket.try_consume(tokens)

        if allowed:
            self._stats[key]["allowed"] += 1
        else:
            self._stats[key]["denied"] += 1

        # One read, so the two numbers agree with each other and with the
        # decision above: what is left and how long until it is one are
        # answers to the same question.
        current_tokens = bucket.tokens
        return RateLimitResult(
            allowed=allowed,
            remaining=current_tokens,
            limit=bucket.capacity,
            retry_after=None if allowed else (tokens - current_tokens) / bucket.refill_rate,
        )

    def get_stats(self, key: str = "default") -> dict[str, int]:
        """Return allowed/denied counters for *key*."""
        return dict(self._stats.get(key, {"allowed": 0, "denied": 0}))

    def reset(self, key: str | None = None) -> None:
        """Reset one or all buckets."""
        if key is not None:
            if key in self._buckets:
                self._buckets[key].reset()
                self._stats[key] = {"allowed": 0, "denied": 0}
        else:
            self._buckets.clear()
            self._stats.clear()

    def __repr__(self) -> str:
        return (
            f"RateLimiter(max_requests={self._config.max_requests}, "
            f"window={self._config.window_seconds}s, keys={len(self._buckets)})"
        )
