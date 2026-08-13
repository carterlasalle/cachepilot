"""SQLite WAL telemetry store — PRD §81-82, AGENTS.md invariant 10.

Persistent storage contains only hashes, timestamps, usage, prices, route
identities and cache outcomes (PRD §30, §83): never prompts, conversation
history, API keys, authorization headers or tool arguments. Raw content is
hashed by the caller before it ever reaches this store.

Design:
- default path ``~/.hermes/cachepilot/cachepilot.db`` (PRD §81), overridable
  via ``CACHEPILOT_TELEMETRY_DB``;
- WAL journal mode with safe fallback when the filesystem cannot support it
  (PRD §81 — ``PRAGMA journal_mode=WAL`` may return a different mode);
- a single connection guarded by a threading lock: relay writes and CLI
  reads are fast local ops, so a small sync wrapper is enough (no aiosqlite);
- writes are autocommit (``isolation_level=None``) so concurrent readers
  (the CLI) always see committed rows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from cachepilot_core.leases import CacheLease
from cachepilot_core.route_intel import (
    RouteChangeEvent,
    RouteIntelStats,
    RouteMissVerdict,
)
from cachepilot_core.telemetry import (
    CacheHealthStats,
    ChurnEvent,
    Outcome,
    TelemetryEvent,
)
from cachepilot_core.ttl import StoredTTLObservation, TTLProfile

logger = logging.getLogger("cachepilot_core.storage")

#: Environment override for the telemetry database path (PRD §81).
ENV_TELEMETRY_DB = "CACHEPILOT_TELEMETRY_DB"

#: Default telemetry database location (PRD §81).
DEFAULT_TELEMETRY_DB = "~/.hermes/cachepilot/cachepilot.db"

_REQUEST_EVENT_COLUMNS = (
    "id",
    "session_hash",
    "timestamp",
    "provider",
    "model",
    "route_hash",
    "request_fingerprint",
    "cache_fingerprint",
    "system_hash",
    "tools_hash",
    "history_hash",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
    "request_kind",
    "outcome",
)

#: ``churn_events`` columns in row order (PRD §82; P10 adds the classifier
#: enrichment columns — PRD §25/§75/§137).
_CHURN_EVENT_COLUMNS = (
    "id",
    "timestamp",
    "session_hash",
    "previous_cache_fingerprint",
    "new_cache_fingerprint",
    "provider",
    "model",
    "route_hash",
    "system_changed",
    "tools_changed",
    "history_changed",
    "route_changed",
    "cache_key_changed",
    "model_changed",
    "likely_cause",
    "confidence",
    "estimated_prefix_loss_tokens",
    "first_divergent_offset",
    "first_divergent_layer",
)

#: P10 columns added to pre-existing ``churn_events`` tables (fresh databases
#: get them from ``_SCHEMA``; older ones need an ALTER — see
#: :func:`_ensure_churn_columns`).
_CHURN_ENRICHMENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("likely_cause", "TEXT"),
    ("confidence", "REAL"),
    ("estimated_prefix_loss_tokens", "INTEGER"),
    ("first_divergent_offset", "INTEGER"),
    ("first_divergent_layer", "TEXT"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS request_events (
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
);
CREATE INDEX IF NOT EXISTS idx_request_events_session ON request_events(session_hash);
CREATE INDEX IF NOT EXISTS idx_request_events_cache_fp ON request_events(cache_fingerprint);
CREATE INDEX IF NOT EXISTS idx_request_events_timestamp ON request_events(timestamp);

CREATE TABLE IF NOT EXISTS churn_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_hash TEXT,
    previous_cache_fingerprint TEXT NOT NULL,
    new_cache_fingerprint TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    route_hash TEXT,
    system_changed INTEGER NOT NULL DEFAULT 0,
    tools_changed INTEGER NOT NULL DEFAULT 0,
    history_changed INTEGER NOT NULL DEFAULT 0,
    route_changed INTEGER NOT NULL DEFAULT 0,
    cache_key_changed INTEGER NOT NULL DEFAULT 0,
    model_changed INTEGER NOT NULL DEFAULT 0,
    likely_cause TEXT,
    confidence REAL,
    estimated_prefix_loss_tokens INTEGER,
    first_divergent_offset INTEGER,
    first_divergent_layer TEXT
);
CREATE INDEX IF NOT EXISTS idx_churn_events_timestamp ON churn_events(timestamp);

CREATE TABLE IF NOT EXISTS leases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lease_id TEXT NOT NULL UNIQUE,
    session_hash TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    api_mode TEXT NOT NULL,
    base_url_hash TEXT NOT NULL,
    auth_scope_hash TEXT NOT NULL,
    route_fingerprint TEXT,
    request_fingerprint TEXT NOT NULL,
    cache_fingerprint TEXT NOT NULL,
    system_fingerprint TEXT NOT NULL,
    tools_fingerprint TEXT NOT NULL,
    history_prefix_fingerprint TEXT NOT NULL,
    last_real_request_at REAL NOT NULL,
    last_cache_touch_at REAL,
    last_confirmed_hit_at REAL,
    estimated_ttl_s REAL NOT NULL,
    ttl_confidence REAL NOT NULL,
    active_targets_json TEXT NOT NULL DEFAULT '[]',
    generation INTEGER NOT NULL DEFAULT 0,
    warm_count INTEGER NOT NULL DEFAULT 0,
    warm_cost_usd TEXT NOT NULL DEFAULT '0',
    estimated_cold_resume_cost_usd TEXT,
    estimated_cached_resume_cost_usd TEXT,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leases_session_cache ON leases(session_hash, cache_fingerprint);

CREATE TABLE IF NOT EXISTS provider_profiles (
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    api_mode TEXT NOT NULL,
    endpoint_hash TEXT NOT NULL,
    route_hash TEXT,
    ttl_lower_s REAL,
    ttl_upper_s REAL,
    ttl_estimate_s REAL,
    ttl_confidence REAL NOT NULL,
    latency_p50_ms REAL,
    latency_p95_ms REAL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    profile_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_provider_profiles_route
    ON provider_profiles(provider, model, api_mode, endpoint_hash, route_hash);

CREATE TABLE IF NOT EXISTS ttl_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    cache_fingerprint TEXT NOT NULL,
    route_hash TEXT,
    idle_age_s REAL,
    outcome TEXT NOT NULL,
    clean INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ttl_observations_cache_fp
    ON ttl_observations(cache_fingerprint, timestamp);

CREATE TABLE IF NOT EXISTS route_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_hash TEXT,
    cache_fingerprint TEXT NOT NULL,
    request_fingerprint TEXT,
    previous_route_hash TEXT,
    new_route_hash TEXT,
    gateway TEXT,
    upstream_provider TEXT,
    endpoint TEXT,
    region TEXT,
    deployment TEXT,
    verdict TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_route_events_timestamp ON route_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_route_events_session ON route_events(session_hash);
"""


