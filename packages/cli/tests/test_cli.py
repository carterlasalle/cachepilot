"""cachepilot CLI — status/leases/costs against a seeded tmp telemetry DB (PRD §77-79)."""

from __future__ import annotations

import socket
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from cachepilot_cli.main import main
from cachepilot_core.leases import CacheLease, LeaseState
from cachepilot_core.route_intel import RouteChangeEvent, RouteMissVerdict
from cachepilot_core.storage import ENV_TELEMETRY_DB, TelemetryStore
from cachepilot_core.telemetry import ChurnEvent, Outcome, TelemetryEvent
from cachepilot_core.ttl import TTLProfile, endpoint_hash
from cachepilot_core.usage import TokenUsage


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


def test_status_relay_healthy_when_listening(tmp_path, capsys, monkeypatch):
    _seed_db(tmp_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        monkeypatch.setenv("CACHEPILOT_RELAY_LISTEN", f"127.0.0.1:{port}")
        assert main(["status", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "Relay: healthy" in out


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
