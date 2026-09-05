"""Adapters: concrete implementations of the core ports.

Adapters depend on `ledgerloop.core`; the core never depends on them. Swap a
memory adapter for a Postgres one and the runtime is unchanged.
"""

from ledgerloop.adapters.clock import ManualClock, SystemClock

__all__ = ["ManualClock", "SystemClock"]
