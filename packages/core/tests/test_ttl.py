"""TTL learning — PRD §55-59, §82, §135 (Phase 8).

Pure profile refinement (§55/§57/§58), route separation (§56), the store
round-trips (§82) and the lease-scheduling wiring (§59 override hierarchy)
against a real tmp SQLite store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cachepilot_core.adapters import TTLHint
from cachepilot_core.leases import LeaseManager, LeaseSettings
from cachepilot_core.storage import TelemetryStore
from cachepilot_core.telemetry import ChurnEvent, Outcome
from cachepilot_core.ttl import (
    HIGH_CONFIDENCE_THRESHOLD,
    TTLLearner,
    TTLObservation,
    TTLProfile,
    TTLResolver,
    build_profile_key,
    endpoint_hash,
)

T0 = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)
ENDPOINT = "https://fake-provider.invalid/v1"
FP = "cache-fp-1"
ROUTE = "route-1"


def _fresh_profile(route: str = ROUTE) -> TTLProfile:
    return TTLProfile(
        provider="fake-provider",
        model="gpt-5.2",
        api_mode="chat",
        endpoint_hash=endpoint_hash(ENDPOINT),
        route_hash=route,
    )


def _key(route: str = ROUTE) -> str:
    return build_profile_key(
        provider="fake-provider",
        model="gpt-5.2",
        api_mode="chat",
        endpoint=ENDPOINT,
        route_hash=route,
    )


def _obs(
    outcome: Outcome,
    *,
    age_s: float,
    route: str = ROUTE,
    fp: str = FP,
) -> TTLObservation:
    """An observation whose timestamp is ``age_s`` after T0."""
    return TTLObservation(
        outcome=outcome,
        cache_fingerprint=fp,
        route_hash=route,
        provider="fake-provider",
        model="gpt-5.2",
        api_mode="chat",
        endpoint=ENDPOINT,
        timestamp=T0 + timedelta(seconds=age_s),
    )


def _store(tmp_path) -> TelemetryStore:
    return TelemetryStore(tmp_path / "ttl.db")


# -- pure profile refinement (PRD §55, §57, §58) ------------------------------


def test_hit_raises_lower_bound():
    profile = _fresh_profile()
    profile.observe(Outcome.CONFIRMED_HIT, 183.0)
    assert profile.lower_bound_s == 183.0
    assert profile.upper_bound_s is None
    assert profile.estimated_ttl_s == 183.0
    # a later hit at a SMALLER idle age must not shrink the bound
    profile.observe(Outcome.CONFIRMED_HIT, 90.0)
    assert profile.lower_bound_s == 183.0


def test_miss_caps_upper_bound_prd55_sequence():
    profile = _fresh_profile()
    profile.observe(Outcome.CONFIRMED_HIT, 183.0)
    profile.observe(Outcome.MISS_REBUILT, 302.0)
    assert profile.lower_bound_s == 183.0
    assert profile.upper_bound_s == 302.0
    # §55: TTL ∈ (183s, 302s]; §57: favor the lower side of the interval.
    assert profile.estimated_ttl_s == pytest.approx(183.0 + (302.0 - 183.0) * 0.35)


def test_estimate_favors_lower_side_and_uses_hint_without_upper():
    profile = _fresh_profile()
    # no evidence and no hint → unknown, never silently guessed (§59)
    assert profile.estimate() is None
    # an adapter hint is a known quantity, not a guess (§57 fallback)
    assert profile.estimate(adapter_hint=240.0) == 240.0
    profile.observe(Outcome.CONFIRMED_HIT, 183.0)
    # upper unknown → max(adapter_hint, lower_bound)
    assert profile.estimate(adapter_hint=240.0) == 240.0
    assert profile.estimate() == 183.0
    profile.observe(Outcome.MISS_REBUILT, 302.0)
    # upper known → the interval estimate wins; the hint is irrelevant
    assert profile.estimate(adapter_hint=240.0) == pytest.approx(
        183.0 + (302.0 - 183.0) * 0.35
    )


def test_confidence_increases_on_consistent_observations():
    profile = _fresh_profile()
    assert profile.confidence == 0.5
    profile.observe(Outcome.CONFIRMED_HIT, 100.0)
    profile.observe(Outcome.CONFIRMED_HIT, 200.0)
    assert profile.confidence == pytest.approx(0.60)
    assert profile.sample_count == 2


def test_unverified_lowers_confidence_but_counts_as_observation():
    profile = _fresh_profile()
    profile.observe(Outcome.CONFIRMED_HIT, 100.0)
    profile.observe(Outcome.SUCCESS_UNVERIFIED, None)
    assert profile.confidence == pytest.approx(0.50)
    assert profile.sample_count == 2
    # bounds are untouched by an unverified response
    assert profile.lower_bound_s == 100.0


def test_inconsistent_evidence_lowers_confidence():
    profile = _fresh_profile()
    profile.observe(Outcome.MISS_REBUILT, 200.0)  # TTL ≤ 200
    before = profile.confidence
    profile.observe(Outcome.CONFIRMED_HIT, 250.0)  # TTL > 250 — contradicts
    assert profile.confidence < before
    # both bounds stay honest
    assert profile.lower_bound_s == 250.0
    assert profile.upper_bound_s == 200.0


def test_failed_is_not_ttl_evidence():
    profile = _fresh_profile()
    profile.observe(Outcome.FAILED, 100.0)
    assert profile.sample_count == 0
    assert profile.lower_bound_s is None
    assert profile.upper_bound_s is None
    assert profile.confidence == 0.5


def test_profile_key_includes_all_route_components():
    key = _key("route-1")
    assert endpoint_hash(ENDPOINT) in key
    assert "fake-provider" in key and "gpt-5.2" in key and "chat" in key
    assert key != _key("route-2")
    assert _fresh_profile().profile_key == key
    # a different provider/model/api_mode changes the key
    other = build_profile_key(
        provider="anthropic",
        model="gpt-5.2",
        api_mode="chat",
        endpoint=ENDPOINT,
        route_hash="route-1",
    )
    assert other != key


# -- learner (PRD §55-56) -----------------------------------------------------


def test_learner_first_observation_records_without_refining(tmp_path):
    store = _store(tmp_path)
    learner = TTLLearner(store)
    assert learner.learn(_obs(Outcome.CONFIRMED_HIT, age_s=0.0)) is None
    assert store.list_profiles() == []
    row = store.last_ttl_observation(FP)
    assert row is not None
    assert row.idle_age_s is None
    assert not row.clean
    store.close()


def test_learner_hit_pair_raises_lower_bound(tmp_path):
    store = _store(tmp_path)
    learner = TTLLearner(store)
    learner.learn(_obs(Outcome.CONFIRMED_HIT, age_s=0.0))
    profile = learner.learn(_obs(Outcome.CONFIRMED_HIT, age_s=183.0))
    assert profile is not None
    assert profile.lower_bound_s == 183.0
    assert profile.sample_count == 1
    assert profile.confidence == pytest.approx(0.55)
    row = store.last_ttl_observation(FP)
    assert row is not None and row.clean and row.idle_age_s == 183.0
    store.close()


def test_learner_prd55_full_sequence(tmp_path):
    store = _store(tmp_path)
    learner = TTLLearner(store)
    learner.learn(_obs(Outcome.CONFIRMED_HIT, age_s=0.0))
    learner.learn(_obs(Outcome.CONFIRMED_HIT, age_s=183.0))
    # §55 example: the miss arrives at idle age 302s — i.e. 302s AFTER the
    # previous observation (t=183) → absolute timestamp t=485.
    profile = learner.learn(_obs(Outcome.MISS_REBUILT, age_s=183.0 + 302.0))
    assert profile is not None
    assert profile.lower_bound_s == 183.0
    assert profile.upper_bound_s == 302.0
    assert profile.estimated_ttl_s == pytest.approx(183.0 + (302.0 - 183.0) * 0.35)
    assert profile.sample_count == 2
    store.close()


def test_learner_route_change_never_corrupts_old_route(tmp_path):
    """PRD §56: a miss under a changed route never touches the old bounds."""
    store = _store(tmp_path)
    learner = TTLLearner(store)
    learner.learn(_obs(Outcome.CONFIRMED_HIT, age_s=0.0, route="route-a"))
    p1 = learner.learn(_obs(Outcome.CONFIRMED_HIT, age_s=183.0, route="route-a"))
    assert p1 is not None and p1.lower_bound_s == 183.0
    # Route change → new cache identity (route is part of the fingerprint);
    # the miss at a tiny idle age must NOT cap route-a's upper bound.
    learner.learn(
        _obs(Outcome.MISS_REBUILT, age_s=50.0, route="route-b", fp="cache-fp-2")
    )
    after = store.profile_for(_key("route-a"))
    assert after is not None
    assert after.lower_bound_s == 183.0
    assert after.upper_bound_s is None
    assert after.confidence == p1.confidence  # old profile untouched
    store.close()


def test_learner_route_change_resets_new_context_confidence(tmp_path):
    """PRD §56: the new context's confidence starts fresh on a route change."""
    store = _store(tmp_path)
    learner = TTLLearner(store)
    learner.learn(_obs(Outcome.CONFIRMED_HIT, age_s=0.0, route="route-a"))
    learner.learn(_obs(Outcome.CONFIRMED_HIT, age_s=183.0, route="route-a"))
    # route-b already carries its own learned profile
    store.upsert_profile(
        TTLProfile(
            provider="fake-provider",
            model="gpt-5.2",
            api_mode="chat",
            endpoint_hash=endpoint_hash(ENDPOINT),
            route_hash="route-b",
            lower_bound_s=10.0,
            confidence=0.8,
            sample_count=4,
        )
    )
    # same cache fingerprint, but now observed under route-b → not clean;
    # the new context's confidence starts fresh, bounds untouched.
    profile = learner.learn(
        _obs(Outcome.MISS_REBUILT, age_s=400.0, route="route-b", fp=FP)
    )
    assert profile is not None
    assert profile.route_hash == "route-b"
    assert profile.confidence == 0.5
    assert profile.lower_bound_s == 10.0
    assert profile.upper_bound_s is None  # the miss was not applied
    store.close()


