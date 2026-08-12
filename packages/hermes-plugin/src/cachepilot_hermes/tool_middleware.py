"""Tool middleware factories (PRD §16 / §125 — tool_middleware.py).

Pass-through middleware for ``tool_request`` and ``tool_execution``,
matching Hermes v0.20.0's middleware contract (hermes_cli/middleware.py):

- ``tool_request``:  callback(tool_name, args, original_args, **context).
  Returning ``None`` tells the apply_*_middleware runner "no change" —
  the effective args stay the original object, no trace entry, exactly as
  stock behaves with zero middleware installed.
- ``tool_execution``: callback(tool_name, args, original_args, next_call,
  **context). The chain runner (``_run_execution_chain``) wraps the real
  tool execution in ``next_call``; calling it once with the original args
  and returning its result reproduces stock behavior exactly.

Both only emit a structured DEBUG line with safe metadata (tool name, arg
keys/counts — never arg values, AGENTS.md rule 10) and are fail-open for
traffic: an execution callback with a missing ``next_call`` returns the
payload untouched instead of raising.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from cachepilot_hermes.config import PLUGIN_LOGGER_NAME, CachePilotConfig, emit_debug

logger = logging.getLogger(PLUGIN_LOGGER_NAME)


def make_tool_request_middleware(
    config: CachePilotConfig,
) -> Callable[..., Any]:
    """Return a pass-through ``tool_request`` middleware callback."""

    def _tool_request_middleware(
        tool_name: str = "",
        args: Any = None,
        original_args: Any = None,
        **kwargs: Any,
    ) -> None:
        emit_debug(
            config,
            logger,
            "cachepilot.middleware.tool_request",
            kind="tool_request",
            tool_name=tool_name,
            args_keys=_keys(args),
            args_n=_count(args),
        )
        # Hermes treats None as "no change" — pure observer.
        return None  # noqa: RET501, PLR1711 — pass-through contract

    return _tool_request_middleware


def make_tool_execution_middleware(
    config: CachePilotConfig,
) -> Callable[..., Any]:
    """Return a pass-through ``tool_execution`` middleware callback."""

    def _tool_execution_middleware(
        tool_name: str = "",
        args: Any = None,
        original_args: Any = None,
        next_call: Callable[[Any], Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        emit_debug(
            config,
            logger,
            "cachepilot.middleware.tool_execution",
            kind="tool_execution",
            tool_name=tool_name,
            args_keys=_keys(args),
            args_n=_count(args),
        )
        if next_call is None:
            # Fail open: nothing downstream to invoke — let the payload flow.
            return args
        return next_call(args)

    return _tool_execution_middleware


def _keys(payload: Any) -> tuple[str, ...]:
    return tuple(sorted(payload)) if isinstance(payload, dict) else ()


def _count(payload: Any) -> int:
    return len(payload) if isinstance(payload, (dict, list, tuple)) else 0
