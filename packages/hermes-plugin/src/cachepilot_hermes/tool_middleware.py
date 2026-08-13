"""Tool middleware factories (PRD §16 / §40 / §125 — tool_middleware.py).

``tool_request`` middleware matches Hermes v0.20.0's middleware contract
(hermes_cli/middleware.py):

- callback(tool_name, args, original_args, **context). Returning ``None``
  tells the ``apply_*_middleware`` runner "no change" — the effective args
  stay the original object, exactly as stock behaves with zero middleware.
- Returning ``{"args": ..., "source": ..., "reason": ...}`` replaces the
  effective args (PRD §40 terminal auto-backgrounding).

``tool_execution`` middleware wraps tool execution: calling ``next_call``
once with the original args and returning its result reproduces stock
behavior exactly.

Both fail open for traffic: any classification/database error degrades to a
pass-through ``None`` (never raise, never block), and only ``terminal`` calls
are ever candidates for modification — every other tool is byte-identical to
stock.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from cachepilot_hermes.classifier import LONG_RUNNING, LongTaskClassifier
from cachepilot_hermes.config import PLUGIN_LOGGER_NAME, CachePilotConfig, emit_debug
from cachepilot_hermes.duration_history import CommandDurationHistory

logger = logging.getLogger(PLUGIN_LOGGER_NAME)

# PRD §40 promotion payload — literal "source"/"reason" values the judge and
# downstream tooling can rely on.
_PROMOTION_SOURCE = "cachepilot"
_PROMOTION_REASON = "long-running command"


def make_tool_request_middleware(
    config: CachePilotConfig,
    classifier: LongTaskClassifier | None = None,
    history: CommandDurationHistory | None = None,
) -> Callable[..., Any]:
    """Return a ``tool_request`` middleware callback with auto-backgrounding.

    Args:
        config: Plugin settings (``long_tasks.*`` gate the behavior).
        classifier: Deterministic classifier; built from *config* when None.
        history: Optional duration learner whose learned stats feed the
            classifier (PRD §43 promotion). None disables learned promotion.
    """
    classifier = classifier or LongTaskClassifier(config)

    def _tool_request_middleware(
        tool_name: str = "",
        args: Any = None,
        original_args: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        emit_debug(
            config,
            logger,
            "cachepilot.middleware.tool_request",
            kind="tool_request",
            tool_name=tool_name,
            args_keys=_keys(args),
            args_n=_count(args),
        )
        try:
            # Only terminal calls are candidates; everything else passes
            # through unchanged. Fail open on anything unexpected.
            if tool_name != "terminal":
                return None
            if not config.long_tasks.enabled or not config.long_tasks.auto_background:
                return None
            if not isinstance(args, dict):
                return None

            stats = None
            if history is not None and config.long_tasks.learn_command_durations:
                command = args.get("command")
                if isinstance(command, str):
                    stats = history.stats(command)

            decision = classifier.classify(args, history=stats)
            if decision.decision != LONG_RUNNING:
                return None
            emit_debug(
                config,
                logger,
                "cachepilot.middleware.tool_request.promote",
                tool_name=tool_name,
                decision=decision.decision,
                reason=decision.reason,
            )
            updated = dict(args)
            updated["background"] = True
            if config.long_tasks.notify_on_complete:
                updated["notify_on_complete"] = True
            return {
                "args": updated,
                "source": _PROMOTION_SOURCE,
                "reason": _PROMOTION_REASON,
            }
        except Exception:  # noqa: BLE001 — fail open for traffic: never raise, never block
            return None

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
    return tuple(sorted(payload)) if isinstance(payload, Mapping) else ()


def _count(payload: Any) -> int:
    return len(payload) if isinstance(payload, (dict, list, tuple)) else 0
