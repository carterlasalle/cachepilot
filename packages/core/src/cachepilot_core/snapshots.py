"""Memory-only request snapshot store — PRD §30, §91 (Phase 6).

The relay maintains the **last cache-producing request** per cache identity
for an active lease, so a warm (PRD §147) can replay it as a bounded
cache-equivalent request.

Hard rules (PRD §30, AGENTS.md invariant 10):

- The complete request body lives **IN MEMORY ONLY**. Nothing here is ever
  persisted: prompts, conversation history, API keys, authorization headers
  and tool arguments never reach storage, logs or telemetry.
- A freshly constructed :class:`SnapshotStore` is empty: after a relay
  restart every lease becomes non-warmable (the scheduler skips it with
  ``SKIPPED_UNSUPPORTED``) instead of attempting an unsafe reconstruction.
  That is acceptable because provider caches are ephemeral anyway (PRD §30).

The store is keyed by ``cache_fingerprint`` (physical cache identity, never
``session_id`` — AGENTS.md invariant 7), matching PRD §147's
``snapshot_store.get(lease.cache_fingerprint)``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RequestSnapshot:
    """One in-memory snapshot of a cache-producing request (PRD §30).

    ``body`` is the raw provider request body as JSON (the warm replay's
    source). ``upstream_url`` is where the warm is sent; ``authorization``
    is the request's Authorization header value, held in memory so the warm
    can authenticate — never persisted, never logged.

    This dataclass deliberately carries raw content: it is a *memory-only*
    object by contract (PRD §30) and must never be serialized.
    """

    cache_fingerprint: str
    body: dict[str, Any]
    upstream_url: str
    authorization: str | None = None
    stored_at: float = field(default_factory=time.time)


class SnapshotStore:
    """Memory-only request snapshot store (PRD §30, §91).

    Thread-safety: the relay is single-threaded asyncio, and every mutation
    happens inside the lease controller's request path; no locking is needed
    beyond the asyncio event loop.
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, RequestSnapshot] = {}

    def store(self, snapshot: RequestSnapshot) -> None:
        """Remember the latest cache-producing request for a cache identity."""
        self._snapshots[snapshot.cache_fingerprint] = snapshot

    def get(self, cache_fingerprint: str) -> RequestSnapshot | None:
        """The latest snapshot for a cache identity, or None (non-warmable)."""
        return self._snapshots.get(cache_fingerprint)

    def drop(self, cache_fingerprint: str) -> None:
        """Forget a snapshot (e.g. the request that produced it failed)."""
        self._snapshots.pop(cache_fingerprint, None)

    def clear(self) -> None:
        """Forget every snapshot (relay restart semantics)."""
        self._snapshots.clear()

    @property
    def fingerprints(self) -> frozenset[str]:
        """Cache identities with a live in-memory snapshot."""
        return frozenset(self._snapshots)

    @property
    def snapshots(self) -> tuple[RequestSnapshot, ...]:
        """Every live snapshot (test/observability use)."""
        return tuple(self._snapshots.values())

    def __len__(self) -> int:
        return len(self._snapshots)
