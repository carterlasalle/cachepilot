"""The lease-arming chain end to end inside the plugin (PRD §40 / §46 / §132).

Three surfaces have to agree on ONE identity for a cache lease to ever be
armed in a real deployment:

- ``lifecycle`` registers background targets under the Hermes session id from
  the hook payload;
- ``tool_middleware`` registers a ``process`` target when it auto-backgrounds a
  long command, and ``post_tool_call`` releases it on completion;
- ``llm_middleware`` reads the active-target COUNT back for the
  ``X-CachePilot-Targets`` header the relay uses to keep the lease armed.

These tests exercise the wiring as the plugin actually builds it (default
providers, no test-only session id injected on both sides), because the whole
class of bug here is two surfaces silently using different namespaces.
"""

from __future__ import annotations

import pytest
from cachepilot_hermes.config import CachePilotConfig, LongTasksSettings
from cachepilot_hermes.lifecycle import (
    make_hook_handlers,
    make_on_session_start,
    make_post_tool_call,
    make_subagent_start,
)
from cachepilot_hermes.llm_middleware import (
    CORRELATION_HEADER_SESSION,
    CORRELATION_HEADER_TARGETS,
    make_llm_request_middleware,
)
from cachepilot_hermes.plugin import create_plugin
from cachepilot_hermes.session import (
    current_session_id,
    forget_session_id,
    process_session_id,
)
from cachepilot_hermes.targets import (
    BackgroundTarget,
    BackgroundTargetRegistry,
    process_target_id,
)
from cachepilot_hermes.tool_middleware import make_tool_request_middleware

CONFIG = CachePilotConfig()

#: A Hermes session id, deliberately not a uuid4 and never equal to the
#: per-process id, so a namespace mismatch cannot pass by coincidence.
HERMES_SESSION = "hermes-session-abc123"

#: Promoted by the static known-long families (PRD §42).
LONG_COMMAND = "pytest packages/core/tests -q"


@pytest.fixture(autouse=True)
def _clean_session_identity():
    """The session holder is process-wide — never leak one test's id."""
    forget_session_id()
    yield
    forget_session_id()


def _targets_header(registry: BackgroundTargetRegistry) -> str | None:
    """Read the header exactly as the plugin's llm_request middleware does."""
    callback = make_llm_request_middleware(CONFIG, targets_registry=registry)
    result = callback(request={"model": "gpt-5.2", "headers": {}}, original_request={})
    assert result is not None
    return result["request"]["headers"].get(CORRELATION_HEADER_TARGETS)


# -- B2: the registering and reading sides share one identity -----------------


def test_hermes_session_id_is_not_the_process_id():
    assert HERMES_SESSION != process_session_id()
    assert current_session_id() == process_session_id()  # nothing published yet


def test_subagent_target_is_counted_in_the_targets_header():
    """§46/§132: a registered subagent target must be visible to the header.

    The lifecycle hook keys the target on the Hermes session id from its
    payload; the middleware must resolve the same identity. A per-process uuid
    on the reading side is a disjoint namespace, so the count would be 0 and no
    lease would ever arm.
    """
    registry = BackgroundTargetRegistry()
    make_on_session_start(CONFIG)(session_id=HERMES_SESSION)
    make_subagent_start(CONFIG, targets=registry)(
        parent_session_id=HERMES_SESSION,
        child_session_id="child-1",
        child_role="researcher",
    )
    assert registry.active_count(HERMES_SESSION) == 1
    assert _targets_header(registry) == "1"


def test_subagent_start_alone_publishes_the_session_identity():
    """A build that never fires on_session_start must still correlate."""
    registry = BackgroundTargetRegistry()
    make_subagent_start(CONFIG, targets=registry)(
        parent_session_id=HERMES_SESSION,
        child_session_id="child-1",
    )
    assert current_session_id() == HERMES_SESSION
    assert _targets_header(registry) == "1"


def test_session_header_carries_the_hermes_session_id():
    make_on_session_start(CONFIG)(session_id=HERMES_SESSION)
    callback = make_llm_request_middleware(CONFIG)
    result = callback(request={"model": "gpt-5.2", "headers": {}}, original_request={})
    assert result["request"]["headers"][CORRELATION_HEADER_SESSION] == HERMES_SESSION


def test_targets_header_is_zero_when_no_target_is_registered():
    make_on_session_start(CONFIG)(session_id=HERMES_SESSION)
    assert _targets_header(BackgroundTargetRegistry()) == "0"


# -- B3: auto-backgrounded terminal commands arm and release ------------------


def test_promoted_terminal_command_registers_and_releases_a_process_target():
    """§40 + §46: backgrounding a long command is what the lease is FOR.

    Promotion without registration leaves the relay with zero targets, so the
    flagship feature — keep the cache lease alive for a long background command
    — has no arming path at all.
    """
    registry = BackgroundTargetRegistry()
    make_on_session_start(CONFIG)(session_id=HERMES_SESSION)
    promote = make_tool_request_middleware(CONFIG, targets=registry)
    post_tool_call = make_post_tool_call(CONFIG, None, registry)

    args = {"command": LONG_COMMAND}
    promotion = promote(tool_name="terminal", args=args, original_args=args)
    assert promotion is not None  # the call WAS promoted
    assert promotion["args"]["background"] is True

    target_id = process_target_id(LONG_COMMAND)
    assert registry.refcount(target_id) == 1
    assert registry.active_count(HERMES_SESSION) == 1
    (target,) = registry.active_targets(HERMES_SESSION)
    assert target.kind == "process"
    assert _targets_header(registry) == "1"

    # The command completes → the target is released and the lease can disarm.
    post_tool_call(
        tool_name="terminal",
        args=promotion["args"],
        result={"exit_code": 0},
        session_id=HERMES_SESSION,
        duration_ms=61_000,
    )
    assert registry.active_count(HERMES_SESSION) == 0
    assert _targets_header(registry) == "0"


