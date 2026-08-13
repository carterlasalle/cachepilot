"""P09 relay integration — router-miss analysis over the real relay (PRD UC-5).

Runs the production ``RelayServer`` against an offline fake upstream that
simulates OpenRouter-style route instability (PRD §5.4, UC-5): the same
physical request warms the cache on route 1, then the upstream starts
serving from a DIFFERENT deployment (route 2) where the cache is cold.

Assertions:
- the relay classifies the miss after the route change as ROUTE_INSTABILITY
  and records the route-change event (``route_events``);
- the instability miss is NOT fed to TTL refinement: the route-1 profile
  keeps its verified lower bound with NO upper bound, and the instability
  observation is stored not-clean (PRD §56, §72.2-72.3);
- the route switch is also visible as a churn event with ``route_changed``.
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
from cachepilot_core.fingerprint import cache_fingerprint
from cachepilot_core.identity import ApiMode, CanonicalRequest
from cachepilot_core.route_intel import RouteMissVerdict
from cachepilot_core.storage import TelemetryStore
from cachepilot_core.telemetry import Outcome
from cachepilot_relay.observation import (
    build_canonical_request,
    extract_route_identity,
)
from helpers import DifferentialHarness
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response

_COMPLETION_REQUEST = {
    "model": "gpt-5.2",
    "messages": [{"role": "user", "content": "hello"}],
    "stream": False,
}


class RouteSwitchUpstream:
    """Fake upstream whose physical route switches after two requests.

    Requests 1-2 are served by deployment ``edge-route-1`` (route 1 warms,
    then hits); request 3+ is served by ``edge-route-2`` where the fake
    provider's cache is cold — exactly UC-5's router instability.
    """

    def __init__(self) -> None:
        self.provider = FakeProvider(FakeProviderConfig(seed=7, completion_tokens=42))
        self.count = 0

    def _route(self) -> str:
        return "route-1" if self.count <= 2 else "route-2"

    def app(self) -> Starlette:
        app = Starlette()

        async def chat_completions(request: Request) -> Response:
            self.count += 1
            route = self._route()
            self.provider.config.route = route
            self.provider.config.deployment = f"edge-{route}"
            body = await request.json()
            # The fake provider's cache identity tracks the ROUTE, so the
            # route switch makes its cache cold on route 2 (PRD §5.4).
            canonical = CanonicalRequest.from_content(
                provider="fake-provider",
                model=body["model"],
                api_mode=ApiMode.CHAT,
                endpoint="https://fake-provider.invalid/v1",
                auth_scope="test-scope",
                prompt_prefix=json.dumps(body.get("messages") or [], sort_keys=True),
                system="system prompt",
                route=route,
            )
            result = self.provider.complete(canonical)
            response = provider_result_to_http_response(result)
            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=dict(response.headers),
            )

        app.add_route("/v1/chat/completions", chat_completions, methods=["POST"])
        return app


def test_route_switch_miss_classified_instability_and_not_ttl_evidence(tmp_path):
    asyncio.run(_scenario_route_switch(tmp_path))


async def _scenario_route_switch(tmp_path) -> None:
    upstream = RouteSwitchUpstream()
    db = tmp_path / "telemetry.db"
    kwargs = {"telemetry_db_path": str(db), "observation_enabled": True}
    async with DifferentialHarness(upstream.app(), relay_kwargs=kwargs) as harness:
        assert harness.relay is not None and harness.upstream is not None
        headers = {
            "X-CachePilot-Session": "sess-route",
            "X-CachePilot-Request": "req-r",
            "X-CachePilot-Turn": "turn-r",
        }
        # 1: cold on route-1 → MISS_REBUILT (writes the prefix)
        resp_a = await harness.send(
            harness.relay.base_url, "POST", "/v1/chat/completions",
            json=_COMPLETION_REQUEST, headers=headers,
        )
        assert resp_a.status_code == 200
        await asyncio.sleep(0.01)  # strictly positive idle age for the pair
        # 2: identical physical request on route-1 → CONFIRMED_HIT
        resp_b = await harness.send(
            harness.relay.base_url, "POST", "/v1/chat/completions",
            json=_COMPLETION_REQUEST, headers=headers,
        )
        assert resp_b.status_code == 200
        # 3: SAME logical request, route switched to route-2 → cold cache →
        #    MISS_REBUILT where a hit was expected
        resp_c = await harness.send(
            harness.relay.base_url, "POST", "/v1/chat/completions",
            json=_COMPLETION_REQUEST, headers=headers,
        )
        assert resp_c.status_code == 200

    upstream_url = f"{harness.upstream.base_url}/v1/chat/completions"
    route_a = extract_route_identity(
        upstream_url, {"x-provider": "fake-provider", "x-served-by": "edge-route-1"}
    ).route_hash()
    route_b = extract_route_identity(
        upstream_url, {"x-provider": "fake-provider", "x-served-by": "edge-route-2"}
    ).route_hash()
    assert route_a != route_b

    store = TelemetryStore(db)
    try:
        events = store.recent_events(limit=10)
        assert len(events) == 3
        c, b, a = events  # newest first
        assert a.outcome is Outcome.MISS_REBUILT
        assert b.outcome is Outcome.CONFIRMED_HIT
        assert c.outcome is Outcome.MISS_REBUILT
        assert a.route_hash == route_a and b.route_hash == route_a
        assert c.route_hash == route_b

        # -- router-miss analysis (PRD UC-5) ---------------------------------
        route_events = store.recent_route_events(limit=10)
        assert len(route_events) == 1
        event = route_events[0]
        assert event.verdict is RouteMissVerdict.ROUTE_INSTABILITY
        assert event.previous_route_hash == route_a
        assert event.new_route_hash == route_b
        assert event.session_hash == hashlib.sha256(b"sess-route").hexdigest()
        # observable route identity of the NEW route (PRD §71)
        assert event.gateway == "127.0.0.1"
        assert event.upstream_provider == "fake-provider"
        assert event.deployment == "edge-route-2"
        assert event.endpoint == upstream_url
        assert event.region is None

        stats = store.route_intel_stats()
        assert stats.route_switches == 1
        assert stats.instability_verdicts == 1
        assert stats.last_switch_at is not None

        # -- the instability miss never reaches TTL bounds (PRD §56, §72) ----
        profiles = store.list_profiles(limit=10)
        assert len(profiles) == 1
        profile = profiles[0]
        # the verified hit on route-1 raised the lower bound…
        assert profile.route_hash == route_a
        assert profile.lower_bound_s is not None and profile.lower_bound_s > 0
        # …but the route-2 miss never capped the upper bound
        assert profile.upper_bound_s is None
        # route-2's context was never refined (confidence starts fresh)

        # the instability observation IS recorded (pairing continuity) but
        # as not-clean — it can never refine bounds
        canonical_c = build_canonical_request(
            json.dumps(_COMPLETION_REQUEST).encode("utf-8"),
            path="/v1/chat/completions",
            upstream_url=upstream_url,
            route_hash=route_b,
            auth_scope="relay-default",
        )
        instability_obs = store.recent_ttl_observations(
            cache_fingerprint(canonical_c), limit=5
        )
        assert len(instability_obs) == 1
        assert instability_obs[0].outcome is Outcome.MISS_REBUILT
        assert instability_obs[0].clean is False

        # -- the switch also surfaces in churn (route_changed) ----------------
        churn = store.churn_list(limit=10)
        assert len(churn) == 1
        assert churn[0].route_changed is True
        assert store.aggregates().route_changes == 1
    finally:
        store.close()