def test_learner_churn_between_invalidates_pair(tmp_path):
    store = _store(tmp_path)
    learner = TTLLearner(store)
    learner.learn(_obs(Outcome.CONFIRMED_HIT, age_s=0.0))
    store.record_churn(
        ChurnEvent(
            timestamp=T0 + timedelta(seconds=100),
            session_hash="s1",
            previous_cache_fingerprint="other-fp",
            new_cache_fingerprint=FP,
        )
    )
    assert learner.learn(_obs(Outcome.CONFIRMED_HIT, age_s=183.0)) is None
    row = store.last_ttl_observation(FP)
    assert row is not None and not row.clean
    store.close()


def test_learner_failed_records_but_never_refines(tmp_path):
    store = _store(tmp_path)
    learner = TTLLearner(store)
    learner.learn(_obs(Outcome.CONFIRMED_HIT, age_s=0.0))
    learner.learn(_obs(Outcome.CONFIRMED_HIT, age_s=183.0))
    profile = learner.learn(_obs(Outcome.FAILED, age_s=250.0))
    assert profile is not None
    assert profile.lower_bound_s == 183.0
    assert profile.sample_count == 1  # FAILED adds no sample
    last = store.last_ttl_observation(FP)
    assert last is not None and last.outcome is Outcome.FAILED
    store.close()


