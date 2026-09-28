"""Metrics collection for agent operations."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MetricPoint:
    """A single metric data point."""

    name: str
    value: float
    timestamp: float = field(default_factory=time.time)
    tags: dict[str, str] = field(default_factory=dict)


class MetricsCollector:
    """Thread-safe in-memory metrics collector.

    Collects counters, gauges, and histograms.
    Designed to be swapped out for Prometheus/StatsD in production.

    Example:
        metrics = MetricsCollector()
        metrics.increment("agent.run.count", tags={"agent": "my-agent"})
        metrics.gauge("memory.usage.bytes", 1024)
        metrics.histogram("agent.run.duration_seconds", 1.4)
        summary = metrics.summary()
    """

    def __init__(self) -> None:
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def increment(self, name: str, amount: float = 1.0, tags: dict[str, str] | None = None) -> None:
        """Increment a counter metric."""
        key = self._make_key(name, tags)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def gauge(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        """Set a gauge metric."""
        key = self._make_key(name, tags)
        with self._lock:
            self._gauges[key] = value

    def histogram(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        """Record one observation of a distribution, e.g. a request duration."""
        key = self._make_key(name, tags)
        with self._lock:
            self._histograms.setdefault(key, []).append(value)

    def summary(self) -> dict[str, Any]:
        """Return a snapshot of all collected metrics.

        Histograms are reduced to count/min/max/avg/sum rather than handed
        back as raw observations - a summary is meant to be read, and a
        collector running for days would otherwise grow the reply without
        bound.
        """
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": {
                    key: _histogram_stats(values) for key, values in self._histograms.items()
                },
            }

    def reset(self) -> None:
        """Clear all metrics."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()

    @staticmethod
    def _make_key(name: str, tags: dict[str, str] | None) -> str:
        if not tags:
            return name
        tag_str = ",".join(f"{k}={v}" for k, v in sorted(tags.items()))
        return f"{name}{{{tag_str}}}"


def _histogram_stats(values: list[float]) -> dict[str, float]:
    """Reduce a series of observations to the figures a dashboard wants."""
    return {
        "count": len(values),
        "sum": sum(values),
        "min": min(values),
        "max": max(values),
        "avg": sum(values) / len(values),
    }