class StoredRequestEvent(BaseModel):
    """A ``request_events`` row read back from storage."""

    model_config = ConfigDict(extra="forbid")

    id: int
    session_hash: str | None = None
    timestamp: datetime
    provider: str
    model: str
    route_hash: str | None = None
    request_fingerprint: str
    cache_fingerprint: str
    system_hash: str | None = None
    tools_hash: str | None = None
    history_hash: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Decimal | None = None
    request_kind: str = "normal"
    outcome: Outcome


#: ``leases`` columns in row order (id first, then the stored lease fields).
_LEASE_COLUMNS = (
    "id",
    "lease_id",
    "session_hash",
    "provider",
    "model",
    "api_mode",
    "base_url_hash",
    "auth_scope_hash",
    "route_fingerprint",
    "request_fingerprint",
    "cache_fingerprint",
    "system_fingerprint",
    "tools_fingerprint",
    "history_prefix_fingerprint",
    "last_real_request_at",
    "last_cache_touch_at",
    "last_confirmed_hit_at",
    "estimated_ttl_s",
    "ttl_confidence",
    "active_targets_json",
    "generation",
    "warm_count",
    "warm_cost_usd",
    "estimated_cold_resume_cost_usd",
    "estimated_cached_resume_cost_usd",
    "state",
    "updated_at",
)


