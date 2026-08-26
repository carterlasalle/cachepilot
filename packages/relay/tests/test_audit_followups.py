"""Follow-up audit fixes: endpoint identity, breaker evidence, error status.

These cover the relay-side items of the same audit as the lease-arming chain:

- C7g/C7h — a query string must not fragment cache identity, and must never be
  persisted in the clear (it is where ``?key=<secret>`` providers put their
  credential);
- C7i-adjacent — the §94 warm circuit breaker must only reopen on VERIFIED
  cache evidence, not on any non-failed response;
- C1 — a streaming response's missing usage telemetry must be visible to an
  operator, since it silently disables cost estimation, TTL learning and
  therefore all warming;
- C8n — an upstream timeout is a different condition from an unreachable
  upstream, and the diagnostic body must never echo the URL.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from cachepilot_core.fingerprint import cache_fingerprint
from cachepilot_core.leases import LeaseManager, LeaseSettings
from cachepilot_core.telemetry import Outcome
from cachepilot_relay.observation import (
    RequestObserver,
    build_canonical_request,
    endpoint_identity,
    extract_route_identity,
    request_route_identity,
)

_BASE = "https://gateway.invalid/v1/chat/completions"
_BODY = b'{"model":"gpt-5.2","messages":[{"role":"user","content":"hello"}]}'


# -- C7g/C7h: the endpoint used for identity and storage ---------------------


def test_endpoint_identity_strips_query_fragment_and_userinfo():
    assert endpoint_identity(f"{_BASE}?key=SECRET&alt=sse") == _BASE
    assert endpoint_identity(f"{_BASE}#frag") == _BASE
    assert endpoint_identity("https://user:pw@gateway.invalid/v1") == "https://gateway.invalid/v1"
    assert endpoint_identity(_BASE) == _BASE  # already clean → unchanged
    assert endpoint_identity("gateway.invalid/v1?a=b") == "gateway.invalid/v1"


def test_query_parameters_do_not_fragment_cache_identity():
    """PRD §22/§23 + invariant 8: identity is the route, not the request's query.

    A varying query parameter would spawn a fresh ``endpoint_hash`` and TTL
    profile per request, and ``?alt=sse`` would put the STREAM flag inside cache
    identity — which invariant 8 excludes precisely so a bounded warm can
    refresh the entry a streaming request depends on.
    """
    plain = build_canonical_request(
        _BODY, path="/v1/chat/completions", upstream_url=_BASE, route_hash=None, auth_scope="a"
    )
    with_query = build_canonical_request(
        _BODY,
        path="/v1/chat/completions",
        upstream_url=f"{_BASE}?alt=sse&request_id=42",
        route_hash=None,
        auth_scope="a",
    )
    assert plain.endpoint == with_query.endpoint == _BASE
    assert cache_fingerprint(plain) == cache_fingerprint(with_query)


def test_route_identity_never_carries_a_query_string():
    """``route_events.endpoint`` is persisted in the CLEAR (AGENTS.md rule 10)."""
    signalled = extract_route_identity(f"{_BASE}?key=SECRET", {"x-provider": "other"})
    at_request_time = request_route_identity(f"{_BASE}?key=SECRET")
    assert signalled.endpoint == _BASE
    assert at_request_time.endpoint == _BASE
    assert "SECRET" not in (signalled.endpoint or "")
    assert "SECRET" not in (at_request_time.endpoint or "")


# -- C7i-adjacent: the §94 breaker only reopens on verified evidence ----------


def _breaker_manager():
    manager = LeaseManager(LeaseSettings(jitter_fraction=0.0))
    lease = manager.find_or_create_lease(
        session_id="s1",
        provider="fake",
        model="gpt-5.2",
        api_mode="chat",
        base_url="https://fake.invalid/v1",
        auth_scope_hash="auth-1",
        route_fingerprint=None,
        request_fingerprint="req-1",
        cache_fingerprint="cache-1",
        system_fingerprint="sys-1",
        tools_fingerprint="tools-1",
        history_prefix_fingerprint="hist-1",
    )
    manager.target_started(lease.lease_id, "t1")
    runtime = manager._runtime_for(lease.lease_id)
    runtime.circuit_open = True
    runtime.consecutive_warm_misses = 2
    return manager, lease, runtime


def test_unverified_normal_request_does_not_reopen_the_warm_circuit():
    """§94: the breaker tracks verifiability, so non-evidence cannot clear it.

    Every streaming response is SUCCESS_UNVERIFIED, so clearing the breaker on
    it defeated the guard on exactly the traffic it exists for.
    """
    manager, lease, runtime = _breaker_manager()
    manager.before_normal_request(lease.lease_id)
    manager.after_normal_request(lease.lease_id, Outcome.SUCCESS_UNVERIFIED)
    assert runtime.circuit_open is True
    assert runtime.consecutive_warm_misses == 2
    # §64 still applies: the prefix was physically re-sent, so the age resets.
    assert lease.last_cache_touch_at is not None


def test_verified_normal_request_reopens_the_warm_circuit():
    for outcome in (Outcome.CONFIRMED_HIT, Outcome.MISS_REBUILT):
        manager, lease, runtime = _breaker_manager()
        manager.before_normal_request(lease.lease_id)
        manager.after_normal_request(lease.lease_id, outcome)
        assert runtime.circuit_open is False
        assert runtime.consecutive_warm_misses == 0


# -- C1: the streaming observation gap is visible -----------------------------


def test_streaming_observation_warns_about_its_own_inertness(tmp_path, caplog):
    observer = RequestObserver(db_path=str(tmp_path / "obs.db"))
    with caplog.at_level(logging.WARNING, logger="cachepilot_relay.observation"):
        outcome = observer.observe_streaming(
            _BODY,
            path="/v1/chat/completions",
            upstream_url=_BASE,
            status_code=200,
            response_headers={"content-type": "text/event-stream"},
        )
    assert outcome is Outcome.SUCCESS_UNVERIFIED
    assert observer.streaming_unverified == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0]
    # The consequence is named, not just the fact.
    assert "SKIP_UNKNOWN_PRICING" in message
    assert "TTL bounds never move" in message


def test_streaming_warning_is_emitted_once_but_always_counted(tmp_path, caplog):
    observer = RequestObserver(db_path=str(tmp_path / "obs.db"))
    with caplog.at_level(logging.WARNING, logger="cachepilot_relay.observation"):
        for _ in range(3):
            observer.observe_streaming(
                _BODY,
                path="/v1/chat/completions",
                upstream_url=_BASE,
                status_code=200,
                response_headers={},
            )
    assert observer.streaming_unverified == 3
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


# -- C8n: upstream transport failures ----------------------------------------


def _forward_with_transport(exc: Exception):
    from cachepilot_relay.config import RelayConfig
    from cachepilot_relay.proxy import RelayProxy
    from starlette.requests import Request

    class _FailingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise exc

    async def run():
        async with httpx.AsyncClient(transport=_FailingTransport()) as client:
            proxy = RelayProxy(
                RelayConfig(upstream="https://upstream.invalid/v1", observation_enabled=False),
                client,
            )
            scope = {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "raw_path": b"/v1/chat/completions",
                "query_string": b"key=SECRET",
                "headers": [(b"content-type", b"application/json")],
                "client": ("127.0.0.1", 1234),
                "server": ("127.0.0.1", 8787),
                "scheme": "http",
                "root_path": "",
            }
            sent = {"body": _BODY, "more": False}

            async def receive():
                return {"type": "http.request", "body": sent["body"], "more_body": False}

            return await proxy.forward(Request(scope, receive))

    return asyncio.run(run())


def test_upstream_timeout_answers_504():
    response = _forward_with_transport(httpx.ConnectTimeout("timed out"))
    assert response.status_code == 504
    assert b"upstream_timeout" in response.body


def test_unreachable_upstream_answers_502_without_leaking_the_url():
    response = _forward_with_transport(httpx.ConnectError("refused"))
    assert response.status_code == 502
    assert b"upstream_unreachable" in response.body
    # The diagnostic body must never echo the URL — the query string can carry
    # a credential (AGENTS.md rule 10).
    assert b"SECRET" not in response.body
    assert b"upstream.invalid" not in response.body
