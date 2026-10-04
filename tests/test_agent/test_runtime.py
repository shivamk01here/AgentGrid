"""Tests for the agent runtime."""

from datetime import UTC, datetime

import pytest

from ledgerloop.agent.base import Agent, AgentConfig
from ledgerloop.agent.runtime import AgentRuntime
from ledgerloop.events.bus import EventBus
from ledgerloop.llm.provider import LLMError


class DummyAgent(Agent):
    async def run(self, input_data: str = "") -> str:
        return f"echo: {input_data}"


class RecordingClock:
    """Records every wait the runtime asks for, and waits for none of them."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def now(self) -> datetime:
        return datetime(2026, 5, 1, tzinfo=UTC)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class FailingAgent(Agent):
    def __init__(self):
        super().__init__(AgentConfig(name="failing"))
        self._attempts = 0

    async def run(self, input_data: str = "") -> str:
        self._attempts += 1
        if self._attempts < 3:
            raise RuntimeError("transient failure")
        return "recovered"


class TestAgentRuntime:
    @pytest.mark.asyncio
    async def test_successful_execution(self):
        agent = DummyAgent()
        runtime = AgentRuntime(agent)
        result = await runtime.execute("hello")
        assert result["success"] is True
        assert result["output"] == "echo: hello"
        assert result["attempt"] == 1

    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        agent = FailingAgent()
        runtime = AgentRuntime(agent, max_retries=3, clock=RecordingClock())
        result = await runtime.execute("test")
        assert result["success"] is True
        assert result["output"] == "recovered"
        assert result["attempt"] == 3

    @pytest.mark.asyncio
    async def test_all_retries_exhausted(self):
        class AlwaysFail(Agent):
            async def run(self, input_data: str = "") -> str:
                raise RuntimeError("always fails")

        agent = AlwaysFail()
        runtime = AgentRuntime(agent, max_retries=2, clock=RecordingClock())
        result = await runtime.execute("test")
        assert result["success"] is False
        assert "always fails" in result["error"]
        assert result["attempt"] == 2

    def test_reset(self):
        agent = DummyAgent()
        runtime = AgentRuntime(agent)
        runtime._iteration = 5
        runtime.reset()
        assert runtime._iteration == 0

    def test_invalid_max_retries(self):
        agent = DummyAgent()
        with pytest.raises(ValueError, match="max_retries"):
            AgentRuntime(agent, max_retries=0)

    @pytest.mark.asyncio
    async def test_iterations_are_per_execution(self):
        agent = DummyAgent()
        runtime = AgentRuntime(agent, max_retries=3, clock=RecordingClock())
        await runtime.execute("hello")
        assert runtime._iteration == 1
        await runtime.execute("hello again")
        assert runtime._iteration == 1

    @pytest.mark.asyncio
    async def test_emits_events_on_success(self):
        agent = DummyAgent()
        bus = EventBus()
        agent.attach_event_bus(bus)
        runtime = AgentRuntime(agent)
        await runtime.execute("hello")
        history = bus.get_history()
        topics = [e.topic for e in history]
        assert "agent.run.started" in topics
        assert "agent.run.completed" in topics

    @pytest.mark.asyncio
    async def test_emits_events_on_failure(self):
        class AlwaysFail(Agent):
            async def run(self, input_data: str = "") -> str:
                raise RuntimeError("boom")

        agent = AlwaysFail()
        bus = EventBus()
        agent.attach_event_bus(bus)
        runtime = AgentRuntime(agent, max_retries=2, clock=RecordingClock())
        await runtime.execute("test")
        history = bus.get_history()
        topics = [e.topic for e in history]
        assert "agent.run.started" in topics
        assert "agent.run.failed" in topics
        assert "agent.run.completed" not in topics

    @pytest.mark.asyncio
    async def test_no_events_without_event_bus(self):
        agent = DummyAgent()
        runtime = AgentRuntime(agent)
        result = await runtime.execute("hello")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_rate_limited_result_has_duration(self):
        agent = DummyAgent()
        from ledgerloop.ratelimit.limiter import RateLimitConfig, RateLimiter
        cfg = RateLimitConfig(max_requests=1, window_seconds=60, burst=1)
        limiter = RateLimiter(cfg)
        runtime = AgentRuntime(agent, rate_limiter=limiter)
        # burst=1 lets the first run through; the second is over the limit.
        assert (await runtime.execute("hello"))["success"] is True
        result = await runtime.execute("hello")
        assert result["success"] is False
        assert "duration" in result
        assert result["duration"] == 0


class TestBackoff:
    """A retry made the instant after a rate limit meets the same rate limit."""

    async def test_retries_wait_and_the_wait_doubles(self):
        clock = RecordingClock()
        runtime = AgentRuntime(FailingAgent(), max_retries=3, clock=clock)

        result = await runtime.execute("test")

        assert result["success"] is True
        assert clock.slept == [1.0, 2.0]

    async def test_there_is_no_wait_after_the_last_attempt(self):
        class AlwaysFail(Agent):
            async def run(self, input_data: str = "") -> str:
                raise LLMError("rate limited", retryable=True)

        clock = RecordingClock()
        runtime = AgentRuntime(AlwaysFail(), max_retries=3, clock=clock)

        result = await runtime.execute("test")

        assert result["success"] is False
        assert clock.slept == [1.0, 2.0]

    async def test_an_unretryable_error_is_not_waited_on(self):
        class BadRequest(Agent):
            async def run(self, input_data: str = "") -> str:
                raise LLMError("malformed request", retryable=False)

        clock = RecordingClock()
        runtime = AgentRuntime(BadRequest(), max_retries=3, clock=clock)

        result = await runtime.execute("test")

        assert result["attempt"] == 1
        assert clock.slept == []

    async def test_the_base_is_configurable(self):
        clock = RecordingClock()
        runtime = AgentRuntime(
            FailingAgent(), max_retries=3, retry_backoff_seconds=0.25, clock=clock
        )

        await runtime.execute("test")

        assert clock.slept == [0.25, 0.5]

    async def test_a_zero_base_turns_the_wait_off(self):
        clock = RecordingClock()
        runtime = AgentRuntime(
            FailingAgent(), max_retries=3, retry_backoff_seconds=0, clock=clock
        )

        result = await runtime.execute("test")

        assert result["success"] is True
        assert clock.slept == []

    def test_a_negative_base_is_refused(self):
        with pytest.raises(ValueError, match="retry_backoff_seconds"):
            AgentRuntime(DummyAgent(), retry_backoff_seconds=-1)


class TestRuntimeConfig:
    def test_from_env_defaults(self):
        from ledgerloop.agent.config import RuntimeConfig
        cfg = RuntimeConfig.from_env()
        assert cfg.log_level == "INFO"
        assert cfg.tracing_enabled is True
        assert cfg.metrics_enabled is True
        assert cfg.auth_enabled is False

    def test_from_env_parses_1_as_true(self):
        import os
        os.environ["LEDGERLOOP_TRACING_ENABLED"] = "1"
        try:
            from ledgerloop.agent.config import RuntimeConfig
            cfg = RuntimeConfig.from_env()
            assert cfg.tracing_enabled is True
        finally:
            del os.environ["LEDGERLOOP_TRACING_ENABLED"]

    def test_from_env_parses_yes_as_true(self):
        import os
        os.environ["LEDGERLOOP_AUTH_ENABLED"] = "yes"
        try:
            from ledgerloop.agent.config import RuntimeConfig
            cfg = RuntimeConfig.from_env()
            assert cfg.auth_enabled is True
        finally:
            del os.environ["LEDGERLOOP_AUTH_ENABLED"]
