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

from cachepilot_core.telemetry import (
    CacheHealthStats,
    ChurnEvent,
    Outcome,
    TelemetryEvent,
)

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
    model_changed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_churn_events_timestamp ON churn_events(timestamp);
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
                     route_changed, cache_key_changed, model_changed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
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

    def churn_list(self, limit: int = 50) -> list[ChurnEvent]:
        """Most recent churn events, newest first."""
        with self._lock:
            rows = self._require_conn().execute(
                """
                SELECT id, timestamp, session_hash, previous_cache_fingerprint,
                       new_cache_fingerprint, provider, model, route_hash,
                       system_changed, tools_changed, history_changed,
                       route_changed, cache_key_changed, model_changed
                FROM churn_events ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_churn_event(row) for row in rows]

    def route_changes(self, limit: int = 50) -> list[ChurnEvent]:
        """Most recent churn events whose route identity changed."""
        with self._lock:
            rows = self._require_conn().execute(
                """
                SELECT id, timestamp, session_hash, previous_cache_fingerprint,
                       new_cache_fingerprint, provider, model, route_hash,
                       system_changed, tools_changed, history_changed,
                       route_changed, cache_key_changed, model_changed
                FROM churn_events WHERE route_changed = 1 ORDER BY id DESC LIMIT ?
                """,
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

    # -- internals ----------------------------------------------------------

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("telemetry store is not connected")
        return self._conn
