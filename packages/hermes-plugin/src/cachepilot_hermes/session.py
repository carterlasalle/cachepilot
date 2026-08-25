"""Hermes session identity (PRD §29 / §46 / §48 — session.py).

Background targets are registered under the Hermes session id that the
lifecycle hooks are handed (``on_session_start`` / ``subagent_start``), and the
``llm_request`` middleware must read the active-target COUNT back under the
SAME id for ``X-CachePilot-Targets`` to ever be anything but ``0`` (PRD §132).
A per-process ``uuid4`` is a different namespace and never intersects it, so
the plugin needs one place that answers "which Hermes session is this?".

That is this module: the lifecycle hooks publish the session id from their
payload, everything else reads it. Until a hook has published one (no session
started yet), the answer falls back to :func:`process_session_id` so the other
correlation headers are still emitted — fail open (PRD §29).

Session identity comes exclusively from hook payloads; it is never inferred
from conversation text or request bodies (PRD §48). Only the id travels here —
never prompts, args or auth material (AGENTS.md rule 10).
"""

from __future__ import annotations

import threading
import uuid

#: Per-process session id, cached for the lifetime of the process. Used only as
#: the fallback below: it correlates a process's requests to each other, but it
#: is NOT a Hermes session id and must never be used to key session state.
_process_session_id: str | None = None
_process_lock = threading.Lock()


def process_session_id() -> str:
    """Return the per-process session id, creating it on first use."""
    global _process_session_id
    with _process_lock:
        if _process_session_id is None:
            _process_session_id = str(uuid.uuid4())
        return _process_session_id


class SessionIdentity:
    """Thread-safe holder for the session id reported by the Hermes hooks.

    Hooks may fire from different threads, so publication and reads are
    lock-guarded. Empty ids are ignored rather than overwriting a known one:
    a payload without the field must never blind the plugin.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session_id: str | None = None

    def publish(self, session_id: str) -> None:
        """Record the Hermes session id from a hook payload (empty ignored)."""
        if not session_id:
            return
        with self._lock:
            self._session_id = session_id

    def forget(self) -> None:
        """Drop the recorded id (session end / test teardown)."""
        with self._lock:
            self._session_id = None

    @property
    def known(self) -> bool:
        """True once a hook has published a Hermes session id."""
        with self._lock:
            return self._session_id is not None

    def current(self) -> str:
        """The Hermes session id, else the per-process id (fail open)."""
        with self._lock:
            session_id = self._session_id
        return session_id if session_id is not None else process_session_id()


#: Process-wide default holder. One Hermes agent process runs one session at a
#: time, so the middleware defaults and the hook callbacks share this instance
#: instead of threading it through every factory.
SESSION_IDENTITY = SessionIdentity()


def publish_session_id(session_id: str) -> None:
    """Publish *session_id* to the process-wide holder."""
    SESSION_IDENTITY.publish(session_id)


def current_session_id() -> str:
    """The current Hermes session id, else the per-process fallback."""
    return SESSION_IDENTITY.current()


def forget_session_id() -> None:
    """Clear the process-wide holder (session end / test teardown)."""
    SESSION_IDENTITY.forget()