# -- store round-trips (PRD §82) ----------------------------------------------


def test_profile_roundtrip_and_upsert(tmp_path):
    store = _store(tmp_path)
    profile = TTLProfile(
        provider="fake-provider",
        model="gpt-5.2",
        api_mode="chat",
        endpoint_hash=endpoint_hash(ENDPOINT),
        route_hash=ROUTE,
        lower_bound_s=183.0,
        upper_bound_s=302.0,
        estimated_ttl_s=224.65,
        confidence=0.7,
        sample_count=5,
        updated_at=T0,
    )
    store.upsert_profile(profile)
    assert store.profile_for(profile.profile_key) == profile
    # upsert updates the same row in place
    profile.confidence = 0.75
    store.upsert_profile(profile)
    assert store.profile_for(profile.profile_key).confidence == 0.75
    assert len(store.list_profiles()) == 1
    store.close()


def test_ttl_observation_roundtrip_and_recent(tmp_path):
    store = _store(tmp_path)
    rid = store.record_ttl_observation(
        timestamp=T0,
        cache_fingerprint=FP,
        route_hash=ROUTE,
        idle_age_s=183.0,
        outcome=Outcome.CONFIRMED_HIT,
        clean=True,
    )
    row = store.last_ttl_observation(FP)
    assert row is not None
    assert row.id == rid
    assert row.outcome is Outcome.CONFIRMED_HIT
    assert row.clean is True
    assert row.idle_age_s == 183.0
    assert row.timestamp == T0
    store.record_ttl_observation(
        timestamp=T0 + timedelta(seconds=302),
        cache_fingerprint=FP,
        route_hash=ROUTE,
        idle_age_s=302.0,
        outcome=Outcome.MISS_REBUILT,
        clean=True,
    )
    rows = store.recent_ttl_observations(FP)
    assert len(rows) == 2
    last = store.last_ttl_observation(FP)
    assert last is not None and last.outcome is Outcome.MISS_REBUILT
    assert store.churn_between(FP, T0, T0 + timedelta(seconds=302)) is False
    store.close()


