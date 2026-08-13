"""CachePilot dashboard backend — read-only telemetry JSON server (PRD §122/§139).

The optional React dashboard's data backend. It exposes the SAME query
surface as the ``cachepilot`` CLI (status / leases / costs / ttl / routes /
churn / explain-miss / topology) as JSON over HTTP for the frontend.

Read-only discipline (AGENTS.md invariant 3/10 and the CLI's never-fabricate
rule, mirrored here):

- The store is opened strictly read-only (SQLite ``mode=ro`` URI) by
  :class:`ReadOnlyTelemetryStore`, which skips the base class's schema
  creation / migration statements entirely. SQLite's standard WAL journal
  bookkeeping may create EMPTY ``-wal``/``-shm`` sidecar files next to a
  cleanly-closed WAL database (any read-only connection does — the CLI's
  reads leave the same artifacts); the database file itself is never
  modified, and ``smoke_test.py`` proves it byte-identical. A WAL database
  whose ``-shm`` file is missing and whose journal is hot falls back to the
  standard (CLI-sanctioned) store open, whose ``CREATE TABLE IF NOT EXISTS``
  / idempotent ALTERs are no-ops on an existing schema and never touch
  telemetry rows.
- Every endpoint is derived from the store's public query methods — nothing
  is invented. A missing database (or one that cannot be opened) renders
  EMPTY states: zeros and empty lists, never fabricated numbers.
- This backend never writes telemetry; it exists only to read it. The
  dashboard is optional — no core package imports it (PRD §139).

Run (from the repo root, so ``uv`` resolves the workspace venv):

    uv run python dashboard/backend/server.py [--db PATH] [--host HOST] [--port PORT]

``--db`` defaults to ``CACHEPILOT_TELEMETRY_DB``, else
``~/.hermes/cachepilot/cachepilot.db`` (same resolution as the CLI).
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import socket
import sqlite3
import time
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from cachepilot_core.churn import changed_frequency
from cachepilot_core.route_intel import RouteIntelStats
from cachepilot_core.storage import (
    ENV_TELEMETRY_DB,
    StoredLease,
    TelemetryStore,
    resolve_db_path,
)
from cachepilot_core.survival import curve_from_profile
from cachepilot_core.telemetry import CacheHealthStats, ChurnEvent
from cachepilot_core.topology import topology_from_store

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788

#: Relay probe (status view): the dashboard mirrors the CLI's honest
#: "healthy / unreachable" relay check, never asserting more than a TCP
#: connect proves (AGENTS.md invariant 3).
ENV_RELAY_LISTEN = "CACHEPILOT_RELAY_LISTEN"
DEFAULT_RELAY_LISTEN = "127.0.0.1:8787"
_RELAY_PROBE_TIMEOUT_S = 1.0

#: PRD §24/§75 layers, in the order the boolean flags live on ChurnEvent —
#: the same list the CLI aggregates over.
_LAYERS: tuple[tuple[str, str], ...] = (
    ("system", "system_changed"),
    ("tools", "tools_changed"),
    ("history", "history_changed"),
    ("route", "route_changed"),
    ("cache key", "cache_key_changed"),
    ("model", "model_changed"),
)

#: The one environment variable that switches the plugin; used for the
#: status view's plugin-state readout (mirrors the CLI).
ENV_PLUGIN_ENABLED = "CACHEPILOT_ENABLED"

#: Production build output dir (``yarn build``) the backend serves when
#: present, relative to the backend script's parent.
_DIST_DIR = Path(__file__).resolve().parent.parent / "dist"


class ReadOnlyTelemetryStore(TelemetryStore):
    """TelemetryStore whose ``connect()`` never writes to the database.

    The base class creates the schema and runs idempotent ALTER migrations
    on connect; this subclass opens the SQLite file strictly read-only and
    skips all schema work, so the dashboard can never modify the store.
    """

    def connect(self) -> None:
        if self._conn is not None:
            return
        conn = sqlite3.connect(
            f"file:{self._path}?mode=ro",
            uri=True,
            check_same_thread=False,
            isolation_level=None,
        )
        try:
            conn.execute("PRAGMA busy_timeout=5000")
        except Exception:
            conn.close()
            raise
        self._conn: sqlite3.Connection | None = conn


def open_store(db_path: str | None = None) -> TelemetryStore | None:
    """Open the telemetry store read-only, or return None for an EMPTY store.

    None means "no readable telemetry" — every endpoint then renders its
    empty state (zeros / empty lists). This is the never-fabricate path: a
    missing file, a corrupt file, or an unreadable database is an honest
    empty store, not an error page full of invented numbers.
    """
    path = resolve_db_path(db_path)
    if not path.is_file():
        return None
    try:
        return ReadOnlyTelemetryStore(path)
    except sqlite3.Error:
        # A WAL database without its -shm file cannot open strictly
        # read-only; fall back to the CLI-sanctioned store open (its schema
        # statements are no-ops on an existing database).
        try:
            return TelemetryStore(path)
        except sqlite3.Error:
            return None


def _json_default(value: Any) -> Any:
    """json.dumps fallback for pydantic dumps: Decimal, datetime, enums."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, default=_json_default)