class StoredLease(BaseModel):
    """A ``leases`` row read back from storage.

    Mirrors :class:`~cachepilot_core.leases.CacheLease` with the invariant-10
    transforms applied: ``session_id`` → ``session_hash``, ``base_url`` →
    ``base_url_hash``, and the target-id set serialized as a JSON tuple.
    Prices read back as ``Decimal`` (the TEXT-storage convention used by
    ``request_events.cost_usd``).
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    lease_id: str
    session_hash: str | None = None
    provider: str
    model: str
    api_mode: str
    base_url_hash: str
    auth_scope_hash: str
    route_fingerprint: str | None = None
    request_fingerprint: str
    cache_fingerprint: str
    system_fingerprint: str
    tools_fingerprint: str
    history_prefix_fingerprint: str
    last_real_request_at: float
    last_cache_touch_at: float | None = None
    last_confirmed_hit_at: float | None = None
    estimated_ttl_s: float
    ttl_confidence: float
    active_targets: tuple[str, ...] = ()
    generation: int = 0
    warm_count: int = 0
    warm_cost_usd: Decimal = Decimal(0)
    estimated_cold_resume_cost_usd: Decimal | None = None
    estimated_cached_resume_cost_usd: Decimal | None = None
    state: str
    updated_at: datetime


def default_db_path() -> Path:
    """Default telemetry database path: ``~/.hermes/cachepilot/cachepilot.db``."""
    return Path(os.path.expanduser(DEFAULT_TELEMETRY_DB))


def resolve_db_path(
    db_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve an explicit path, else ``CACHEPILOT_TELEMETRY_DB``, else the default."""
    if db_path is not None:
        return Path(os.path.expanduser(str(db_path)))
    env = os.environ if env is None else env
    return Path(os.path.expanduser(env.get(ENV_TELEMETRY_DB) or DEFAULT_TELEMETRY_DB))


def _try_enable_wal(conn: sqlite3.Connection) -> bool:
    """Enable WAL journal mode; report whether it actually engaged.

    SQLite returns the *effective* journal mode from the pragma, so a
    filesystem that cannot support WAL falls back silently to whatever mode
    the engine picks (PRD §81 "safe fallback").
    """
    try:
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        return bool(row and row[0].lower() == "wal")
    except sqlite3.Error:
        return False


def _decimal_to_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _text_to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None


def _row_to_request_event(row: tuple[Any, ...]) -> StoredRequestEvent:
    return StoredRequestEvent(
        id=row[0],
        session_hash=row[1],
        timestamp=datetime.fromisoformat(row[2]),
        provider=row[3],
        model=row[4],
        route_hash=row[5],
        request_fingerprint=row[6],
        cache_fingerprint=row[7],
        system_hash=row[8],
        tools_hash=row[9],
        history_hash=row[10],
        input_tokens=row[11],
        output_tokens=row[12],
        cache_read_tokens=row[13],
        cache_write_tokens=row[14],
        cost_usd=_text_to_decimal(row[15]),
        request_kind=row[16],
        outcome=Outcome(row[17]),
    )


def _row_to_churn_event(row: tuple[Any, ...]) -> ChurnEvent:
    return ChurnEvent(
        id=row[0],
        timestamp=datetime.fromisoformat(row[1]),
        session_hash=row[2],
        previous_cache_fingerprint=row[3],
        new_cache_fingerprint=row[4],
        provider=row[5],
        model=row[6],
        route_hash=row[7],
        system_changed=bool(row[8]),
        tools_changed=bool(row[9]),
        history_changed=bool(row[10]),
        route_changed=bool(row[11]),
        cache_key_changed=bool(row[12]),
        model_changed=bool(row[13]),
        likely_cause=row[14],
        confidence=row[15],
        estimated_prefix_loss_tokens=row[16],
        first_divergent_offset=row[17],
        first_divergent_layer=row[18],
    )


