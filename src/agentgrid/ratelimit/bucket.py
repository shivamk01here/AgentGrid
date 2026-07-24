"""Token bucket algorithm for rate limiting."""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Token bucket rate limiter.

    Tokens are refilled at a steady rate up to the bucket capacity.
    Each consume() call removes one token; if no tokens remain the
    caller blocks until a token is available.
    """

    def __init__(self, capacity: int, refill_rate: float) -> None:
        """Initialise the bucket.

        Args:
            capacity: Maximum number of tokens the bucket can hold.
            refill_rate: Tokens added per second.
        """
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_rate <= 0:
            raise ValueError("refill_rate must be positive")

        self._capacity = capacity
        self._refill_rate = refill_rate
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now

    async def consume(self, n: int = 1) -> bool:
        """Try to consume *n* tokens.

        Returns True immediately if tokens are available, otherwise
        sleeps until enough tokens have been refilled.
        """
        if n <= 0:
            raise ValueError("n must be positive")

        async with self._lock:
            self._refill()
            if self._tokens >= n:
                self._tokens -= n
                return True

        wait_time = (n - self._tokens) / self._refill_rate
        await asyncio.sleep(wait_time)

        async with self._lock:
            self._refill()
            if self._tokens >= n:
                self._tokens -= n
                return True
        return False

    async def try_consume(self, n: int = 1) -> bool:
        """Non-blocking consume. Returns False if tokens are unavailable."""
        if n <= 0:
            raise ValueError("n must be positive")

        async with self._lock:
            self._refill()
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def reset(self) -> None:
        """Reset the bucket to full capacity."""
        self._tokens = float(self._capacity)
        self._last_refill = time.monotonic()
