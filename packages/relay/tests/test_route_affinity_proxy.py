"""P09 relay proxy — economic route affinity application (PRD §72.4, §73-74).

Runs the production ``RelayProxy`` against an in-memory httpx transport with
an adapter that CAN pin routes (OpenRouter-style). Covers:

- an active, economic affinity is applied to the forwarded request body;
- the pin is lease-scoped and REVERSIBLE: the next request's generation
  advance consumes it (PRD §74 — no indefinite pinning);
- global user routing is never overwritten (PRD §74);
- affinity errors fail open — the request is forwarded verbatim
  (AGENTS.md invariant 9).
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

import httpx
from cachepilot_core.adapters import CacheCapabilities, OpenAICompatibleAdapter
from cachepilot_core.fake_provider import (
    FakeProvider,
    FakeProviderConfig,
    provider_result_to_http_response,
)
from cachepilot_core.fingerprint import cache_fingerprint, request_fingerprint
from cachepilot_core.identity import ApiMode, CanonicalRequest
from cachepilot_core.leases import LeaseManager, LeaseSettings
from cachepilot_core.route_affinity import AffinityConfig
from cachepilot_core.storage import TelemetryStore
from cachepilot_relay.config import RelayConfig
from cachepilot_relay.lease_controller import LeaseController
from cachepilot_relay.observation import (
    build_canonical_request,
    request_route_identity,
)
from cachepilot_relay.proxy import RelayProxy
from starlette.requests import Request

UPSTREAM_BASE = "http://upstream.invalid"
UPSTREAM_URL = "http://upstream.invalid/v1/chat/completions"
PATH = "/v1/chat/completions"
_BODY = {"model": "gpt-5.2", "messages": [{"role": "user", "content": "hi"}]}


class PinningAdapter(OpenAICompatibleAdapter):
    """Fake adapter with an OpenRouter-style route-pinning mechanism."""

    capabilities = CacheCapabilities(
        supports_cache_telemetry=True,
        supports_cache_write_telemetry=False,
        supports_prompt_cache_key=True,
        supports_explicit_cache_control=False,
        supports_output_bound=True,
        supports_stream_cancel=False,
        read_refreshes_ttl="unknown",
        route_identity_available=True,
        route_affinity_available=True,
    )

    def can_pin_route(self) -> bool:
        return True

    def apply_route_affinity(self, request, route):
        # PRD §74: never overwrite global user routing — a request that
        # already carries a routing hint is left untouched.
        if "route" in request or "sort" in request:
            return request
        modified = dict(request)
        modified["route"] = route
        return modified


class RaisingAdapter(PinningAdapter):
    """Adapter whose affinity application always blows up (fail-open test)."""

    def apply_route_affinity(self, request, route):
        raise RuntimeError("simulated affinity failure")


def _http_request(body: dict, session: str = "sess-aff") -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": PATH,
        "raw_path": PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"127.0.0.1:8787"),
            (b"content-type", b"application/json"),
            (b"x-cachepilot-session", session.encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8787),
    }
    encoded = json.dumps(body).encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": encoded, "more_body": False}

    return Request(scope, receive)


def _build_proxy(
    tmp_path,
    *,
    adapter,
    captured: list[dict],
    affinity_enabled: bool = True,
) -> tuple[RelayProxy, LeaseController, LeaseManager]:
    """RelayProxy + controller over an offline transport, with one lease."""
    provider = FakeProvider(FakeProviderConfig(seed=7, completion_tokens=42))

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        model = _BODY["model"]
        assert isinstance(model, str)
        canonical = CanonicalRequest.from_content(
            provider="fake-provider",
            model=model,
            api_mode=ApiMode.CHAT,
            endpoint=UPSTREAM_URL,
            auth_scope="test-scope",
            prompt_prefix=json.dumps(captured[-1].get("messages") or [], sort_keys=True),
            system="system prompt",
        )
        # Build a STREAM-based response (httpx treats content=/json= responses
        # as already-consumed, which breaks the proxy's aiter_raw()).
        buffered = provider_result_to_http_response(provider.complete(canonical))
        headers = dict(buffered.headers)
        headers["content-length"] = str(len(buffered.content))
        return httpx.Response(
            buffered.status_code,
            headers=headers,
            stream=httpx.ByteStream(buffered.content),
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = TelemetryStore(tmp_path / "affinity.db")
    manager = LeaseManager(settings=LeaseSettings())
    # Pre-create the lease the request will map onto (same session + body +
    # request-time route → same cache fingerprint).
    route_hash = request_route_identity(UPSTREAM_URL).route_hash()
    canonical = build_canonical_request(
        json.dumps(_BODY).encode(),
        path=PATH,
        upstream_url=UPSTREAM_URL,
        route_hash=route_hash,
        auth_scope="relay-default",
    )
    manager.find_or_create_lease(
        session_id="sess-aff",
        provider=canonical.provider,
        model=canonical.model,
        api_mode=canonical.api_mode.value,
        base_url=canonical.endpoint,
        auth_scope_hash=canonical.auth_scope,
        route_fingerprint=canonical.route,
        request_fingerprint=request_fingerprint(canonical),
        cache_fingerprint=cache_fingerprint(canonical),
        system_fingerprint=canonical.system_hash,
        tools_fingerprint=canonical.tools_hash,
        history_prefix_fingerprint=canonical.prompt_key,
    )
    controller = LeaseController(
        manager=manager,
        store=store,
        enabled=True,
        affinity_config=AffinityConfig(enabled=affinity_enabled),
        affinity_extra_cost_usd=Decimal("0.0"),
    )
    config = RelayConfig(
        upstream=UPSTREAM_BASE,
        listen="127.0.0.1:0",
        observation_enabled=True,
        telemetry_db_path=str(tmp_path / "affinity.db"),
        route_affinity_enabled=affinity_enabled,
    )
    proxy = RelayProxy(config, client, adapter=adapter, lease_controller=controller)
    return proxy, controller, manager


def test_proxy_applies_economic_affinity_then_consumes_it(tmp_path):
    asyncio.run(_scenario_apply_then_consume(tmp_path))


async def _scenario_apply_then_consume(tmp_path) -> None:
    captured: list[dict] = []
    proxy, controller, manager = _build_proxy(
        tmp_path, adapter=PinningAdapter(), captured=captured
    )
    lease = next(iter(manager.lease_ids))
    # Economic affinity is active for this lease's next request.
    controller.affinity_registry.set(
        lease_id=lease,
        route="route-previous",
        expires_at=time.time() + 3600,
        generation=0,
    )
    try:
        # request 1: the pin is applied to the forwarded body
        response = await proxy.forward(_http_request(_BODY))
        assert response.status_code == 200
        assert captured[-1].get("route") == "route-previous"

        # PRD §74 reversible: the generation advance consumed the pin
        lease_after = manager.get(lease)
        assert lease_after is not None
        assert (
            controller.affinity_registry.active_route_for(
                lease, generation=lease_after.generation
            )
            is None
        )

        # request 2: no affinity — forwarded verbatim, no pin
        response2 = await proxy.forward(_http_request(_BODY))
        assert response2.status_code == 200
        assert "route" not in captured[-1]
    finally:
        await proxy._client.aclose()
        proxy.close()
        if controller.store is not None:
            controller.store.close()


def test_proxy_never_overwrites_user_routing(tmp_path):
    asyncio.run(_scenario_user_routing(tmp_path))


async def _scenario_user_routing(tmp_path) -> None:
    adapter = PinningAdapter()
    # unit-level: the adapter itself refuses to clobber a user routing hint
    assert adapter.apply_route_affinity(
        {"model": "m", "route": "user-route"}, "route-previous"
    ) == {"model": "m", "route": "user-route"}
    assert adapter.apply_route_affinity(
        {"model": "m", "sort": "price"}, "route-previous"
    ) == {"model": "m", "sort": "price"}
    assert adapter.apply_route_affinity(
        {"model": "m"}, "route-previous"
    ) == {"model": "m", "route": "route-previous"}

    # end-to-end: the user's routing hint reaches the upstream untouched
    captured: list[dict] = []
    proxy, controller, manager = _build_proxy(
        tmp_path, adapter=PinningAdapter(), captured=captured
    )
    lease = next(iter(manager.lease_ids))
    controller.affinity_registry.set(
        lease_id=lease,
        route="route-previous",
        expires_at=time.time() + 3600,
        generation=0,
    )
    try:
        body = {**_BODY, "route": "user-route"}
        response = await proxy.forward(_http_request(body))
        assert response.status_code == 200
        assert captured[-1]["route"] == "user-route"  # user routing wins
    finally:
        await proxy._client.aclose()
        proxy.close()
        if controller.store is not None:
            controller.store.close()


def test_proxy_affinity_failure_fails_open(tmp_path):
    asyncio.run(_scenario_fail_open(tmp_path))


async def _scenario_fail_open(tmp_path) -> None:
    captured: list[dict] = []
    proxy, controller, manager = _build_proxy(
        tmp_path, adapter=RaisingAdapter(), captured=captured
    )
    lease = next(iter(manager.lease_ids))
    controller.affinity_registry.set(
        lease_id=lease,
        route="route-previous",
        expires_at=time.time() + 3600,
        generation=0,
    )
    try:
        # the adapter blows up while applying the pin — the request MUST
        # still be forwarded verbatim (AGENTS.md invariant 9)
        response = await proxy.forward(_http_request(_BODY))
        assert response.status_code == 200
        assert "route" not in captured[-1]
    finally:
        await proxy._client.aclose()
        proxy.close()
        if controller.store is not None:
            controller.store.close()


def test_proxy_affinity_disabled_never_pins(tmp_path):
    asyncio.run(_scenario_disabled(tmp_path))


async def _scenario_disabled(tmp_path) -> None:
    captured: list[dict] = []
    proxy, controller, manager = _build_proxy(
        tmp_path,
        adapter=PinningAdapter(),
        captured=captured,
        affinity_enabled=False,
    )
    lease = next(iter(manager.lease_ids))
    controller.affinity_registry.set(
        lease_id=lease,
        route="route-previous",
        expires_at=time.time() + 3600,
        generation=0,
    )
    try:
        response = await proxy.forward(_http_request(_BODY))
        assert response.status_code == 200
        assert "route" not in captured[-1]  # optional, off by default
    finally:
        await proxy._client.aclose()
        proxy.close()
        if controller.store is not None:
            controller.store.close()
