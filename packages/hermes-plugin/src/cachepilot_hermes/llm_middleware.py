"""LLM middleware factories (PRD §16 / §125 — llm_middleware.py).

Pass-through middleware for ``llm_request`` and ``llm_execution``,
matching Hermes v0.20.0's middleware contract (hermes_cli/middleware.py):

- ``llm_request``:  callback(request, original_request, **context).
  Returning ``None`` means "no change" to ``apply_llm_request_middleware`` —
  the effective provider kwargs stay the original, exactly like stock with
  no middleware installed.
- ``llm_execution``: callback(request, original_request, next_call,
  **context). ``_run_execution_chain`` wraps the real provider call in
  ``next_call``; invoking it once with the original request and returning
  its result reproduces stock behavior exactly.

Both emit a structured DEBUG line with safe metadata only (request key
names/counts — never message content or any value, AGENTS.md rule 10) and
fail open for traffic: an execution callback without ``next_call`` returns
the payload untouched.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from cachepilot_hermes.config import PLUGIN_LOGGER_NAME, CachePilotConfig, emit_debug

logger = logging.getLogger(PLUGIN_LOGGER_NAME)


def make_llm_request_middleware(
    config: CachePilotConfig,
) -> Callable[..., Any]:
    """Return a pass-through ``llm_request`` middleware callback."""

    def _llm_request_middleware(
        request: Any = None,
        original_request: Any = None,
        **kwargs: Any,
    ) -> None:
        emit_debug(
            config,
            logger,
            "cachepilot.middleware.llm_request",
            kind="llm_request",
            request_keys=_keys(request),
            request_n=_count(request),
        )
        # Hermes treats None as "no change" — pure observer.
        return None  # noqa: RET501, PLR1711 — pass-through contract

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
