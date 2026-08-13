"""PRD §46 / §48 — background target registry + subagent hook wiring tests.

Covers refcount inc/dec (never negative), subagent start/stop lifecycle
wiring, per-session active counts, and the invariant that target existence
comes exclusively from hook payloads (never conversation text).
"""

import logging

from cachepilot_hermes.config import CachePilotConfig, LongTasksSettings
from cachepilot_hermes.lifecycle import make_subagent_start, make_subagent_stop
from cachepilot_hermes.targets import BackgroundTarget, BackgroundTargetRegistry, TargetKind

CONFIG = CachePilotConfig()


def make_target(
    target_id="t1",
    kind: TargetKind = "process",
    session_id="s1",
    started_at=100.0,
):
    return BackgroundTarget(
        id=target_id, kind=kind, session_id=session_id, started_at=started_at
    )


# ---------------------------------------------------------------------------
# Registry refcounts
# ---------------------------------------------------------------------------


def test_register_increments_refcount_and_activates():
    registry = BackgroundTargetRegistry()
    assert registry.register(make_target()) == 1
    assert registry.is_active("t1")
    assert registry.refcount("t1") == 1
    assert registry.active_count() == 1


def test_release_decrements_and_removes_at_zero():
    registry = BackgroundTargetRegistry()
    registry.register(make_target())
    registry.register(make_target())
    assert registry.refcount("t1") == 2
    assert registry.release("t1") == 1
    assert registry.is_active("t1")
    assert registry.release("t1") == 0
    assert not registry.is_active("t1")
    assert registry.active_count() == 0


def test_release_never_goes_negative():
    registry = BackgroundTargetRegistry()
    assert registry.release("missing") == 0
    registry.register(make_target())
    registry.release("t1")
    assert registry.release("t1") == 0  # second release is a no-op
    assert registry.refcount("t1") == 0
    assert registry.active_count() == 0


def test_empty_id_ignored():
    registry = BackgroundTargetRegistry()
    assert registry.register(make_target(target_id="")) == 0
    assert registry.release("") == 0
    assert registry.active_count() == 0


def test_active_count_scoped_by_session():
    registry = BackgroundTargetRegistry()
    registry.register(make_target("a", session_id="s1"))
    registry.register(make_target("b", session_id="s1"))
    registry.register(make_target("c", session_id="s2"))
    assert registry.active_count() == 3
    assert registry.active_count("s1") == 2
    assert registry.active_count("s2") == 1
    assert registry.active_count("s3") == 0


def test_target_fields_preserved():
    registry = BackgroundTargetRegistry()
    target = make_target(
        target_id="sa-1", kind="subagent", session_id="parent-1", started_at=42.0
    )
    registry.register(target)
    (stored,) = registry.active_targets("parent-1")
    assert stored == target
    assert stored.kind == "subagent"
    assert stored.started_at == 42.0
    assert stored.expected_completion is True


def test_reset_clears_everything():
    registry = BackgroundTargetRegistry()
    registry.register(make_target())
    registry.register(make_target("t2"))
    registry.reset()
    assert registry.active_count() == 0
    assert registry.active_targets() == ()


# ---------------------------------------------------------------------------
# Subagent lifecycle hook wiring (PRD §48)
# ---------------------------------------------------------------------------


def test_subagent_start_stop_updates_refcounts():
    registry = BackgroundTargetRegistry()
    start = make_subagent_start(CONFIG, targets=registry)
    stop = make_subagent_stop(CONFIG, targets=registry)

    start(
        parent_session_id="parent-1",
        child_session_id="child-1",
        child_role="researcher",
    )
    assert registry.active_count("parent-1") == 1
    assert registry.active_count() == 1
    (target,) = registry.active_targets()
    assert target.id == "child-1"
    assert target.kind == "subagent"
    assert target.session_id == "parent-1"

    stop(parent_session_id="parent-1", child_session_id="child-1")
    assert registry.active_count() == 0


def test_subagent_existence_comes_from_hooks_not_text():
    """§48: a target appears only when subagent_start fires — a stop event for
    an unknown id is a no-op, and conversation text is never consulted."""
    registry = BackgroundTargetRegistry()
    start = make_subagent_start(CONFIG, targets=registry)
    stop = make_subagent_stop(CONFIG, targets=registry)

    stop(
        parent_session_id="parent-1",
        child_session_id="ghost-1",
        child_status="completed",
    )
    assert registry.active_count() == 0

    start(
        parent_session_id="parent-1",
        child_session_id="child-2",
        child_goal="any free-form text is irrelevant",
    )
    assert registry.active_count() == 1
    (target,) = registry.active_targets()
    assert target.id == "child-2"  # id from payload, never from text


def test_hooks_fail_open_without_registry():
    """Without a registry the hooks stay pure observers (return None)."""
    start = make_subagent_start(CONFIG)
    stop = make_subagent_stop(CONFIG)
    assert start(parent_session_id="p", child_session_id="c") is None
    assert stop(parent_session_id="p", child_session_id="c") is None


def test_hooks_disabled_when_long_tasks_turned_off():
    registry = BackgroundTargetRegistry()
    off = CachePilotConfig(long_tasks=LongTasksSettings(enabled=False))
    start = make_subagent_start(off, targets=registry)
    stop = make_subagent_stop(off, targets=registry)
    start(parent_session_id="p", child_session_id="c")
    assert registry.active_count() == 0
    stop(parent_session_id="p", child_session_id="c")
    assert registry.active_count() == 0


def test_subagent_hooks_keep_logs_secret_safe(caplog):
    """Wired hooks still emit only safe metadata (AGENTS.md rule 10)."""
    registry = BackgroundTargetRegistry()
    start = make_subagent_start(CONFIG, targets=registry)
    stop = make_subagent_stop(CONFIG, targets=registry)
    with caplog.at_level(logging.DEBUG, logger="cachepilot_hermes"):
        start(
            parent_session_id="p1",
            child_session_id="c1",
            child_role="researcher",
            child_goal="exfiltrate the SECRET-PLAN",
        )
        stop(
            parent_session_id="p1",
            child_session_id="c1",
            child_status="completed",
            child_summary="The answer is SECRET-PLAN",
        )
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2
    for line in lines:
        assert "SECRET-PLAN" not in line