def _relay_health() -> str:
    """TCP probe of the relay listen address (mirrors ``cachepilot status``)."""
    listen = os.environ.get(ENV_RELAY_LISTEN, DEFAULT_RELAY_LISTEN)
    host, _, port_text = listen.rpartition(":")
    try:
        port = int(port_text)
    except ValueError:
        return "unknown (invalid CACHEPILOT_RELAY_LISTEN)"
    host = host or "127.0.0.1"
    try:
        with socket.create_connection((host, port), timeout=_RELAY_PROBE_TIMEOUT_S):
            return "healthy"
    except OSError:
        return "unreachable"


def _plugin_state(total_requests: int) -> str:
    """Plugin state from CACHEPILOT_ENABLED + telemetry evidence (CLI parity)."""
    enabled = os.environ.get(ENV_PLUGIN_ENABLED, "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enabled:
        return "inactive (CACHEPILOT_ENABLED=false)"
    if total_requests > 0:
        return "active"
    return "active (no telemetry recorded yet)"


def _lease_age_s(lease: StoredLease, now: float | None = None) -> float | None:
    """Cache age = now - last cache touch; None when never touched (CLI parity)."""
    if lease.last_cache_touch_at is None:
        return None
    now = time.time() if now is None else now
    return max(0.0, now - lease.last_cache_touch_at)


def _payload_status(store: TelemetryStore | None) -> dict[str, Any]:
    stats = store.aggregates() if store is not None else CacheHealthStats()
    row = stats.model_dump()
    # Computed properties are excluded from pydantic dumps; the frontend's
    # health cards need them (CLI parity: hit % and telemetry-observed).
    row["telemetry_observed"] = stats.telemetry_observed
    row["hit_rate"] = stats.hit_rate
    return {
        "stats": row,
        "relay": _relay_health(),
        "plugin": _plugin_state(stats.total),
        "providers": store.provider_summary() if store is not None else [],
    }


def _payload_leases(store: TelemetryStore | None) -> dict[str, Any]:
    if store is None:
        return {"leases": []}
    leases = []
    for lease in store.list_leases(limit=50):
        row = lease.model_dump()
        row["cache_age_s"] = _lease_age_s(lease)
        leases.append(row)
    return {"leases": leases}


def _payload_costs(store: TelemetryStore | None) -> dict[str, Any]:
    note = (
        "recorded-cost-only: cost data are incomplete; "
        "net savings are unknown (PRD §79, AGENTS.md invariant 4)"
    )
    if store is None:
        return {"total_usd": 0.0, "per_provider": {}, "note": note, "recent": []}
    totals = store.cost_totals()
    per_provider = {provider: float(cost) for provider, cost in totals.items()}
    recent = []
    for event in store.recent_events(limit=200):
        if event.cost_usd is not None:
            recent.append(
                {
                    "timestamp": event.timestamp.isoformat(),
                    "provider": event.provider,
                    "cost_usd": float(event.cost_usd),
                }
            )
    return {
        "total_usd": float(sum(totals.values(), Decimal(0))),
        "per_provider": per_provider,
        "note": note,
        "recent": recent,
    }


def _payload_ttl(store: TelemetryStore | None) -> dict[str, Any]:
    if store is None:
        return {"profiles": []}
    profiles = []
    for profile in store.list_profiles(limit=100):
        row = profile.model_dump()
        row["profile_key"] = profile.profile_key
        curve = curve_from_profile(store, profile)
        if curve.empty:
            row["survival"] = None
        else:
            ttl = profile.estimated_ttl_s
            row["survival"] = {
                "sample_count": curve.sample_count,
                "horizon_s": curve.horizon_s,
                "p_survive_at_ttl": curve.survival_at(ttl) if ttl is not None else None,
                "median_s": curve.median_survival_s(),
                "steps": [
                    {
                        "age_s": step.age_s,
                        "survival": step.survival,
                        "at_risk": step.at_risk,
                        "events": step.events,
                    }
                    for step in curve.steps
                ],
            }
        profiles.append(row)
    return {"profiles": profiles}


def _payload_routes(store: TelemetryStore | None) -> dict[str, Any]:
    if store is None:
        return {"events": [], "stats": RouteIntelStats().model_dump()}
    events = [event.model_dump() for event in store.recent_route_events(limit=100)]
    stats = store.route_intel_stats().model_dump()
    return {"events": events, "stats": stats}


def _payload_churn(store: TelemetryStore | None) -> dict[str, Any]:
    if store is None:
        events: list[ChurnEvent] = []
    else:
        events = store.churn_list(limit=100)
    layers = []
    for label, attribute in _LAYERS:
        flags = [bool(getattr(event, attribute)) for event in events]
        layers.append(
            {
                "layer": label,
                "changed": sum(1 for flag in flags if flag),
                "total": len(events),
                "frequency": changed_frequency(flags, subject="churn events"),
            }
        )
    causes = Counter(event.likely_cause for event in events if event.likely_cause)
    return {
        "events": [event.model_dump() for event in events],
        "layers": layers,
        "top_causes": [
            {"cause": cause, "count": count}
            for cause, count in causes.most_common(5)
        ],
    }


def _payload_miss(store: TelemetryStore | None, session: str | None) -> dict[str, Any]:
    if store is None:
        return {"event": None, "stable": [], "changed": []}
    events = store.churn_list(limit=1, session_hash=session)
    if not events:
        return {"event": None, "stable": [], "changed": []}
    event = events[0]
    stable = [label for label, attribute in _LAYERS if not getattr(event, attribute)]
    changed = [label for label, attribute in _LAYERS if getattr(event, attribute)]
    return {"event": event.model_dump(), "stable": stable, "changed": changed}


def _payload_topology(store: TelemetryStore | None) -> dict[str, Any]:
    if store is None:
        return {"sessions": 0, "total_pairs": 0, "churn_pairs": 0,
                "prefix_stability_pct": None, "attribution_gaps": 0,
                "unattributed_loss_tokens": 0, "layers": [], "tool_ordering": []}
    report = topology_from_store(store, limit=500)
    return report.model_dump()


class DashboardServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying the dashboard config to handlers."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        db_path: str | None,
        dist_dir: Path = _DIST_DIR,
    ) -> None:
        self.db_path = db_path
        self.dist_dir = dist_dir
        super().__init__(address, handler)


