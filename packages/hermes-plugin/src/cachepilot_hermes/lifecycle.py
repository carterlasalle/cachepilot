"""Lifecycle hook callbacks (PRD §16 / §48 / §125 — lifecycle.py).

Every callback returns ``None`` so Hermes' hook runner collects nothing
(``invoke_hook`` only aggregates non-None returns) and downstream behavior
is byte-identical to stock. Each callback emits one structured DEBUG line
containing only safe metadata — session/task/turn ids, tool names, counts,
booleans. Payload values (tool args, results, error messages, subagent
summaries, request bodies) are NEVER logged (AGENTS.md rule 10).

Phase 2 additions (all fail-open, all gated on ``long_tasks.*`` config):

- ``post_tool_call`` records terminal command durations into the command
  duration learner (PRD §43) — normalized signatures only, no payload.
- ``subagent_start`` / ``subagent_stop`` maintain background-target
  refcounts (PRD §46, §48). Target existence comes exclusively from the
  hook payloads — never inferred from conversation text.

Hook kwargs are documented against the Hermes v0.20.0 call sites
(hermes_cli/hooks.py::_DEFAULT_PAYLOADS, run_agent.py:2846 for
``api_request_error``, tools/delegate_tool.py:1598/2700 for
``subagent_start``/``subagent_stop``). Every callback also tolerates extra
kwargs (``telemetry_schema_version``, future fields) via ``**kwargs``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from cachepilot_hermes.config import PLUGIN_LOGGER_NAME, CachePilotConfig, emit_debug
from cachepilot_hermes.duration_history import CommandDurationHistory
from cachepilot_hermes.targets import BackgroundTarget, BackgroundTargetRegistry

logger = logging.getLogger(PLUGIN_LOGGER_NAME)

# The PRD §16 hook set. Every name is present in Hermes v0.20.0
# hermes_cli/plugins.py::VALID_HOOKS, so no name mapping is required.
HOOK_NAMES: tuple[str, ...] = (
    "post_tool_call",
    "post_api_request",
    "api_request_error",
    "subagent_start",
    "subagent_stop",
    "on_session_start",
    "on_session_end",
    "on_session_reset",
)


def make_post_tool_call(
    config: CachePilotConfig,
    history: CommandDurationHistory | None = None,
) -> Callable[..., Any]:
    """Return the ``post_tool_call`` callback.

    When *history* is provided and duration learning is enabled, terminal
    command durations are recorded (normalized signature only — never the
    command text, per AGENTS.md rule 10).
    """

    def _on_post_tool_call(
        tool_name: str = "",
        args: Any = None,
        result: Any = None,
        task_id: str = "",
        session_id: str = "",
        tool_call_id: str = "",
        duration_ms: Any = None,
        **kwargs: Any,
    ) -> None:
        emit_debug(
            config,
            logger,
            "cachepilot.hook.post_tool_call",
            hook="post_tool_call",
            tool_name=tool_name,
            task_id=task_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            duration_ms=duration_ms,
        )
        try:
            if history is None:
                return None  # noqa: RET501 — observer contract
            if tool_name != "terminal":
                return None  # noqa: RET501 — only terminal is learned
            if not config.long_tasks.enabled or not config.long_tasks.learn_command_durations:
                return None  # noqa: RET501 — learning disabled
            if not isinstance(args, Mapping):
                return None  # noqa: RET501 — malformed payload
            command = args.get("command")
            if not isinstance(command, str) or not command.strip():
                return None  # noqa: RET501 — nothing to learn
            if duration_ms is None:
                return None  # noqa: RET501 — duration unknown
            history.record(
                command,
                float(duration_ms) / 1000.0,
                background=bool(args.get("background", False)),
                success=_result_is_success(result),
            )
        except Exception as exc:  # noqa: BLE001 — fail open: telemetry must never affect the agent loop
            logger.warning("cachepilot hook post_tool_call failed open: %s", type(exc).__name__)
        return None  # noqa: RET501 — observer contract: pass-through

    return _on_post_tool_call


def make_post_api_request(config: CachePilotConfig) -> Callable[..., Any]:
    def _on_post_api_request(
        session_id: str = "",
        task_id: str = "",
        platform: str = "",
        model: str = "",
        provider: str = "",
        base_url: str = "",
        api_mode: str = "",
        api_call_count: Any = None,
        api_duration: Any = None,
        finish_reason: str = "",
        message_count: Any = None,
        response_model: str = "",
        usage: Any = None,
        assistant_content_chars: Any = None,
        assistant_tool_call_count: Any = None,
        **kwargs: Any,
    ) -> None:
        emit_debug(
            config,
            logger,
            "cachepilot.hook.post_api_request",
            hook="post_api_request",
            session_id=session_id,
            task_id=task_id,
            platform=platform,
            model=model,
            provider=provider,
            api_mode=api_mode,
            api_call_count=api_call_count,
            api_duration=api_duration,
            finish_reason=finish_reason,
            message_count=message_count,
            response_model=response_model,
            assistant_content_chars=assistant_content_chars,
            assistant_tool_call_count=assistant_tool_call_count,
        )
        return None  # noqa: RET501, PLR1711 — observer contract: pass-through

    return _on_post_api_request


def make_api_request_error(config: CachePilotConfig) -> Callable[..., Any]:
    def _on_api_request_error(
        task_id: str = "",
        turn_id: str = "",
        api_request_id: str = "",
        session_id: str = "",
        platform: str = "",
        model: str = "",
        provider: str = "",
        base_url: str = "",
        api_mode: str = "",
        api_call_count: Any = None,
        api_duration: Any = None,
        status_code: Any = None,
        retry_count: Any = None,
        max_retries: Any = None,
        retryable: Any = None,
        reason: Any = None,
        error: Any = None,
        request: Any = None,
        **kwargs: Any,
    ) -> None:
        # ``error`` (message), ``reason`` and ``request`` are deliberately
        # not logged: they can carry provider error text or prompt content.
        emit_debug(
            config,
            logger,
            "cachepilot.hook.api_request_error",
            hook="api_request_error",
            task_id=task_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            session_id=session_id,
            platform=platform,
            model=model,
            provider=provider,
            api_mode=api_mode,
            api_call_count=api_call_count,
            api_duration=api_duration,
            status_code=status_code,
            retry_count=retry_count,
            max_retries=max_retries,
            retryable=retryable,
        )
        return None  # noqa: RET501, PLR1711 — observer contract: pass-through

    return _on_api_request_error


def make_subagent_start(
    config: CachePilotConfig,
    targets: BackgroundTargetRegistry | None = None,
) -> Callable[..., Any]:
    """Return the ``subagent_start`` callback.

    Registers a ``subagent`` background target (refcount +1) when a registry
    is attached and the long-task runtime is enabled. Identity comes from the
    hook payload only (PRD §48).
    """

    def _on_subagent_start(
        parent_session_id: str = "",
        parent_turn_id: str = "",
        parent_subagent_id: str = "",
        child_session_id: str = "",
        child_subagent_id: str = "",
        child_role: str = "",
        child_goal: Any = None,
        **kwargs: Any,
    ) -> None:
        # ``child_goal`` is prompt-adjacent free text — never logged.
        emit_debug(
            config,
            logger,
            "cachepilot.hook.subagent_start",
            hook="subagent_start",
            parent_session_id=parent_session_id,
            parent_turn_id=parent_turn_id,
            parent_subagent_id=parent_subagent_id,
            child_session_id=child_session_id,
            child_subagent_id=child_subagent_id,
            child_role=child_role,
        )
        try:
            if targets is None or not config.long_tasks.enabled:
                return None  # noqa: RET501 — observer contract
            target_id = child_session_id or child_subagent_id
            if target_id:
                targets.register(
                    BackgroundTarget(
                        id=target_id,
                        kind="subagent",
                        session_id=parent_session_id,
                        started_at=time.time(),
                    )
                )
        except Exception as exc:  # noqa: BLE001 — fail open: target tracking must never break delegation
            logger.warning("cachepilot hook subagent_start failed open: %s", type(exc).__name__)
        return None  # noqa: RET501 — observer contract: pass-through

    return _on_subagent_start


def make_subagent_stop(
    config: CachePilotConfig,
    targets: BackgroundTargetRegistry | None = None,
) -> Callable[..., Any]:
    """Return the ``subagent_stop`` callback.

    Releases the matching ``subagent`` target (refcount -1, clamped at zero)
    when a registry is attached and the long-task runtime is enabled.
    """

    def _on_subagent_stop(
        parent_session_id: str = "",
        parent_turn_id: str = "",
        child_session_id: str = "",
        child_role: str = "",
        child_summary: Any = None,
        child_status: str = "",
        tool_call_history: Any = None,
        duration_ms: Any = None,
        **kwargs: Any,
    ) -> None:
        # ``child_summary`` (LLM output) and ``tool_call_history`` (tool
        # I/O) are never logged.
        emit_debug(
            config,
            logger,
            "cachepilot.hook.subagent_stop",
            hook="subagent_stop",
            parent_session_id=parent_session_id,
            parent_turn_id=parent_turn_id,
            child_session_id=child_session_id,
            child_role=child_role,
            child_status=child_status,
            duration_ms=duration_ms,
        )
        try:
            if targets is None or not config.long_tasks.enabled:
                return None  # noqa: RET501 — observer contract
            if child_session_id:
                targets.release(child_session_id)
        except Exception as exc:  # noqa: BLE001 — fail open: target tracking must never break delegation
            logger.warning("cachepilot hook subagent_stop failed open: %s", type(exc).__name__)
        return None  # noqa: RET501 — observer contract: pass-through

    return _on_subagent_stop


def make_on_session_start(config: CachePilotConfig) -> Callable[..., Any]:
    def _on_session_start(session_id: str = "", **kwargs: Any) -> None:
        emit_debug(
            config,
            logger,
            "cachepilot.hook.on_session_start",
            hook="on_session_start",
            session_id=session_id,
        )
        return None  # noqa: RET501, PLR1711 — observer contract: pass-through

    return _on_session_start


def make_on_session_end(config: CachePilotConfig) -> Callable[..., Any]:
    def _on_session_end(
        session_id: str = "",
        task_id: str = "",
        turn_id: str = "",
        completed: bool = True,
        failed: bool = False,
        interrupted: bool = False,
        turn_exit_reason: str = "",
        model: str = "",
        platform: str = "",
        **kwargs: Any,
    ) -> None:
        emit_debug(
            config,
            logger,
            "cachepilot.hook.on_session_end",
            hook="on_session_end",
            session_id=session_id,
            task_id=task_id,
            turn_id=turn_id,
            completed=completed,
            failed=failed,
            interrupted=interrupted,
            turn_exit_reason=turn_exit_reason,
            model=model,
            platform=platform,
        )
        return None  # noqa: RET501, PLR1711 — observer contract: pass-through

    return _on_session_end


def make_on_session_reset(config: CachePilotConfig) -> Callable[..., Any]:
    def _on_session_reset(session_id: str = "", **kwargs: Any) -> None:
        emit_debug(
            config,
            logger,
            "cachepilot.hook.on_session_reset",
            hook="on_session_reset",
            session_id=session_id,
        )
        return None  # noqa: RET501, PLR1711 — observer contract: pass-through

    return _on_session_reset


def _result_is_success(result: Any) -> bool:
    """True when a tool result carries no failing exit code (default True)."""
    if isinstance(result, Mapping):
        exit_code = result.get("exit_code")
        if exit_code is not None:
            try:
                return int(exit_code) == 0
            except (TypeError, ValueError):
                return True
    return True


# Factory name -> callback factory, used by plugin.py to build the hook set.
def make_hook_handlers(
    config: CachePilotConfig,
    history: CommandDurationHistory | None = None,
    targets: BackgroundTargetRegistry | None = None,
) -> Mapping[str, Callable[..., Any]]:
    """Build the full set of hook callbacks bound to *config*.

    Args:
        config: Plugin settings.
        history: Optional duration learner wired into ``post_tool_call``.
        targets: Optional target registry wired into the subagent hooks.
    """
    return {
        "post_tool_call": make_post_tool_call(config, history),
        "post_api_request": make_post_api_request(config),
        "api_request_error": make_api_request_error(config),
        "subagent_start": make_subagent_start(config, targets),
        "subagent_stop": make_subagent_stop(config, targets),
        "on_session_start": make_on_session_start(config),
        "on_session_end": make_on_session_end(config),
        "on_session_reset": make_on_session_reset(config),
    }
