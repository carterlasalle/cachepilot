"""SQLite WAL telemetry store — PRD §81-82; invariant 10 (no raw content)."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from cachepilot_core.leases import CacheLease, LeaseState
from cachepilot_core.storage import (
    ENV_TELEMETRY_DB,
    TelemetryStore,
    default_db_path,
    resolve_db_path,
)
from cachepilot_core.telemetry import (
    ChurnEvent,
    Outcome,
    TelemetryEvent,
)
from cachepilot_core.usage import TokenUsage


def _event(**overrides) -> TelemetryEvent:
    base = {
        "request_fingerprint": "req-fp-1",
        "cache_fingerprint": "cache-fp-1",
        "provider": "fake-provider",
        "model": "gpt-5.2",
        "route_hash": "route-1",
        "outcome": Outcome.CONFIRMED_HIT,
        "session_hash": "sess-hash-1",
        "usage": TokenUsage(
            prompt_tokens=4200,
            completion_tokens=42,
            cache_read_tokens=4000,
            cost=Decimal("0.0012"),
        ),
        "system_hash": "sys-hash",
        "tools_hash": "tools-hash",
        "history_hash": "hist-hash",
        "timestamp": datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return TelemetryEvent(**base)


def _churn(**overrides) -> ChurnEvent:
    base = {
        "timestamp": datetime(2026, 8, 13, 12, 1, 0, tzinfo=UTC),
        "session_hash": "sess-hash-1",
        "previous_cache_fingerprint": "cache-fp-1",
        "new_cache_fingerprint": "cache-fp-2",
        "provider": "fake-provider",
        "model": "gpt-5.2",
        "route_hash": "route-2",
        "history_changed": True,
    }
    base.update(overrides)
    return ChurnEvent(**base)


# -- defaults / env resolution ------------------------------------------------


def test_default_db_path_is_hermes_cachepilot_db():
    path = default_db_path()
    assert path.name == "cachepilot.db"
    assert ".hermes" in path.parts and "cachepilot" in path.parts


def test_resolve_explicit_path_wins(tmp_path):
    assert resolve_db_path(tmp_path / "custom.db") == tmp_path / "custom.db"


def test_resolve_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_TELEMETRY_DB, str(tmp_path / "env.db"))
    assert resolve_db_path() == tmp_path / "env.db"


def test_resolve_falls_back_to_default(monkeypatch):
    monkeypatch.delenv(ENV_TELEMETRY_DB, raising=False)
    assert resolve_db_path() == default_db_path()


# -- round-trips --------------------------------------------------------------


def test_request_event_round_trip(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    event = _event()
    row_id = store.record_request(event)
    rows = store.recent_events(limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == row_id
    assert row.request_fingerprint == "req-fp-1"
    assert row.cache_fingerprint == "cache-fp-1"
    assert row.provider == "fake-provider"
    assert row.model == "gpt-5.2"
    assert row.route_hash == "route-1"
    assert row.session_hash == "sess-hash-1"
    assert row.system_hash == "sys-hash"
    assert row.tools_hash == "tools-hash"
    assert row.history_hash == "hist-hash"
    assert row.input_tokens == 4200
    assert row.output_tokens == 42
    assert row.cache_read_tokens == 4000
    assert row.cache_write_tokens == 0
    assert row.cost_usd == Decimal("0.0012")
    assert row.request_kind == "normal"
    assert row.outcome is Outcome.CONFIRMED_HIT
    assert row.timestamp == event.timestamp
    store.close()


def test_recent_events_newest_first(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    store.record_request(_event(cache_fingerprint="a"))
    store.record_request(_event(cache_fingerprint="b"))
    store.record_request(_event(cache_fingerprint="c"))
    assert [row.cache_fingerprint for row in store.recent_events(limit=10)] == ["c", "b", "a"]
    assert [row.cache_fingerprint for row in store.recent_events(limit=2)] == ["c", "b"]
    store.close()


def test_last_event_for_session(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    store.record_request(_event(session_hash="s1", cache_fingerprint="a"))
    store.record_request(_event(session_hash="s2", cache_fingerprint="x"))
    store.record_request(_event(session_hash="s1", cache_fingerprint="b"))
    last = store.last_event_for_session("s1")
    assert last is not None
    assert last.cache_fingerprint == "b"
    assert store.last_event_for_session("nope") is None
    store.close()


def test_churn_event_round_trip(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    churn = _churn()
    churn_id = store.record_churn(churn)
    rows = store.churn_list(limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == churn_id
    assert row.previous_cache_fingerprint == "cache-fp-1"
    assert row.new_cache_fingerprint == "cache-fp-2"
    assert row.session_hash == "sess-hash-1"
    assert row.provider == "fake-provider"
    assert row.model == "gpt-5.2"
    assert row.route_hash == "route-2"
    assert row.history_changed is True
    assert row.system_changed is False
    store.close()


def test_route_changes_filters_churn(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    store.record_churn(_churn(route_changed=True))
    store.record_churn(_churn(route_changed=False))
    store.record_churn(_churn(route_changed=True, new_cache_fingerprint="cache-fp-3"))
    routes = store.route_changes(limit=10)
    assert [r.new_cache_fingerprint for r in routes] == ["cache-fp-3", "cache-fp-2"]
    store.close()


# -- WAL mode -----------------------------------------------------------------


def test_wal_journal_mode_active(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    assert store.wal_active is True
    conn = sqlite3.connect(store.db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()
    store.close()


def test_wal_fallback_does_not_raise_on_memory(tmp_path):
    # :memory: cannot use WAL — the store must fall back safely (PRD §81).
    store = TelemetryStore(":memory:")
    assert store.wal_active is False
    store.record_request(_event())
    assert store.aggregates().total == 1
    store.close()


# -- aggregates ---------------------------------------------------------------


def test_aggregates_breakdown(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    store.record_request(_event(outcome=Outcome.CONFIRMED_HIT, cache_fingerprint="h1"))
    store.record_request(_event(outcome=Outcome.CONFIRMED_HIT, cache_fingerprint="h2"))
    store.record_request(_event(outcome=Outcome.MISS_REBUILT, cache_fingerprint="m1"))
    store.record_request(_event(outcome=Outcome.SUCCESS_UNVERIFIED, cache_fingerprint="u1"))
    store.record_request(_event(outcome=Outcome.FAILED, cache_fingerprint="f1"))
    store.record_churn(_churn(route_changed=True))
    store.record_churn(_churn(route_changed=False, new_cache_fingerprint="cache-fp-9"))
    stats = store.aggregates()
    assert stats.total == 5
    assert stats.confirmed_hits == 2
    assert stats.misses == 1
    assert stats.unverified == 1
    assert stats.failed == 1
    assert stats.hit_rate == pytest.approx(2 / 3)
    assert stats.churn_events == 2
    assert stats.route_changes == 1
    store.close()


def test_aggregates_empty_db(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    stats = store.aggregates()
    assert stats.total == 0
    assert stats.hit_rate is None
    assert stats.churn_events == 0
    store.close()


# -- costs --------------------------------------------------------------------


def test_cost_totals_by_provider(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    store.record_request(
        _event(provider="openai", usage=TokenUsage(cost=Decimal("0.0100")))
    )
    store.record_request(
        _event(provider="openai", usage=TokenUsage(cost=Decimal("0.0200")))
    )
    store.record_request(
        _event(provider="anthropic", usage=TokenUsage(cost=Decimal("0.0300")))
    )
    # No provider-returned cost → excluded (unknown, never zero).
    store.record_request(_event(provider="openrouter", usage=TokenUsage()))
    totals = store.cost_totals()
    assert totals == {
        "openai": Decimal("0.0300"),
        "anthropic": Decimal("0.0300"),
    }
    store.close()


# -- invariant 10: no raw content ---------------------------------------------


def test_raw_prompt_content_never_stored(tmp_path):
    """Insert an event built from prompt-like content; the raw text must not
    appear anywhere in the database bytes (AGENTS.md invariant 10)."""
    secret_prompt = "EXTREMELY-SENSITIVE-PROMPT-CONTENT-xyz"
    store = TelemetryStore(tmp_path / "t.db")
    event = TelemetryEvent(
        request_fingerprint="fp",
        cache_fingerprint="fp",
        provider="fake-provider",
        model="gpt-5.2",
        outcome=Outcome.MISS_REBUILT,
        system_hash="sys",
        tools_hash="tools",
        history_hash="hist",
    )
    # The event model itself must not carry raw content either.
    dump = event.model_dump(mode="json")
    assert secret_prompt not in str(dump)
    store.record_request(event)
    store.close()

    db_bytes = b""
    for path in (store.db_path, tmp_path / "t.db-wal"):
        if path.exists():
            db_bytes += path.read_bytes()
    assert secret_prompt.encode() not in db_bytes


def test_raw_auth_header_never_stored(tmp_path):
    auth_token = "Bearer super-secret-token-value"
    store = TelemetryStore(tmp_path / "t.db")
    store.record_request(_event())
    store.close()
    db_bytes = b""
    for path in (store.db_path, tmp_path / "t.db-wal"):
        if path.exists():
            db_bytes += path.read_bytes()
    assert auth_token.encode() not in db_bytes


# -- leases (PRD §132 Phase 5) ------------------------------------------------


def _lease(**overrides: Any) -> CacheLease:
    lease = CacheLease(
        lease_id="lease-11111111",
        session_id="sess-raw-1",
        provider="fake-provider",
        model="gpt-5.2",
        api_mode="chat",
        base_url="https://fake-provider.invalid/v1",
        auth_scope_hash="auth-hash-1",
        route_fingerprint=None,
        request_fingerprint="req-fp-1",
        cache_fingerprint="cache-fp-1",
        system_fingerprint="sys-fp-1",
        tools_fingerprint="tools-fp-1",
        history_prefix_fingerprint="hist-fp-1",
        last_real_request_at=1000.0,
        last_cache_touch_at=1000.0,
        last_confirmed_hit_at=None,
        estimated_ttl_s=300.0,
        ttl_confidence=0.5,
        active_targets={"t1", "t2"},
        generation=3,
        warm_count=1,
        warm_cost_usd=0.5,
        state=LeaseState.ARMED,
    )
    for key, value in overrides.items():
        setattr(lease, key, value)
    return lease


def test_lease_round_trip(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    lease = _lease()
    store.record_lease(lease)
    rows = store.list_leases()
    assert len(rows) == 1
    row = rows[0]
    assert row.lease_id == "lease-11111111"
    # session id / base url stored as hashes, never raw (invariant 10)
    assert row.session_hash == hashlib.sha256(b"sess-raw-1").hexdigest()
    assert row.base_url_hash == hashlib.sha256(b"https://fake-provider.invalid/v1").hexdigest()
    assert row.provider == "fake-provider"
    assert row.model == "gpt-5.2"
    assert row.cache_fingerprint == "cache-fp-1"
    assert row.last_cache_touch_at == 1000.0
    assert row.estimated_ttl_s == 300.0
    assert set(row.active_targets) == {"t1", "t2"}
    assert row.generation == 3
    assert row.warm_count == 1
    assert row.warm_cost_usd == Decimal("0.5")
    assert row.state == "armed"
    assert row.updated_at is not None
    store.close()


def test_lease_update_replaces_snapshot_in_place(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    lease = _lease()
    store.record_lease(lease)
    lease.generation = 4
    lease.state = LeaseState.WARM_SCHEDULED
    lease.active_targets = {"t1"}
    store.update_lease(lease)
    rows = store.list_leases()
    assert len(rows) == 1  # snapshot table: one row per lease_id
    assert rows[0].generation == 4
    assert rows[0].state == "warm_scheduled"
    assert set(rows[0].active_targets) == {"t1"}
    store.close()


def test_lease_listing_newest_first(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record_lease(_lease(lease_id="lease-aaaaaaaa"))
    store.record_lease(_lease(lease_id="lease-bbbbbbbb"))
    store.record_lease(_lease(lease_id="lease-cccccccc"))
    rows = store.list_leases(limit=2)
    assert [row.lease_id for row in rows] == ["lease-cccccccc", "lease-bbbbbbbb"]
    store.close()


def test_lease_raw_session_and_url_never_in_db_bytes(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    store.record_lease(_lease(session_id="sess-SECRET-raw", base_url="https://secret-host.invalid/v1"))
    store.close()
    db_bytes = b""
    for path in (store.db_path, tmp_path / "telemetry.db-wal"):
        if path.exists():
            db_bytes += path.read_bytes()
    assert b"sess-SECRET-raw" not in db_bytes
    assert b"https://secret-host.invalid/v1" not in db_bytes


# -- P11 (PRD §138): tools_set_hash + ttl_observations identity columns --------


def test_request_event_round_trips_tools_set_hash(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    event = _event(tools_set_hash="set-hash-1")
    store.record_request(event)
    row = store.recent_events(limit=10)[0]
    assert row.tools_set_hash == "set-hash-1"
    # absent on pre-P11-style rows → None, never fabricated
    store.record_request(_event(cache_fingerprint="fp-2", tools_set_hash=None))
    assert store.recent_events(limit=10)[0].tools_set_hash is None
    store.close()


def test_ttl_observation_round_trips_route_identity_columns(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    store.record_ttl_observation(
        timestamp=datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC),
        cache_fingerprint="fp-1",
        route_hash="route-1",
        idle_age_s=183.0,
        outcome=Outcome.CONFIRMED_HIT,
        clean=True,
        provider="openrouter",
        model="deepseek-v4-flash",
        api_mode="chat",
        endpoint_hash="ep-hash-1",
    )
    row = store.last_ttl_observation("fp-1")
    assert row is not None
    assert row.provider == "openrouter"
    assert row.model == "deepseek-v4-flash"
    assert row.api_mode == "chat"
    assert row.endpoint_hash == "ep-hash-1"
    store.close()


def test_clean_observations_for_profile_filters_clean_and_identity(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    base: dict[str, Any] = {
        "timestamp": datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC),
        "provider": "openrouter",
        "model": "deepseek-v4-flash",
        "api_mode": "chat",
        "endpoint_hash": "ep-hash-1",
    }
    store.record_ttl_observation(
        cache_fingerprint="fp-clean", route_hash="route-1", idle_age_s=100.0,
        outcome=Outcome.CONFIRMED_HIT, clean=True, **base,
    )
    store.record_ttl_observation(
        cache_fingerprint="fp-unclean", route_hash="route-1", idle_age_s=100.0,
        outcome=Outcome.MISS_REBUILT, clean=False, **base,
    )
    store.record_ttl_observation(
        cache_fingerprint="fp-other-route", route_hash="route-2", idle_age_s=100.0,
        outcome=Outcome.CONFIRMED_HIT, clean=True, **base,
    )
    store.record_ttl_observation(
        cache_fingerprint="fp-other-provider", route_hash="route-1", idle_age_s=100.0,
        outcome=Outcome.CONFIRMED_HIT, clean=True, **dict(base, provider="anthropic"),
    )
    store.record_ttl_observation(
        # pre-P11-shaped row: no identity columns → NULLs, never matched
        timestamp=datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC),
        cache_fingerprint="fp-legacy", route_hash="route-1", idle_age_s=100.0,
        outcome=Outcome.CONFIRMED_HIT, clean=True,
    )
    rows = store.clean_observations_for_profile(
        provider="openrouter",
        model="deepseek-v4-flash",
        api_mode="chat",
        endpoint_hash="ep-hash-1",
        route_hash="route-1",
    )
    assert [row.cache_fingerprint for row in rows] == ["fp-clean"]
    # NULL route keys match only a NULL route_hash
    no_route = store.clean_observations_for_profile(
        provider="openrouter",
        model="deepseek-v4-flash",
        api_mode="chat",
        endpoint_hash="ep-hash-1",
        route_hash=None,
    )
    assert no_route == []
    store.close()


def test_pre_p11_request_events_gain_tools_set_hash_column(tmp_path):
    """A pre-P11 request_events table (no tools_set_hash) migrates on connect
    and keeps working — the ALTER pattern from P10, applied to request_events."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE request_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_hash TEXT,
            timestamp TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            route_hash TEXT,
            request_fingerprint TEXT NOT NULL,
            cache_fingerprint TEXT NOT NULL,
            system_hash TEXT,
            tools_hash TEXT,
            history_hash TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd TEXT,
            request_kind TEXT NOT NULL DEFAULT 'normal',
            outcome TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    store = TelemetryStore(db)
    try:
        store.record_request(_event(cache_fingerprint="fp-1", tools_set_hash="set-x"))
        assert store.recent_events(limit=10)[0].tools_set_hash == "set-x"
        # re-connecting is idempotent
        store.close()
        store = TelemetryStore(db)
        assert store.recent_events(limit=10)[0].tools_set_hash == "set-x"
    finally:
        store.close()
    conn = sqlite3.connect(db)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(request_events)")}
    finally:
        conn.close()
    assert "tools_set_hash" in columns


