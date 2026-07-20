"""Tests for the memory engine."""

import pytest
from agentgrid.memory.engine import MemoryEngine


class TestMemoryEngine:
    @pytest.mark.asyncio
    async def test_store_and_retrieve(self, memory_engine):
        await memory_engine.store("key1", "value1")
        result = await memory_engine.retrieve("key1")
        assert result == "value1"

    @pytest.mark.asyncio
    async def test_retrieve_missing_key(self, memory_engine):
        result = await memory_engine.retrieve("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete(self, memory_engine):
        await memory_engine.store("key1", "value1")
        deleted = await memory_engine.delete("key1")
        assert deleted is True
        assert await memory_engine.retrieve("key1") is None

    @pytest.mark.asyncio
    async def test_search_by_tag(self, memory_engine):
        await memory_engine.store("k1", "v1", tags=["important"])
        await memory_engine.store("k2", "v2", tags=["minor"])
        results = await memory_engine.search(tag="important")
        assert len(results) == 1
        assert results[0].key == "k1"

    @pytest.mark.asyncio
    async def test_search_by_prefix(self, memory_engine):
        await memory_engine.store("agent.name", "agentgrid")
        await memory_engine.store("agent.version", "0.1")
        await memory_engine.store("system.os", "linux")
        results = await memory_engine.search(prefix="agent.")
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_clear(self, memory_engine):
        await memory_engine.store("k1", "v1")
        await memory_engine.store("k2", "v2")
        count = await memory_engine.clear()
        assert count == 2
        assert memory_engine.size == 0

    @pytest.mark.asyncio
    async def test_namespace_isolation(self):
        engine_a = MemoryEngine(namespace="a")
        engine_b = MemoryEngine(namespace="b")
        await engine_a.store("key", "value_a")
        await engine_b.store("key", "value_b")
        assert await engine_a.retrieve("key") == "value_a"
        assert await engine_b.retrieve("key") == "value_b"
