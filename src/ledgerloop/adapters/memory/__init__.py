"""In-memory adapters.

Correct implementations of the core ports that hold state in the process.
They exist so the runtime can be exercised end to end without infrastructure,
and so a durable adapter has a reference to agree with.
"""

from ledgerloop.adapters.memory.runs import InMemoryRunStore, InMemoryStepStore

__all__ = ["InMemoryRunStore", "InMemoryStepStore"]
