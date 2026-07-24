"""Middleware for rate-limiting agent execution."""

from __future__ import annotations

import logging
from typing import Any

from agentgrid.ratelimit.limiter import RateLimiter
from agentgrid.ratelimit.config import RateLimitConfig

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """Raised when a rate-limited call exceeds its quota."""

    def __init__(self, key: str, retry_after: float | None = None) -> None:
        self.key = key
        self.retry_after = retry_after
        msg = f"Rate limit exceeded for key={key!r}"
        if retry_after is not None:
            msg += f" (retry after {retry_after:.2f}s)"
        super().__init__(msg)


class RateLimitMiddleware:
    """Wraps async callables with per-key rate limiting.

    Usage::

        mw = RateLimitMiddleware(RateLimitConfig(max_requests=10, window_seconds=60))

        async def call_llm(prompt: str) -> str:
            ...

        limited = mw.wrap("agent-1", call_llm)
        result = await limited("hello")
    """

    def __init__(
        self,
        config: RateLimitConfig | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._limiter = limiter or RateLimiter(config)

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    def wrap(self, key: str, func: Any) -> Any:
        """Return a rate-limited version of *func* keyed by *key*."""
        middleware = self

        async def limited_wrapper(*args: Any, **kwargs: Any) -> Any:
            result = await middleware._limiter.try_acquire(key)
            if not result.allowed:
                logger.warning("Rate limit hit for key=%s", key)
                raise RateLimitExceeded(key=key, retry_after=result.retry_after)
            return await func(*args, **kwargs)

        limited_wrapper.__name__ = getattr(func, "__name__", "limited")
        limited_wrapper.__qualname__ = getattr(func, "__qualname__", "limited")
        return limited_wrapper
