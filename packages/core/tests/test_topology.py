"""P11 cross-request prefix topology + tool-ordering stability (PRD §24, §138).

Unit coverage of the topology measurement view: per-layer change frequency /
stability over consecutive request pairs (exact from layered snapshots,
attributed from stored churn events), per-layer estimated prefix-token value,
per-route tool-schema ordering stability (permutation vs set change via the
order-independent tools_set_hash), and the honesty rules (no fabricated rows,
attribution gaps disclosed, identity layers carry no prefix loss).
"""

from __future__ import annotations

from datetime import UTC, datetime

from cachepilot_core.churn import (
    LAYER_HISTORY_TAIL,
    LAYER_SYSTEM_SUFFIX,
    LAYER_TOOLS,
    LayeredHashes,
    RequestContent,
)
from cachepilot_core.storage import TelemetryStore
from cachepilot_core.telemetry import ChurnEvent, Outcome, TelemetryEvent
from cachepilot_core.topology import (
    LAYER_CACHE_KEY,
    LAYER_HISTORY,
    LAYER_MODEL,
    LAYER_ROUTE,
    LAYER_SYSTEM_PREFIX,
    TopologyLayerStats,
    TopologyReport,
    topology_from_snapshots,
    topology_from_store,
)
from cachepilot_core.usage import TokenUsage

_T0 = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)


def _content(**overrides) -> RequestContent:
    base = RequestContent(
        system="You are a helpful assistant.",
        messages=[{"role": "user", "content": "question one"}],
        tools=[{"type": "function", "function": {"name": "get_weather"}}],
        route_hash="route-a",
        model="gpt-5.2",
    )
    return base.model_copy(update=overrides)


def _hashes(**overrides) -> LayeredHashes:
    return _content(**overrides).to_hashes()


def _event(
    *,
    session: str,
    fp: str,
    cache_key: str | None = None,
    timestamp: datetime | None = None,
    **content_overrides,
) -> TelemetryEvent:
    content = _content(**content_overrides)
    hashes = content.to_hashes()
    return TelemetryEvent(
        request_fingerprint=f"req-{fp}",
        cache_fingerprint=fp if cache_key is None else f"{fp}:{cache_key}",
        provider="fake-provider",
        model=content.model or "gpt-5.2",
        route_hash=hashes.route_hash,
        usage=TokenUsage(),
        outcome=Outcome.CONFIRMED_HIT,
        request_kind="normal",
        session_hash=session,
        timestamp=timestamp if timestamp is not None else _T0,
        system_hash=hashes.system_hash,
        tools_hash=hashes.tools_hash,
        history_hash=hashes.history_hash,
        tools_set_hash=hashes.tools_set_hash,
    )


# -- snapshot path (exact layered attribution) ---------------------------------


def test_topology_snapshots_identical_requests_fully_stable():
    report = topology_from_snapshots([_hashes(), _hashes(), _hashes()])
    assert report.total_pairs == 2
    assert report.churn_pairs == 0
    assert report.sessions == 1
    assert report.prefix_stability_pct == 100.0
    for stats in report.layers:
        assert stats.pairs == 2
        assert stats.changes == 0
        assert stats.stability_pct == 100.0
        assert stats.estimated_prefix_loss_tokens is None  # no loss data offline
        assert stats.attribution_based is False
    assert report.tool_ordering[0].pairs == 2
    assert report.tool_ordering[0].order_permutations == 0
    assert report.tool_ordering[0].ordering_stability_pct == 100.0


def test_topology_snapshots_suffix_only_churn():
    previous = _hashes(system="You are helpful.\nCurrent time: 3:14 PM\nBe concise.")
    current = _hashes(system="You are helpful.\nCurrent time: 3:15 PM\nBe concise.")
    report = topology_from_snapshots([previous, current])
    by_layer = {stats.layer: stats for stats in report.layers}
    assert report.total_pairs == 1
    assert report.churn_pairs == 1
    assert report.prefix_stability_pct == 0.0
    assert by_layer[LAYER_SYSTEM_PREFIX].changes == 0
    assert by_layer[LAYER_SYSTEM_SUFFIX].changes == 1
    assert by_layer[LAYER_TOOLS].changes == 0
    assert by_layer[LAYER_HISTORY].changes == 0
    assert by_layer[LAYER_HISTORY_TAIL].changes == 0
    assert by_layer[LAYER_ROUTE].changes == 0
    assert by_layer[LAYER_MODEL].changes == 0
    assert by_layer[LAYER_CACHE_KEY].changes == 0


def test_topology_snapshots_tools_permutation_is_measured():
    tools_a = [{"type": "function", "function": {"name": "get_weather"}}]
    tools_b = [{"type": "function", "function": {"name": "get_time"}}]
    # same tool SET, different order → the set digest is stable
    permuted = _hashes(tools=[tools_a[0], tools_b[0]])
    permuted_back = _hashes(tools=[tools_b[0], tools_a[0]])
    assert permuted.tools_set_hash == permuted_back.tools_set_hash
    assert permuted.tools_hash != permuted_back.tools_hash
    report = topology_from_snapshots([permuted, permuted_back])
    by_layer = {stats.layer: stats for stats in report.layers}
    # the tools LAYER moved (the provider cache key did too — order-sensitive)
    assert by_layer[LAYER_TOOLS].changes == 1
    ordering = report.tool_ordering[0]
    assert ordering.pairs == 1
    assert ordering.order_permutations == 1
    assert ordering.tool_set_changes == 0
    assert ordering.ordering_stability_pct == 0.0


