"""Tests for observability metrics collector."""

import pytest
from agentgrid.observability.metrics import MetricsCollector, MetricPoint


class TestMetricPoint:
    def test_creation(self):
        mp = MetricPoint(name="test", value=42.0)
        assert mp.name == "test"
        assert mp.value == 42.0
        assert mp.tags == {}

    def test_creation_with_tags(self):
        mp = MetricPoint(name="test", value=1.0, tags={"env": "prod"})
        assert mp.tags == {"env": "prod"}


class TestMetricsCollector:
    def test_increment(self):
        m = MetricsCollector()
        m.increment("requests")
        m.increment("requests")
        summary = m.summary()
        assert summary["counters"]["requests"] == 2.0

    def test_increment_with_tags(self):
        m = MetricsCollector()
        m.increment("requests", tags={"method": "GET"})
        m.increment("requests", tags={"method": "POST"})
        summary = m.summary()
        assert summary["counters"]["requests{method=GET}"] == 1.0
        assert summary["counters"]["requests{method=POST}"] == 1.0

    def test_increment_custom_amount(self):
        m = MetricsCollector()
        m.increment("bytes", amount=100)
        m.increment("bytes", amount=50)
        assert m.summary()["counters"]["bytes"] == 150.0

    def test_gauge(self):
        m = MetricsCollector()
        m.gauge("cpu", 75.0)
        m.gauge("cpu", 80.0)
        assert m.summary()["gauges"]["cpu"] == 80.0

    def test_gauge_with_tags(self):
        m = MetricsCollector()
        m.gauge("mem", 1024, tags={"host": "a"})
        summary = m.summary()
        assert summary["gauges"]["mem{host=a}"] == 1024.0

    def test_summary_empty(self):
        m = MetricsCollector()
        s = m.summary()
        assert s == {"counters": {}, "gauges": {}}

    def test_reset(self):
        m = MetricsCollector()
        m.increment("x")
        m.gauge("y", 1.0)
        m.reset()
        s = m.summary()
        assert s == {"counters": {}, "gauges": {}}

    def test_make_key_no_tags(self):
        assert MetricsCollector._make_key("name", None) == "name"
        assert MetricsCollector._make_key("name", {}) == "name"

    def test_make_key_sorted_tags(self):
        key = MetricsCollector._make_key("name", {"b": "2", "a": "1"})
        assert key == "name{a=1,b=2}"
