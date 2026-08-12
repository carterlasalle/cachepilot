"""PRD §128 tasks 2-3 — registration and pure-observer hook behavior.

A duck-typed ``FakePluginContext`` stands in for Hermes'
``hermes_cli.plugins.PluginContext``; the real context's registration
surface is exactly ``register_middleware(kind, cb)`` /
``register_hook(name, cb)`` (verified against Hermes v0.20.0 source), which
is all the plugin uses.
"""

import logging

from cachepilot_hermes.config import CachePilotConfig
from cachepilot_hermes.plugin import HOOK_NAMES, MIDDLEWARE_KINDS, create_plugin, register

EXPECTED_MIDDLEWARE = {"tool_request", "tool_execution", "llm_request", "llm_execution"}
EXPECTED_HOOKS = {
    "post_tool_call",
    "post_api_request",
    "api_request_error",
    "subagent_start",
    "subagent_stop",
    "on_session_start",
    "on_session_end",
    "on_session_reset",
}


class FakePluginContext:
    """Records middleware/hook registrations; nothing else."""

    def __init__(self):
        self.middleware = {}
        self.hooks = {}

    def register_middleware(self, kind, callback):
        self.middleware.setdefault(kind, []).append(callback)

    def register_hook(self, hook_name, callback):
        self.hooks.setdefault(hook_name, []).append(callback)


# ---------------------------------------------------------------------------
# Registration (PRD §128 tasks 2-3)
# ---------------------------------------------------------------------------


def test_register_registers_exactly_four_middleware_kinds():
    ctx = FakePluginContext()
    register(ctx)
    assert set(ctx.middleware) == EXPECTED_MIDDLEWARE
    assert len(ctx.middleware) == 4
    for callback in ctx.middleware.values():
        assert callable(callback[0])


def test_register_registers_all_documented_hooks():
    ctx = FakePluginContext()
    register(ctx)
    assert set(ctx.hooks) == EXPECTED_HOOKS
    assert len(ctx.hooks) == 8
    for callback in ctx.hooks.values():
        assert callable(callback[0])


def test_registered_sets_match_manifest():
    ctx = FakePluginContext()
    register(ctx)
    assert set(ctx.middleware) == set(MIDDLEWARE_KINDS)
    assert set(ctx.hooks) == set(HOOK_NAMES)


def test_create_plugin_register_matches_module_register():
    ctx_a = FakePluginContext()
    ctx_b = FakePluginContext()
    register(ctx_a)
    create_plugin().register(ctx_b)
    assert set(ctx_a.middleware) == set(ctx_b.middleware)
    assert set(ctx_a.hooks) == set(ctx_b.hooks)


def test_register_emits_structured_debug_log(caplog):
    ctx = FakePluginContext()
    with caplog.at_level(logging.DEBUG, logger="cachepilot_hermes"):
        register(ctx)
    lines = [r.getMessage() for r in caplog.records]
    assert any("event=cachepilot.plugin.register" in line for line in lines)
    line = next(line for line in lines if "event=cachepilot.plugin.register" in line)
    assert "middleware_count=4" in line
    assert "hooks_count=8" in line
    assert "plugin=cachepilot-hermes-plugin" in line


def test_register_log_gated_when_disabled(caplog):
    """enabled=False suppresses the registration log (config gate, not traffic)."""
    ctx = FakePluginContext()
    plugin = create_plugin(CachePilotConfig(enabled=False))
    with caplog.at_level(logging.DEBUG, logger="cachepilot_hermes"):
        plugin.register(ctx)
    assert not any("cachepilot.plugin.register" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Hooks are pure observers and never leak payload values (AGENTS.md rule 10)
# ---------------------------------------------------------------------------


def test_all_hooks_return_none_and_tolerate_unknown_kwargs():
    ctx = FakePluginContext()
    register(ctx)
    for name, callbacks in ctx.hooks.items():
        result = callbacks[0](
            hook_name=name,
            extra="ignored",
            telemetry_schema_version="hermes.observer.v1",
        )
        assert result is None, f"hook {name} must be a pure observer (return None)"


def test_post_tool_call_never_logs_args_or_result(caplog):
    ctx = FakePluginContext()
    register(ctx)
    with caplog.at_level(logging.DEBUG, logger="cachepilot_hermes"):
        ctx.hooks["post_tool_call"][0](
            tool_name="write_file",
            args={"path": "/tmp/x.py", "content": "TOP-SECRET-CONTENT"},
            result="OK wrote TOP-SECRET-CONTENT",
            task_id="t1",
            session_id="s1",
            tool_call_id="c1",
            duration_ms=3,
        )
    assert len(caplog.records) == 1
    line = caplog.records[0].getMessage()
    assert "TOP-SECRET-CONTENT" not in line
    assert "event=cachepilot.hook.post_tool_call" in line
    assert "tool_name=write_file" in line
    assert "session_id=s1" in line


def test_api_request_error_never_logs_error_or_request(caplog):
    ctx = FakePluginContext()
    register(ctx)
    with caplog.at_level(logging.DEBUG, logger="cachepilot_hermes"):
        ctx.hooks["api_request_error"][0](
            task_id="t1",
            session_id="s1",
            model="gpt-4",
            provider="openai",
            status_code=401,
            retryable=False,
            error={"type": "auth", "message": "LEAK-ME"},
            request={"messages": [{"role": "user", "content": "LEAK-ME-TOO"}]},
        )
    assert len(caplog.records) == 1
    line = caplog.records[0].getMessage()
    assert "LEAK-ME" not in line
    assert "LEAK-ME-TOO" not in line
    assert "event=cachepilot.hook.api_request_error" in line
    assert "status_code=401" in line


def test_subagent_hooks_never_log_goals_or_summaries(caplog):
    ctx = FakePluginContext()
    register(ctx)
    with caplog.at_level(logging.DEBUG, logger="cachepilot_hermes"):
        ctx.hooks["subagent_start"][0](
            parent_session_id="p1",
            child_subagent_id="c1",
            child_role="researcher",
            child_goal="exfiltrate the SECRET-PLAN",
        )
        ctx.hooks["subagent_stop"][0](
            parent_session_id="p1",
            child_role="researcher",
            child_status="completed",
            child_summary="The answer is SECRET-PLAN",
            tool_call_history=[{"tool_input": {"content": "SECRET-PLAN"}}],
            duration_ms=1200,
        )
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2
    for line in lines:
        assert "SECRET-PLAN" not in line
    assert "event=cachepilot.hook.subagent_start" in lines[0]
    assert "event=cachepilot.hook.subagent_stop" in lines[1]
    assert "child_status=completed" in lines[1]
