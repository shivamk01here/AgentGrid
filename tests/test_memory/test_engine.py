"""Tests for the memory engine."""

import pytest
from agentgrid.memory.engine import MemoryEngine
from agentgrid.memory.store import MemoryEntry


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

    @pytest.mark.asyncio
    async def test_list_all(self, memory_engine):
        await memory_engine.store("k1", "v1")
        await memory_engine.store("k2", "v2", tags=["tag"])
        entries = await memory_engine.list_all()
        assert len(entries) == 2
        keys = {e.key for e in entries}
        assert keys == {"k1", "k2"}

    @pytest.mark.asyncio
    async def test_list_all_excludes_expired(self):
        engine = MemoryEngine(namespace="test")
        await engine.store("alive", "yes")
        await engine.store("dead", "no", ttl_seconds=0)
        import time
        time.sleep(0.01)
        entries = await engine.list_all()
        assert len(entries) == 1
        assert entries[0].key == "alive"

    @pytest.mark.asyncio
    async def test_custom_backend(self):
        class DictBackend:
            def __init__(self):
                self._data: dict[str, MemoryEntry] = {}

            async def store(self, key, value, entry):
                self._data[key] = entry

            async def retrieve(self, key):
                return self._data.get(key)

            async def delete(self, key):
                if key in self._data:
                    del self._data[key]
                    return True
                return False

            async def list_all(self):
                return list(self._data.values())

            async def clear(self):
                count = len(self._data)
                self._data.clear()
                return count

            @property
            def size(self):
                return len(self._data)

        backend = DictBackend()
        engine = MemoryEngine(namespace="custom", backend=backend)
        await engine.store("k1", "v1")
        assert await engine.retrieve("k1") == "v1"
        entries = await engine.list_all()
        assert len(entries) == 1
