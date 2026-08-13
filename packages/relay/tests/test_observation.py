"""End-to-end relay observation — PRD §131 (Phase 4), through the real relay.

All scenarios run the production ``RelayServer`` against an offline fake
upstream on ephemeral ports (the ``DifferentialHarness`` pattern). The
telemetry store is pointed at a tmp path; nothing touches the real
``~/.hermes/cachepilot`` database.

Scenarios:
1. correlation headers are stripped before the upstream ever sees them;
2. a fake-provider completion round-trip records request_events with correct
   fingerprints and outcomes (MISS_REBUILT then CONFIRMED_HIT for the
   identical physical request), churn detection, and no raw auth material;
3. streaming responses pass through byte-identical and record
   SUCCESS_UNVERIFIED with zero usage (the stream is never consumed);
4. upstream errors pass through and record FAILED;
5. observation disabled ⇒ zero events, pure pass-through;
6. an unusable telemetry path fails open — the relay still forwards.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from cachepilot_core.fake_provider import (
    FakeProvider,
    FakeProviderConfig,
    provider_result_to_http_response,
)
from cachepilot_core.fingerprint import cache_fingerprint, request_fingerprint
from cachepilot_core.identity import ApiMode, CanonicalRequest
from cachepilot_core.storage import TelemetryStore
from cachepilot_core.telemetry import Outcome
from cachepilot_core.usage import TokenUsage
from cachepilot_relay.observation import (
    RequestObserver,
    RouteIdentity,
    build_canonical_request,
    derive_auth_scope,
    extract_route_identity,
    parse_targets_count,
    provider_from_upstream,
    request_route_identity,
    strip_correlation_headers,
)
from helpers import DifferentialHarness
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

_COMPLETION_REQUEST = {
    "model": "gpt-5.2",
    "messages": [{"role": "user", "content": "hello"}],
    "stream": False,
}

_SSE_EVENTS = [
    {
        "id": "chatcmpl-obs1",
        "object": "chat.completion.chunk",
        "created": 1755129600,
        "model": "gpt-5.2",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
    },
    {
        "id": "chatcmpl-obs1",
        "object": "chat.completion.chunk",
        "created": 1755129600,
        "model": "gpt-5.2",
        "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}],
    },
    {
        "id": "chatcmpl-obs1",
        "object": "chat.completion.chunk",
        "created": 1755129600,
        "model": "gpt-5.2",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    },
]


async def _sse_body():
    for event in _SSE_EVENTS:
        yield f"data: {json.dumps(event)}\n\n".encode()
        await asyncio.sleep(0.005)
    yield b"data: [DONE]\n\n"


def parse_sse_events(body: bytes) -> list[object]:
    events: list[object] = []
    for block in body.decode("utf-8").split("\n\n"):
        data = "".join(
            line[len("data:") :].lstrip()
            for line in block.splitlines()
            if line.startswith("data:")
        )
        if not data:
            continue
        events.append(data if data == "[DONE]" else json.loads(data))
    return events


def _fake_completion(canonical: CanonicalRequest, provider: FakeProvider) -> Response:
    """Fake-provider completion response, decorated with route identity
    headers (``x-provider`` / ``x-served-by``) for route extraction."""
    result = provider.complete(canonical)
    response = provider_result_to_http_response(result)
    headers = dict(response.headers)
    headers["x-provider"] = result.provider
    headers["x-served-by"] = "edge-fake-1"
    return Response(content=response.content, status_code=response.status_code, headers=headers)


def _fixed_completion() -> Response:
    """Fixed fake-provider completion so every call is byte-identical."""
    canonical = CanonicalRequest.from_content(
        provider="fake-provider",
        model="gpt-5.2",
        api_mode=ApiMode.CHAT,
        endpoint="https://fake-provider.invalid/v1",
        auth_scope="test-scope",
        prompt_prefix="You are a helpful assistant.",
        system="system prompt",
    )
    return _fake_completion(canonical, FakeProvider(FakeProviderConfig(seed=7, completion_tokens=42)))


class ObservationUpstream:
    """Stateful fake-provider upstream that captures incoming request headers."""

    def __init__(
        self,
        *,
        fixed_response: Response | None = None,
        error_status: int | None = None,
    ) -> None:
        # One shared provider for the app's lifetime: the first request with
        # a given physical identity misses (writes the prefix), the second
        # identical one hits — exactly the behaviour the relay must observe.
        self.provider = FakeProvider(FakeProviderConfig(seed=7, completion_tokens=42))
        self.fixed_response = fixed_response
        self.error_status = error_status
        self.seen_headers: list[dict[str, str]] = []

    def _completion_for(self, body: dict) -> Response:
        if self.fixed_response is not None:
            return self.fixed_response
        # The fake provider's cache identity tracks the actual request body,
        # so changing the messages changes the provider-side cache key too.
        canonical = CanonicalRequest.from_content(
            provider="fake-provider",
            model=body["model"],
            api_mode=ApiMode.CHAT,
            endpoint="https://fake-provider.invalid/v1",
            auth_scope="test-scope",
            prompt_prefix=json.dumps(body.get("messages") or [], sort_keys=True),
            system="system prompt",
        )
        return _fake_completion(canonical, self.provider)

    def app(self) -> Starlette:
        app = Starlette()

        async def chat_completions(request: Request) -> Response:
            self.seen_headers.append({key.lower(): value for key, value in request.headers.items()})
            body = await request.json()
            if self.error_status is not None:
                return JSONResponse({"error": {"message": "simulated boom"}}, status_code=self.error_status)
            if body.get("stream"):
                return StreamingResponse(_sse_body(), media_type="text/event-stream")
            return self._completion_for(body)

        app.add_route("/v1/chat/completions", chat_completions, methods=["POST"])
        return app


def _relay_kwargs(tmp_path) -> dict:
    return {"telemetry_db_path": str(tmp_path / "telemetry.db"), "observation_enabled": True}


# -- scenario 1: correlation headers stripped ---------------------------------


def test_correlation_headers_stripped_before_upstream(tmp_path):
    asyncio.run(_scenario_strip(tmp_path))


async def _scenario_strip(tmp_path) -> None:
    upstream = ObservationUpstream(fixed_response=_fixed_completion())
    assert upstream.fixed_response is not None
    async with DifferentialHarness(upstream.app(), relay_kwargs=_relay_kwargs(tmp_path)) as harness:
        assert harness.relay is not None
        resp = await harness.send(
            harness.relay.base_url,
            "POST",
            "/v1/chat/completions",
            json=_COMPLETION_REQUEST,
            headers={
                "X-CachePilot-Session": "sess-abc",
                "X-CachePilot-Request": "req-1",
                "X-CachePilot-Turn": "turn-1",
                "X-CachePilot-Targets": "2",
                "x-test-header": "keep-me",
            },
        )
        assert resp.status_code == 200
        # byte-identical body (the fixed provider response)
        assert resp.content == upstream.fixed_response.body
        assert resp.headers["x-cachepilot-cache-hit"] == "false"
        # the upstream saw NONE of the correlation headers
        seen = upstream.seen_headers[-1]
        assert "x-cachepilot-session" not in seen
        assert "x-cachepilot-request" not in seen
        assert "x-cachepilot-turn" not in seen
        assert "x-cachepilot-targets" not in seen
        # everything else passed through untouched
        assert seen["x-test-header"] == "keep-me"
        assert seen["content-type"] == "application/json"
    store = TelemetryStore(tmp_path / "telemetry.db")
    try:
        events = store.recent_events(limit=5)
        # session header present → stored as a HASH, not the raw id
        assert len(events) == 1
        assert events[0].session_hash == hashlib.sha256(b"sess-abc").hexdigest()
    finally:
        store.close()


# -- scenario 2: hit/miss fingerprints, churn, no raw content -----------------


def test_completion_telemetry_hit_miss_fingerprints_churn(tmp_path):
    asyncio.run(_scenario_hit_miss_churn(tmp_path))


async def _scenario_hit_miss_churn(tmp_path) -> None:
    upstream = ObservationUpstream()
    db = tmp_path / "telemetry.db"
    async with DifferentialHarness(upstream.app(), relay_kwargs=_relay_kwargs(tmp_path)) as harness:
        assert harness.relay is not None
        assert harness.upstream is not None
        # A: cold request → MISS_REBUILT (writes the prefix)
        resp_a = await harness.send(harness.relay.base_url, "POST", "/v1/chat/completions", json=_COMPLETION_REQUEST)
        assert resp_a.status_code == 200
        # B: identical physical request → CONFIRMED_HIT
        resp_b = await harness.send(harness.relay.base_url, "POST", "/v1/chat/completions", json=_COMPLETION_REQUEST)
        assert resp_b.status_code == 200
        # C: same body, different auth scope → different cache identity → cold
        resp_c = await harness.send(
            harness.relay.base_url,
            "POST",
            "/v1/chat/completions",
            json=_COMPLETION_REQUEST,
            headers={"authorization": "Bearer dev-token"},
        )
        assert resp_c.status_code == 200
        # D: first request of session sess-1, different body
        resp_d = await harness.send(
            harness.relay.base_url,
            "POST",
            "/v1/chat/completions",
            json={**_COMPLETION_REQUEST, "messages": [{"role": "user", "content": "question one"}]},
            headers={"X-CachePilot-Session": "sess-1", "X-CachePilot-Request": "req-1", "X-CachePilot-Turn": "turn-1"},
        )
        assert resp_d.status_code == 200
        # E: same session, body changed again → churn event D→E
        resp_e = await harness.send(
            harness.relay.base_url,
            "POST",
            "/v1/chat/completions",
            json={**_COMPLETION_REQUEST, "messages": [{"role": "user", "content": "question two"}]},
            headers={"X-CachePilot-Session": "sess-1", "X-CachePilot-Request": "req-2", "X-CachePilot-Turn": "turn-2"},
        )
        assert resp_e.status_code == 200

    upstream_url = f"{harness.upstream.base_url}/v1/chat/completions"
    expected_route = extract_route_identity(
        upstream_url, {"x-provider": "fake-provider", "x-served-by": "edge-fake-1"}
    )
    expected = build_canonical_request(
        json.dumps(_COMPLETION_REQUEST).encode("utf-8"),
        path="/v1/chat/completions",
        upstream_url=upstream_url,
        route_hash=expected_route.route_hash(),
        auth_scope="relay-default",
    )

    store = TelemetryStore(db)
    try:
        events = store.recent_events(limit=10)
        assert len(events) == 5
        e, d, c, b, a = events  # newest first
        assert a.outcome is Outcome.MISS_REBUILT
        assert a.cache_write_tokens == 4000
        assert a.cache_read_tokens == 0
        assert b.outcome is Outcome.CONFIRMED_HIT
        assert b.cache_read_tokens == 4000
        assert b.cache_write_tokens == 0
        # C carries a different Authorization → different physical auth scope,
        # so the relay's own cache fingerprint differs — but the provider
        # (whose cache key ignores auth) still served from ITS cache, and the
        # relay records that telemetry honestly instead of second-guessing it.
        assert c.outcome is Outcome.CONFIRMED_HIT
        assert d.outcome is Outcome.MISS_REBUILT
        assert e.outcome is Outcome.MISS_REBUILT

        # fingerprints come from the physical request, deterministically
        assert a.cache_fingerprint == cache_fingerprint(expected)
        assert a.request_fingerprint == request_fingerprint(expected)
        assert b.cache_fingerprint == a.cache_fingerprint
        assert b.request_fingerprint == a.request_fingerprint
        # auth scope participates in cache identity (PRD §22)
        assert c.cache_fingerprint != a.cache_fingerprint
        # changed body → changed identity
        assert d.cache_fingerprint != c.cache_fingerprint
        assert e.cache_fingerprint != d.cache_fingerprint

        # route identity from response headers
        assert a.route_hash == expected_route.route_hash()

        # session stored hashed, never raw
        assert a.session_hash is None and b.session_hash is None and c.session_hash is None
        expected_session = hashlib.sha256(b"sess-1").hexdigest()
        assert d.session_hash == expected_session
        assert e.session_hash == expected_session

        # churn: only D→E (same session, fingerprint moved)
        churn = store.churn_list(limit=10)
        assert len(churn) == 1
        assert churn[0].previous_cache_fingerprint == d.cache_fingerprint
        assert churn[0].new_cache_fingerprint == e.cache_fingerprint
        assert churn[0].history_changed is True
        assert churn[0].route_changed is False
        assert churn[0].model_changed is False
        assert churn[0].session_hash == expected_session

        stats = store.aggregates()
        assert stats.total == 5
        assert stats.confirmed_hits == 2
        assert stats.misses == 3
        assert stats.unverified == 0
        assert stats.failed == 0
        assert stats.churn_events == 1
        assert stats.route_changes == 0
        assert stats.hit_rate == 0.4
    finally:
        store.close()

    # invariant 10: the raw auth token never reached the database bytes
    db_bytes = b""
    for path in (db, tmp_path / "telemetry.db-wal"):
        if path.exists():
            db_bytes += path.read_bytes()
    assert b"dev-token" not in db_bytes


# -- scenario 3: streaming ----------------------------------------------------


def test_streaming_passes_through_and_records_unverified(tmp_path):
    asyncio.run(_scenario_streaming(tmp_path))


async def _scenario_streaming(tmp_path) -> None:
    upstream = ObservationUpstream()
    async with DifferentialHarness(upstream.app(), relay_kwargs=_relay_kwargs(tmp_path)) as harness:
        assert harness.client is not None
        assert harness.upstream is not None and harness.relay is not None
        payload = {"model": "gpt-5.2", "messages": [{"role": "user", "content": "stream"}], "stream": True}

        async def collect(base_url: str) -> bytes:
            chunks: list[bytes] = []
            async with harness.client.stream("POST", base_url + "/v1/chat/completions", json=payload) as resp:
                assert resp.status_code == 200
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
            return b"".join(chunks)

        relayed_body = await collect(harness.relay.base_url)
        direct_body = await collect(harness.upstream.base_url)
        # byte-identical stream; the relay never consumed or modified it
        assert relayed_body == direct_body
        assert parse_sse_events(relayed_body) == [*_SSE_EVENTS, "[DONE]"]

    store = TelemetryStore(tmp_path / "telemetry.db")
    try:
        events = store.recent_events(limit=5)
        assert len(events) == 1
        event = events[0]
        assert event.outcome is Outcome.SUCCESS_UNVERIFIED
        assert event.input_tokens == 0
        assert event.output_tokens == 0
        assert event.cache_read_tokens == 0
        assert event.cache_write_tokens == 0
    finally:
        store.close()


# -- scenario 4: upstream error -----------------------------------------------


def test_upstream_error_passes_through_and_records_failed(tmp_path):
    asyncio.run(_scenario_error(tmp_path))


async def _scenario_error(tmp_path) -> None:
    upstream = ObservationUpstream(error_status=503)
    async with DifferentialHarness(upstream.app(), relay_kwargs=_relay_kwargs(tmp_path)) as harness:
        assert harness.relay is not None
        resp = await harness.send(harness.relay.base_url, "POST", "/v1/chat/completions", json=_COMPLETION_REQUEST)
        assert resp.status_code == 503
        assert resp.json() == {"error": {"message": "simulated boom"}}
    store = TelemetryStore(tmp_path / "telemetry.db")
    try:
        events = store.recent_events(limit=5)
        assert len(events) == 1
        assert events[0].outcome is Outcome.FAILED
        assert events[0].cache_read_tokens == 0
        stats = store.aggregates()
        assert stats.total == 1
        assert stats.failed == 1
    finally:
        store.close()


# -- scenario 5: observation disabled -----------------------------------------


def test_observation_disabled_is_pure_pass_through(tmp_path):
    asyncio.run(_scenario_disabled(tmp_path))


async def _scenario_disabled(tmp_path) -> None:
    upstream = ObservationUpstream(fixed_response=_fixed_completion())
    assert upstream.fixed_response is not None
    kwargs = {"telemetry_db_path": str(tmp_path / "telemetry.db"), "observation_enabled": False}
    async with DifferentialHarness(upstream.app(), relay_kwargs=kwargs) as harness:
        assert harness.relay is not None
        resp = await harness.send(
            harness.relay.base_url,
            "POST",
            "/v1/chat/completions",
            json=_COMPLETION_REQUEST,
            headers={"X-CachePilot-Session": "sess-x", "X-CachePilot-Request": "r", "X-CachePilot-Turn": "t"},
        )
        assert resp.status_code == 200
        assert resp.content == upstream.fixed_response.body
    # zero events written; the telemetry file was never even created
    assert not (tmp_path / "telemetry.db").exists()
    store = TelemetryStore(tmp_path / "telemetry.db")
    try:
        assert store.aggregates().total == 0
        assert store.recent_events(limit=5) == []
    finally:
        store.close()


# -- scenario 6: broken telemetry path fails open ------------------------------


def test_broken_telemetry_path_fails_open(tmp_path):
    asyncio.run(_scenario_broken_store(tmp_path))


async def _scenario_broken_store(tmp_path) -> None:
    # A regular FILE where a directory is required → store cannot open.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file")
    upstream = ObservationUpstream(fixed_response=_fixed_completion())
    assert upstream.fixed_response is not None
    kwargs = {
        "telemetry_db_path": str(blocker / "sub" / "telemetry.db"),
        "observation_enabled": True,
    }
    async with DifferentialHarness(upstream.app(), relay_kwargs=kwargs) as harness:
        assert harness.relay is not None
        # the relay started and forwards normally despite the broken store
        resp = await harness.send(harness.relay.base_url, "POST", "/v1/chat/completions", json=_COMPLETION_REQUEST)
        assert resp.status_code == 200
        assert resp.content == upstream.fixed_response.body


# -- unit-level observation helpers -------------------------------------------


def test_auth_scope_is_stable_hash_and_never_the_credential():
    headers = {"authorization": "Bearer secret-token-abc"}
    scope = derive_auth_scope(headers)
    assert scope.startswith("auth-")
    assert "secret-token-abc" not in scope
    assert scope == derive_auth_scope(headers)
    assert derive_auth_scope({"authorization": "Bearer other-token"}) != scope
    assert derive_auth_scope({}) == "relay-default"
    assert derive_auth_scope({"x-whatever": "1"}) == "relay-default"


def test_provider_from_upstream_known_hosts_and_fallback():
    assert provider_from_upstream("https://api.openai.com/v1") == "openai"
    assert provider_from_upstream("https://openrouter.ai/api/v1") == "openrouter"
    assert provider_from_upstream("https://api.anthropic.com/v1") == "anthropic"
    assert provider_from_upstream("https://api.deepseek.com/v1") == "deepseek"
    assert provider_from_upstream("http://127.0.0.1:8787/v1") == "127.0.0.1"


def test_route_identity_populates_only_observable_fields():
    route = extract_route_identity(
        "http://127.0.0.1:9999/v1/chat/completions",
        {"x-provider": "fake-provider", "x-served-by": "edge-1"},
    )
    assert route.gateway == "127.0.0.1"
    assert route.upstream_provider == "fake-provider"
    assert route.deployment == "edge-1"
    assert route.endpoint == "http://127.0.0.1:9999/v1/chat/completions"
    assert route.region is None
    assert route.route_hash() is not None
    assert RouteIdentity().route_hash() is None
    # case-insensitive header lookup
    route2 = extract_route_identity(
        "http://127.0.0.1:9999/v1/chat/completions",
        {"X-Provider": "other"},
    )
    assert route2.upstream_provider == "other"


def test_strip_correlation_headers_removes_only_correlation_names():
    headers = {
        "X-CachePilot-Session": "s",
        "X-CachePilot-Request": "r",
        "X-CachePilot-Turn": "t",
        "X-CachePilot-Targets": "2",
        "authorization": "Bearer x",
        "content-type": "application/json",
        "x-test-header": "keep",
    }
    strip_correlation_headers(headers)
    lowered = {key.lower() for key in headers}
    assert "x-cachepilot-session" not in lowered
    assert "x-cachepilot-request" not in lowered
    assert "x-cachepilot-turn" not in lowered
    assert "x-cachepilot-targets" not in lowered
    assert lowered == {"authorization", "content-type", "x-test-header"}


def test_parse_targets_count_fails_open():
    assert parse_targets_count("3") == 3
    assert parse_targets_count(" 0 ") == 0
    assert parse_targets_count("-2") == 0
    assert parse_targets_count("many") == 0
    assert parse_targets_count("") == 0
    assert parse_targets_count(None) == 0


def test_build_canonical_request_api_mode_from_path():
    canonical = build_canonical_request(
        json.dumps(_COMPLETION_REQUEST).encode(),
        path="/v1/chat/completions",
        upstream_url="http://127.0.0.1:1/v1",
        route_hash=None,
        auth_scope="relay-default",
    )
    assert canonical.api_mode is ApiMode.CHAT
    assert canonical.model == "gpt-5.2"
    assert canonical.stream is False
    completion = build_canonical_request(
        json.dumps({"model": "m", "prompt": "hi"}).encode(),
        path="/v1/completions",
        upstream_url="http://127.0.0.1:1/v1",
        route_hash=None,
        auth_scope="relay-default",
    )
    assert completion.api_mode is ApiMode.COMPLETION


def test_request_route_identity_matches_response_route_without_header_signals():
    """P08: the request-time route key equals the observed one when the
    response carries no route headers — so the lease resolver finds the
    learned profile (PRD §82)."""
    upstream = "https://fake-provider.invalid/v1"
    request_route = request_route_identity(upstream)
    response_route = extract_route_identity(upstream, {})
    assert request_route == response_route
    assert request_route.route_hash() == response_route.route_hash()
    assert request_route.route_hash() is not None
    # a response-header signal changes the observed route, never the
    # request-time one
    signalled = extract_route_identity(upstream, {"x-provider": "other"})
    assert signalled.route_hash() != request_route.route_hash()


# -- P08: TTL observation feed (PRD §55-56) -----------------------------------


def test_observer_feeds_ttl_learner(tmp_path):
    asyncio.run(_scenario_observer_feeds_ttl(tmp_path))


async def _scenario_observer_feeds_ttl(tmp_path) -> None:
    store = TelemetryStore(tmp_path / "obs.db")
    observer = RequestObserver(store=store)
    try:
        # One shared fake provider: the first identical request misses
        # (writes the prefix), the second hits — exactly PRD §55's evidence.
        provider = FakeProvider(FakeProviderConfig(seed=7, completion_tokens=42))
        canonical = CanonicalRequest.from_content(
            provider="fake-provider",
            model="gpt-5.2",
            api_mode=ApiMode.CHAT,
            endpoint="https://fake-provider.invalid/v1",
            auth_scope="test-scope",
            prompt_prefix="You are a helpful assistant.",
            system="system prompt",
        )

        def _roundtrip() -> tuple[Outcome | None, TokenUsage]:
            result = provider.complete(canonical)
            response = provider_result_to_http_response(result)
            headers = dict(response.headers)
            headers["x-provider"] = result.provider
            return observer.observe_bounded(
                json.dumps(_COMPLETION_REQUEST).encode(),
                response.content,
                path="/v1/chat/completions",
                upstream_url="https://fake-provider.invalid/v1",
                status_code=response.status_code,
                response_headers=headers,
                session_header="sess-ttl-1",
                auth_headers={"authorization": "Bearer sk-test-short"},
            )

        first_outcome, _ = _roundtrip()
        assert first_outcome is Outcome.MISS_REBUILT
        # ensure a strictly positive idle age between the two observations
        await asyncio.sleep(0.01)
        second_outcome, _ = _roundtrip()
        assert second_outcome is Outcome.CONFIRMED_HIT

        profiles = store.list_profiles()
        assert len(profiles) == 1
        profile = profiles[0]
        assert profile.sample_count == 1  # the hit pair refined the profile
        assert profile.lower_bound_s is not None and profile.lower_bound_s > 0
        assert profile.confidence > 0.5  # verified hit raised confidence
    finally:
        observer.close()
        store.close()
