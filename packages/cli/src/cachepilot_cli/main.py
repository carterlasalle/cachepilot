"""cachepilot CLI — PRD §76-79, §126 (Phase 4: status/leases/costs).

Subcommands:
- ``status``: PRD §77-style output — version, mode, relay health (TCP probe
  of the relay listen address), Hermes plugin state, then cache health
  aggregated from the telemetry store (request count, hit %, per-outcome
  counts, churn events, route changes). Empty databases say "no telemetry
  recorded yet" — never fabricated numbers.
- ``leases``: honest Phase 5 placeholder (the lease manager does not exist
  yet, so no lease rows are invented).
- ``costs``: recorded provider-returned cost from ``request_events`` (total
  and per provider). Labeled recorded-cost-only: "money saved" is never
  shown when cost data are incomplete (PRD §79, AGENTS.md invariant 4).

The telemetry database comes from ``--db``, else ``CACHEPILOT_TELEMETRY_DB``,
else ``~/.hermes/cachepilot/cachepilot.db`` (PRD §81).
"""

from __future__ import annotations

import argparse
import os
import socket
from collections.abc import Sequence
from decimal import Decimal

from cachepilot_core.storage import ENV_TELEMETRY_DB, TelemetryStore, default_db_path
from cachepilot_core.telemetry import CacheHealthStats, ChurnEvent
from cachepilot_relay.config import DEFAULT_LISTEN, ENV_LISTEN, parse_listen

from cachepilot_cli import __version__ as CLI_VERSION

ENV_PLUGIN_ENABLED = "CACHEPILOT_ENABLED"

#: Failure modes that prove a positive plugin-activity claim; the CLI is
#: honest about the absence of evidence either way.
_RELAY_PROBE_TIMEOUT_S = 1.0


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point (``cachepilot``). Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="cachepilot",
        description="CachePilot observability CLI (PRD §76-79).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    db_flag = argparse.ArgumentParser(add_help=False)
    db_flag.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help=(
            "telemetry database path (default: "
            f"${ENV_TELEMETRY_DB} if set, else {default_db_path()})"
        ),
    )

    status_parser = sub.add_parser(
        "status",
        parents=[db_flag],
        help="relay + plugin + cache health from the telemetry database",
    )
    status_parser.set_defaults(handler=cmd_status)

    leases_parser = sub.add_parser(
        "leases",
        parents=[db_flag],
        help="active cache leases (lease manager ships in Phase 5)",
    )
    leases_parser.set_defaults(handler=cmd_leases)

    costs_parser = sub.add_parser(
        "costs",
        parents=[db_flag],
        help="recorded costs from request_events (recorded-cost-only)",
    )
    costs_parser.set_defaults(handler=cmd_costs)

    args = parser.parse_args(argv)
    return int(args.handler(args))


# -- status -----------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    store = TelemetryStore(args.db)
    try:
        stats = store.aggregates()
        churn = store.churn_list(limit=1)
        routes = store.route_changes(limit=1)
    finally:
        store.close()

    print(f"CachePilot {CLI_VERSION}")
    print()
    print("Mode: relay")
    print(f"Relay: {_relay_health()}")
    print(f"Hermes plugin: {_plugin_state(stats)}")
    print()
    print("Cache health (telemetry):")
    if stats.total == 0:
        print("  no telemetry recorded yet")
        return 0
    print(f"  requests            {stats.total}")
    _print_hit_rate(stats)
    print(f"  CONFIRMED_HIT       {stats.confirmed_hits}")
    print(f"  MISS_REBUILT        {stats.misses}")
    print(f"  SUCCESS_UNVERIFIED  {stats.unverified}")
    print(f"  FAILED              {stats.failed}")
    print(f"  churn events        {stats.churn_events}")
    if churn:
        print(f"    most recent       {_describe_churn(churn[0])}")
    print(f"  route changes       {stats.route_changes}")
    if routes:
        print(f"    most recent       {_describe_churn(routes[0])}")
    return 0


def _print_hit_rate(stats: CacheHealthStats) -> None:
    rate = stats.hit_rate
    if rate is None:
        print("  cache hit rate      n/a (no cache telemetry observed)")
    else:
        print(
            f"  cache hit rate      {rate * 100:.1f}% "
            f"({stats.confirmed_hits} hits / {stats.telemetry_observed} with telemetry)"
        )


def _describe_churn(churn: ChurnEvent) -> str:
    when = churn.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"{when} UTC prev={churn.previous_cache_fingerprint[:12]}\u2026 "
        f"new={churn.new_cache_fingerprint[:12]}\u2026"
    )


def _relay_health() -> str:
    """TCP probe of the relay listen address (PRD §77 'Relay: healthy')."""
    listen = os.environ.get(ENV_LISTEN) or DEFAULT_LISTEN
    try:
        host, port = parse_listen(listen)
    except ValueError:
        return f"unreachable (invalid {ENV_LISTEN}={listen!r})"
    try:
        with socket.create_connection((host, port), timeout=_RELAY_PROBE_TIMEOUT_S):
            return "healthy"
    except OSError:
        return "unreachable"


def _plugin_state(stats: CacheHealthStats) -> str:
    """Hermes plugin state from CACHEPILOT_ENABLED + telemetry evidence.

    Honest by construction: "active" is only claimed when the plugin is
    enabled AND its telemetry is actually present; otherwise the output
    states exactly what is and is not known.
    """
    enabled = os.environ.get(ENV_PLUGIN_ENABLED, "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enabled:
        return "inactive (CACHEPILOT_ENABLED=false)"
    if stats.total > 0:
        return "active"
    return "active (no telemetry recorded yet)"


# -- leases -----------------------------------------------------------------


def cmd_leases(args: argparse.Namespace) -> int:
    # Honest Phase 5 placeholder: the lease manager does not exist yet, so
    # there are no leases to list and none are fabricated (PRD §78, §132).
    print("no active leases — lease manager ships in Phase 5")
    return 0


# -- costs ------------------------------------------------------------------


def cmd_costs(args: argparse.Namespace) -> int:
    store = TelemetryStore(args.db)
    try:
        totals = store.cost_totals()
    finally:
        store.close()
    total = sum(totals.values(), Decimal(0))
    print("Recorded costs (from request_events telemetry)")
    print(
        "  NOTE: recorded-cost-only — cost data are incomplete; "
        "net savings are unknown (PRD §79)"
    )
    print(f"  total recorded      ${total:.6f}")
    if totals:
        print("  by provider:")
        width = max(len(provider) for provider in totals)
        for provider in sorted(totals):
            print(f"    {provider:<{width}}  ${totals[provider]:.6f}")
    return 0
