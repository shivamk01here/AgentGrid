"""Tests for workflow engine and step."""

import asyncio
import pytest
from ledgerloop.workflow.step import Step, StepResult
from ledgerloop.workflow.engine import WorkflowEngine


async def noop_handler(step: Step, ctx: dict) -> str:
    return "ok"


async def failing_handler(step: Step, ctx: dict) -> str:
    raise RuntimeError("boom")


async def slow_handler(step: Step, ctx: dict) -> str:
    await asyncio.sleep(0.5)
    return "done"


async def context_writer(step: Step, ctx: dict) -> str:
    return f"val-{len(ctx)}"


class TestStep:
    @pytest.mark.asyncio
    async def test_execute_success(self):
        step = Step(name="s1", handler=noop_handler)
        result = await step.execute({})
        assert result.success is True
        assert result.output == "ok"
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_execute_failure(self):
        step = Step(name="s1", handler=failing_handler)
        result = await step.execute({})
        assert result.success is False
        assert "boom" in result.error

    @pytest.mark.asyncio
    async def test_execute_retry(self):
        call_count = 0

        async def flaky(step: Step, ctx: dict) -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("fail")
            return "recovered"

        step = Step(name="s1", handler=flaky, retry_count=2)
        result = await step.execute({})
        assert result.success is True
        assert result.output == "recovered"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_execute_retry_exhausted(self):
        step = Step(name="s1", handler=failing_handler, retry_count=2)
        result = await step.execute({})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_execute_timeout(self):
        step = Step(name="s1", handler=slow_handler, timeout_seconds=0.1)
        result = await step.execute({})
        assert result.success is False
        assert "timed out" in result.error.lower()


class TestStepResult:
    def test_ok_factory(self):
        r = StepResult.ok("s1", "out", 1.5)
        assert r.success is True
        assert r.output == "out"
        assert r.duration_ms == 1.5

    def test_fail_factory(self):
        r = StepResult.fail("s1", "err", 2.0)
        assert r.success is False
        assert r.error == "err"


class TestWorkflowEngine:
    @pytest.mark.asyncio
    async def test_run_single_step(self):
        engine = WorkflowEngine(name="test")
        engine.add_step(Step(name="a", handler=noop_handler))
        results = await engine.run()
        assert "a" in results
        assert results["a"].success is True

    @pytest.mark.asyncio
    async def test_run_dependency_order(self):
        order = []

        async def track(step: Step, ctx: dict) -> str:
            order.append(step.name)
            return step.name

        engine = WorkflowEngine()
        engine.add_step(Step(name="c", handler=track, dependencies=["b"]))
        engine.add_step(Step(name="a", handler=track))
        engine.add_step(Step(name="b", handler=track, dependencies=["a"]))
        await engine.run()
        assert order == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_context_passing(self):
        engine = WorkflowEngine()
        engine.add_step(Step(name="s1", handler=context_writer))
        engine.add_step(Step(name="s2", handler=context_writer, dependencies=["s1"]))
        results = await engine.run({"init": True})
        assert results["s1"].success is True
        assert results["s2"].success is True

    @pytest.mark.asyncio
    async def test_stops_on_failure(self):
        engine = WorkflowEngine()
        engine.add_step(Step(name="a", handler=failing_handler))
        engine.add_step(Step(name="b", handler=noop_handler))
        results = await engine.run()
        assert "a" in results
        assert results["a"].success is False
        assert "b" not in results


    def test_missing_dependency(self):
        engine = WorkflowEngine()
        with pytest.raises(ValueError, match="not registered"):
            engine.add_step(Step(name="a", handler=noop_handler, dependencies=["missing"]))

    def test_duplicate_step(self):
        engine = WorkflowEngine()
        engine.add_step(Step(name="a", handler=noop_handler))
        with pytest.raises(ValueError, match="already registered"):
            engine.add_step(Step(name="a", handler=noop_handler))

    def test_remove_step(self):
        engine = WorkflowEngine()
        engine.add_step(Step(name="a", handler=noop_handler))
        assert engine.remove_step("a") is True
        assert engine.remove_step("nonexistent") is False

    def test_remove_step_with_dependents_raises(self):
        engine = WorkflowEngine()
        engine.add_step(Step(name="a", handler=noop_handler))
        engine.add_step(Step(name="b", handler=noop_handler, dependencies=["a"]))
        with pytest.raises(ValueError, match="depended on"):
            engine.remove_step("a")

    def test_negative_retry_count_raises(self):
        with pytest.raises(ValueError, match="retry_count"):
            Step(name="bad", handler=noop_handler, retry_count=-1)

    def test_remove_step_with_dependents_raises(self):
        engine = WorkflowEngine()
        engine.add_step(Step(name="a", handler=noop_handler))
        engine.add_step(Step(name="b", handler=noop_handler, dependencies=["a"]))
        with pytest.raises(ValueError, match="depended on"):
            engine.remove_step("a")

def test_cycle_detection():
    engine = WorkflowEngine()
    
    async def dummy(step, ctx):
        return "ok"
        
    step_a = Step(name="A", handler=dummy)
    engine.add_step(step_a)
    
    step_b = Step(name="B", handler=dummy, dependencies=["A"])
    engine.add_step(step_b)
    
    # Mutate to create a cycle
    step_a.dependencies.append("B")
    
    # Trigger a rebuild of the execution order
    step_c = Step(name="C", handler=dummy)
    with pytest.raises(ValueError, match="Cycle detected"):
        engine.add_step(step_c)
def test_negative_timeout_raises():
    with pytest.raises(ValueError, match="timeout_seconds"):
        Step(name="bad_timeout", handler=noop_handler, timeout_seconds=-1.0)
