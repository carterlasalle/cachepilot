"""Phase 5 lease-manager integration — PRD §132 through the real relay.

The production ``RelayServer`` runs against an offline fake-provider upstream
(the ``DifferentialHarness`` pattern) with a tmp telemetry store. A request
carrying the plugin's correlation headers — including the active
background-target COUNT (``X-CachePilot-Targets``) — arms a cache lease, and
the background scheduler task emits dry-run output.

Assertions (Phase 5 gate):
1. the scheduler logs ``WOULD WARM`` (dry-run, PRD §132) for the armed lease;
2. NO warm request ever leaves the relay — the upstream sees EXACTLY the
   normal requests the test sent (dry-run NEVER sends a network request);
3. a lease row was persisted with the observed state/targets/generation;
4. correlation headers never reach the upstream (PRD §29).
"""

from __future__ import annotations

import asyncio
import logging

from cachepilot_core.leases import LeaseSettings
from cachepilot_core.storage import TelemetryStore
from helpers import DifferentialHarness
from test_observation import _COMPLETION_REQUEST, ObservationUpstream


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