def test_foreground_command_never_registers_a_target():
    registry = BackgroundTargetRegistry()
    make_on_session_start(CONFIG)(session_id=HERMES_SESSION)
    promote = make_tool_request_middleware(CONFIG, targets=registry)
    args = {"command": "git status"}  # known-fast family
    assert promote(tool_name="terminal", args=args, original_args=args) is None
    assert registry.active_count() == 0


def test_concurrent_promotions_of_one_command_refcount():
    registry = BackgroundTargetRegistry()
    make_on_session_start(CONFIG)(session_id=HERMES_SESSION)
    promote = make_tool_request_middleware(CONFIG, targets=registry)
    post_tool_call = make_post_tool_call(CONFIG, None, registry)
    args = {"command": LONG_COMMAND}
    promote(tool_name="terminal", args=args, original_args=args)
    promote(tool_name="terminal", args=args, original_args=args)
    assert registry.refcount(process_target_id(LONG_COMMAND)) == 2
    assert registry.active_count(HERMES_SESSION) == 1  # one identity, two runs
    post_tool_call(tool_name="terminal", args=args, session_id=HERMES_SESSION)
    assert registry.active_count(HERMES_SESSION) == 1  # one still running
    post_tool_call(tool_name="terminal", args=args, session_id=HERMES_SESSION)
    assert registry.active_count(HERMES_SESSION) == 0


def test_process_target_release_does_not_need_duration_learning():
    """Releasing a finished target must not be gated on telemetry being on."""
    registry = BackgroundTargetRegistry()
    no_learning = CachePilotConfig(
        long_tasks=LongTasksSettings(learn_command_durations=False)
    )
    make_on_session_start(no_learning)(session_id=HERMES_SESSION)
    promote = make_tool_request_middleware(no_learning, targets=registry)
    post_tool_call = make_post_tool_call(no_learning, None, registry)
    args = {"command": LONG_COMMAND}
    promote(tool_name="terminal", args=args, original_args=args)
    assert registry.active_count(HERMES_SESSION) == 1
    post_tool_call(tool_name="terminal", args=args, session_id=HERMES_SESSION)
    assert registry.active_count(HERMES_SESSION) == 0


def test_promotion_still_happens_without_a_registry():
    """Fail open for traffic: no registry must not block the promotion."""
    promote = make_tool_request_middleware(CONFIG)
    args = {"command": LONG_COMMAND}
    promotion = promote(tool_name="terminal", args=args, original_args=args)
    assert promotion is not None
    assert promotion["args"]["background"] is True


def test_target_registration_disabled_with_long_tasks_off():
    registry = BackgroundTargetRegistry()
    off = CachePilotConfig(long_tasks=LongTasksSettings(enabled=False))
    promote = make_tool_request_middleware(off, targets=registry)
    args = {"command": LONG_COMMAND}
    assert promote(tool_name="terminal", args=args, original_args=args) is None
    assert registry.active_count() == 0


# -- the assembled plugin wires all of it together ---------------------------


def test_plugin_wiring_arms_from_a_promoted_command():
    """The chain as `create_plugin()` builds it — no injected test providers."""
    plugin = create_plugin(
        CachePilotConfig(long_tasks=LongTasksSettings(learn_command_durations=False))
    )
    plugin.hooks["on_session_start"](session_id=HERMES_SESSION)
    args = {"command": LONG_COMMAND}
    promotion = plugin.middleware["tool_request"](
        tool_name="terminal", args=args, original_args=args
    )
    assert promotion is not None
    effective = plugin.middleware["llm_request"](
        request={"model": "gpt-5.2", "headers": {}}, original_request={}
    )
    headers = effective["request"]["headers"]
    assert headers[CORRELATION_HEADER_SESSION] == HERMES_SESSION
    assert headers[CORRELATION_HEADER_TARGETS] == "1"

    plugin.hooks["post_tool_call"](
        tool_name="terminal",
        args=promotion["args"],
        result={"exit_code": 0},
        session_id=HERMES_SESSION,
        duration_ms=90_000,
    )
    effective = plugin.middleware["llm_request"](
        request={"model": "gpt-5.2", "headers": {}}, original_request={}
    )
    assert effective["request"]["headers"][CORRELATION_HEADER_TARGETS] == "0"


def test_hook_handlers_wire_the_registry_into_post_tool_call():
    registry = BackgroundTargetRegistry()
    hooks = make_hook_handlers(CONFIG, history=None, targets=registry)
    registry.register(
        BackgroundTarget(
            id=process_target_id(LONG_COMMAND),
            kind="process",
            session_id=HERMES_SESSION,
        )
    )
    hooks["post_tool_call"](
        tool_name="terminal",
        args={"command": LONG_COMMAND},
        session_id=HERMES_SESSION,
    )
    assert registry.active_count(HERMES_SESSION) == 0
