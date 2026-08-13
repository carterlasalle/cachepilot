"""cachepilot CLI — status/leases/costs against a seeded tmp telemetry DB (PRD §77-79)."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import uvicorn
from cachepilot_cli.main import main
from cachepilot_core.leases import CacheLease, LeaseState
from cachepilot_core.route_intel import RouteChangeEvent, RouteMissVerdict
from cachepilot_core.storage import ENV_TELEMETRY_DB, TelemetryStore
from cachepilot_core.telemetry import ChurnEvent, Outcome, TelemetryEvent
from cachepilot_core.ttl import TTLProfile, endpoint_hash
from cachepilot_core.usage import TokenUsage
from cachepilot_relay.config import RelayConfig
from cachepilot_relay.server import create_app


def _event(**overrides) -> TelemetryEvent:
    base = {
        "request_fingerprint": "req-fp",
        "cache_fingerprint": "cache-fp",
        "provider": "openai",
        "model": "gpt-5.2",
        "outcome": Outcome.CONFIRMED_HIT,
        "timestamp": datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return TelemetryEvent(**base)


def _seed_db(tmp_path, *, with_costs: bool = False) -> TelemetryStore:
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record_request(
        _event(
            cache_fingerprint="fp-hit-1",
            outcome=Outcome.CONFIRMED_HIT,
            usage=TokenUsage(prompt_tokens=4200, cache_read_tokens=4000, completion_tokens=42),
        )
    )
    store.record_request(
        _event(
            cache_fingerprint="fp-hit-2",
            outcome=Outcome.CONFIRMED_HIT,
            usage=TokenUsage(prompt_tokens=4200, cache_read_tokens=4000, completion_tokens=42),
        )
    )
    store.record_request(
        _event(
            cache_fingerprint="fp-miss-1",
            provider="anthropic",
            outcome=Outcome.MISS_REBUILT,
            usage=TokenUsage(prompt_tokens=4200, cache_write_tokens=4000, completion_tokens=42),
        )
    )
    store.record_request(
        _event(
            cache_fingerprint="fp-unv-1",
            provider="openrouter",
            outcome=Outcome.SUCCESS_UNVERIFIED,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        )
    )
    store.record_request(
        _event(
            cache_fingerprint="fp-fail-1",
            provider="openrouter",
            outcome=Outcome.FAILED,
            usage=TokenUsage(),
        )
    )
    store.record_churn(
        ChurnEvent(
            timestamp=datetime(2026, 8, 13, 12, 1, 0, tzinfo=UTC),
            session_hash="s1",
            previous_cache_fingerprint="fp-hit-1",
            new_cache_fingerprint="fp-hit-2",
            history_changed=True,
        )
    )
    store.record_churn(
        ChurnEvent(
            timestamp=datetime(2026, 8, 13, 12, 2, 0, tzinfo=UTC),
            session_hash="s2",
            previous_cache_fingerprint="fp-miss-1",
            new_cache_fingerprint="fp-hit-1",
            route_changed=True,
        )
    )
    if with_costs:
        store.record_request(
            _event(
                cache_fingerprint="fp-cost-1",
                provider="openai",
                outcome=Outcome.CONFIRMED_HIT,
                usage=TokenUsage(cost=Decimal("0.0100")),
            )
        )
        store.record_request(
            _event(
                cache_fingerprint="fp-cost-2",
                provider="anthropic",
                outcome=Outcome.MISS_REBUILT,
                usage=TokenUsage(cost=Decimal("0.0300")),
            )
        )
    store.close()
    return store


def test_status_shows_version_mode_and_relay_probe(tmp_path, capsys, monkeypatch):
    _seed_db(tmp_path)
    # a definitely-closed port → deterministic "unreachable"
    monkeypatch.setenv("CACHEPILOT_RELAY_LISTEN", "127.0.0.1:1")
    assert main(["status", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "CachePilot 0.1.0" in out
    assert "Mode: relay" in out
    assert "Relay: unreachable" in out


def _start_real_relay() -> tuple[uvicorn.Server, threading.Thread, int]:
    """Boot a real cachepilotd app (``create_app``) on an ephemeral port.

    The relay runs in a daemon thread via uvicorn's blocking ``run()``; the
    probe only ever touches the local control endpoint, so the upstream
    (a closed loopback port) is never reached. Returns (server, thread,
    bound_port).
    """
    config = RelayConfig(
        upstream="http://127.0.0.1:1",
        listen="127.0.0.1:0",
        observation_enabled=False,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(config),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15.0
    while not server.started:
        if time.time() > deadline:
            thread.join(5)
            raise RuntimeError("relay did not start within 15s")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, thread, port


def test_status_relay_healthy_when_listening(tmp_path, capsys, monkeypatch):
    """E2E-002: 'healthy' requires the REAL relay's control endpoint."""
    _seed_db(tmp_path)
    server, thread, port = _start_real_relay()
    try:
        monkeypatch.setenv("CACHEPILOT_RELAY_LISTEN", f"127.0.0.1:{port}")
        assert main(["status", "--db", str(tmp_path / "telemetry.db")]) == 0
    finally:
        server.should_exit = True
        thread.join(15)
    out = capsys.readouterr().out
    assert "Relay: healthy" in out


