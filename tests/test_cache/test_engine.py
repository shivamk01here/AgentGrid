"""Tests for the cache engine."""

import pytest
from agentgrid.cache.engine import CacheEngine, InMemoryCache
from agentgrid.cache.entry import CacheEntry


class TestCacheEngine:
    @pytest.mark.asyncio
    async def test_set_and_get(self):
        engine = CacheEngine(namespace="test")
        await engine.set("key1", "value1")
        result = await engine.get("key1")
        assert result == "value1"

    @pytest.mark.asyncio
    async def test_get_missing_key(self):
        engine = CacheEngine(namespace="test")
        result = await engine.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete(self):
        engine = CacheEngine(namespace="test")
        await engine.set("key1", "value1")
        deleted = await engine.delete("key1")
        assert deleted is True
        assert await engine.get("key1") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self):
        engine = CacheEngine(namespace="test")
        deleted = await engine.delete("nonexistent")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_clear(self):
        engine = CacheEngine(namespace="test")
        await engine.set("k1", "v1")
        await engine.set("k2", "v2")
        count = await engine.clear()
        assert count == 2
        assert engine.size == 0

    @pytest.mark.asyncio
    async def test_ttl_expiration(self):
        engine = CacheEngine(namespace="test")
        await engine.set("short", "value", ttl=0)
        import time
        time.sleep(0.01)
        result = await engine.get("short")
        assert result is None

    @pytest.mark.asyncio
    async def test_ttl_no_expiration(self):
        engine = CacheEngine(namespace="test")
        await engine.set("permanent", "value", ttl=None)
        result = await engine.get("permanent")
        assert result == "value"

    @pytest.mark.asyncio
    async def test_search_by_tag(self):
        engine = CacheEngine(namespace="test")
        await engine.set("k1", "v1", tags=["important"])
        await engine.set("k2", "v2", tags=["minor"])
        results = await engine.search(tag="important")
        assert len(results) == 1
        assert results[0].key == "k1"

    @pytest.mark.asyncio
    async def test_search_by_prefix(self):
        engine = CacheEngine(namespace="test")
        await engine.set("user.name", "alice")
        await engine.set("user.role", "admin")
        await engine.set("config.theme", "dark")
        results = await engine.search(prefix="user.")
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_invalidate_by_tag(self):
        engine = CacheEngine(namespace="test")
        await engine.set("k1", "v1", tags=["temporary"])
        await engine.set("k2", "v2", tags=["persistent"])
        count = await engine.invalidate(tag="temporary")
        assert count == 1
        assert await engine.get("k1") is None
        assert await engine.get("k2") == "v2"

    @pytest.mark.asyncio
    async def test_invalidate_by_prefix(self):
        engine = CacheEngine(namespace="test")
        await engine.set("session.a", "data1")
        await engine.set("session.b", "data2")
        await engine.set("config.c", "data3")
        count = await engine.invalidate(prefix="session.")
        assert count == 2
        assert await engine.get("session.a") is None
        assert await engine.get("session.b") is None
        assert await engine.get("config.c") == "data3"

    @pytest.mark.asyncio
    async def test_namespace_isolation(self):
        engine_a = CacheEngine(namespace="a")
        engine_b = CacheEngine(namespace="b")
        await engine_a.set("key", "value_a")
        await engine_b.set("key", "value_b")
        assert await engine_a.get("key") == "value_a"
        assert await engine_b.get("key") == "value_b"

    @pytest.mark.asyncio
    async def test_custom_backend(self):
        class DictBackend:
            def __init__(self):
                self._data: dict[str, CacheEntry] = {}

            async def get(self, key):
                return self._data.get(key)

            async def set(self, key, entry):
                self._data[key] = entry

            async def delete(self, key):
                if key in self._data:
                    del self._data[key]
                    return True
                return False

            async def clear(self):
                count = len(self._data)
                self._data.clear()
                return count

            async def list_all(self):
                return list(self._data.values())

            @property
            def size(self):
                return len(self._data)

        backend = DictBackend()
        engine = CacheEngine(namespace="custom", backend=backend)
        await engine.set("k1", "v1")
        assert await engine.get("k1") == "v1"


class TestInMemoryCache:
    @pytest.mark.asyncio
    async def test_set_and_get(self):
        cache = InMemoryCache()
        entry = CacheEntry(key="k", value="v", created_at=0.0)
        await cache.set("k", entry)
        result = await cache.get("k")
        assert result is entry

    @pytest.mark.asyncio
    async def test_get_missing(self):
        cache = InMemoryCache()
        result = await cache.get("missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete(self):
        cache = InMemoryCache()
        entry = CacheEntry(key="k", value="v", created_at=0.0)
        await cache.set("k", entry)
        assert await cache.delete("k") is True
        assert await cache.delete("k") is False

    @pytest.mark.asyncio
    async def test_clear(self):
        cache = InMemoryCache()
        await cache.set("k1", CacheEntry(key="k1", value="v1"))
        await cache.set("k2", CacheEntry(key="k2", value="v2"))
        count = await cache.clear()
        assert count == 2
        assert cache.size == 0

    @pytest.mark.asyncio
    async def test_list_all_excludes_expired(self):
        cache = InMemoryCache()
        import time
        await cache.set("alive", CacheEntry(key="alive", value="yes", created_at=time.time()))
        await cache.set("dead", CacheEntry(key="dead", value="no", ttl=0, created_at=0.0))
        entries = await cache.list_all()
        assert len(entries) == 1
        assert entries[0].key == "alive"

    @pytest.mark.asyncio
    async def test_size(self):
        cache = InMemoryCache()
        assert cache.size == 0
        await cache.set("k", CacheEntry(key="k", value="v"))
        assert cache.size == 1