class Handler(BaseHTTPRequestHandler):
    """GET-only JSON API + static frontend serving (when ``dist/`` exists)."""

    server: DashboardServer

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:  # http.server API (capitalized by contract)
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            return self._send_json(200, {"ok": True})
        if parsed.path == "/api/status":
            return self._api(_payload_status)
        if parsed.path == "/api/leases":
            return self._api(_payload_leases)
        if parsed.path == "/api/costs":
            return self._api(_payload_costs)
        if parsed.path == "/api/ttl":
            return self._api(_payload_ttl)
        if parsed.path == "/api/routes":
            return self._api(_payload_routes)
        if parsed.path == "/api/churn":
            return self._api(_payload_churn)
        if parsed.path == "/api/miss":
            query = parse_qs(parsed.query)
            session = query.get("session", [None])[0]
            return self._api(lambda store: _payload_miss(store, session))
        if parsed.path == "/api/topology":
            return self._api(_payload_topology)
        if parsed.path.startswith("/api/"):
            return self._send_json(404, {"error": f"unknown endpoint: {parsed.path}"})
        return self._serve_static(parsed.path)

    def do_POST(self) -> None:  # http.server API (capitalized by contract)
        self._send_json(405, {"error": "the dashboard backend is read-only (GET only)"})

    # -- helpers -----------------------------------------------------------

    def _api(self, build: Any) -> None:
        """Run one payload builder over a freshly-opened store and close it.

        Opening per request keeps the data live (the relay may create the DB
        or write rows at any time) and guarantees the connection — and any
        SQLite journal sidecar it created — is released after every read.
        """
        store = open_store(self.server.db_path)
        try:
            payload = build(store)
        except Exception as exc:  # noqa: BLE001 — fail open: a bad store must never crash the request thread
            self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        finally:
            if store is not None:
                store.close()
        self._send_json(200, payload)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        root = self.server.dist_dir
        if not root.is_dir():
            self._send_json(404, {"error": "no API endpoint; frontend not built"})
            return
        relative = path.lstrip("/") or "index.html"
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            self._send_json(404, {"error": "forbidden path"})
            return
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            # SPA fallback: any non-API path serves the built app.
            candidate = root / "index.html"
            if not candidate.is_file():
                self._send_json(404, {"error": "frontend not built"})
                return
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet access log
        print(f"[dashboard] {self.address_string()} {fmt % args}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cachepilot-dashboard-backend",
        description="Read-only telemetry JSON backend for the optional CachePilot dashboard (PRD §122/§139).",
    )
    parser.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help=(
            "telemetry database path (default: "
            f"${ENV_TELEMETRY_DB} if set, else ~/.hermes/cachepilot/cachepilot.db)"
        ),
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"bind host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"bind port (default: {DEFAULT_PORT})")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = DashboardServer(
        (args.host, args.port),
        Handler,
        db_path=args.db,
    )
    print(f"[dashboard] read-only telemetry backend on http://{args.host}:{args.port}")
    print(f"[dashboard] telemetry DB: {resolve_db_path(args.db)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
