"""cachepilot CLI — PRD §76-79, §126 (Phase 4: status/leases/costs).

Subcommands:
- ``status``: PRD §77-style output — version, mode, relay health (TCP probe
  of the relay listen address), Hermes plugin state, then cache health
  aggregated from the telemetry store (request count, hit %, per-outcome
  counts, churn events, route changes). Empty databases say "no telemetry
  recorded yet" — never fabricated numbers.
- ``leases``: real lease rows from the telemetry store (PRD §78) — the
  relay persists a snapshot per lease, and empty databases say so (never
  fabricated rows).
- ``costs``: recorded provider-returned cost from ``request_events`` (total
  and per provider). Labeled recorded-cost-only: "money saved" is never
  shown when cost data are incomplete (PRD §79, AGENTS.md invariant 4).
- ``ttl``: route-keyed learned TTL profiles from the telemetry store (PRD
  §76, §82) — estimated TTL, lower/upper bounds, confidence, sample count
  per route (provider/model/api_mode/endpoint_hash/route_hash), plus the P11
  survival view (PRD §99/§138): P(cache survives) at the estimated TTL and
  the median survival age from CLEAN observations. Empty databases say "no
  TTL profiles yet" — never fabricated profiles.
- ``routes``: observed route identities (gateway/upstream/endpoint/region/
  deployment where observable, PRD §71) with instability stats (route
  switch count, last switch time, instability verdicts count) from the
  ``route_events`` table (PRD §72.1, UC-5). Empty databases say "no
  observed route changes yet" — never fabricated routes.
- ``churn`` (P10): per-layer change frequency over recorded churn events
  plus the most common classifier diagnoses (PRD §25, §76). Empty
  databases say "no churn events" — never fabricated numbers.
- ``explain-miss`` (P10): explains the latest (or --session-scoped) churn
  event — layers changed, likely cause, confidence, estimated prefix loss
  (PRD §75, §137).
- ``topology`` (P11): cross-request prefix topology (PRD §24/§138) — per-
  layer change frequency, stability % and estimated prefix-token value over
  consecutive request pairs, plus per-route tool-schema ordering stability.
  DETECT/measurement-only: nothing here reorders tools or rewrites prompts.

The telemetry database comes from ``--db``, else ``CACHEPILOT_TELEMETRY_DB``,
else ``~/.hermes/cachepilot/cachepilot.db`` (PRD §81).
"""

from __future__ import annotations

import argparse
import os
import socket
import time
from collections.abc import Sequence
from decimal import Decimal

from cachepilot_core.storage import (
    ENV_TELEMETRY_DB,
    StoredLease,
    TelemetryStore,
    default_db_path,
)
from cachepilot_core.survival import curve_from_profile
from cachepilot_core.telemetry import CacheHealthStats, ChurnEvent
from cachepilot_core.topology import topology_from_store
from cachepilot_core.ttl import TTLProfile
from cachepilot_relay.config import DEFAULT_LISTEN, ENV_LISTEN, parse_listen

from cachepilot_cli import __version__ as CLI_VERSION
from cachepilot_cli.churn import cmd_churn, cmd_explain_miss

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
        help="active cache leases from the telemetry store (PRD §78)",
    )
    leases_parser.set_defaults(handler=cmd_leases)

    costs_parser = sub.add_parser(
        "costs",
        parents=[db_flag],
        help="recorded costs from request_events (recorded-cost-only)",
    )
    costs_parser.set_defaults(handler=cmd_costs)

    ttl_parser = sub.add_parser(
        "ttl",
        parents=[db_flag],
        help="route-keyed learned TTL profiles from the telemetry store (PRD §76, §82)",
    )
    ttl_parser.set_defaults(handler=cmd_ttl)

    routes_parser = sub.add_parser(
        "routes",
        parents=[db_flag],
        help="observed route identities and instability stats (PRD §71, §76, UC-5)",
    )
    routes_parser.set_defaults(handler=cmd_routes)

    churn_parser = sub.add_parser(
        "churn",
        parents=[db_flag],
        help="per-layer cache churn frequency + most common causes (PRD §25, §76)",
    )
    churn_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        metavar="N",
        help="most recent churn events to aggregate (default: 100)",
    )
    churn_parser.set_defaults(handler=cmd_churn)

    explain_parser = sub.add_parser(
        "explain-miss",
        parents=[db_flag],
        help="explain the latest (or --session-scoped) cache miss (PRD §75, §137)",
    )
    explain_parser.add_argument(
        "--session",
        default=None,
        metavar="HASH",
        help="session hash to scope the explanation to (default: latest overall)",
    )
    explain_parser.set_defaults(handler=cmd_explain_miss)

    topology_parser = sub.add_parser(
        "topology",
        parents=[db_flag],
        help="per-layer prefix stability + tool-ordering stability (PRD §24, §138)",
    )
    topology_parser.add_argument(
        "--limit",
        type=int,
        default=500,
        metavar="N",
        help="most recent request events to measure (default: 500)",
    )
    topology_parser.set_defaults(handler=cmd_topology)

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
    """Real lease listing from the telemetry store (PRD §78, Phase 5).

    Rows are whatever the relay actually persisted — never fabricated
    (honest by construction; an empty database says so).
    """
    store = TelemetryStore(args.db)
    try:
        leases = store.list_leases(limit=50)
    finally:
        store.close()
    if not leases:
        print("no active leases")
        return 0
    print(f"{'LEASE':<10} {'TARGETS':<9} {'CACHE AGE':<11} {'TTL':<8} STATE")
    for lease in leases:
        print(
            f"{lease.lease_id[:8]:<10} {len(lease.active_targets):<9} "
            f"{_lease_age_s(lease):<11} {_lease_ttl_s(lease):<8} {lease.state.upper()}"
        )
    return 0


