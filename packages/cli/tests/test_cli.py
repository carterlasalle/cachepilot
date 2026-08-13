"""cachepilot CLI — status/leases/costs against a seeded tmp telemetry DB (PRD §77-79)."""

from __future__ import annotations

import socket
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from cachepilot_cli.main import main
from cachepilot_core.storage import ENV_TELEMETRY_DB, TelemetryStore
from cachepilot_core.telemetry import ChurnEvent, Outcome, TelemetryEvent
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


def test_leases_is_honest_phase5_placeholder(tmp_path, capsys):
    _seed_db(tmp_path)
    assert main(["leases", "--db", str(tmp_path / "telemetry.db")]) == 0
    out = capsys.readouterr().out
    assert "no active leases — lease manager ships in Phase 5" in out
    assert "LEASE" not in out  # no fabricated lease table


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