def test_pre_p11_ttl_observations_gain_identity_columns(tmp_path):
    """A pre-P11 ttl_observations table (no identity columns) migrates on
    connect; old rows keep NULL identity and are excluded from profile queries."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE ttl_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            cache_fingerprint TEXT NOT NULL,
            route_hash TEXT,
            idle_age_s REAL,
            outcome TEXT NOT NULL,
            clean INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()

    store = TelemetryStore(db)
    try:
        store.record_ttl_observation(
            timestamp=datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC),
            cache_fingerprint="fp-1",
            route_hash="route-1",
            idle_age_s=183.0,
            outcome=Outcome.CONFIRMED_HIT,
            clean=True,
            provider="openrouter",
            model="deepseek-v4-flash",
            api_mode="chat",
            endpoint_hash="ep-hash-1",
        )
        rows = store.clean_observations_for_profile(
            provider="openrouter",
            model="deepseek-v4-flash",
            api_mode="chat",
            endpoint_hash="ep-hash-1",
            route_hash="route-1",
        )
        assert [row.cache_fingerprint for row in rows] == ["fp-1"]
        assert rows[0].provider == "openrouter"
    finally:
        store.close()
    conn = sqlite3.connect(db)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ttl_observations)")}
    finally:
        conn.close()
    assert {"provider", "model", "api_mode", "endpoint_hash"} <= columns
