"""Background target registry with refcounts (PRD §46 / §48 — targets.py).

Normalizes every kind of background work — processes, subagents, external
jobs — into a single :class:`BackgroundTarget` identity. A target is *active*
while its refcount is positive; the cache lease (Phase 5+) remains relevant
while ``active_targets > 0`` (PRD §46).

Subagent existence is driven EXCLUSIVELY by the ``subagent_start`` /
``subagent_stop`` lifecycle hooks wired in ``lifecycle.py``; ``process``
target existence is driven by the ``tool_request`` middleware's
auto-background promotion and the matching ``post_tool_call`` completion —
never inferred from conversation text (PRD §48).

The registry is deliberately small and thread-safe (hooks may fire from
different threads); refcounts clamp at zero and never go negative.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal

from cachepilot_hermes.duration_history import normalize_signature

TargetKind = Literal["process", "subagent", "external"]

#: Namespace prefix for ``process`` target ids (PRD §46).
PROCESS_TARGET_PREFIX = "process:"


def process_target_id(command: str) -> str:
    """Stable ``process`` target id for one auto-backgrounded terminal command.

    Derived from the normalized command signature so the two surfaces that must
    agree — the ``tool_request`` middleware that registers the target when it
    promotes the call, and the ``post_tool_call`` hook that releases it on
    completion — compute the same id from the payload they both receive
    (``args["command"]``), without depending on a tool-call id only one of them
    is guaranteed to be handed. Concurrent runs of the same command share the
    id and are balanced by the registry's refcount. The signature is
    normalized, so no raw command text becomes a persisted identity
    (AGENTS.md rule 10).
    """
    return f"{PROCESS_TARGET_PREFIX}{normalize_signature(command)}"


@dataclass(frozen=True)
class BackgroundTarget:
    """One unit of background work that keeps a session's lease relevant.

    Attributes:
        id: Stable identity (process session id, subagent session id, ...).
        kind: ``process`` (managed shell), ``subagent`` (delegated agent),
            or ``external`` (out-of-band job).
        session_id: Hermes session whose cache lease this target guards.
        started_at: Unix timestamp when the target started.
        expected_completion: True when the target is expected to finish
            (and thus to eventually release the lease).
    """

    id: str
    kind: TargetKind
    session_id: str = ""
    started_at: float = 0.0
    expected_completion: bool = True


class BackgroundTargetRegistry:
    """Refcounted registry of active background targets.

    Registering the same id twice (e.g. duplicate start events) yields a
    refcount of 2; every matching release decrements it, and the target is
    removed exactly when the count reaches zero. Releases never go negative
    and unknown ids are no-ops (fail open for traffic).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._targets: dict[str, BackgroundTarget] = {}
        self._refcounts: dict[str, int] = {}

    def register(self, target: BackgroundTarget) -> int:
        """Add *target* (idempotent by id), returning its new refcount.

        Empty ids are ignored so hook callbacks without identity payloads
        never corrupt the registry.
        """
        if not target.id:
            return 0
        with self._lock:
            self._targets[target.id] = target
            self._refcounts[target.id] = self._refcounts.get(target.id, 0) + 1
            return self._refcounts[target.id]

    def release(self, target_id: str) -> int:
        """Decrement the refcount for *target_id*; remove at zero.

        Returns the remaining refcount (0 when inactive or unknown). Never
        returns a negative number.
        """
        if not target_id:
            return 0
        with self._lock:
            remaining = self._refcounts.get(target_id, 0) - 1
            if remaining <= 0:
                self._targets.pop(target_id, None)
                self._refcounts.pop(target_id, None)
                return 0
            self._refcounts[target_id] = remaining
            return remaining

    def refcount(self, target_id: str) -> int:
        with self._lock:
            return self._refcounts.get(target_id, 0)

    def is_active(self, target_id: str) -> bool:
        return self.refcount(target_id) > 0

    def active_count(self, session_id: str | None = None) -> int:
        """Number of active targets, optionally scoped to one session."""
        with self._lock:
            if session_id is None:
                return len(self._refcounts)
            return sum(1 for t in self._targets.values() if t.session_id == session_id)

    def active_targets(self, session_id: str | None = None) -> tuple[BackgroundTarget, ...]:
        """Active targets (optionally one session), newest-registered first."""
        with self._lock:
            targets = [
                t for t in self._targets.values() if session_id is None or t.session_id == session_id
            ]
            return tuple(reversed(targets))

    def reset(self) -> None:
        """Drop every target and refcount (session teardown / tests)."""
        with self._lock:
            self._targets.clear()
            self._refcounts.clear()