def test_topology_snapshots_set_change_not_permutation():
    tools_a = [{"type": "function", "function": {"name": "get_weather"}}]
    tools_b = [{"type": "function", "function": {"name": "get_time"}}]
    previous = _hashes(tools=tools_a)
    current = _hashes(tools=tools_b)
    report = topology_from_snapshots([previous, current])
    ordering = report.tool_ordering[0]
    assert ordering.tool_set_changes == 1
    assert ordering.order_permutations == 0
    assert ordering.ordering_stability_pct == 100.0


def test_topology_snapshots_route_change():
    report = topology_from_snapshots([_hashes(), _hashes(route_hash="route-b")])
    by_layer = {stats.layer: stats for stats in report.layers}
    assert report.churn_pairs == 1
    assert by_layer[LAYER_ROUTE].changes == 1
    assert by_layer[LAYER_MODEL].changes == 0


def test_topology_snapshots_empty_and_single():
    empty = topology_from_snapshots([])
    assert empty.total_pairs == 0
    assert empty.layers == []
    single = topology_from_snapshots([_hashes()])
    assert single.total_pairs == 0
    assert single.prefix_stability_pct is None


def test_topology_layers_always_in_prd_24_order():
    report = topology_from_snapshots([_hashes(), _hashes()])
    assert [stats.layer for stats in report.layers] == [
        LAYER_SYSTEM_PREFIX,
        LAYER_SYSTEM_SUFFIX,
        LAYER_TOOLS,
        LAYER_HISTORY,
        LAYER_HISTORY_TAIL,
        LAYER_ROUTE,
        LAYER_MODEL,
        LAYER_CACHE_KEY,
    ]


# -- stored view (attributed layered sub-layers + churn losses) ---------------


def _seed_store(
    store: TelemetryStore,
    *,
    gap: bool = False,
    route: str = "route-a",
) -> None:
    """Session s1: 4 consecutive requests → 3 churn pairs.

    - e1→e2: dynamic system suffix churn (loss 8000);
    - e2→e3: tool-schema set change (loss 2000);
    - e3→e4: recent-conversation-tail churn (loss 1500) — a pure tool ORDER
      permutation happens between e3 and e4 too (tools_set_hash stable).
    ``gap=True`` drops the layered attribution of the first churn event.
    """
    suffix_current = "You are helpful.\nCurrent time: 3:15 PM\nBe concise."
    tools_b = [
        {"type": "function", "function": {"name": "get_weather"}},
        {"type": "function", "function": {"name": "get_time"}},
    ]

    e1 = _event(session="s1", fp="fp-1", route_hash=route)
    e2 = _event(session="s1", fp="fp-2", system=suffix_current, route_hash=route)
    e3 = _event(session="s1", fp="fp-3", system=suffix_current, tools=tools_b, route_hash=route)
    # same tool SET as e3 in a different order → set hash stable
    e4 = _event(session="s1", fp="fp-4", system=suffix_current, tools=list(reversed(tools_b)), route_hash=route)
    for index, event in enumerate((e1, e2, e3, e4)):
        store.record_request(event)

    store.record_churn(
        ChurnEvent(
            timestamp=_T0,
            session_hash="s1",
            previous_cache_fingerprint="fp-1",
            new_cache_fingerprint="fp-2",
            system_changed=True,
            likely_cause="system_suffix_churn (volatile value in dynamic system suffix)",
            confidence=0.85,
            estimated_prefix_loss_tokens=8000,
            first_divergent_offset=19,
            first_divergent_layer=None if gap else LAYER_SYSTEM_SUFFIX,
        )
    )
    store.record_churn(
        ChurnEvent(
            timestamp=_T0,
            session_hash="s1",
            previous_cache_fingerprint="fp-2",
            new_cache_fingerprint="fp-3",
            tools_changed=True,
            likely_cause="tool list mutation",
            confidence=0.80,
            estimated_prefix_loss_tokens=2000,
            first_divergent_offset=3,
            first_divergent_layer=LAYER_TOOLS,
        )
    )
    store.record_churn(
        ChurnEvent(
            timestamp=_T0,
            session_hash="s1",
            previous_cache_fingerprint="fp-3",
            new_cache_fingerprint="fp-4",
            history_changed=True,
            likely_cause="history-boundary churn (recent conversation tail moved)",
            confidence=0.70,
            estimated_prefix_loss_tokens=1500,
            first_divergent_offset=9,
            first_divergent_layer=LAYER_HISTORY_TAIL,
        )
    )


def _by_layer(report: TopologyReport) -> dict[str, TopologyLayerStats]:
    return {stats.layer: stats for stats in report.layers}