def _lease_age_s(lease: StoredLease, now: float | None = None) -> str:
    """Cache age = now - last cache touch (PRD §78 ``CACHE AGE`` column)."""
    if lease.last_cache_touch_at is None:
        return "n/a"
    now = time.time() if now is None else now
    return f"{max(0, int(now - lease.last_cache_touch_at))}s"


def _lease_ttl_s(lease: StoredLease) -> str:
    return f"{int(lease.estimated_ttl_s)}s"


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


# -- ttl -------------------------------------------------------------------


def cmd_ttl(args: argparse.Namespace) -> int:
    """Route-keyed learned TTL profiles (PRD §76 ``cachepilot ttl``, §82).

    Rows are whatever the learner actually persisted — never fabricated.
    An empty database says so, and unknown TTL values are shown as
    ``unknown`` (PRD §59: never silently guess). P11 (PRD §99/§138) adds the
    survival view per profile: P(cache survives) at the estimated TTL and
    the median survival age, estimated over CLEAN observations — a
    diagnostic layer, never a warm decision input.
    """
    store = TelemetryStore(args.db)
    try:
        profiles = store.list_profiles(limit=100)
        if not profiles:
            print("no TTL profiles yet (learning needs repeated observations of a stable route)")
            return 0
        print("TTL profiles (route-keyed, PRD §82)")
        print()
        for profile in profiles:
            print(f"Route: {profile.provider} | {profile.model} | {profile.api_mode}")
            print(f"  endpoint      {_short_hash(profile.endpoint_hash)}")
            print(f"  route         {_short_hash(profile.route_hash)}")
            print(f"  estimated     {_ttl_value(profile.estimated_ttl_s)}")
            print(f"  lower bound   {_ttl_value(profile.lower_bound_s)}")
            print(f"  upper bound   {_ttl_value(profile.upper_bound_s)}")
            print(f"  confidence    {profile.confidence:.2f}")
            print(f"  samples       {profile.sample_count}")
            _print_survival(store, profile)
    finally:
        store.close()
    return 0


def _print_survival(store: TelemetryStore, profile: TTLProfile) -> None:
    """P11 (PRD §99/§138): P(survive) at the profile's estimated TTL.

    Derived on demand from CLEAN ttl_observations (Kaplan-Meier-style) —
    never fabricated: no clean observations, no estimate. Beyond the observed
    horizon the CLI says so instead of inventing a probability.
    """
    curve = curve_from_profile(store, profile)
    if curve.empty:
        print("  survival      no clean observations yet")
        return
    ttl = profile.estimated_ttl_s
    probability = curve.survival_at(ttl) if ttl is not None else None
    if probability is None:
        print(
            f"  survival      P(survive) n/a — beyond observed horizon "
            f"(n={curve.sample_count} clean observations)"
        )
    else:
        print(
            f"  survival      P(survive at TTL) = {probability:.2f} "
            f"(n={curve.sample_count} clean observations)"
        )
    median = curve.median_survival_s()
    if median is not None:
        print(f"  median        {median:.0f}s")


def _short_hash(value: str | None) -> str:
    """Short display form of a hash (route key component, PRD §82)."""
    return f"{value[:12]}\u2026" if value else "none"


def _ttl_value(value: float | None) -> str:
    """Display a TTL bound; unknown stays unknown (PRD §59)."""
    return "unknown" if value is None else f"{value:.0f}s"


# -- routes (P09, PRD §71, §76, UC-5) ----------------------------------------


