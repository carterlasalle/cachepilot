"""cachepilot churn / explain-miss — PRD §76, §137 (Phase 10).

- ``churn`` (PRD §76): per-layer change frequency over the recorded churn
  events (PRD §25 output shape: ``changed 11/12 requests``) plus the most
  common classifier diagnoses. Rows are whatever the relay actually recorded
  — an empty database says "no churn events", never fabricated numbers.
- ``explain-miss`` (PRD §137; §75 output shape): explains the LATEST churn
  event (a cache-fingerprint transition — the stored moment a reusable prefix
  was destroyed) with the layers that changed, the likely cause, the
  confidence and the estimated prefix loss. ``--session`` scopes to one
  session's latest event. Rows recorded before Phase 10 (or with content
  unavailable at record time) show ``n/a`` for the classifier fields — honest
  unknowns, never guesses.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from cachepilot_core.churn import changed_frequency
from cachepilot_core.storage import TelemetryStore, resolve_db_path
from cachepilot_core.telemetry import ChurnEvent

#: PRD §24/§75 layers, in the order the boolean flags live on ChurnEvent.
_LAYERS = (
    ("system", "system_changed"),
    ("tools", "tools_changed"),
    ("history", "history_changed"),
    ("route", "route_changed"),
    ("cache key", "cache_key_changed"),
    ("model", "model_changed"),
)


def _is_readable_sqlite(path: Path) -> bool:
    """Return True if ``path`` is a valid, readable SQLite database.

    Opens a scratch read-only connection and runs ``PRAGMA quick_check``.
    A present-but-corrupt or non-SQLite file raises ``sqlite3.DatabaseError``
    (``file is not a database``); any ``sqlite3.Error`` means the path is
    NOT safe to hand to the store, and the caller treats it as absent.
    """
    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA quick_check")
        return True
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()


def open_read_only_store(db_path: str | None) -> TelemetryStore | None:
    """Open the telemetry store read-only; None for a missing DB (E2E-004).

    CLI read commands must never create the database: a typo'd ``--db`` or
    stale ``CACHEPILOT_TELEMETRY_DB`` used to silently materialize a stray
    ~84 KB empty store. When the resolved path does not exist, print a
    notice naming it (reads are read-only; the relay creates the DB on its
    first write) and return None — the caller renders its honest empty
    output and exits 0.

    The same holds for a present-but-corrupt or non-SQLite ``--db`` file
    (E2E-008): it is an honest empty store, NOT a crash. The up-front
    :func:`_is_readable_sqlite` probe treats such a file exactly like a
    missing one — print the same notice naming the path and return None —
    so the caller renders honest empty output and exits 0 with no
    traceback.
    """
    path = resolve_db_path(db_path)
    if not path.is_file():
        print(
            f"no telemetry database at {path} — nothing recorded yet "
            "(CLI reads are read-only; the relay creates the DB on first write)",
            file=sys.stderr,
        )
        return None
    if not _is_readable_sqlite(path):
        print(
            f"telemetry database at {path} is corrupt or not SQLite — "
            "treating it as an empty store "
            "(CLI reads are read-only; the relay creates the DB on first write)",
            file=sys.stderr,
        )
        return None
    return TelemetryStore(path, read_only=True)


def cmd_churn(args: argparse.Namespace) -> int:
    """Per-layer churn counts + most common likely causes (PRD §76 ``churn``)."""
    store = open_read_only_store(args.db)
    if store is None:
        print("no churn events")
        return 0
    try:
        events = store.churn_list(limit=args.limit)
    finally:
        store.close()
    if not events:
        print("no churn events")
        return 0
    print(f"Cache churn (last {len(events)} churn events, PRD §25 detector)")
    print()
    print("Per-layer change frequency:")
    for label, attribute in _LAYERS:
        flags = [bool(getattr(event, attribute)) for event in events]
        print(f"  {label:<12} {changed_frequency(flags, subject='churn events')}")
    causes = Counter(event.likely_cause for event in events if event.likely_cause)
    if causes:
        print()
        print("Most common likely causes:")
        for cause, count in causes.most_common(5):
            print(f"  {count:>3}  {cause}")
    return 0


def cmd_explain_miss(args: argparse.Namespace) -> int:
    """Explain the latest (or --session-scoped) miss (PRD §75, §137)."""
    store = open_read_only_store(args.db)
    if store is None:
        if args.session:
            print("no churn events recorded for this session — nothing to explain")
        else:
            print("no churn events recorded — nothing to explain")
        return 0
    try:
        events = store.churn_list(limit=1, session_hash=args.session)
    finally:
        store.close()
    if not events:
        if args.session:
            print("no churn events recorded for this session — nothing to explain")
        else:
            print("no churn events recorded — nothing to explain")
        return 0
    _print_explanation(events[0])
    return 0


def _print_explanation(event: ChurnEvent) -> None:
    when = event.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    print(f"Cache miss — churn event #{event.id} ({when} UTC)")
    print(f"  session        {event.session_hash or 'n/a'}")
    print(
        f"  cache key      {_short(event.previous_cache_fingerprint)} "
        f"\u2192 {_short(event.new_cache_fingerprint)}"
    )
    stable = [label for label, attribute in _LAYERS if not getattr(event, attribute)]
    changed = [label for label, attribute in _LAYERS if getattr(event, attribute)]
    print()
    if stable:
        print("Stable:")
        for label in stable:
            print(f"  {label}")
    else:
        print("Stable: (none)")
    if changed:
        print()
        print("Changed:")
        for label in changed:
            print(f"  {label}")
    print()
    print(f"Likely cause:\n  {event.likely_cause or 'n/a (not classified)'}")
    confidence = event.confidence
    print(
        "Confidence:\n  "
        + (f"{confidence:.2f}" if confidence is not None else "n/a")
    )
    print(
        "Estimated reusable prefix lost:\n  "
        + (
            f"~{event.estimated_prefix_loss_tokens} tokens"
            if event.estimated_prefix_loss_tokens is not None
            else "n/a (previous request content unavailable)"
        )
    )
    if event.first_divergent_offset is not None and event.first_divergent_layer is not None:
        print(
            f"First divergent byte:\n  offset ~{event.first_divergent_offset} "
            f"within '{event.first_divergent_layer}'"
        )


def _short(fingerprint: str) -> str:
    return f"{fingerprint[:12]}\u2026"
