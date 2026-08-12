"""Lifecycle hook callbacks (PRD §16 / §125 — lifecycle.py).

Every callback is a pure observer: it returns ``None`` so Hermes' hook
runner collects nothing (``invoke_hook`` only aggregates non-None returns)
and downstream behavior is byte-identical to stock. Each callback emits one
structured DEBUG line containing only safe metadata — session/task/turn ids,
tool names, counts, booleans. Payload values (tool args, results, error
messages, subagent summaries, request bodies) are NEVER logged (AGENTS.md
rule 10).

Hook kwargs are documented against the Hermes v0.20.0 call sites
(hermes_cli/hooks.py::_DEFAULT_PAYLOADS, run_agent.py:2846 for
``api_request_error``, tools/delegate_tool.py:1598/2700 for
``subagent_start``/``subagent_stop``). Every callback also tolerates extra
kwargs (``telemetry_schema_version``, future fields) via ``**kwargs``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from cachepilot_hermes.config import PLUGIN_LOGGER_NAME, CachePilotConfig, emit_debug

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


def make_post_tool_call(config: CachePilotConfig) -> Callable[..., Any]:
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
        return None  # noqa: RET501, PLR1711 — observer contract: pass-through

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


def make_subagent_start(config: CachePilotConfig) -> Callable[..., Any]:
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
        return None  # noqa: RET501, PLR1711 — observer contract: pass-through

    return _on_subagent_start


def make_subagent_stop(config: CachePilotConfig) -> Callable[..., Any]:
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
        return None  # noqa: RET501, PLR1711 — observer contract: pass-through

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


# Factory name -> callback factory, used by plugin.py to build the hook set.
_HOOK_FACTORIES: dict[str, Callable[[CachePilotConfig], Callable[..., Any]]] = {
    "post_tool_call": make_post_tool_call,
    "post_api_request": make_post_api_request,
    "api_request_error": make_api_request_error,
    "subagent_start": make_subagent_start,
    "subagent_stop": make_subagent_stop,
    "on_session_start": make_on_session_start,
    "on_session_end": make_on_session_end,
    "on_session_reset": make_on_session_reset,
}


def make_hook_handlers(config: CachePilotConfig) -> Mapping[str, Callable[..., Any]]:
    """Build the full set of hook callbacks bound to *config*."""
    return {name: factory(config) for name, factory in _HOOK_FACTORIES.items()}