# -- resolver (PRD §59 hierarchy) ---------------------------------------------


def _resolver(store: TelemetryStore, **kwargs) -> TTLResolver:
    return TTLResolver(
        profile_lookup=store.profile_for,
        force_seconds=kwargs.pop("force_seconds", None),
        default_ttl_s=kwargs.pop("default_ttl_s", 300.0),
        minimum_samples=kwargs.pop("minimum_samples", 3),
    )


def _resolve(resolver: TTLResolver, adapter_hint: TTLHint | None = None):
    return resolver.resolve(
        provider="fake-provider",
        model="gpt-5.2",
        api_mode="chat",
        endpoint=ENDPOINT,
        route_hash=ROUTE,
        adapter_hint=adapter_hint,
    )


def _learn_four_hits(store: TelemetryStore) -> None:
    """5 observations → 4 clean hit pairs → lower=400, confidence=0.70."""
    learner = TTLLearner(store)
    for age in (0.0, 100.0, 300.0, 600.0, 1000.0):
        learner.learn(_obs(Outcome.CONFIRMED_HIT, age_s=age))


def test_resolver_force_seconds_wins(tmp_path):
    store = _store(tmp_path)
    resolver = _resolver(
        store,
        force_seconds=500.0,
        default_ttl_s=300.0,
    )
    result = _resolve(
        resolver,
        adapter_hint=TTLHint(ttl_s=200.0, confidence=0.6, source="adapter"),
    )
    assert result.ttl_s == 500.0
    assert result.source == "force"
    store.close()


def test_resolver_learned_beats_adapter_hint(tmp_path):
    store = _store(tmp_path)
    _learn_four_hits(store)
    resolver = _resolver(store)
    result = _resolve(
        resolver,
        adapter_hint=TTLHint(ttl_s=200.0, confidence=0.6, source="adapter"),
    )
    assert result.source == "learned"
    assert result.ttl_s == 400.0  # lower bound 400 → estimate 400
    assert result.confidence >= HIGH_CONFIDENCE_THRESHOLD
    store.close()


def test_resolver_low_confidence_falls_back_to_hint(tmp_path):
    store = _store(tmp_path)
    learner = TTLLearner(store)
    learner.learn(_obs(Outcome.CONFIRMED_HIT, age_s=0.0))
    learner.learn(_obs(Outcome.CONFIRMED_HIT, age_s=100.0))  # confidence 0.55
    resolver = _resolver(store)
    result = _resolve(
        resolver,
        adapter_hint=TTLHint(ttl_s=240.0, confidence=0.6, source="adapter"),
    )
    assert result.source == "adapter_hint"
    assert result.ttl_s == 240.0
    store.close()