class _ForeignHttpHandler(BaseHTTPRequestHandler):
    """Minimal non-relay HTTP server (stands in for hermes-webui on 8787)."""

    def do_GET(self) -> None:
        body = b"<html><body>hermes web ui</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # quiet access log
        pass


def test_status_relay_foreign_http_server_is_not_healthy(tmp_path, capsys, monkeypatch):
    """E2E-002 repro: no cachepilotd, but an HTTP server on the relay port.

    ANY HTTP server on the port must NOT read 'Relay: healthy' — the probe
    now requires the relay's own control body, so hermes-webui squatting on
    8787 reads 'occupied by another service' instead.
    """
    _seed_db(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ForeignHttpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("CACHEPILOT_RELAY_LISTEN", f"127.0.0.1:{server.server_address[1]}")
        assert main(["status", "--db", str(tmp_path / "telemetry.db")]) == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    out = capsys.readouterr().out
    assert "Relay: healthy" not in out
    assert "Relay: occupied by another service" in out


def test_status_cache_health_from_telemetry(tmp_path, capsys):
    _seed_db(tmp_path)
    assert main(["status", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    # 5 seed events (hits=2, miss=1, unverified=1, failed=1) → 66.7% of 3
    assert "requests            5" in out
    assert "cache hit rate      66.7% (2 hits / 3 with telemetry)" in out
    assert "CONFIRMED_HIT       2" in out
    assert "MISS_REBUILT        1" in out
    assert "SUCCESS_UNVERIFIED  1" in out
    assert "FAILED              1" in out
    assert "churn events        2" in out
    assert "route changes       1" in out
    assert "most recent" in out


def test_status_empty_db_says_no_telemetry(tmp_path, capsys):
    TelemetryStore(tmp_path / "telemetry.db").close()
    assert main(["status", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "no telemetry recorded yet" in out
    assert "requests            0" not in out


def test_status_env_db_override(tmp_path, capsys, monkeypatch):
    _seed_db(tmp_path)
    monkeypatch.setenv(ENV_TELEMETRY_DB, str(tmp_path / "telemetry.db"))
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "requests            5" in out


def test_status_plugin_state_honest(tmp_path, capsys, monkeypatch):
    _seed_db(tmp_path)
    monkeypatch.setenv("CACHEPILOT_ENABLED", "true")
    assert main(["status", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "Hermes plugin: active" in out

    # Enabled but no telemetry rows → honest "no telemetry recorded yet".
    monkeypatch.setenv("CACHEPILOT_ENABLED", "true")
    empty = tmp_path / "empty.db"
    TelemetryStore(empty).close()
    assert main(["status", "--db", str(empty)]) == 0
    out = capsys.readouterr().out
    assert "Hermes plugin: active (no telemetry recorded yet)" in out

    # Disabled → inactive, never fabricated as active.
    monkeypatch.setenv("CACHEPILOT_ENABLED", "false")
    assert main(["status", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "Hermes plugin: inactive (CACHEPILOT_ENABLED=false)" in out


def test_leases_empty_db_says_no_active_leases(tmp_path, capsys):
    _seed_db(tmp_path)
    assert main(["leases", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "no active leases" in out
    assert "LEASE" not in out  # no fabricated lease table


def test_leases_lists_real_lease_rows(tmp_path, capsys):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record_lease(
        CacheLease(
            lease_id="lease-11111111",
            session_id="sess-1",
            provider="fake-provider",
            model="gpt-5.2",
            api_mode="chat",
            base_url="https://fake-provider.invalid/v1",
            auth_scope_hash="auth-x",
            route_fingerprint=None,
            request_fingerprint="req-fp",
            cache_fingerprint="cache-fp",
            system_fingerprint="sys-fp",
            tools_fingerprint="tools-fp",
            history_prefix_fingerprint="hist-fp",
            last_real_request_at=time.time() - 100,
            last_cache_touch_at=time.time() - 81.25,  # → CACHE AGE 81s
            last_confirmed_hit_at=None,
            estimated_ttl_s=300.0,
            ttl_confidence=0.5,
            active_targets={"t1", "t2"},
            generation=3,
            warm_count=0,
            warm_cost_usd=0.0,
            state=LeaseState.ARMED,
        )
    )
    store.close()
    assert main(["leases", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    # PRD §78 header + a real row (never fabricated)
    assert "LEASE" in out and "TARGETS" in out and "CACHE AGE" in out
    assert "TTL" in out and "STATE" in out
    assert "lease-11" in out  # lease id short
    assert "81s" in out  # cache age
    assert "300s" in out  # ttl
    assert "ARMED" in out  # state, upper-cased like the PRD example
    assert "no active leases" not in out


def test_costs_shows_recorded_totals_and_never_money_saved(tmp_path, capsys):
    _seed_db(tmp_path, with_costs=True)
    assert main(["costs", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "Recorded costs" in out
    assert "total recorded      $0.040000" in out
    assert "openai" in out
    assert "anthropic" in out
    assert "$0.010000" in out
    assert "$0.030000" in out
    assert "money saved" not in out
    assert "Net CachePilot savings" not in out
    assert "recorded-cost-only" in out


def test_costs_empty_db_still_never_claims_savings(tmp_path, capsys):
    TelemetryStore(tmp_path / "telemetry.db").close()
    assert main(["costs", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "total recorded      $0.000000" in out
    assert "money saved" not in out


def test_unknown_subcommand_fails(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["frobnicate"])


# -- ttl (P08, PRD §76/§82) ---------------------------------------------------


def test_ttl_empty_db_says_no_profiles(tmp_path, capsys):
    TelemetryStore(tmp_path / "telemetry.db").close()
    assert main(["ttl", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "no TTL profiles yet" in out
    assert "Route:" not in out  # no fabricated profiles


def test_ttl_lists_learned_profiles(tmp_path, capsys):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.upsert_profile(
        TTLProfile(
            provider="openrouter",
            model="deepseek-v4-flash",
            api_mode="chat",
            endpoint_hash=endpoint_hash("https://openrouter.ai/api/v1"),
            route_hash="route-abc",
            lower_bound_s=183.0,
            upper_bound_s=302.0,
            estimated_ttl_s=224.65,
            confidence=0.7,
            sample_count=5,
        )
    )
    store.close()
    assert main(["ttl", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "TTL profiles (route-keyed, PRD §82)" in out
    assert "Route: openrouter | deepseek-v4-flash | chat" in out
    assert "route-abc" in out  # short route hash is shown
    assert "estimated     225s" in out  # 224.65 → 225s
    assert "lower bound   183s" in out
    assert "upper bound   302s" in out
    assert "confidence    0.70" in out
    assert "samples       5" in out


def test_ttl_unknown_values_stay_unknown(tmp_path, capsys):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.upsert_profile(
        TTLProfile(
            provider="openrouter",
            model="deepseek-v4-flash",
            api_mode="chat",
            endpoint_hash=endpoint_hash("https://openrouter.ai/api/v1"),
            route_hash=None,
            sample_count=1,
        )
    )
    store.close()
    assert main(["ttl", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "unknown" in out  # never silently guess (PRD §59)
    assert "route         none" in out


# -- routes (P09, PRD §71/§76, UC-5) ------------------------------------------


def test_routes_empty_db_says_no_route_changes(tmp_path, capsys):
    TelemetryStore(tmp_path / "telemetry.db").close()
    assert main(["routes", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "no observed route changes yet" in out
    assert "route switches" not in out  # no fabricated stats


def test_routes_lists_observed_identities_and_instability_stats(tmp_path, capsys):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record_route_event(
        RouteChangeEvent(
            timestamp=datetime(2026, 8, 13, 12, 3, 0, tzinfo=UTC),
            session_hash="s1",
            cache_fingerprint="fp-1",
            request_fingerprint="req-1",
            previous_route_hash="route-aaaa",
            new_route_hash="route-bbbb",
            gateway="openrouter",
            upstream_provider="provider-x",
            endpoint="https://openrouter.ai/api/v1",
            region="us-west",
            deployment="edge-x",
            verdict=RouteMissVerdict.ROUTE_INSTABILITY,
        )
    )
    store.close()
    assert main(["routes", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "Observed routes (PRD §71 identity, UC-5 instability)" in out
    assert "verdict=route_instability" in out
    assert "gateway    openrouter" in out
    assert "upstream   provider-x" in out
    assert "endpoint   https://openrouter.ai/api/v1" in out
    assert "region     us-west" in out
    assert "deployment edge-x" in out
    assert "route switches        1" in out
    assert "last switch           2026-08-13 12:03:00 UTC" in out
    assert "instability verdicts  1" in out
    assert "short-TTL verdicts    0" in out
    assert "route-aaaa" in out and "route-bbbb" in out  # short route hashes


# -- churn (P10, PRD §25/§76) -------------------------------------------------


def _seed_churn_events(store: TelemetryStore) -> None:
    """10 churn events: 8 history, 1 route, 1 system (one without a cause)."""
    cause = "history-boundary churn (recent conversation tail moved)"
    for index in range(10):
        overrides = {
            "timestamp": datetime(2026, 8, 13, 12, index, 0, tzinfo=UTC),
            "session_hash": "s1",
            "previous_cache_fingerprint": f"fp-prev-{index}",
            "new_cache_fingerprint": f"fp-new-{index}",
        }
        if index == 8:
            store.record_churn(
                ChurnEvent(route_changed=True, likely_cause="router affinity loss", **overrides)
            )
        elif index == 9:
            store.record_churn(ChurnEvent(system_changed=True, **overrides))
        else:
            store.record_churn(
                ChurnEvent(
                    history_changed=True,
                    likely_cause=cause,
                    confidence=0.70,
                    estimated_prefix_loss_tokens=1200,
                    first_divergent_offset=9,
                    first_divergent_layer="recent conversation tail",
                    **overrides,
                )
            )


def test_churn_empty_db_says_no_events(tmp_path, capsys):
    TelemetryStore(tmp_path / "telemetry.db").close()
    assert main(["churn", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "no churn events" in out
    assert "changed" not in out  # no fabricated frequencies


def test_churn_counts_and_most_common_causes(tmp_path, capsys):
    store = TelemetryStore(tmp_path / "telemetry.db")
    _seed_churn_events(store)
    store.close()
    assert main(["churn", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "Cache churn (last 10 churn events, PRD §25 detector)" in out
    assert "Per-layer change frequency:" in out
    assert "changed 8/10 churn events" in out  # history
    assert "changed 1/10 churn events" in out  # route
    assert "changed 1/10 churn events" in out  # system
    assert "unchanged in the last 10 churn events" in out  # tools/model/cache key
    assert "Most common likely causes:" in out
    assert "8  history-boundary churn (recent conversation tail moved)" in out
    assert "1  router affinity loss" in out


# -- explain-miss (P10, PRD §75/§137) -----------------------------------------


def test_explain_miss_empty_db_honest(tmp_path, capsys):
    TelemetryStore(tmp_path / "telemetry.db").close()
    assert main(["explain-miss", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "no churn events recorded — nothing to explain" in out
    assert "Likely cause" not in out  # never a fabricated explanation


def test_explain_miss_latest_event(tmp_path, capsys):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record_churn(
        ChurnEvent(
            timestamp=datetime(2026, 8, 13, 12, 30, 0, tzinfo=UTC),
            session_hash="s1",
            previous_cache_fingerprint="fp-prev-1",
            new_cache_fingerprint="fp-new-1",
            history_changed=True,
            likely_cause="history-boundary churn (recent conversation tail moved)",
            confidence=0.70,
            estimated_prefix_loss_tokens=1200,
            first_divergent_offset=9,
            first_divergent_layer="recent conversation tail",
        )
    )
    store.record_churn(
        ChurnEvent(
            timestamp=datetime(2026, 8, 13, 12, 31, 0, tzinfo=UTC),
            session_hash="s2",
            previous_cache_fingerprint="fp-prev-2",
            new_cache_fingerprint="fp-new-2",
            route_changed=True,
            likely_cause="router affinity loss",
            confidence=0.92,
        )
    )
    store.close()
    assert main(["explain-miss", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    # the LATEST event is explained (s2, route churn)
    assert "Cache miss — churn event #2 (2026-08-13 12:31:00 UTC)" in out
    assert "session        s2" in out
    assert "Changed:" in out and "  route" in out
    assert "Stable:" in out and "  system" in out and "  history" in out
    assert "Likely cause:" in out
    assert "router affinity loss" in out
    assert "Confidence:" in out and "0.92" in out
    assert "Estimated reusable prefix lost:" in out
    assert "n/a (previous request content unavailable)" in out  # honest unknown


def test_explain_miss_session_scoped(tmp_path, capsys):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record_churn(
        ChurnEvent(
            timestamp=datetime(2026, 8, 13, 12, 30, 0, tzinfo=UTC),
            session_hash="s1",
            previous_cache_fingerprint="fp-prev-1",
            new_cache_fingerprint="fp-new-1",
            history_changed=True,
            likely_cause="history-boundary churn (recent conversation tail moved)",
            confidence=0.70,
            estimated_prefix_loss_tokens=1200,
            first_divergent_offset=9,
            first_divergent_layer="recent conversation tail",
        )
    )
    store.record_churn(
        ChurnEvent(
            timestamp=datetime(2026, 8, 13, 12, 31, 0, tzinfo=UTC),
            session_hash="s2",
            previous_cache_fingerprint="fp-prev-2",
            new_cache_fingerprint="fp-new-2",
            route_changed=True,
            likely_cause="router affinity loss",
            confidence=0.92,
        )
    )
    store.close()
    db = str(tmp_path / "telemetry.db")
    assert main(["explain-miss", "--db", db, "--session", "s1"]) == 0
    out = capsys.readouterr().out
    assert "session        s1" in out
    assert "history-boundary churn" in out
    assert "~1200 tokens" in out
    assert "offset ~9 within 'recent conversation tail'" in out
    # unknown session → honest
    assert main(["explain-miss", "--db", db, "--session", "nope"]) == 0
    out = capsys.readouterr().out
    assert "no churn events recorded for this session — nothing to explain" in out


def test_explain_miss_unclassified_event_shows_n_a(tmp_path, capsys):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record_churn(
        ChurnEvent(
            timestamp=datetime(2026, 8, 13, 12, 30, 0, tzinfo=UTC),
            session_hash="s1",
            previous_cache_fingerprint="fp-prev-1",
            new_cache_fingerprint="fp-new-1",
            history_changed=True,
        )
    )
    store.close()
    assert main(["explain-miss", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "n/a (not classified)" in out
    assert "Confidence:\n  n/a" in out


# -- P11 volatile isolation surfaces in churn / explain-miss -------------------


def test_churn_most_common_causes_include_p11_volatile_causes(tmp_path, capsys):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record_churn(
        ChurnEvent(
            timestamp=datetime(2026, 8, 13, 12, 30, 0, tzinfo=UTC),
            session_hash="s1",
            previous_cache_fingerprint="fp-a",
            new_cache_fingerprint="fp-b",
            system_changed=True,
            likely_cause="system_suffix_churn (volatile value in dynamic system suffix)",
            confidence=0.85,
        )
    )
    store.record_churn(
        ChurnEvent(
            timestamp=datetime(2026, 8, 13, 12, 31, 0, tzinfo=UTC),
            session_hash="s1",
            previous_cache_fingerprint="fp-b",
            new_cache_fingerprint="fp-c",
            system_changed=True,
            likely_cause="volatile_value_in_prefix (volatile value inside static system prefix)",
            confidence=0.85,
        )
    )
    store.close()
    assert main(["churn", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "1  system_suffix_churn (volatile value in dynamic system suffix)" in out
    assert "1  volatile_value_in_prefix (volatile value inside static system prefix)" in out


def test_explain_miss_surfaces_p11_volatile_cause(tmp_path, capsys):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record_churn(
        ChurnEvent(
            timestamp=datetime(2026, 8, 13, 12, 30, 0, tzinfo=UTC),
            session_hash="s1",
            previous_cache_fingerprint="fp-a",
            new_cache_fingerprint="fp-b",
            system_changed=True,
            likely_cause="system_suffix_churn (volatile value in dynamic system suffix)",
            confidence=0.85,
            estimated_prefix_loss_tokens=22400,
            first_divergent_offset=19,
            first_divergent_layer="dynamic system suffix",
        )
    )
    store.close()
    assert main(["explain-miss", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "system_suffix_churn (volatile value in dynamic system suffix)" in out
    assert "~22400 tokens" in out
    assert "offset ~19 within 'dynamic system suffix'" in out


# -- topology (P11, PRD §24/§138) ----------------------------------------------


def _seed_topology_events(store: TelemetryStore) -> None:
    """Session s1 with 3 requests → 2 consecutive pairs:

    - e1→e2: dynamic system suffix churn (loss 8000, attributed);
    - e2→e3: same tool set in a different order (order permutation).
    """
    suffix_old = "You are helpful.\nCurrent time: 3:14 PM\nBe concise."
    suffix_new = "You are helpful.\nCurrent time: 3:15 PM\nBe concise."
    tools = [
        {"type": "function", "function": {"name": "get_weather"}},
        {"type": "function", "function": {"name": "get_time"}},
    ]

    def event(fp: str, system: str, tool_list) -> None:
        from cachepilot_core.churn import request_content_from_payload

        content = request_content_from_payload(
            {"model": "gpt-5.2", "system": system, "messages": [], "tools": tool_list},
            route_hash="route-abc",
        )
        hashes = content.to_hashes()
        store.record_request(
            TelemetryEvent(
                request_fingerprint=f"req-{fp}",
                cache_fingerprint=fp,
                provider="openai",
                model="gpt-5.2",
                route_hash="route-abc",
                outcome=Outcome.CONFIRMED_HIT,
                session_hash="s1",
                timestamp=datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC),
                system_hash=hashes.system_hash,
                tools_hash=hashes.tools_hash,
                history_hash=hashes.history_hash,
                tools_set_hash=hashes.tools_set_hash,
            )
        )

    event("fp-1", suffix_old, tools)
    event("fp-2", suffix_new, tools)
    event("fp-3", suffix_new, list(reversed(tools)))
    store.record_churn(
        ChurnEvent(
            timestamp=datetime(2026, 8, 13, 12, 1, 0, tzinfo=UTC),
            session_hash="s1",
            previous_cache_fingerprint="fp-1",
            new_cache_fingerprint="fp-2",
            system_changed=True,
            likely_cause="system_suffix_churn (volatile value in dynamic system suffix)",
            confidence=0.85,
            estimated_prefix_loss_tokens=8000,
            first_divergent_offset=19,
            first_divergent_layer="dynamic system suffix",
        )
    )
    store.record_churn(
        ChurnEvent(
            timestamp=datetime(2026, 8, 13, 12, 2, 0, tzinfo=UTC),
            session_hash="s1",
            previous_cache_fingerprint="fp-2",
            new_cache_fingerprint="fp-3",
            tools_changed=True,
            likely_cause="tool list mutation",
            confidence=0.80,
            estimated_prefix_loss_tokens=0,
            first_divergent_offset=3,
            first_divergent_layer="tool schemas",
        )
    )


def test_topology_empty_db_says_nothing_to_measure(tmp_path, capsys):
    TelemetryStore(tmp_path / "telemetry.db").close()
    assert main(["topology", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "no consecutive request pairs recorded yet — nothing to measure" in out
    assert "per-layer" not in out.lower()  # no fabricated table


def test_topology_reports_layers_stability_loss_and_ordering(tmp_path, capsys):
    store = TelemetryStore(tmp_path / "telemetry.db")
    _seed_topology_events(store)
    store.close()
    assert main(["topology", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "Cache topology (PRD §24/§138 measurement view) — last 500 requests" in out
    assert "sessions                  1" in out
    assert "consecutive pairs         2" in out
    assert "cache fingerprint churn   2  (prefix stability 0.0%)" in out
    # the dynamic system suffix is attributed as the destructive layer
    assert "dynamic system suffix" in out
    assert "changed 1/2 requests" in out
    assert "~8,000 tokens" in out
    assert "static system prefix" in out
    # tools moved (permutation) → tools layer churn + ordering stats
    assert "tool schemas" in out
    assert "Tool-schema ordering stability (per route):" in out
    assert "order permutations" in out
    # the attribution disclosure footnote
    assert "* layered sub-layer rows are attributed from classified churn" in out


# -- P11 survival in `cachepilot ttl` (PRD §99/§138) ---------------------------


def _seed_survival(store: TelemetryStore) -> None:
    from cachepilot_core.ttl import endpoint_hash

    ep_hash = endpoint_hash("https://openrouter.ai/api/v1")
    store.upsert_profile(
        TTLProfile(
            provider="openrouter",
            model="deepseek-v4-flash",
            api_mode="chat",
            endpoint_hash=ep_hash,
            route_hash="route-abc",
            lower_bound_s=183.0,
            upper_bound_s=302.0,
            estimated_ttl_s=224.65,
            confidence=0.7,
            sample_count=5,
        )
    )
    for age, outcome in (
        (100.0, Outcome.CONFIRMED_HIT),
        (200.0, Outcome.CONFIRMED_HIT),
        (250.0, Outcome.MISS_REBUILT),
        (300.0, Outcome.CONFIRMED_HIT),
    ):
        store.record_ttl_observation(
            timestamp=datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC),
            cache_fingerprint=f"fp-{int(age)}",
            route_hash="route-abc",
            idle_age_s=age,
            outcome=outcome,
            clean=True,
            provider="openrouter",
            model="deepseek-v4-flash",
            api_mode="chat",
            endpoint_hash=ep_hash,
        )


def test_ttl_shows_survival_at_estimated_ttl(tmp_path, capsys):
    store = TelemetryStore(tmp_path / "telemetry.db")
    _seed_survival(store)
    store.close()
    assert main(["ttl", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    # hits at 100/200 left the risk set; the death at 250 had 2 at risk →
    # S = 0.5; the estimated TTL 224.65s sits below it → P(survive) = 1.00
    assert "P(survive at TTL) = 1.00 (n=4 clean observations)" in out
    assert "median        250s" in out  # first step at or below 0.5


def test_ttl_survival_honest_without_clean_observations(tmp_path, capsys):
    store = TelemetryStore(tmp_path / "telemetry.db")
    from cachepilot_core.ttl import endpoint_hash

    store.upsert_profile(
        TTLProfile(
            provider="openrouter",
            model="deepseek-v4-flash",
            api_mode="chat",
            endpoint_hash=endpoint_hash("https://openrouter.ai/api/v1"),
            route_hash="route-abc",
            estimated_ttl_s=300.0,
            confidence=0.5,
            sample_count=1,
        )
    )
    store.close()
    assert main(["ttl", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "survival      no clean observations yet" in out
    assert "P(survive" not in out  # never fabricated


# -- E2E-004: CLI reads open the DB read-only ---------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "status",
        "leases",
        "costs",
        "ttl",
        "routes",
        "churn",
        "explain-miss",
        "topology",
    ],
)
def test_read_command_missing_db_creates_nothing_and_notices(
    tmp_path, capsys, monkeypatch, command
):
    """E2E-004: a missing --db path is never created; the notice names it."""
    monkeypatch.setenv("CACHEPILOT_RELAY_LISTEN", "127.0.0.1:1")
    missing = tmp_path / "no-such-dir" / "typo.db"
    assert main([command, "--db", str(missing)]) == 0
    out = capsys.readouterr()
    assert not missing.exists()
    assert not missing.parent.exists()
    assert f"no telemetry database at {missing}" in out.err
    assert "read-only" in out.err
    if command == "status":
        assert "no telemetry recorded yet" in out.out  # honest empty output continues


def test_status_missing_env_db_notices_resolved_path(tmp_path, capsys, monkeypatch):
    """E2E-004: a stale CACHEPILOT_TELEMETRY_DB is named, not silently created."""
    monkeypatch.setenv("CACHEPILOT_RELAY_LISTEN", "127.0.0.1:1")
    monkeypatch.setenv(ENV_TELEMETRY_DB, str(tmp_path / "stale" / "env.db"))
    assert main(["status"]) == 0
    out = capsys.readouterr()
    assert f"no telemetry database at {tmp_path / 'stale' / 'env.db'}" in out.err
    assert not (tmp_path / "stale").exists()
    assert "no telemetry recorded yet" in out.out
