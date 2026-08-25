"""Correlation header injection — PRD §29 primary mechanism (Phase 4).

The ``llm_request`` middleware attaches ``X-CachePilot-Session`` /
``X-CachePilot-Request`` / ``X-CachePilot-Turn`` to the provider request
when it carries a ``headers`` mapping. The relay strips them before
forwarding upstream. Injection is deterministic, fail-open, never mutates
the caller's dicts and never touches message content.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from cachepilot_hermes.config import CachePilotConfig
from cachepilot_hermes.llm_middleware import (
    CORRELATION_HEADER_REQUEST,
    CORRELATION_HEADER_SESSION,
    CORRELATION_HEADER_TARGETS,
    CORRELATION_HEADER_TURN,
    CORRELATION_HEADERS,
    attach_correlation_headers,
    compute_turn_id,
    make_correlation_headers,
    make_llm_request_middleware,
)
from cachepilot_hermes.session import process_session_id
from cachepilot_hermes.targets import BackgroundTarget, BackgroundTargetRegistry

# Deterministic id providers for the middleware tests.
SESSION_ID = "00000000-0000-4000-8000-000000000001"
REQUESTS = iter(["req-1", "req-2", "req-3"])


def _id_providers() -> tuple[Callable[[], str], Callable[[], str]]:
    def session() -> str:
        return SESSION_ID

    def request_id() -> str:
        return next(REQUESTS)

    return session, request_id


# -- id scheme ---------------------------------------------------------------


def test_process_session_id_is_cached_per_process():
    assert process_session_id() == process_session_id()
    assert uuid.UUID(process_session_id())  # a valid uuid


def test_turn_id_is_deterministic_per_session_request_pair():
    a = compute_turn_id("sess-1", "req-1")
    assert a == compute_turn_id("sess-1", "req-1")  # stable
    assert a != compute_turn_id("sess-1", "req-2")  # request changes turn
    assert a != compute_turn_id("sess-2", "req-1")  # session changes turn
    assert uuid.UUID(a)


def test_make_correlation_headers_shape():
    headers = make_correlation_headers("sess-1", "req-9")
    assert set(headers) == {CORRELATION_HEADER_SESSION, CORRELATION_HEADER_REQUEST, CORRELATION_HEADER_TURN}
    assert headers[CORRELATION_HEADER_SESSION] == "sess-1"
    assert headers[CORRELATION_HEADER_REQUEST] == "req-9"
    assert headers[CORRELATION_HEADER_TURN] == compute_turn_id("sess-1", "req-9")


# -- attach_correlation_headers ----------------------------------------------


def test_attach_merges_without_clobbering_existing_headers():
    request = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}], "headers": {"X-CachePilot-Session": "existing-session", "authorization": "Bearer x"}}
    original = dict(request)
    attached = attach_correlation_headers(request, "new-session", "new-request")
    assert attached is not None
    # existing header values win (never clobbered)
    assert attached["headers"][CORRELATION_HEADER_SESSION] == "existing-session"
    assert attached["headers"]["authorization"] == "Bearer x"
    # the new headers are present
    assert attached["headers"][CORRELATION_HEADER_REQUEST] == "new-request"
    assert attached["headers"][CORRELATION_HEADER_TURN] == compute_turn_id("new-session", "new-request")
    # the original dicts are untouched
    assert request == original
    assert request["headers"] == original["headers"]


def test_attach_returns_none_when_no_headers_mapping():
    assert attach_correlation_headers({"model": "gpt-4"}, "s", "r") is None
    assert attach_correlation_headers({"model": "gpt-4", "headers": None}, "s", "r") is None
    assert attach_correlation_headers("not-a-dict", "s", "r") is None
    assert attach_correlation_headers(None, "s", "r") is None
    assert attach_correlation_headers(["list"], "s", "r") is None


def test_attach_never_touches_message_content():
    request = {"messages": [{"role": "user", "content": "SECRET-MESSAGE"}], "headers": {}}
    attached = attach_correlation_headers(request, "s", "r")
    assert attached is not None
    assert attached["messages"] == request["messages"]
    assert attached is not request  # a new dict, the original is untouched


# -- middleware ---------------------------------------------------------------


def test_middleware_injects_headers_via_injected_id_providers():
    cb = make_llm_request_middleware(CachePilotConfig(), session_id_provider=_id_providers()[0], request_id_provider=_id_providers()[1])
    request = {"model": "gpt-4", "messages": [], "headers": {"authorization": "Bearer x"}}
    result = cb(request=request, original_request={})
    assert isinstance(result, dict)
    effective = result["request"]
    assert effective is not request
    assert effective["headers"][CORRELATION_HEADER_SESSION] == SESSION_ID
    assert effective["headers"][CORRELATION_HEADER_REQUEST] == "req-1"
    assert effective["headers"][CORRELATION_HEADER_TURN] == compute_turn_id(SESSION_ID, "req-1")
    assert effective["headers"]["authorization"] == "Bearer x"
    assert request["headers"] == {"authorization": "Bearer x"}  # never mutated


def test_middleware_request_ids_are_per_call():
    session_provider, _ = _id_providers()
    ids = iter(["r-a", "r-b"])
    cb = make_llm_request_middleware(
        CachePilotConfig(),
        session_id_provider=session_provider,
        request_id_provider=lambda: next(ids),
    )
    request = {"model": "gpt-4", "headers": {}}
    first = cb(request=request, original_request={})["request"]
    second = cb(request=request, original_request={})["request"]
    assert first["headers"][CORRELATION_HEADER_REQUEST] == "r-a"
    assert second["headers"][CORRELATION_HEADER_REQUEST] == "r-b"
    assert first["headers"][CORRELATION_HEADER_SESSION] == second["headers"][CORRELATION_HEADER_SESSION]


def test_middleware_pass_through_without_headers_mapping():
    cb = make_llm_request_middleware(CachePilotConfig(), session_id_provider=_id_providers()[0], request_id_provider=_id_providers()[1])
    request = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    assert cb(request=request, original_request={}) is None  # Hermes "no change"
    assert request == {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}


def test_middleware_disabled_flag_is_pure_observer():
    cb = make_llm_request_middleware(
        CachePilotConfig(correlation_headers=False),
        session_id_provider=_id_providers()[0],
        request_id_provider=_id_providers()[1],
    )
    request = {"model": "gpt-4", "headers": {}}
    assert cb(request=request, original_request={}) is None
    assert request == {"model": "gpt-4", "headers": {}}


def test_middleware_fails_open_on_non_dict_request():
    cb = make_llm_request_middleware(CachePilotConfig(), session_id_provider=_id_providers()[0], request_id_provider=_id_providers()[1])
    assert cb(request=None, original_request=None) is None
    assert cb(request="string-payload", original_request=None) is None


def test_middleware_config_from_env_flag(monkeypatch):
    monkeypatch.setenv("CACHEPILOT_CORRELATION_HEADERS", "false")
    assert CachePilotConfig.from_env().correlation_headers is False
    monkeypatch.setenv("CACHEPILOT_CORRELATION_HEADERS", "1")
    assert CachePilotConfig.from_env().correlation_headers is True
    monkeypatch.delenv("CACHEPILOT_CORRELATION_HEADERS")
    assert CachePilotConfig.from_env().correlation_headers is True  # default true


# -- X-CachePilot-Targets (Phase 5, PRD §46/§132) -----------------------------


def test_correlation_headers_include_targets_name():
    assert CORRELATION_HEADER_TARGETS in CORRELATION_HEADERS


def _targets_middleware(registry):
    session_provider, _ = _id_providers()
    return make_llm_request_middleware(
        CachePilotConfig(),
        session_id_provider=session_provider,
        request_id_provider=lambda: "req-1",
        targets_registry=registry,
    )


def test_middleware_injects_targets_count_with_registry():
    registry = BackgroundTargetRegistry()
    registry.register(BackgroundTarget(id="t1", kind="process", session_id=SESSION_ID))
    registry.register(BackgroundTarget(id="t2", kind="subagent", session_id=SESSION_ID))
    cb = _targets_middleware(registry)
    result = cb(request={"model": "gpt-4", "headers": {}}, original_request={})
    assert result is not None
    effective = result["request"]
    assert effective["headers"][CORRELATION_HEADER_TARGETS] == "2"
    # the three classic correlation headers are still present
    assert effective["headers"][CORRELATION_HEADER_SESSION] == SESSION_ID
    assert effective["headers"][CORRELATION_HEADER_REQUEST] == "req-1"


def test_middleware_targets_count_tracks_registry_changes():
    registry = BackgroundTargetRegistry()
    cb = _targets_middleware(registry)
    request = {"model": "gpt-4", "headers": {}}
    assert cb(request=request, original_request={})["request"]["headers"][
        CORRELATION_HEADER_TARGETS
    ] == "0"
    registry.register(BackgroundTarget(id="t1", kind="external", session_id=SESSION_ID))
    assert cb(request=request, original_request={})["request"]["headers"][
        CORRELATION_HEADER_TARGETS
    ] == "1"
    registry.release("t1")
    assert cb(request=request, original_request={})["request"]["headers"][
        CORRELATION_HEADER_TARGETS
    ] == "0"


def test_middleware_omits_targets_header_without_registry():
    cb = make_llm_request_middleware(
        CachePilotConfig(),
        session_id_provider=lambda: SESSION_ID,
        request_id_provider=lambda: "req-1",
    )
    result = cb(request={"model": "gpt-4", "headers": {}}, original_request={})
    assert result is not None
    headers = result["request"]["headers"]
    assert CORRELATION_HEADER_TARGETS not in headers
    assert CORRELATION_HEADER_SESSION in headers  # fail open: only Targets omitted


def test_middleware_targets_registry_error_fails_open():
    class BrokenRegistry:
        def active_count(self, session_id):
            raise RuntimeError("registry boom")

    cb = _targets_middleware(BrokenRegistry())
    result = cb(request={"model": "gpt-4", "headers": {}}, original_request={})
    assert result is not None  # the request still flows
    headers = result["request"]["headers"]
    assert CORRELATION_HEADER_TARGETS not in headers
    assert CORRELATION_HEADER_SESSION in headers
