"""Phase 5/6 lease-manager integration — PRD §132-133 through the real relay.

The production ``RelayServer`` runs against an offline fake-provider upstream
(the ``DifferentialHarness`` pattern) with a tmp telemetry store. A request
carrying the plugin's correlation headers — including the active
background-target COUNT (``X-CachePilot-Targets``) — arms a cache lease, and
the background scheduler task drives it.

Phase 5 assertions (dry-run gate):
1. the scheduler logs ``WOULD WARM`` (dry-run, PRD §132) for the armed lease;
2. NO warm request ever leaves the relay — the upstream sees EXACTLY the
   normal requests the test sent (dry-run NEVER sends a network request);
3. a lease row was persisted with the observed state/targets/generation;
4. correlation headers never reach the upstream (PRD §29).

Phase 6 assertions (real-warm gate, PRD §133, §147):
5. with ``dry_run=False`` the scheduler sends ONE bounded warm
   (``max_tokens=1``) to the upstream and records it in
   ``warm_count`` / ``warm_cost_usd``;
6. the warm never re-enters observation: no extra telemetry event, no
   generation bump, no recursive lease tracking;
7. the warm re-authenticates with the snapshot's Authorization header.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from cachepilot_core.leases import LeaseSettings
from cachepilot_core.pricing import PricingTable
from cachepilot_core.storage import TelemetryStore
from cachepilot_relay.config import RelayConfig
from helpers import DifferentialHarness
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from test_observation import _COMPLETION_REQUEST, ObservationUpstream, _sse_body

#: P07: the relay tests configure the pricing snapshot so the economic gate
#: can evaluate (PRD §65). Mirrors the FakeProvider defaults — the observed
#: 4000-token prefix costs $0.00352 cold / $0.00032 cached.
_PRICE = PricingTable(
    input_per_mtok=Decimal("0.80"),
    output_per_mtok=Decimal("2.40"),
    cache_read_per_mtok=Decimal("0.08"),
    cache_write_per_mtok=Decimal("0.88"),
)


def test_lease_scheduler_dry_run_warms_nothing_upstream(tmp_path, caplog):
    asyncio.run(_scenario_scheduler_dry_run(tmp_path, caplog))


async def _scenario_scheduler_dry_run(tmp_path, caplog) -> None:
    upstream = ObservationUpstream()
    # TTL shorter than the minimum network margin → the safe deadline is
    # already in the past → the scheduler immediately evaluates the warm as
    # due and logs "WOULD WARM NOW" on every tick (fast, deterministic test).
    settings = LeaseSettings(
        dry_run=True,
        default_ttl_s=2.0,
        minimum_margin_s=10.0,
        scheduler_interval_s=0.05,
        jitter_fraction=0.0,
        # P07: without pricing the economic gate would refuse every warm
        # (SKIP_UNKNOWN_PRICING) — configure the snapshot so the dry-run
        # scheduler can evaluate and log its (virtual) warm.
        pricing=_PRICE,
    )
    kwargs = {
        "telemetry_db_path": str(tmp_path / "telemetry.db"),
        "observation_enabled": True,
        "lease_settings": settings,
    }
    caplog.set_level(logging.INFO, logger="cachepilot_core.leases")
    async with DifferentialHarness(upstream.app(), relay_kwargs=kwargs) as harness:
        assert harness.relay is not None
        resp = await harness.send(
            harness.relay.base_url,
            "POST",
            "/v1/chat/completions",
            json=_COMPLETION_REQUEST,
            headers={
                "X-CachePilot-Session": "sess-lease-1",
                "X-CachePilot-Request": "req-1",
                "X-CachePilot-Turn": "turn-1",
                "X-CachePilot-Targets": "2",
            },
        )
        assert resp.status_code == 200
        # Let the background scheduler task tick a few times.
        await asyncio.sleep(0.4)

    # 1: the scheduler emitted dry-run output.
    assert any("WOULD WARM" in record.message for record in caplog.records), caplog.text

    # 2: NO warm request ever hit the upstream — exactly the one normal
    # request the test sent (dry-run never sends a network request).
    assert len(upstream.seen_headers) == 1

    # 4: correlation headers never reached the upstream.
    seen = upstream.seen_headers[0]
    assert "x-cachepilot-session" not in seen
    assert "x-cachepilot-request" not in seen
    assert "x-cachepilot-turn" not in seen
    assert "x-cachepilot-targets" not in seen

    # 3: a lease row was persisted with the observed state.
    store = TelemetryStore(tmp_path / "telemetry.db")
    try:
        rows = store.list_leases()
        assert len(rows) == 1
        row = rows[0]
        assert row.state in ("armed", "warm_scheduled")
        assert len(row.active_targets) == 2  # the header count
        assert row.generation == 1  # one real request
        assert row.last_cache_touch_at is not None  # refreshed by the real request
        assert row.session_hash  # stored hashed, never raw
        assert row.session_hash != "sess-lease-1"
        # P07: the observed usage was priced into the persisted resume-cost
        # estimates (PRD §65) — a 4000-token prefix at the configured rates
        # (cold = write-rate only, per estimate_resume_costs).
        assert row.estimated_cold_resume_cost_usd == Decimal("0.00352")
        assert row.estimated_cached_resume_cost_usd == Decimal("0.00032")
    finally:
        store.close()


def test_lease_controller_tracks_multiple_requests_same_session(tmp_path):
    asyncio.run(_scenario_multiple_requests(tmp_path))


async def _scenario_multiple_requests(tmp_path) -> None:
    upstream = ObservationUpstream()
    settings = LeaseSettings(
        dry_run=True,
        default_ttl_s=2.0,
        minimum_margin_s=10.0,
        scheduler_interval_s=0.05,
        jitter_fraction=0.0,
        pricing=_PRICE,
    )
    kwargs = {
        "telemetry_db_path": str(tmp_path / "telemetry.db"),
        "observation_enabled": True,
        "lease_settings": settings,
    }
    async with DifferentialHarness(upstream.app(), relay_kwargs=kwargs) as harness:
        assert harness.relay is not None
        # First request with targets → arms the lease (generation 1).
        resp = await harness.send(
            harness.relay.base_url,
            "POST",
            "/v1/chat/completions",
            json=_COMPLETION_REQUEST,
            headers={
                "X-CachePilot-Session": "sess-multi",
                "X-CachePilot-Targets": "1",
            },
        )
        assert resp.status_code == 200
        # Second request, targets now gone → the lease disarms (generation 2).
        resp = await harness.send(
            harness.relay.base_url,
            "POST",
            "/v1/chat/completions",
            json=_COMPLETION_REQUEST,
            headers={
                "X-CachePilot-Session": "sess-multi",
                "X-CachePilot-Targets": "0",
            },
        )
        assert resp.status_code == 200
        await asyncio.sleep(0.2)

    # Still exactly the two normal requests upstream — nothing else.
    assert len(upstream.seen_headers) == 2

    store = TelemetryStore(tmp_path / "telemetry.db")
    try:
        rows = store.list_leases()
        assert len(rows) == 1  # one lease, reused across both requests
        assert rows[0].generation == 2
        assert rows[0].active_targets == ()  # disarmed after targets reached 0
        assert rows[0].state == "inactive"
    finally:
        store.close()


# -- Phase 6: real warm replay (PRD §133, §147) ------------------------------


class BodyRecordingUpstream(ObservationUpstream):
    """ObservationUpstream that also records every request body.

    Lets the integration test prove the warm that left the relay was
    bounded (``max_tokens=1``) and cache-equivalent otherwise.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.seen_bodies: list[dict] = []

    def app(self) -> Starlette:
        app = Starlette()

        async def chat_completions(request: Request) -> Response:
            self.seen_headers.append({key.lower(): value for key, value in request.headers.items()})
            body = await request.json()
            self.seen_bodies.append(body)
            if body.get("stream"):
                return StreamingResponse(_sse_body(), media_type="text/event-stream")
            return self._completion_for(body)

        app.add_route("/v1/chat/completions", chat_completions, methods=["POST"])
        return app