def _ensure_churn_columns(conn: sqlite3.Connection) -> None:
    """Add the P10 classifier columns to pre-existing ``churn_events`` tables.

    ``CREATE TABLE IF NOT EXISTS`` only creates the new shape on fresh
    databases; an existing table from an earlier phase keeps its old columns,
    so each P10 column is ALTERed in (idempotent — "duplicate column name" is
    expected and ignored).
    """
    for name, ddl in _CHURN_ENRICHMENT_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE churn_events ADD COLUMN {name} {ddl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


#: ``provider_profiles`` columns in row order (P08, PRD §82).
_PROFILE_COLUMNS = (
    "provider",
    "model",
    "api_mode",
    "endpoint_hash",
    "route_hash",
    "ttl_lower_s",
    "ttl_upper_s",
    "ttl_estimate_s",
    "ttl_confidence",
    "latency_p50_ms",
    "latency_p95_ms",
    "sample_count",
    "updated_at",
    "profile_key",
)

#: ``ttl_observations`` columns in row order (P08, PRD §82).
_TTL_OBSERVATION_COLUMNS = (
    "id",
    "timestamp",
    "cache_fingerprint",
    "route_hash",
    "idle_age_s",
    "outcome",
    "clean",
)

#: ``route_events`` columns in row order (P09, PRD §72.1/§75).
_ROUTE_EVENT_COLUMNS = (
    "id",
    "timestamp",
    "session_hash",
    "cache_fingerprint",
    "request_fingerprint",
    "previous_route_hash",
    "new_route_hash",
    "gateway",
    "upstream_provider",
    "endpoint",
    "region",
    "deployment",
    "verdict",
)


def _row_to_profile(row: tuple[Any, ...]) -> TTLProfile:
    return TTLProfile(
        provider=row[0],
        model=row[1],
        api_mode=row[2],
        endpoint_hash=row[3],
        route_hash=row[4],
        lower_bound_s=row[5],
        upper_bound_s=row[6],
        estimated_ttl_s=row[7],
        confidence=row[8],
        latency_p50_ms=row[9],
        latency_p95_ms=row[10],
        sample_count=row[11],
        updated_at=datetime.fromisoformat(row[12]),
    )


def _row_to_ttl_observation(row: tuple[Any, ...]) -> StoredTTLObservation:
    return StoredTTLObservation(
        id=row[0],
        timestamp=datetime.fromisoformat(row[1]),
        cache_fingerprint=row[2],
        route_hash=row[3],
        idle_age_s=row[4],
        outcome=Outcome(row[5]),
        clean=bool(row[6]),
    )


def _row_to_route_event(row: tuple[Any, ...]) -> RouteChangeEvent:
    return RouteChangeEvent(
        id=row[0],
        timestamp=datetime.fromisoformat(row[1]),
        session_hash=row[2],
        cache_fingerprint=row[3],
        request_fingerprint=row[4],
        previous_route_hash=row[5],
        new_route_hash=row[6],
        gateway=row[7],
        upstream_provider=row[8],
        endpoint=row[9],
        region=row[10],
        deployment=row[11],
        verdict=RouteMissVerdict(row[12]),
    )


def _lease_row_values(lease: CacheLease) -> tuple[Any, ...]:
    """Serialize a live lease for the ``leases`` table (invariant 10).

    Only hashes/timestamps/usage/prices/state are stored: ``session_id`` is
    hashed to ``session_hash``, ``base_url`` to ``base_url_hash``, and the
    active target ids are stored as a JSON array (they are internal ids,
    never secrets — PRD §83).
    """
    session_hash = (
        hashlib.sha256(lease.session_id.encode("utf-8")).hexdigest() if lease.session_id else None
    )
    base_url_hash = hashlib.sha256(lease.base_url.encode("utf-8")).hexdigest()
    return (
        lease.lease_id,
        session_hash,
        lease.provider,
        lease.model,
        lease.api_mode,
        base_url_hash,
        lease.auth_scope_hash,
        lease.route_fingerprint,
        lease.request_fingerprint,
        lease.cache_fingerprint,
        lease.system_fingerprint,
        lease.tools_fingerprint,
        lease.history_prefix_fingerprint,
        lease.last_real_request_at,
        lease.last_cache_touch_at,
        lease.last_confirmed_hit_at,
        lease.estimated_ttl_s,
        lease.ttl_confidence,
        json.dumps(sorted(lease.active_targets)),
        lease.generation,
        lease.warm_count,
        str(lease.warm_cost_usd),
        None if lease.estimated_cold_resume_cost_usd is None else str(lease.estimated_cold_resume_cost_usd),
        None if lease.estimated_cached_resume_cost_usd is None else str(lease.estimated_cached_resume_cost_usd),
        lease.state.value,
        datetime.now(UTC).isoformat(timespec="seconds"),
    )


def _row_to_lease(row: tuple[Any, ...]) -> StoredLease:
    return StoredLease(
        id=row[0],
        lease_id=row[1],
        session_hash=row[2],
        provider=row[3],
        model=row[4],
        api_mode=row[5],
        base_url_hash=row[6],
        auth_scope_hash=row[7],
        route_fingerprint=row[8],
        request_fingerprint=row[9],
        cache_fingerprint=row[10],
        system_fingerprint=row[11],
        tools_fingerprint=row[12],
        history_prefix_fingerprint=row[13],
        last_real_request_at=row[14],
        last_cache_touch_at=row[15],
        last_confirmed_hit_at=row[16],
        estimated_ttl_s=row[17],
        ttl_confidence=row[18],
        active_targets=tuple(json.loads(row[19])),
        generation=row[20],
        warm_count=row[21],
        # The column is NOT NULL DEFAULT '0', but keep the type honest: a
        # NULL read can never happen and still coerces to zero.
        warm_cost_usd=_text_to_decimal(row[22]) or Decimal(0),
        estimated_cold_resume_cost_usd=_text_to_decimal(row[23]),
        estimated_cached_resume_cost_usd=_text_to_decimal(row[24]),
        state=row[25],
        updated_at=datetime.fromisoformat(row[26]),
    )


class TelemetryStore:
    """SQLite WAL telemetry store (PRD §81-82), thread-safe and fail-safe.

    Fail-open behaviour lives in the callers (relay observer wraps every
    call); the store itself raises on hard errors so the observer can log
    and continue. All writes are autocommit, so the CLI can read the same
    database from another process while the relay is running.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        *,
        connect: bool = True,
    ) -> None:
        self._path = resolve_db_path(db_path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self.wal_active = False
        if connect:
            self.connect()

    @property
    def db_path(self) -> Path:
        """The resolved database path."""
        return self._path

    def connect(self) -> None:
        """Open the database, enable WAL, and ensure the schema exists."""
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), check_same_thread=False, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            self.wal_active = _try_enable_wal(conn)
            conn.executescript(_SCHEMA)
            # P10: existing churn_events tables predate the classifier columns.
            _ensure_churn_columns(conn)
        except Exception:
            conn.close()
            raise
        self._conn = conn

    def close(self) -> None:
        """Close the underlying connection (idempotent)."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # -- writes -------------------------------------------------------------

    def record_request(self, event: TelemetryEvent) -> int:
        """Insert one request event; returns its row id."""
        usage = event.usage
        with self._lock:
            cur = self._require_conn().execute(
                """
                INSERT INTO request_events
                    (session_hash, timestamp, provider, model, route_hash,
                     request_fingerprint, cache_fingerprint, system_hash,
                     tools_hash, history_hash, input_tokens, output_tokens,
                     cache_read_tokens, cache_write_tokens, cost_usd,
                     request_kind, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.session_hash,
                    event.timestamp.astimezone(UTC).isoformat(timespec="seconds"),
                    event.provider,
                    event.model,
                    event.route_hash,
                    event.request_fingerprint,
                    event.cache_fingerprint,
                    event.system_hash,
                    event.tools_hash,
                    event.history_hash,
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    usage.cache_read_tokens,
                    usage.cache_write_tokens,
                    _decimal_to_text(usage.cost),
                    event.request_kind,
                    event.outcome.value,
                ),
            )
            return int(cur.lastrowid or 0)

    def record_churn(self, churn: ChurnEvent) -> int:
        """Insert one churn event; returns its row id."""
        with self._lock:
            cur = self._require_conn().execute(
                """
                INSERT INTO churn_events
                    (timestamp, session_hash, previous_cache_fingerprint,
                     new_cache_fingerprint, provider, model, route_hash,
                     system_changed, tools_changed, history_changed,
                     route_changed, cache_key_changed, model_changed,
                     likely_cause, confidence, estimated_prefix_loss_tokens,
                     first_divergent_offset, first_divergent_layer)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    churn.timestamp.astimezone(UTC).isoformat(timespec="seconds"),
                    churn.session_hash,
                    churn.previous_cache_fingerprint,
                    churn.new_cache_fingerprint,
                    churn.provider,
                    churn.model,
                    churn.route_hash,
                    int(churn.system_changed),
                    int(churn.tools_changed),
                    int(churn.history_changed),
                    int(churn.route_changed),
                    int(churn.cache_key_changed),
                    int(churn.model_changed),
                    churn.likely_cause,
                    churn.confidence,
                    churn.estimated_prefix_loss_tokens,
                    churn.first_divergent_offset,
                    churn.first_divergent_layer,
                ),
            )
            return int(cur.lastrowid or 0)

    # -- leases (PRD §132 Phase 5) ------------------------------------------

    def record_lease(self, lease: CacheLease) -> int:
        """Insert one lease snapshot; returns its row id.

        Only hashes/timestamps/usage/prices/state are written (invariant
        10): ``session_id`` → ``session_hash``, ``base_url`` →
        ``base_url_hash``, target ids as a JSON array.
        """
        with self._lock:
            return self._insert_lease(self._require_conn(), lease)

    def update_lease(self, lease: CacheLease) -> None:
        """Replace the stored snapshot for ``lease.lease_id`` (insert on first write).

        The ``leases`` table is a snapshot table — one row per live lease,
        keyed by ``lease_id`` — so an update is a delete + insert under the
        same lock.
        """
        with self._lock:
            conn = self._require_conn()
            conn.execute("DELETE FROM leases WHERE lease_id = ?", (lease.lease_id,))
            self._insert_lease(conn, lease)

    def list_leases(self, limit: int = 50) -> list[StoredLease]:
        """Most recent lease snapshots, newest first (PRD §78 ``cachepilot leases``)."""
        with self._lock:
            rows = self._require_conn().execute(
                f"SELECT {', '.join(_LEASE_COLUMNS)} FROM leases ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_lease(row) for row in rows]

    def _insert_lease(self, conn: sqlite3.Connection, lease: CacheLease) -> int:
        columns = ", ".join(_LEASE_COLUMNS[1:])
        placeholders = ", ".join("?" for _ in _LEASE_COLUMNS[1:])
        cur = conn.execute(
            f"INSERT INTO leases ({columns}) VALUES ({placeholders})",
            _lease_row_values(lease),
        )
        return int(cur.lastrowid or 0)

    # -- reads --------------------------------------------------------------

    def recent_events(self, limit: int = 50) -> list[StoredRequestEvent]:
        """Most recent request events, newest first."""
        with self._lock:
            rows = self._require_conn().execute(
                f"SELECT {', '.join(_REQUEST_EVENT_COLUMNS)} FROM request_events "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_request_event(row) for row in rows]

    def last_event_for_session(self, session_hash: str) -> StoredRequestEvent | None:
        """Most recent request event for one session (for churn deltas)."""
        with self._lock:
            row = self._require_conn().execute(
                f"SELECT {', '.join(_REQUEST_EVENT_COLUMNS)} FROM request_events "
                "WHERE session_hash = ? ORDER BY id DESC LIMIT 1",
                (session_hash,),
            ).fetchone()
        return _row_to_request_event(row) if row is not None else None

    def aggregates(self) -> CacheHealthStats:
        """Cache-health aggregates over every stored event (PRD §77, §131)."""
        with self._lock:
            conn = self._require_conn()
            total = conn.execute("SELECT COUNT(*) FROM request_events").fetchone()[0]
            counts = dict(
                conn.execute("SELECT outcome, COUNT(*) FROM request_events GROUP BY outcome").fetchall()
            )
            churn_events = conn.execute("SELECT COUNT(*) FROM churn_events").fetchone()[0]
            route_changes = conn.execute(
                "SELECT COUNT(*) FROM churn_events WHERE route_changed = 1"
            ).fetchone()[0]
        return CacheHealthStats(
            total=total,
            confirmed_hits=counts.get(Outcome.CONFIRMED_HIT.value, 0),
            misses=counts.get(Outcome.MISS_REBUILT.value, 0),
            unverified=counts.get(Outcome.SUCCESS_UNVERIFIED.value, 0),
            failed=counts.get(Outcome.FAILED.value, 0),
            churn_events=churn_events,
            route_changes=route_changes,
        )

    def churn_list(self, limit: int = 50, session_hash: str | None = None) -> list[ChurnEvent]:
        """Most recent churn events, newest first (optionally one session)."""
        columns = ", ".join(_CHURN_EVENT_COLUMNS)
        with self._lock:
            if session_hash is not None:
                rows = self._require_conn().execute(
                    f"SELECT {columns} FROM churn_events "
                    "WHERE session_hash = ? ORDER BY id DESC LIMIT ?",
                    (session_hash, limit),
                ).fetchall()
            else:
                rows = self._require_conn().execute(
                    f"SELECT {columns} FROM churn_events ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [_row_to_churn_event(row) for row in rows]

    def route_changes(self, limit: int = 50) -> list[ChurnEvent]:
        """Most recent churn events whose route identity changed."""
        columns = ", ".join(_CHURN_EVENT_COLUMNS)
        with self._lock:
            rows = self._require_conn().execute(
                f"SELECT {columns} FROM churn_events WHERE route_changed = 1 "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_churn_event(row) for row in rows]

    def cost_totals(self) -> dict[str, Decimal]:
        """Recorded provider-returned cost summed per provider (PRD §79).

        Only rows carrying a provider-returned ``cost_usd`` are included —
        an absent cost is unknown, never zero (PRD §65 priority, invariant 4).
        """
        totals: dict[str, Decimal] = {}
        with self._lock:
            rows = self._require_conn().execute(
                "SELECT provider, cost_usd FROM request_events WHERE cost_usd IS NOT NULL"
            ).fetchall()
        for provider, cost_text in rows:
            cost = _text_to_decimal(cost_text)
            if cost is not None:
                totals[provider] = totals.get(provider, Decimal(0)) + cost
        return totals

    # -- TTL learning (P08: PRD §55-59, §82, §135) --------------------------

    def upsert_profile(self, profile: TTLProfile) -> None:
        """Insert or update one route profile (PRD §82 ``provider_profiles``).

        Keyed by the derived ``profile_key`` (provider/model/api_mode/
        endpoint_hash/route_hash — PRD §82); the identity columns are also
        stored for inspection. TTL/latency values are REAL — the Decimal→
        TEXT convention is reserved for money (invariant 4), never for TTLs.
        """
        with self._lock:
            self._require_conn().execute(
                """
                INSERT INTO provider_profiles
                    (provider, model, api_mode, endpoint_hash, route_hash,
                     ttl_lower_s, ttl_upper_s, ttl_estimate_s, ttl_confidence,
                     latency_p50_ms, latency_p95_ms, sample_count, updated_at,
                     profile_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_key) DO UPDATE SET
                    ttl_lower_s = excluded.ttl_lower_s,
                    ttl_upper_s = excluded.ttl_upper_s,
                    ttl_estimate_s = excluded.ttl_estimate_s,
                    ttl_confidence = excluded.ttl_confidence,
                    latency_p50_ms = excluded.latency_p50_ms,
                    latency_p95_ms = excluded.latency_p95_ms,
                    sample_count = excluded.sample_count,
                    updated_at = excluded.updated_at
                """,
                (
                    profile.provider,
                    profile.model,
                    profile.api_mode,
                    profile.endpoint_hash,
                    profile.route_hash,
                    profile.lower_bound_s,
                    profile.upper_bound_s,
                    profile.estimated_ttl_s,
                    profile.confidence,
                    profile.latency_p50_ms,
                    profile.latency_p95_ms,
                    profile.sample_count,
                    (
                        profile.updated_at.astimezone(UTC).isoformat(timespec="seconds")
                        if profile.updated_at is not None
                        else datetime.now(UTC).isoformat(timespec="seconds")
                    ),
                    profile.profile_key,
                ),
            )

    def profile_for(self, key: str) -> TTLProfile | None:
        """The route profile for one profile_key, or None (PRD §82)."""
        with self._lock:
            row = self._require_conn().execute(
                f"SELECT {', '.join(_PROFILE_COLUMNS)} FROM provider_profiles "
                "WHERE profile_key = ?",
                (key,),
            ).fetchone()
        return _row_to_profile(row) if row is not None else None

    def list_profiles(self, limit: int = 100) -> list[TTLProfile]:
        """Route profiles, most recently updated first (PRD §76 ``cachepilot ttl``)."""
        with self._lock:
            rows = self._require_conn().execute(
                f"SELECT {', '.join(_PROFILE_COLUMNS)} FROM provider_profiles "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_profile(row) for row in rows]

    def record_ttl_observation(
        self,
        *,
        timestamp: datetime,
        cache_fingerprint: str,
        route_hash: str | None,
        idle_age_s: float | None,
        outcome: Outcome,
        clean: bool,
    ) -> int:
        """Append one TTL observation row (PRD §82 ``ttl_observations``)."""
        with self._lock:
            cur = self._require_conn().execute(
                """
                INSERT INTO ttl_observations
                    (timestamp, cache_fingerprint, route_hash, idle_age_s,
                     outcome, clean)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp.astimezone(UTC).isoformat(timespec="seconds"),
                    cache_fingerprint,
                    route_hash,
                    idle_age_s,
                    outcome.value,
                    int(clean),
                ),
            )
            return int(cur.lastrowid or 0)

    def last_ttl_observation(self, cache_fingerprint: str) -> StoredTTLObservation | None:
        """Most recent TTL observation for one cache fingerprint (pairing key)."""
        with self._lock:
            row = self._require_conn().execute(
                f"SELECT {', '.join(_TTL_OBSERVATION_COLUMNS)} FROM ttl_observations "
                "WHERE cache_fingerprint = ? ORDER BY id DESC LIMIT 1",
                (cache_fingerprint,),
            ).fetchone()
        return _row_to_ttl_observation(row) if row is not None else None

    def recent_ttl_observations(
        self,
        cache_fingerprint: str,
        limit: int = 10,
    ) -> list[StoredTTLObservation]:
        """Consecutive observations of one cache fingerprint, newest first.

        The learner's pairing query (PRD §55): idle age is the delta
        between consecutive rows for the SAME cache fingerprint.
        """
        with self._lock:
            rows = self._require_conn().execute(
                f"SELECT {', '.join(_TTL_OBSERVATION_COLUMNS)} FROM ttl_observations "
                "WHERE cache_fingerprint = ? ORDER BY id DESC LIMIT ?",
                (cache_fingerprint, limit),
            ).fetchall()
        return [_row_to_ttl_observation(row) for row in rows]

    def churn_between(
        self,
        cache_fingerprint: str,
        start: datetime,
        end: datetime,
    ) -> bool:
        """True when a churn event touched the fingerprint inside (start, end).

        PRD §56 clean-check: an intervening identity change means the two
        observations are NOT consecutive with stable cache identity, so the
        pair must not refine TTL bounds.
        """
        with self._lock:
            row = self._require_conn().execute(
                """
                SELECT 1 FROM churn_events
                WHERE (previous_cache_fingerprint = ? OR new_cache_fingerprint = ?)
                  AND timestamp > ? AND timestamp < ?
                LIMIT 1
                """,
                (
                    cache_fingerprint,
                    cache_fingerprint,
                    start.astimezone(UTC).isoformat(timespec="seconds"),
                    end.astimezone(UTC).isoformat(timespec="seconds"),
                ),
            ).fetchone()
        return row is not None

    # -- route intelligence (P09: PRD §71-72, §75, UC-5) ---------------------

    def record_route_event(self, event: RouteChangeEvent) -> int:
        """Insert one route-change event (PRD §72.1, §75); returns its row id.

        Only route identities (whitelisted by AGENTS.md invariant 10),
        hashes, timestamps and the verdict are stored — never prompts or
        auth material.
        """
        with self._lock:
            cur = self._require_conn().execute(
                """
                INSERT INTO route_events
                    (timestamp, session_hash, cache_fingerprint,
                     request_fingerprint, previous_route_hash, new_route_hash,
                     gateway, upstream_provider, endpoint, region, deployment,
                     verdict)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.timestamp.astimezone(UTC).isoformat(timespec="seconds"),
                    event.session_hash,
                    event.cache_fingerprint,
                    event.request_fingerprint,
                    event.previous_route_hash,
                    event.new_route_hash,
                    event.gateway,
                    event.upstream_provider,
                    event.endpoint,
                    event.region,
                    event.deployment,
                    event.verdict.value,
                ),
            )
            return int(cur.lastrowid or 0)

    def recent_route_events(self, limit: int = 100) -> list[RouteChangeEvent]:
        """Most recent route-change events, newest first (PRD §76
        ``cachepilot routes``)."""
        with self._lock:
            rows = self._require_conn().execute(
                f"SELECT {', '.join(_ROUTE_EVENT_COLUMNS)} FROM route_events "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_route_event(row) for row in rows]

    def last_route_event_for_session(
        self,
        session_hash: str,
        *,
        since: float | None = None,
    ) -> RouteChangeEvent | None:
        """Newest route event for one session, optionally only events recorded
        after ``since`` (epoch seconds — the lease's current request start).

        The relay controller uses ``since`` to only see the instability event
        recorded DURING the current request, never a stale one from an
        earlier request (the affinity must react to fresh evidence only).
        """
        if since is not None:
            since_iso = datetime.fromtimestamp(since, tz=UTC).isoformat(
                timespec="seconds"
            )
            sql = (
                f"SELECT {', '.join(_ROUTE_EVENT_COLUMNS)} FROM route_events "
                "WHERE session_hash = ? AND timestamp >= ? ORDER BY id DESC LIMIT 1"
            )
            params: tuple[Any, ...] = (session_hash, since_iso)
        else:
            sql = (
                f"SELECT {', '.join(_ROUTE_EVENT_COLUMNS)} FROM route_events "
                "WHERE session_hash = ? ORDER BY id DESC LIMIT 1"
            )
            params = (session_hash,)
        with self._lock:
            row = self._require_conn().execute(sql, params).fetchone()
        return _row_to_route_event(row) if row is not None else None

    def route_intel_stats(self) -> RouteIntelStats:
        """Route-switch and instability aggregates (PRD §76 ``cachepilot routes``)."""
        with self._lock:
            conn = self._require_conn()
            switches = conn.execute("SELECT COUNT(*) FROM route_events").fetchone()[0]
            instability = conn.execute(
                "SELECT COUNT(*) FROM route_events WHERE verdict = ?",
                (RouteMissVerdict.ROUTE_INSTABILITY.value,),
            ).fetchone()[0]
            short_ttl = conn.execute(
                "SELECT COUNT(*) FROM route_events WHERE verdict = ?",
                (RouteMissVerdict.SHORT_TTL.value,),
            ).fetchone()[0]
            last = conn.execute(
                "SELECT timestamp FROM route_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return RouteIntelStats(
            route_switches=switches,
            instability_verdicts=instability,
            short_ttl_verdicts=short_ttl,
            last_switch_at=(
                datetime.fromisoformat(last[0]) if last is not None else None
            ),
        )

    # -- internals ----------------------------------------------------------

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("telemetry store is not connected")
        return self._conn