def test_resolver_minimum_samples_gate(tmp_path):
    store = _store(tmp_path)
    _learn_four_hits(store)  # 4 samples, confidence 0.70
    resolver = TTLResolver(store.profile_for, default_ttl_s=300.0, minimum_samples=5)
    result = _resolve(resolver)
    assert result.source == "default"
    assert result.ttl_s == 300.0
    store.close()


def test_resolver_no_profile_falls_back_to_default(tmp_path):
    store = _store(tmp_path)
    resolver = _resolver(store)
    result = _resolve(resolver)
    assert result.source == "default"
    assert result.ttl_s == 300.0
    store.close()


# -- lease scheduling wiring (PRD §59) ----------------------------------------


def test_lease_creation_uses_learned_ttl(tmp_path):
    store = _store(tmp_path)
    _learn_four_hits(store)
    manager = LeaseManager(
        settings=LeaseSettings(default_ttl_s=300.0),
        ttl_resolver=_resolver(store),
    )
    lease = manager.find_or_create_lease(
        session_id="sess-1",
        provider="fake-provider",
        model="gpt-5.2",
        api_mode="chat",
        base_url=ENDPOINT,
        auth_scope_hash="auth-1",
        route_fingerprint=ROUTE,
        request_fingerprint="req-fp",
        cache_fingerprint="cache-fp-1",
        system_fingerprint="sys-fp",
        tools_fingerprint="tools-fp",
        history_prefix_fingerprint="hist-fp",
    )
    assert lease.estimated_ttl_s == 400.0
    assert lease.ttl_confidence == pytest.approx(0.70)
    store.close()


def test_refresh_ttl_picks_up_new_profile(tmp_path):
    store = _store(tmp_path)
    manager = LeaseManager(
        settings=LeaseSettings(default_ttl_s=300.0),
        ttl_resolver=_resolver(store),
    )
    lease = manager.find_or_create_lease(
        session_id="sess-1",
        provider="fake-provider",
        model="gpt-5.2",
        api_mode="chat",
        base_url=ENDPOINT,
        auth_scope_hash="auth-1",
        route_fingerprint=ROUTE,
        request_fingerprint="req-fp",
        cache_fingerprint="cache-fp-1",
        system_fingerprint="sys-fp",
        tools_fingerprint="tools-fp",
        history_prefix_fingerprint="hist-fp",
    )
    assert lease.estimated_ttl_s == 300.0  # no profile yet → default
    _learn_four_hits(store)
    manager.refresh_ttl(lease.lease_id)
    assert lease.estimated_ttl_s == 400.0
    assert lease.ttl_confidence == pytest.approx(0.70)
    store.close()


def test_lease_creation_without_resolver_uses_bootstrap_default():
    manager = LeaseManager(settings=LeaseSettings(default_ttl_s=300.0))
    lease = manager.find_or_create_lease(
        session_id="sess-1",
        provider="fake-provider",
        model="gpt-5.2",
        api_mode="chat",
        base_url=ENDPOINT,
        auth_scope_hash="auth-1",
        route_fingerprint=ROUTE,
        request_fingerprint="req-fp",
        cache_fingerprint="cache-fp-1",
        system_fingerprint="sys-fp",
        tools_fingerprint="tools-fp",
        history_prefix_fingerprint="hist-fp",
    )
    assert lease.estimated_ttl_s == 300.0
    assert lease.ttl_confidence == 0.5


def test_lease_settings_force_seconds(tmp_path):
    settings = LeaseSettings(ttl_force_seconds=500.0)
    store = _store(tmp_path)
    manager = LeaseManager(
        settings=settings,
        ttl_resolver=_resolver(store, force_seconds=settings.ttl_force_seconds),
    )
    lease = manager.find_or_create_lease(
        session_id="sess-1",
        provider="fake-provider",
        model="gpt-5.2",
        api_mode="chat",
        base_url=ENDPOINT,
        auth_scope_hash="auth-1",
        route_fingerprint=ROUTE,
        request_fingerprint="req-fp",
        cache_fingerprint="cache-fp-1",
        system_fingerprint="sys-fp",
        tools_fingerprint="tools-fp",
        history_prefix_fingerprint="hist-fp",
    )
    assert lease.estimated_ttl_s == 500.0
    store.close()
