"""PRD §128 task 5 — middleware callbacks are pure pass-through.

The runner replicas below mirror the exact invocation semantics of Hermes
v0.20.0 (hermes_cli/middleware.py):

- ``apply_llm_request_middleware`` / ``apply_tool_request_middleware``:
  collect each callback's return; only a dict with ``"request"`` /
  ``"args"`` changes the effective payload. ``None`` → unchanged.
- ``_run_execution_chain``: ``callback(**{payload_key: payload,
  "next_call": next, **context})``; the middleware must call ``next_call``
  (single-use) with the payload and return its result.

The plugin's callbacks are exercised through those shapes, proving value
AND structure pass through unchanged with the stock call graph intact.
"""

import copy

from cachepilot_hermes.config import CachePilotConfig, LongTasksSettings
from cachepilot_hermes.duration_history import CommandDurationHistory
from cachepilot_hermes.llm_middleware import (
    make_llm_execution_middleware,
    make_llm_request_middleware,
)
from cachepilot_hermes.tool_middleware import (
    make_tool_execution_middleware,
    make_tool_request_middleware,
)

CONFIG = CachePilotConfig()
SCHEMA_CTX = {
    "telemetry_schema_version": "hermes.observer.v1",
    "middleware_schema_version": "hermes.middleware.v1",
    "session_id": "sess-1",
}


# -- replicas of the Hermes v0.20.0 runner semantics (single callback) ------


def _apply_request_middleware(callback, payload_key, payload, **context):
    """Mirror apply_llm/tool_request_middleware for one callback."""
    kwargs = dict(context)
    kwargs[payload_key] = payload
    kwargs[f"original_{payload_key}"] = copy.deepcopy(payload)
    result = callback(**kwargs)
    if not isinstance(result, dict):
        return payload, False
    return result.get(payload_key, payload), True


def _run_execution_chain(callback, payload_key, payload, terminal, **context):
    """Mirror _run_execution_chain with a single middleware callback."""
    calls = []

    def next_call(next_payload=None):
        calls.append(payload if next_payload is None else next_payload)
        return terminal(payload if next_payload is None else next_payload)

    kwargs = dict(context)
    kwargs[payload_key] = payload
    kwargs["next_call"] = next_call
    result = callback(**kwargs)
    return result, calls


# -- tool_request -----------------------------------------------------------


def test_tool_request_passes_args_through_unchanged():
    args = {"command": "ls -la", "nested": {"a": [1, 2, 3]}}
    before = copy.deepcopy(args)
    cb = make_tool_request_middleware(CONFIG)
    effective, changed = _apply_request_middleware(cb, "args", args, tool_name="terminal", **SCHEMA_CTX)
    assert changed is False
    assert effective == args
    assert effective is args  # same object: no copy, no rewrite
    assert args == before  # never mutated


def test_tool_request_returns_none_contract():
    cb = make_tool_request_middleware(CONFIG)
    assert cb(tool_name="terminal", args={}, original_args={}, **SCHEMA_CTX) is None


# -- tool_request auto-background promotion (PRD §40, Phase 2) -------------


def test_tool_request_backgrounds_long_running_terminal():
    args = {"command": "uv run pytest -x --tb=short", "timeout": 60}
    original = copy.deepcopy(args)
    cb = make_tool_request_middleware(CONFIG)
    result = cb(
        tool_name="terminal",
        args=args,
        original_args={},
        **SCHEMA_CTX,
    )
    assert result is not None
    assert result["source"] == "cachepilot"
    assert result["reason"] == "long-running command"
    assert result["args"]["command"] == "uv run pytest -x --tb=short"
    assert result["args"]["background"] is True
    assert result["args"]["notify_on_complete"] is True
    # The original args object is never mutated (fail open for the caller).
    assert args == original
    assert "background" not in args


def test_tool_request_non_terminal_passes_through():
    cb = make_tool_request_middleware(CONFIG)
    assert (
        cb(
            tool_name="write_file",
            args={"path": "/tmp/x.py", "content": "print(1)"},
            original_args={},
            **SCHEMA_CTX,
        )
        is None
    )


def test_tool_request_respects_explicit_foreground():
    """PRD §44: background=false is respected — no promotion."""
    cb = make_tool_request_middleware(CONFIG)
    result = cb(
        tool_name="terminal",
        args={"command": "uv run pytest", "background": False},
        original_args={},
        **SCHEMA_CTX,
    )
    assert result is None