def test_topology_store_pairs_and_stability(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    try:
        _seed_store(store)
        report = topology_from_store(store, limit=100)
    finally:
        store.close()
    assert report.total_pairs == 3
    assert report.churn_pairs == 3
    assert report.sessions == 1
    assert report.prefix_stability_pct == 0.0
    assert report.attribution_gaps == 0
    assert report.unattributed_loss_tokens == 0


def test_topology_store_layered_attribution_and_loss(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    try:
        _seed_store(store)
        report = topology_from_store(store, limit=100)
    finally:
        store.close()
    layers = _by_layer(report)
    suffix = layers[LAYER_SYSTEM_SUFFIX]
    assert suffix.pairs == 3
    assert suffix.changes == 1
    assert suffix.attribution_based is True
    assert suffix.estimated_prefix_loss_tokens == 8000
    assert suffix.stability_pct == round(100 * (1 - 1 / 3), 1)
    prefix = layers[LAYER_SYSTEM_PREFIX]
    assert prefix.changes == 0
    assert prefix.estimated_prefix_loss_tokens is None  # no churn attributed
    tools = layers[LAYER_TOOLS]
    assert tools.changes == 2  # set change e2→e3 + order permutation e3→e4
    assert tools.estimated_prefix_loss_tokens == 2000
    tail = layers[LAYER_HISTORY_TAIL]
    assert tail.changes == 1
    assert tail.estimated_prefix_loss_tokens == 1500
    history = layers[LAYER_HISTORY]
    assert history.changes == 0
    assert layers[LAYER_ROUTE].changes == 0
    assert layers[LAYER_MODEL].changes == 0
    assert layers[LAYER_CACHE_KEY].changes == 0
    # identity layers carry no prefix-token loss — honest n/a
    assert layers[LAYER_ROUTE].estimated_prefix_loss_tokens is None


def test_topology_store_tool_ordering_per_route(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    try:
        _seed_store(store)
        report = topology_from_store(store, limit=100)
    finally:
        store.close()
    assert len(report.tool_ordering) == 1
    ordering = report.tool_ordering[0]
    assert ordering.route == "route-a"
    assert ordering.pairs == 3
    assert ordering.tool_set_changes == 1  # e2→e3 (different tool)
    assert ordering.order_permutations == 1  # e3→e4 (same set, new order)
    assert ordering.ordering_stability_pct == round(100 * (1 - 1 / 3), 1)


def test_topology_store_attribution_gap_disclosed(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    try:
        _seed_store(store, gap=True)
        report = topology_from_store(store, limit=100)
    finally:
        store.close()
    assert report.attribution_gaps == 1
    # the un-attributed first pair cannot resolve the system sub-layers
    layers = _by_layer(report)
    assert layers[LAYER_SYSTEM_SUFFIX].pairs == 2  # only the decidable pairs
    assert layers[LAYER_SYSTEM_SUFFIX].changes == 0
    # its loss is disclosed as unattributed, never silently dropped
    assert report.unattributed_loss_tokens == 8000


def test_topology_store_cache_key_residual_churn(tmp_path):
    """A fingerprint move with NO tracked flat layer changing is cache-key /
    untracked-identity churn (PRD §137 residual)."""
    store = TelemetryStore(tmp_path / "t.db")
    try:
        store.record_request(_event(session="s2", fp="fp-a"))
        store.record_request(
            _event(session="s2", fp="fp-b", cache_key="other")
        )
        report = topology_from_store(store, limit=100)
    finally:
        store.close()
    layers = _by_layer(report)
    assert layers[LAYER_CACHE_KEY].pairs == 1
    assert layers[LAYER_CACHE_KEY].changes == 1
    assert report.churn_pairs == 1


def test_topology_store_empty_db(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    try:
        report = topology_from_store(store, limit=100)
    finally:
        store.close()
    assert report.total_pairs == 0
    assert report.layers == []
    assert report.tool_ordering == []
    assert report.prefix_stability_pct is None


def test_topology_store_requires_consecutive_same_session_pairs(tmp_path):
    """Events from DIFFERENT sessions are never paired together."""
    store = TelemetryStore(tmp_path / "t.db")
    try:
        store.record_request(_event(session="s1", fp="fp-1"))
        store.record_request(_event(session="s2", fp="fp-2"))
        store.record_request(_event(session="s1", fp="fp-3"))
        report = topology_from_store(store, limit=100)
    finally:
        store.close()
    # s1: (fp-1 → fp-3) is one pair; s2's lone event pairs with nothing
    assert report.sessions == 2
    assert report.total_pairs == 1
    assert report.churn_pairs == 1


def test_topology_tools_set_hash_roundtrips_through_store(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    try:
        event = _event(session="s1", fp="fp-1")
        store.record_request(event)
        rows = store.recent_events(limit=10)
        assert rows[0].tools_set_hash == event.tools_set_hash
        assert rows[0].tools_set_hash is not None
    finally:
        store.close()


def test_topology_report_models_extra_forbidden():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TopologyReport(bogus=True)
