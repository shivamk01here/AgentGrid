"""Tests for the ratelimit module."""

import asyncio

import pytest

from agentgrid.ratelimit.bucket import TokenBucket
from agentgrid.ratelimit.config import RateLimitConfig
from agentgrid.ratelimit.limiter import RateLimiter, RateLimitResult
from agentgrid.ratelimit.middleware import RateLimitExceeded, RateLimitMiddleware


# ── RateLimitConfig ──────────────────────────────────────────────


class TestRateLimitConfig:
    def test_defaults(self):
        cfg = RateLimitConfig()
        assert cfg.max_requests == 60
        assert cfg.window_seconds == 60.0
        assert cfg.burst is None

    def test_custom(self):
        cfg = RateLimitConfig(max_requests=10, window_seconds=30.0, burst=15)
        assert cfg.max_requests == 10
        assert cfg.window_seconds == 30.0
        assert cfg.burst == 15

    def test_frozen(self):
        cfg = RateLimitConfig()
        with pytest.raises(AttributeError):
            cfg.max_requests = 5  # type: ignore[misc]

    def test_invalid_max_requests(self):
        with pytest.raises(ValueError, match="max_requests"):
            RateLimitConfig(max_requests=0)

    def test_invalid_window(self):
        with pytest.raises(ValueError, match="window_seconds"):
            RateLimitConfig(window_seconds=-1)

    def test_invalid_burst(self):
        with pytest.raises(ValueError, match="burst"):
            RateLimitConfig(burst=0)


# ── TokenBucket ──────────────────────────────────────────────────


class TestTokenBucket:
    @pytest.fixture
    def bucket(self):
        return TokenBucket(capacity=5, refill_rate=10.0)

    def test_initial_capacity(self, bucket):
        assert bucket.capacity == 5
        assert bucket.tokens == 5.0

    def test_consume_decrements(self, bucket):
        asyncio.get_event_loop().run_until_complete(bucket.consume(2))
        assert bucket.tokens <= 3.01  # small float tolerance

    def test_try_consume_fails_when_empty(self):
        b = TokenBucket(capacity=2, refill_rate=0.001)
        asyncio.get_event_loop().run_until_complete(b.consume(2))
        assert asyncio.get_event_loop().run_until_complete(b.try_consume(1)) is False

    def test_try_consume_succeeds(self):
        b = TokenBucket(capacity=5, refill_rate=100.0)
        assert asyncio.get_event_loop().run_until_complete(b.try_consume(3)) is True
        assert b.tokens <= 2.01

    def test_invalid_capacity(self):
        with pytest.raises(ValueError, match="capacity"):
            TokenBucket(capacity=0, refill_rate=1.0)

    def test_invalid_refill_rate(self):
        with pytest.raises(ValueError, match="refill_rate"):
            TokenBucket(capacity=1, refill_rate=0)

    def test_reset(self):
        b = TokenBucket(capacity=3, refill_rate=1.0)
        asyncio.get_event_loop().run_until_complete(b.consume(3))
        b.reset()
        assert b.tokens == 3.0


# ── RateLimiter ──────────────────────────────────────────────────


class TestRateLimiter:
    @pytest.fixture
    def limiter(self):
        cfg = RateLimitConfig(max_requests=5, window_seconds=60.0, burst=5)
        return RateLimiter(cfg)

    def test_acquire_success(self, limiter):
        result = asyncio.get_event_loop().run_until_complete(limiter.acquire("a"))
        assert result.allowed is True
        assert result.limit == 5

    def test_try_acquire_denies(self):
        cfg = RateLimitConfig(max_requests=2, window_seconds=60.0, burst=2)
        rl = RateLimiter(cfg)
        asyncio.get_event_loop().run_until_complete(rl.try_acquire("x"))
        asyncio.get_event_loop().run_until_complete(rl.try_acquire("x"))
        result = asyncio.get_event_loop().run_until_complete(rl.try_acquire("x"))
        assert result.allowed is False
        assert result.retry_after is not None

    def test_per_key_isolation(self, limiter):
        for _ in range(5):
            asyncio.get_event_loop().run_until_complete(limiter.try_acquire("a"))
        result_b = asyncio.get_event_loop().run_until_complete(limiter.try_acquire("b"))
        assert result_b.allowed is True

    def test_stats(self, limiter):
        asyncio.get_event_loop().run_until_complete(limiter.try_acquire("a"))
        asyncio.get_event_loop().run_until_complete(limiter.try_acquire("a"))
        stats = limiter.get_stats("a")
        assert stats["allowed"] == 2
        assert stats["denied"] == 0

    def test_reset_single_key(self, limiter):
        asyncio.get_event_loop().run_until_complete(limiter.try_acquire("a"))
        limiter.reset("a")
        stats = limiter.get_stats("a")
        assert stats["allowed"] == 0

    def test_reset_all(self, limiter):
        asyncio.get_event_loop().run_until_complete(limiter.try_acquire("a"))
        asyncio.get_event_loop().run_until_complete(limiter.try_acquire("b"))
        limiter.reset()
        assert limiter.get_stats("a") == {"allowed": 0, "denied": 0}
        assert limiter.get_stats("b") == {"allowed": 0, "denied": 0}

    def test_repr(self, limiter):
        r = repr(limiter)
        assert "RateLimiter" in r
        assert "max_requests=5" in r


# ── RateLimitMiddleware ──────────────────────────────────────────


class TestRateLimitMiddleware:
    def test_wrap_allows(self):
        cfg = RateLimitConfig(max_requests=10, window_seconds=60.0)
        mw = RateLimitMiddleware(cfg)

        async def hello():
            return "ok"

        wrapped = mw.wrap("k", hello)
        result = asyncio.get_event_loop().run_until_complete(wrapped())
        assert result == "ok"

    def test_wrap_raises_on_limit(self):
        cfg = RateLimitConfig(max_requests=1, window_seconds=60.0, burst=1)
        mw = RateLimitMiddleware(cfg)

        async def noop():
            return None

        wrapped = mw.wrap("k", noop)
        asyncio.get_event_loop().run_until_complete(wrapped())
        with pytest.raises(RateLimitExceeded):
            asyncio.get_event_loop().run_until_complete(wrapped())

    def test_exceeded_attributes(self):
        cfg = RateLimitConfig(max_requests=1, window_seconds=60.0, burst=1)
        mw = RateLimitMiddleware(cfg)

        async def noop():
            return None

        wrapped = mw.wrap("mykey", noop)
        asyncio.get_event_loop().run_until_complete(wrapped())
        try:
            asyncio.get_event_loop().run_until_complete(wrapped())
            assert False, "should have raised"
        except RateLimitExceeded as exc:
            assert exc.key == "mykey"
            assert exc.retry_after is not None