def test_lease_real_warm_fires_only_when_dry_run_disabled(tmp_path):
    asyncio.run(_scenario_real_warm(tmp_path))


async def _scenario_real_warm(tmp_path) -> None:
    upstream = BodyRecordingUpstream()
    # TTL 12s with a 10s network margin → safe deadline = touch + 2s (PRD
    # §53: min(12*0.8, 12-10)); the warm fires exactly once at +2s and the
    # refreshed deadline then sits in the future again.
    settings = LeaseSettings(
        dry_run=False,
        default_ttl_s=12.0,
        minimum_margin_s=10.0,
        scheduler_interval_s=0.05,
        jitter_fraction=0.0,
        # P07: the pricing snapshot makes the warm economically positive
        # (PRD §65) — the gate must pass for the bounded replay to fire.
        pricing=_PRICE,
    )
    kwargs = {
        "telemetry_db_path": str(tmp_path / "telemetry.db"),
        "observation_enabled": True,
        "lease_settings": settings,
    }
    async with DifferentialHarness(upstream.app(), relay_kwargs=kwargs) as harness:
        assert harness.relay is not None
        resp = await harness.send(
            harness.relay.base_url,
            "POST",
            "/v1/chat/completions",
            # An output-bound field makes the warm fire: the adapter bounds
            # it to max_tokens=1 instead of skipping (fail closed otherwise).
            json={**_COMPLETION_REQUEST, "max_tokens": 256},
            headers={
                "X-CachePilot-Session": "sess-warm-1",
                "X-CachePilot-Request": "req-1",
                "X-CachePilot-Turn": "turn-1",
                "X-CachePilot-Targets": "1",
                "Authorization": "Bearer dev-token",
            },
        )
        assert resp.status_code == 200
        # Let the scheduler run past the +2s deadline so the warm fires once.
        await asyncio.sleep(3.0)

    # The upstream saw the normal request AND exactly ONE bounded warm.
    assert len(upstream.seen_bodies) == 2
    normal, warm = upstream.seen_bodies
    assert warm["max_tokens"] == 1  # bounded replay
    assert warm["messages"] == normal["messages"]  # cache-equivalent body

    # The warm re-authenticated with the snapshot's Authorization header.
    assert upstream.seen_headers[1]["authorization"] == "Bearer dev-token"

    store = TelemetryStore(tmp_path / "telemetry.db")
    try:
        rows = store.list_leases()
        assert len(rows) == 1
        row = rows[0]
        # The warm's usage is visible (invariant 4), never hidden.
        assert row.warm_count == 1
        assert row.warm_cost_usd >= 0
        # The warm never re-entered observation: exactly ONE request event
        # (the normal one) and no generation bump (no recursive tracking).
        events = store.recent_events(limit=10)
        assert len(events) == 1
        assert row.generation == 1
    finally:
        store.close()


def test_relay_config_env_wires_lease_dry_run(monkeypatch):
    # Warming is opt-in via CACHEPILOT_LEASE_DRY_RUN=false (PRD §133);
    # the default stays dry-run (fail closed for warming, invariant 9).
    monkeypatch.setenv("CACHEPILOT_UPSTREAM", "https://api.openai.com/v1")
    monkeypatch.setenv("CACHEPILOT_LEASE_DRY_RUN", "false")
    config = RelayConfig.from_env()
    assert config.lease_settings.dry_run is False

    monkeypatch.delenv("CACHEPILOT_LEASE_DRY_RUN")
    default_config = RelayConfig.from_env()
    assert default_config.lease_settings.dry_run is True
