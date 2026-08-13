"""Cross-request prefix topology + tool-ordering stability — PRD §24, §138 (Phase 11).

PRD §138 candidates are implemented as DETECT/measurement-first capabilities.
This module builds the measurement view over stored events (and offline
snapshots):

1. **Per-layer stability** — change frequency across consecutive
   ``request_events`` for the same session/cache fingerprint, for every PRD
   §24 layer (static system prefix / dynamic system suffix / tool schemas /
   historical conversation / recent conversation tail + the identity layers
   route / model / cache key). The four layered sub-layers are attributed
   from the stored churn events' ``first_divergent_layer`` when available
   (the layered hashes themselves are memory-only per PRD §30, so the stored
   view is honest about attribution gaps).
2. **Per-layer economic value** — estimated reusable prefix tokens lost per
   layer, summed from classified churn events (``estimated_prefix_loss_tokens``
   attributed to their ``first_divergent_layer``).
3. **Tool-schema ordering stability per route** — how often the SAME tool set
   arrives in a different order: ``tools_hash`` (order-sensitive) vs the
   order-independent ``tools_set_hash``. A permutation moves only the former;
   a tool-list mutation moves both. This is measurement only — NO automatic
   tool reordering is implemented (PRD §138: "Only if proven semantically
   safe"; this phase only measures).

Nothing here reorders tools, rewrites prompts, or touches warm decisions —
the view is computed on demand by ``cachepilot topology``. Only hashes,
timestamps, outcomes and route identities are read (AGENTS.md invariant 10).
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from cachepilot_core.churn import (
    LAYER_CACHE_KEY,
    LAYER_HISTORY,
    LAYER_HISTORY_TAIL,
    LAYER_MODEL,
    LAYER_ROUTE,
    LAYER_SYSTEM_PREFIX,
    LAYER_SYSTEM_SUFFIX,
    LAYER_TOOLS,
    LayeredHashes,
    changed_frequency,
)
from cachepilot_core.storage import StoredRequestEvent
from cachepilot_core.telemetry import ChurnEvent

#: PRD §24 layered content layers whose status must be attributed from churn
#: events in the stored view (the layered hashes are memory-only, PRD §30).
_LAYERED_CONTENT_LAYERS: frozenset[str] = frozenset(
    {LAYER_SYSTEM_PREFIX, LAYER_SYSTEM_SUFFIX, LAYER_HISTORY, LAYER_HISTORY_TAIL}
)

#: Display order of the topology layers (PRD §24 prefix order, then identity).
TOPOLOGY_LAYER_ORDER: tuple[str, ...] = (
    LAYER_SYSTEM_PREFIX,
    LAYER_SYSTEM_SUFFIX,
    LAYER_TOOLS,
    LAYER_HISTORY,
    LAYER_HISTORY_TAIL,
    LAYER_ROUTE,
    LAYER_MODEL,
    LAYER_CACHE_KEY,
)

#: Content layers that carry an estimated prefix-token loss (identity layers
#: have no prefix-content loss to attribute).
_LOSS_LAYERS: frozenset[str] = frozenset(
    {LAYER_SYSTEM_PREFIX, LAYER_SYSTEM_SUFFIX, LAYER_TOOLS, LAYER_HISTORY, LAYER_HISTORY_TAIL}
)


class TopologyLayerStats(BaseModel):
    """One PRD §24 layer's stability + economic value over the window.

    ``pairs`` is the denominator the layer's status was decidable for
    (consecutive request pairs; for the layered sub-layers in the stored view
    only pairs whose attribution resolved the layer count — ``attribution_based``
    marks those rows so the CLI can disclose the difference). ``changes`` is
    the decidable pairs where the layer changed. ``estimated_prefix_loss_tokens``
    sums the churn-attributed loss for content layers (None for identity
    layers and for the offline snapshot view, which carries no loss data).
    """

    model_config = ConfigDict(extra="forbid")

    layer: str = Field(..., min_length=1)
    pairs: int = Field(default=0, ge=0)
    changes: int = Field(default=0, ge=0)
    stability_pct: float | None = None
    change_frequency: str = "no observations"
    estimated_prefix_loss_tokens: int | None = None
    attribution_based: bool = False

    @property
    def unchanged_pairs(self) -> int:
        return self.pairs - self.changes


class ToolOrderingStats(BaseModel):
    """One route's tool-schema ordering stability (PRD §138, measurement only).

    ``order_permutations`` counts consecutive pairs where the tool SET stayed
    identical but arrived in a different order (``tools_hash`` moved,
    ``tools_set_hash`` stable); ``tool_set_changes`` counts pairs where the
    set itself mutated (both digests moved). ``ordering_stability_pct`` is
    the fraction of decidable pairs that did NOT permute.
    """

    model_config = ConfigDict(extra="forbid")

    route: str | None = None
    pairs: int = Field(default=0, ge=0)
    tool_set_changes: int = Field(default=0, ge=0)
    order_permutations: int = Field(default=0, ge=0)
    ordering_stability_pct: float | None = None


class TopologyReport(BaseModel):
    """The full topology measurement view (PRD §24/§138)."""

    model_config = ConfigDict(extra="forbid")

    layers: list[TopologyLayerStats] = Field(default_factory=list)
    tool_ordering: list[ToolOrderingStats] = Field(default_factory=list)
    sessions: int = Field(default=0, ge=0)
    total_pairs: int = Field(default=0, ge=0)
    churn_pairs: int = Field(default=0, ge=0)
    prefix_stability_pct: float | None = None
    #: Churn pairs whose system/history change could not be attributed to a
    #: layered sub-layer (no content-level classification was stored for it).
    attribution_gaps: int = Field(default=0, ge=0)
    #: Churn-attributed loss that could not be pinned to a content layer.
    unattributed_loss_tokens: int = Field(default=0, ge=0)


# -- pair-level primitives ----------------------------------------------------


def _diff(previous: str | None, current: str | None) -> bool | None:
    """Hash comparison: None on either side ⇒ not decidable; both None ⇒ equal."""
    if previous is None or current is None:
        return None
    return previous != current


def _sub_layer_status(
    system_hash_a: str | None,
    system_hash_b: str | None,
    history_hash_a: str | None,
    history_hash_b: str | None,
    first_divergent_layer: str | None,
) -> dict[str, bool | None]:
    """Resolve the four layered sub-layers for ONE consecutive pair.

    First-divergence semantics (the stored ``first_divergent_layer`` names the
    FIRST content layer that diverged):

    - attribution inside the system suffix ⇒ the static prefix is identical;
    - attribution inside the system prefix ⇒ the suffix is unknown (later
      layers may also have moved — the flat hashes resolve the other parents);
    - attribution at/before ``tools`` ⇒ the whole system layer is identical;
    - attribution at ``history``/``tail`` ⇒ system identical, and the sibling
      history sub-layer stays unknown only when it was attributed.
    - the flat ``system_hash`` / ``history_hash`` comparisons resolve a
      sub-layer to "unchanged" whenever the whole parent is identical.

    Returns per sub-layer: True (changed), False (decidable unchanged) or
    None (not decidable — attribution gap).
    """
    status: dict[str, bool | None] = {
        LAYER_SYSTEM_PREFIX: None,
        LAYER_SYSTEM_SUFFIX: None,
        LAYER_HISTORY: None,
        LAYER_HISTORY_TAIL: None,
    }
    system_known = system_hash_a is not None and system_hash_b is not None
    history_known = history_hash_a is not None and history_hash_b is not None
    system_identical = system_known and system_hash_a == system_hash_b
    history_identical = history_known and history_hash_a == history_hash_b

    if system_identical:
        status[LAYER_SYSTEM_PREFIX] = False
        status[LAYER_SYSTEM_SUFFIX] = False
    if history_identical:
        status[LAYER_HISTORY] = False
        status[LAYER_HISTORY_TAIL] = False

    if first_divergent_layer == LAYER_SYSTEM_SUFFIX:
        status[LAYER_SYSTEM_PREFIX] = False
        status[LAYER_SYSTEM_SUFFIX] = True
    elif first_divergent_layer == LAYER_SYSTEM_PREFIX:
        status[LAYER_SYSTEM_PREFIX] = True
        # suffix stays unknown — later layers may also have moved
    elif first_divergent_layer in (LAYER_TOOLS, LAYER_HISTORY, LAYER_HISTORY_TAIL):
        if not system_identical:
            status[LAYER_SYSTEM_PREFIX] = False
            status[LAYER_SYSTEM_SUFFIX] = False
    if first_divergent_layer == LAYER_HISTORY:
        status[LAYER_HISTORY] = True
        # tail stays unknown
    elif first_divergent_layer == LAYER_HISTORY_TAIL:
        status[LAYER_HISTORY] = False
        status[LAYER_HISTORY_TAIL] = True
    elif first_divergent_layer in (
        LAYER_SYSTEM_PREFIX,
        LAYER_SYSTEM_SUFFIX,
        LAYER_TOOLS,
    ) and not history_identical:
        status[LAYER_HISTORY] = False
        status[LAYER_HISTORY_TAIL] = False
    return status


# -- aggregation --------------------------------------------------------------


def _empty_layers() -> dict[str, TopologyLayerStats]:
    return {
        layer: TopologyLayerStats(layer=layer, change_frequency="no observations")
        for layer in TOPOLOGY_LAYER_ORDER
    }


def _finalize_report(
    report: TopologyReport,
    layers: dict[str, TopologyLayerStats],
    tool_routes: Mapping[str | None, ToolOrderingStats],
) -> TopologyReport:
    """Fill derived fields (stability %, frequency strings) and order rows.

    A report with zero observed pairs carries NO layer rows — the CLI says
    "nothing to measure" instead of printing empty rows (never fabricated).
    """
    if report.total_pairs > 0:
        for layer in TOPOLOGY_LAYER_ORDER:
            stats = layers[layer]
            if stats.pairs > 0:
                stats.stability_pct = round(100.0 * (1 - stats.changes / stats.pairs), 1)
                flags = [True] * stats.changes + [False] * stats.unchanged_pairs
                stats.change_frequency = changed_frequency(flags, subject="requests")
            report.layers.append(stats)
        report.prefix_stability_pct = round(
            100.0 * (1 - report.churn_pairs / report.total_pairs), 1
        )
        report.tool_ordering = [
            tool_routes[route]
            for route in sorted(
                tool_routes, key=lambda key: (key is not None, key or "")
            )
        ]
        for stats in report.tool_ordering:
            if stats.pairs > 0:
                stats.ordering_stability_pct = round(
                    100.0 * (1 - stats.order_permutations / stats.pairs), 1
                )
    return report


def _pair_flags_from_hashes(
    previous: LayeredHashes, current: LayeredHashes
) -> dict[str, bool | None]:
    """Exact per-layer change flags from two hash snapshots (offline path)."""
    return {
        LAYER_SYSTEM_PREFIX: _diff(previous.system_prefix_hash, current.system_prefix_hash),
        LAYER_SYSTEM_SUFFIX: _diff(previous.system_suffix_hash, current.system_suffix_hash),
        LAYER_TOOLS: _diff(previous.tools_hash, current.tools_hash),
        LAYER_HISTORY: _diff(previous.history_prefix_hash, current.history_prefix_hash),
        LAYER_HISTORY_TAIL: _diff(previous.history_tail_hash, current.history_tail_hash),
        LAYER_ROUTE: _diff(previous.route_hash, current.route_hash),
        LAYER_MODEL: _diff(previous.model, current.model),
        # snapshots carry the prompt-cache key directly: absent on both sides
        # is decidable-unchanged, a moved key is a change
        LAYER_CACHE_KEY: previous.cache_key != current.cache_key,
    }


def topology_from_snapshots(snapshots: Sequence[LayeredHashes]) -> TopologyReport:
    """Aggregate over consecutive layered-hash snapshots (offline path).

    Every layered hash is present on content-derived snapshots, so all eight
    layers are exact (``attribution_based`` False); the tool-ordering view
    uses the snapshots' ``tools_set_hash``. No loss data exists in the
    snapshot path — ``estimated_prefix_loss_tokens`` stays None (never
    fabricated).
    """
    report = TopologyReport()
    layers = _empty_layers()
    tool_routes: dict[str | None, ToolOrderingStats] = {}
    if len(snapshots) < 2:
        return _finalize_report(report, layers, tool_routes)
    report.sessions = 1
    report.total_pairs = len(snapshots) - 1
    for previous, current in itertools.pairwise(snapshots):
        flags = _pair_flags_from_hashes(previous, current)
        fp_moved = previous.cache_key != current.cache_key or any(
            value is True for value in flags.values()
        )
        if fp_moved:
            report.churn_pairs += 1
        for layer in TOPOLOGY_LAYER_ORDER:
            changed = flags[layer]
            if changed is None:
                continue
            layers[layer].pairs += 1
            if changed:
                layers[layer].changes += 1
        if (
            previous.tools_set_hash is not None
            and current.tools_set_hash is not None
            and previous.tools_hash is not None
            and current.tools_hash is not None
        ):
            route = current.route_hash if current.route_hash is not None else previous.route_hash
            route_stats = tool_routes.setdefault(route, ToolOrderingStats(route=route))
            route_stats.pairs += 1
            if previous.tools_hash != current.tools_hash:
                if previous.tools_set_hash == current.tools_set_hash:
                    route_stats.order_permutations += 1
                else:
                    route_stats.tool_set_changes += 1
    return _finalize_report(report, layers, tool_routes)


# -- stored view --------------------------------------------------------------


class TopologyStore(Protocol):
    """The minimal store surface the stored topology view needs."""

    def recent_events(self, limit: int = 50) -> list[StoredRequestEvent]: ...

    def churn_list(
        self, limit: int = 50, session_hash: str | None = None
    ) -> list[ChurnEvent]: ...


def _churn_index(
    churn_events: Sequence[ChurnEvent],
) -> dict[tuple[str | None, str, str], ChurnEvent]:
    """Index churn events by ``(session_hash, previous, new)`` fingerprint pair.

    One churn event is recorded per fingerprint transition, so the index is
    essentially a map; a repeated transition in the same session keeps the
    LAST event (the flags and losses are identical for the pair either way —
    the index only resolves layered attribution).
    """
    index: dict[tuple[str | None, str, str], ChurnEvent] = {}
    for event in churn_events:
        index[(event.session_hash, event.previous_cache_fingerprint, event.new_cache_fingerprint)] = event
    return index


def topology_from_store(
    store: TopologyStore, *, limit: int = 500
) -> TopologyReport:
    """Topology measurement over the last ``limit`` stored request events.

    Consecutive pairs are formed per session (ordered by row id); churn
    events whose (session, previous→new) fingerprint pair falls inside the
    window resolve the layered sub-layer attribution and the per-layer loss.
    Pairs whose flat hashes are missing, and churn events whose pair lies
    outside the window, are excluded — never fabricated. Attribution gaps
    and unattributed loss are disclosed on the report.
    """
    events = store.recent_events(limit=limit)
    churn_index = _churn_index(store.churn_list(limit=limit))
    report = TopologyReport()
    layers = _empty_layers()
    tool_routes: dict[str | None, ToolOrderingStats] = {}

    sessions: dict[str | None, list[StoredRequestEvent]] = {}
    for event in events:
        sessions.setdefault(event.session_hash, []).append(event)

    for session_events in sessions.values():
        session_events.sort(key=lambda event: event.id)
        report.sessions += 1
        for previous, current in itertools.pairwise(session_events):
            report.total_pairs += 1
            churn = churn_index.get(
                (
                    previous.session_hash,
                    previous.cache_fingerprint,
                    current.cache_fingerprint,
                )
            )
            first_divergent = churn.first_divergent_layer if churn is not None else None
            loss = churn.estimated_prefix_loss_tokens if churn is not None else None
            fp_moved = previous.cache_fingerprint != current.cache_fingerprint
            if fp_moved:
                report.churn_pairs += 1
            system_changed = _diff(previous.system_hash, current.system_hash)
            history_changed = _diff(previous.history_hash, current.history_hash)
            tools_changed = _diff(previous.tools_hash, current.tools_hash)
            route_changed = _diff(previous.route_hash, current.route_hash)
            model_changed = _diff(previous.model, current.model)
            cache_key_changed: bool | None = None
            if fp_moved:
                flat_moved = any(
                    flag is True
                    for flag in (
                        system_changed,
                        tools_changed,
                        history_changed,
                        route_changed,
                        model_changed,
                    )
                )
                cache_key_changed = not flat_moved

            # exact flat layers
            for layer, changed in (
                (LAYER_TOOLS, tools_changed),
                (LAYER_ROUTE, route_changed),
                (LAYER_MODEL, model_changed),
                (LAYER_CACHE_KEY, cache_key_changed),
            ):
                if changed is None:
                    continue
                layers[layer].pairs += 1
                if changed:
                    layers[layer].changes += 1

            # layered sub-layers (attributed; gaps disclosed)
            status = _sub_layer_status(
                previous.system_hash,
                current.system_hash,
                previous.history_hash,
                current.history_hash,
                first_divergent,
            )
            for sub_layer, changed in status.items():
                if changed is None:
                    continue
                stats = layers[sub_layer]
                stats.pairs += 1
                stats.attribution_based = True
                if changed:
                    stats.changes += 1
            if (system_changed is True or history_changed is True) and (
                first_divergent not in _LAYERED_CONTENT_LAYERS
            ):
                report.attribution_gaps += 1

            # loss attribution
            if first_divergent is not None and first_divergent in _LOSS_LAYERS:
                if loss is not None:
                    target = layers[first_divergent]
                    target.estimated_prefix_loss_tokens = (
                        (target.estimated_prefix_loss_tokens or 0) + loss
                    )
            elif loss is not None:
                report.unattributed_loss_tokens += loss

            # tool ordering per route
            route = current.route_hash if current.route_hash is not None else previous.route_hash
            if (
                previous.tools_set_hash is not None
                and current.tools_set_hash is not None
                and previous.tools_hash is not None
                and current.tools_hash is not None
            ):
                route_stats = tool_routes.setdefault(route, ToolOrderingStats(route=route))
                route_stats.pairs += 1
                if previous.tools_hash != current.tools_hash:
                    if previous.tools_set_hash == current.tools_set_hash:
                        route_stats.order_permutations += 1
                    else:
                        route_stats.tool_set_changes += 1

    return _finalize_report(report, layers, tool_routes)