def cmd_routes(args: argparse.Namespace) -> int:
    """Observed route identities + instability stats (PRD §71, §76, UC-5).

    Rows are whatever the relay's route intelligence actually recorded —
    never fabricated. Route identities show only the observable fields
    (gateway / upstream / endpoint / region / deployment); an empty
    database says so.
    """
    store = TelemetryStore(args.db)
    try:
        events = store.recent_route_events(limit=100)
        stats = store.route_intel_stats()
    finally:
        store.close()
    if not events:
        print("no observed route changes yet (route intelligence records switches between repeated logical requests)")
        return 0
    print("Observed routes (PRD §71 identity, UC-5 instability)")
    print()
    for event in events:
        when = event.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        print(f"{when} UTC  verdict={event.verdict.value}")
        print(f"  route      {_short_hash(event.previous_route_hash)} -> {_short_hash(event.new_route_hash)}")
        print(f"  gateway    {event.gateway or 'n/a'}")
        print(f"  upstream   {event.upstream_provider or 'n/a'}")
        print(f"  endpoint   {event.endpoint or 'n/a'}")
        print(f"  region     {event.region or 'n/a'}")
        print(f"  deployment {event.deployment or 'n/a'}")
    print()
    print(f"route switches        {stats.route_switches}")
    if stats.last_switch_at is not None:
        last = stats.last_switch_at.strftime("%Y-%m-%d %H:%M:%S")
        print(f"last switch           {last} UTC")
    print(f"instability verdicts  {stats.instability_verdicts}")
    print(f"short-TTL verdicts    {stats.short_ttl_verdicts}")
    return 0


# -- topology (P11, PRD §24/§138) ---------------------------------------------


def cmd_topology(args: argparse.Namespace) -> int:
    """Cross-request prefix topology + tool-ordering stability (PRD §24/§138).

    DETECT/measurement-only view over stored request/churn events: per-layer
    change frequency, stability % and estimated prefix-token value over
    consecutive request pairs, plus per-route tool-schema ordering stability.
    Nothing here reorders tools or rewrites prompts (PRD §138: "Only after
    measurement"; automatic tool reordering additionally requires proof of
    semantic safety — this phase only measures). Rows are whatever the relay
    actually recorded — an empty database says so, never fabricated numbers.
    """
    store = TelemetryStore(args.db)
    try:
        report = topology_from_store(store, limit=args.limit)
    finally:
        store.close()
    if report.total_pairs == 0:
        print("no consecutive request pairs recorded yet — nothing to measure")
        return 0
    print(
        f"Cache topology (PRD §24/§138 measurement view) — "
        f"last {args.limit} requests"
    )
    print()
    print(f"  sessions                  {report.sessions}")
    print(f"  consecutive pairs         {report.total_pairs}")
    stability = _pct(report.prefix_stability_pct)
    print(
        f"  cache fingerprint churn   {report.churn_pairs}  "
        f"(prefix stability {stability})"
    )
    if report.attribution_gaps:
        print(
            f"  attribution gaps          {report.attribution_gaps}  "
            "(system/history churn without layered attribution)"
        )
    if report.unattributed_loss_tokens:
        print(f"  unattributed loss         ~{report.unattributed_loss_tokens:,} tokens")
    print()
    print("Per-layer change frequency (consecutive request pairs):")
    print(f"  {'layer':<24} {'changed':<34} {'stability':<10} est. prefix loss")
    for stats in report.layers:
        marker = "*" if stats.attribution_based else " "
        loss = _loss_text(stats.estimated_prefix_loss_tokens)
        print(
            f"  {stats.layer:<24} {stats.change_frequency:<34} "
            f"{_pct(stats.stability_pct):<10} {loss}{marker}"
        )
    if any(stats.attribution_based for stats in report.layers):
        print()
        print("  * layered sub-layer rows are attributed from classified churn")
        print("    events (exact layered hashes are memory-only, PRD §30)")
    print()
    print("Tool-schema ordering stability (per route):")
    print(
        f"  {'route':<16} {'pairs':<7} {'set changes':<13} "
        f"{'order permutations':<20} stability"
    )
    if not report.tool_ordering:
        print(
            "  no decidable tool-set pairs recorded "
            "(tools_set_hash missing on pre-P11 rows)"
        )
    for stats in report.tool_ordering:
        print(
            f"  {_short_hash(stats.route):<16} {stats.pairs:<7} "
            f"{stats.tool_set_changes:<13} {stats.order_permutations:<20} "
            f"{_pct(stats.ordering_stability_pct)}"
        )
    return 0


def _pct(value: float | None) -> str:
    """Render an optional percentage; unknown stays unknown (never fabricated)."""
    return "n/a" if value is None else f"{value:.1f}%"


def _loss_text(tokens: int | None) -> str:
    """Render an estimated prefix-token loss; None stays n/a (identity layers)."""
    return "n/a" if tokens is None else f"~{tokens:,} tokens"
