"""LLM middleware factories (PRD §16 / §125 — llm_middleware.py).

Pass-through middleware for ``llm_request`` and ``llm_execution``,
matching Hermes v0.20.0's middleware contract (hermes_cli/middleware.py):

- ``llm_request``:  callback(request, original_request, **context).
  Returning ``None`` means "no change" to ``apply_llm_request_middleware`` —
  the effective provider kwargs stay the original, exactly like stock with
  no middleware installed. Phase 4: when the request dict carries a
  ``headers`` mapping, the callback returns ``{"request": <copy with the
  correlation headers merged in>}`` (PRD §29 primary mechanism).
- ``llm_execution``: callback(request, original_request, next_call,
  **context). ``_run_execution_chain`` wraps the real provider call in
  ``next_call``; invoking it once with the original request and returning
  its result reproduces stock behavior exactly.

Correlation headers (PRD §29): ``X-CachePilot-Session`` (cached per
process), ``X-CachePilot-Request`` (fresh per ``llm_request`` call) and
``X-CachePilot-Turn`` (deterministic per session/request pair). The relay
strips them before forwarding upstream, so they never affect provider cache
identity. Injection is strictly fail-open: it never raises, never mutates
the caller's dict, never touches message content, and passes through
unchanged when the request has no ``headers`` mapping.

Both kinds emit a structured DEBUG line with safe metadata only (request key
names/counts — never message content or any value, AGENTS.md rule 10) and
fail open for traffic: an execution callback without ``next_call`` returns
the payload untouched.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from cachepilot_hermes.config import PLUGIN_LOGGER_NAME, CachePilotConfig, emit_debug

logger = logging.getLogger(PLUGIN_LOGGER_NAME)

#: Correlation headers (PRD §29 primary mechanism). The relay strips these
#: before forwarding upstream — they must never affect provider cache
#: identity.
CORRELATION_HEADER_SESSION = "X-CachePilot-Session"
CORRELATION_HEADER_REQUEST = "X-CachePilot-Request"
CORRELATION_HEADER_TURN = "X-CachePilot-Turn"
CORRELATION_HEADERS: tuple[str, ...] = (
    CORRELATION_HEADER_SESSION,
    CORRELATION_HEADER_REQUEST,
    CORRELATION_HEADER_TURN,
)

#: Fixed namespace (uuid.NAMESPACE_URL) so turn ids are deterministic for a
#: given (session, request) pair across processes and restarts.
_TURN_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")

#: Per-process session id, cached for the lifetime of the process.
_process_session_id: str | None = None


def process_session_id() -> str:
    """Return the per-process session id, creating it on first use."""
    global _process_session_id
    if _process_session_id is None:
        _process_session_id = str(uuid.uuid4())
    return _process_session_id


def compute_turn_id(session_id: str, request_id: str) -> str:
    """Deterministic turn id for a (session, request) pair — stable scheme.

    The same (session, request) pair always maps to the same turn id, so
    retries/duplicates of one request stay correlated to one turn.
    """
    return str(uuid.uuid5(_TURN_NAMESPACE, f"{session_id}:{request_id}"))


def make_correlation_headers(session_id: str, request_id: str) -> dict[str, str]:
    """Build the three correlation header values for one request."""
    return {
        CORRELATION_HEADER_SESSION: session_id,
        CORRELATION_HEADER_REQUEST: request_id,
        CORRELATION_HEADER_TURN: compute_turn_id(session_id, request_id),
    }


def attach_correlation_headers(request: Any, session_id: str, request_id: str) -> dict[str, Any] | None:
    """Merge the correlation headers into a copy of *request*.

    Returns a shallow copy of ``request`` with the three headers merged into
    its ``headers`` mapping, or None when they cannot be attached (fail
    open). Existing headers are never clobbered (the caller's values win)
    and the original dicts are never mutated.
    """
    if not isinstance(request, dict):
        return None
    headers = request.get("headers")
    if not isinstance(headers, Mapping):
        return None
    merged = {**make_correlation_headers(session_id, request_id), **headers}
    return {**request, "headers": merged}


def make_llm_request_middleware(
    config: CachePilotConfig,
    *,
    session_id_provider: Callable[[], str] = process_session_id,
    request_id_provider: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> Callable[..., Any]:
    """Return the ``llm_request`` middleware callback.

    Args:
        config: plugin settings (``correlation_headers`` gates injection).
        session_id_provider: session id source (default: per-process cached).
        request_id_provider: per-call request id source (default: fresh
            uuid4). Both are injectable so tests are deterministic.
    """

    def _llm_request_middleware(
        request: Any = None,
        original_request: Any = None,
        **kwargs: Any,
    ) -> Any:
        emit_debug(
            config,
            logger,
            "cachepilot.middleware.llm_request",
            kind="llm_request",
            request_keys=_keys(request),
            request_n=_count(request),
        )
        if not config.correlation_headers:
            # Hermes treats None as "no change" — pure observer.
            return None
        try:
            attached = attach_correlation_headers(
                request, session_id_provider(), request_id_provider()
            )
        except Exception:
            logger.debug("cachepilot correlation headers skipped (unexpected error)", exc_info=True)
            return None
        if attached is None:
            # No headers mapping on the request — pass through unchanged.
            emit_debug(
                config,
                logger,
                "cachepilot.middleware.llm_request.correlation",
                attached=False,
            )
            return None
        emit_debug(
            config,
            logger,
            "cachepilot.middleware.llm_request.correlation",
            attached=True,
        )
        return {"request": attached}

    return _llm_request_middleware


def make_llm_execution_middleware(
    config: CachePilotConfig,
) -> Callable[..., Any]:
    """Return a pass-through ``llm_execution`` middleware callback."""

    def _llm_execution_middleware(
        request: Any = None,
        original_request: Any = None,
        next_call: Callable[[Any], Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        emit_debug(
            config,
            logger,
            "cachepilot.middleware.llm_execution",
            kind="llm_execution",
            request_keys=_keys(request),
            request_n=_count(request),
        )
        if next_call is None:
            # Fail open: nothing downstream to invoke — let the payload flow.
            return request
        return next_call(request)

    return _llm_execution_middleware


def _keys(payload: Any) -> tuple[str, ...]:
    return tuple(sorted(payload)) if isinstance(payload, dict) else ()


def _count(payload: Any) -> int:
    return len(payload) if isinstance(payload, (dict, list, tuple)) else 0