def test_tool_request_respects_explicit_background():
    """PRD §44: an explicit background=true call is completed with the
    notification flag rather than being left without one."""
    cb = make_tool_request_middleware(CONFIG)
    result = cb(
        tool_name="terminal",
        args={"command": "pwd", "background": True},
        original_args={},
        **SCHEMA_CTX,
    )
    assert result is not None
    assert result["args"]["background"] is True
    assert result["args"]["notify_on_complete"] is True
    assert result["args"]["command"] == "pwd"


def test_tool_request_disabled_runtime_is_pass_through():
    off = CachePilotConfig(long_tasks=LongTasksSettings(enabled=False))
    cb = make_tool_request_middleware(off)
    assert (
        cb(
            tool_name="terminal",
            args={"command": "uv run pytest"},
            original_args={},
            **SCHEMA_CTX,
        )
        is None
    )


def test_tool_request_fails_open_on_malformed_args():
    cb = make_tool_request_middleware(CONFIG)
    assert cb(tool_name="terminal", args=None, original_args=None, **SCHEMA_CTX) is None
    assert (
        cb(tool_name="terminal", args=["not", "a", "dict"], original_args=None, **SCHEMA_CTX)
        is None
    )


def test_tool_request_promotes_on_learned_duration(tmp_path):
    """PRD §43: learned p90 promotes a command with no static hint."""
    history = CommandDurationHistory(tmp_path / "history.db")
    for _ in range(5):
        history.record("data-crunch.sh --full", 60.0)
    cb = make_tool_request_middleware(CONFIG, history=history)
    result = cb(
        tool_name="terminal",
        args={"command": "data-crunch.sh --full"},
        original_args={},
        **SCHEMA_CTX,
    )
    assert result is not None
    assert result["args"]["background"] is True
    assert result["args"]["notify_on_complete"] is True

    # Without learned history the same command stays foreground.
    cb2 = make_tool_request_middleware(CONFIG)
    assert (
        cb2(
            tool_name="terminal",
            args={"command": "data-crunch.sh --full"},
            original_args={},
            **SCHEMA_CTX,
        )
        is None
    )


# -- llm_request ------------------------------------------------------------


def test_llm_request_passes_request_through_unchanged():
    request = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
        "extra": {"nested": [1, 2]},
    }
    before = copy.deepcopy(request)
    cb = make_llm_request_middleware(CONFIG)
    effective, changed = _apply_request_middleware(cb, "request", request, **SCHEMA_CTX)
    assert changed is False
    assert effective == request
    assert effective is request
    assert request == before


def test_llm_request_returns_none_contract():
    cb = make_llm_request_middleware(CONFIG)
    assert cb(request={}, original_request={}, **SCHEMA_CTX) is None


# -- tool_execution ---------------------------------------------------------


def test_tool_execution_calls_next_once_with_original_args():
    args = {"command": "sleep 1", "nested": {"x": [1]}}
    original = copy.deepcopy(args)
    seen = []

    def terminal(payload):
        seen.append(copy.deepcopy(payload))
        return {"output": "done"}

    cb = make_tool_execution_middleware(CONFIG)
    result, calls = _run_execution_chain(
        cb, "args", args, terminal, tool_name="terminal", **SCHEMA_CTX
    )
    assert result == {"output": "done"}
    assert len(calls) == 1  # next_call is single-use; we call it exactly once
    assert calls[0] == original
    assert calls[0] == args
    assert args == original  # middleware never mutated the payload


def test_tool_execution_fails_open_without_next_call():
    args = {"command": "ls"}
    cb = make_tool_execution_middleware(CONFIG)
    assert cb(tool_name="terminal", args=args, original_args=args) == args


# -- llm_execution ----------------------------------------------------------


def test_llm_execution_calls_next_once_with_original_request():
    request = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
    }
    original = copy.deepcopy(request)
    seen = []

    def terminal(payload):
        seen.append(copy.deepcopy(payload))
        return {"content": "ok", "usage": {"total_tokens": 5}}

    cb = make_llm_execution_middleware(CONFIG)
    result, calls = _run_execution_chain(cb, "request", request, terminal, **SCHEMA_CTX)
    assert result == {"content": "ok", "usage": {"total_tokens": 5}}
    assert len(calls) == 1
    assert calls[0] == original
    assert calls[0] == request
    assert request == original


def test_llm_execution_fails_open_without_next_call():
    request = {"model": "gpt-4", "messages": []}
    cb = make_llm_execution_middleware(CONFIG)
    assert cb(request=request, original_request=request) == request
