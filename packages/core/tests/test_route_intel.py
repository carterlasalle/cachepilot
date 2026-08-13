"""P09 route intelligence — route identity + router-miss classification (PRD §71-72, UC-5).

Pure unit coverage of the core building blocks: the PRD §71 RouteIdentity
(observable fields only, stable route_hash, lossless str round-trip), the
UC-5 RouterMissClassifier verdicts, and the guarantee that an instability
miss never refines TTL bounds (PRD §56 clean-check, PRD §72.2-72.3) plus the
``route_events`` store round-trip.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cachepilot_core.route_intel import (
    RouteChangeEvent,
    RouteIdentity,
    RouteIntelStats,
    RouteMissVerdict,
    RouterMissClassifier,
)
from cachepilot_core.storage import TelemetryStore
from cachepilot_core.telemetry import Outcome
from cachepilot_core.ttl import TTLLearner, TTLObservation

T0 = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)
ENDPOINT = "https://fake-provider.invalid/v1"
FP = "cache-fp-1"


def _key(route: str | None) -> str:
    from cachepilot_core.ttl import build_profile_key

    return build_profile_key(
        provider="fake-provider",
        model="gpt-5.2",
        api_mode="chat",
        endpoint=ENDPOINT,
        route_hash=route,
    )


def _obs(outcome: Outcome, *, age_s: float, route: str | None) -> TTLObservation:
    return TTLObservation(
        outcome=outcome,
        cache_fingerprint=FP,
        route_hash=route,
        provider="fake-provider",
        model="gpt-5.2",
        api_mode="chat",
        endpoint=ENDPOINT,
        timestamp=T0 + timedelta(seconds=age_s),
    )


# -- RouteIdentity (PRD §71) --------------------------------------------------


def test_route_identity_only_observable_fields_and_stable_hash():
    route = RouteIdentity(gateway="openrouter", upstream_provider="provider-a")
    assert route.endpoint is None
    assert route.region is None
    assert route.deployment is None
    digest = route.route_hash()
    assert digest is not None and len(digest) == 64
    # stable: identical observable fields → identical hash
    assert RouteIdentity(gateway="openrouter", upstream_provider="provider-a").route_hash() == digest
    # distinct observable fields → distinct hash
    assert RouteIdentity(gateway="openrouter", upstream_provider="provider-b").route_hash() != digest
    # nothing observable → None, never a fabricated hash
    assert RouteIdentity().route_hash() is None


def test_route_identity_str_roundtrip():
    route = RouteIdentity(
        gateway="openrouter",
        upstream_provider="provider-a",
        endpoint="https://openrouter.ai/api/v1",
        region="us-west",
        deployment="edge-1",
    )
    restored = RouteIdentity.from_str(route.to_str())
    assert restored == route
    assert restored.route_hash() == route.route_hash()
    # partial identities round-trip too
    partial = RouteIdentity(gateway="deepseek")
    assert RouteIdentity.from_str(partial.to_str()) == partial
    assert RouteIdentity.from_str(partial.to_str()).route_hash() == partial.route_hash()


def test_route_identity_rejects_unknown_fields():
    import pytest

    with pytest.raises(ValueError):
        RouteIdentity(gateway="openrouter", bogus="x")  # extra="forbid"


# -- RouterMissClassifier (PRD UC-5) ------------------------------------------


def test_classifier_route_change_with_expected_hit_is_instability():
    """same logical request, previous HIT on route A, route switched to B,
    current MISS → ROUTE_INSTABILITY (never short-TTL evidence)."""
    verdict = RouterMissClassifier().classify(
        previous_outcome=Outcome.CONFIRMED_HIT,
        previous_route_hash="route-a",
        current_outcome=Outcome.MISS_REBUILT,
        current_route_hash="route-b",
        identity_stable=True,
    )
    assert verdict is RouteMissVerdict.ROUTE_INSTABILITY


def test_classifier_same_route_miss_is_short_ttl():
    """same route, previous HIT, current MISS → SHORT_TTL (genuine TTL evidence)."""
    verdict = RouterMissClassifier().classify(
        previous_outcome=Outcome.CONFIRMED_HIT,
        previous_route_hash="route-a",
        current_outcome=Outcome.MISS_REBUILT,
        current_route_hash="route-a",
        identity_stable=True,
    )
    assert verdict is RouteMissVerdict.SHORT_TTL


def test_classifier_clean_cases():
    classifier = RouterMissClassifier()
    # route changed but the previous observation was NOT a hit (no expectation)
    assert (
        classifier.classify(
            previous_outcome=Outcome.MISS_REBUILT,
            previous_route_hash="route-a",
            current_outcome=Outcome.MISS_REBUILT,
            current_route_hash="route-b",
            identity_stable=True,
        )
        is RouteMissVerdict.CLEAN
    )
    # route changed AND the logical request content changed → CLEAN
    assert (
        classifier.classify(
            previous_outcome=Outcome.CONFIRMED_HIT,
            previous_route_hash="route-a",
            current_outcome=Outcome.MISS_REBUILT,
            current_route_hash="route-b",
            identity_stable=False,
        )
        is RouteMissVerdict.CLEAN
    )
    # observability gap: one side has no route identity → CLEAN, never instability
    assert (
        classifier.classify(
            previous_outcome=Outcome.CONFIRMED_HIT,
            previous_route_hash=None,
            current_outcome=Outcome.MISS_REBUILT,
            current_route_hash="route-b",
            identity_stable=True,
        )
        is RouteMissVerdict.CLEAN
    )
    # no previous observation → CLEAN
    assert (
        classifier.classify(
            previous_outcome=None,
            previous_route_hash=None,
            current_outcome=Outcome.MISS_REBUILT,
            current_route_hash="route-b",
            identity_stable=True,
        )
        is RouteMissVerdict.CLEAN
    )
    # a hit after a route change is not a miss at all → CLEAN
    assert (
        classifier.classify(
            previous_outcome=Outcome.CONFIRMED_HIT,
            previous_route_hash="route-a",
            current_outcome=Outcome.CONFIRMED_HIT,
            current_route_hash="route-b",
            identity_stable=True,
        )
        is RouteMissVerdict.CLEAN
    )


# -- instability miss is never TTL evidence (PRD §56, §72.2-72.3) -------------


def test_instability_miss_never_refines_ttl_bounds(tmp_path):
    store = TelemetryStore(tmp_path / "intel.db")
    learner = TTLLearner(store)
    try:
        # 1. cold observation on route A (no pair yet)
        learner.learn(_obs(Outcome.MISS_REBUILT, age_s=0.0, route="route-a"))
        # 2. verified hit on route A at idle age 10 → refines the A profile
        learner.learn(_obs(Outcome.CONFIRMED_HIT, age_s=10.0, route="route-a"))
        # 3. SAME logical fingerprint, route switched to B, miss where a hit
        #    was expected → the learner must NOT treat it as TTL evidence
        learner.learn(_obs(Outcome.MISS_REBUILT, age_s=20.0, route="route-b"))

        profile_a = store.profile_for(_key("route-a"))
        assert profile_a is not None
        assert profile_a.lower_bound_s is not None and profile_a.lower_bound_s > 0
        # the route-B miss never capped route A's upper bound
        assert profile_a.upper_bound_s is None
        # route B's context has no refined profile (confidence starts fresh)
        assert store.profile_for(_key("route-b")) is None

        observations = store.recent_ttl_observations(FP, limit=10)
        # the instability observation was recorded (pairing continuity) but
        # marked not-clean — it can never refine bounds
        assert observations[0].outcome is Outcome.MISS_REBUILT
        assert observations[0].clean is False
        assert observations[0].route_hash == "route-b"
    finally:
        store.close()


# -- route_events store round-trip (PRD §72.1, §75) ---------------------------


def test_route_event_store_roundtrip_and_stats(tmp_path):
    store = TelemetryStore(tmp_path / "events.db")
    try:
        store.record_route_event(
            RouteChangeEvent(
                timestamp=T0,
                session_hash="sess-hash-1",
                cache_fingerprint=FP,
                request_fingerprint="req-fp-1",
                previous_route_hash="route-a",
                new_route_hash="route-b",
                gateway="openrouter",
                upstream_provider="provider-b",
                endpoint=ENDPOINT,
                region="us-west",
                deployment="edge-b",
                verdict=RouteMissVerdict.ROUTE_INSTABILITY,
            )
        )
        store.record_route_event(
            RouteChangeEvent(
                timestamp=T0 + timedelta(seconds=60),
                session_hash="sess-hash-2",
                cache_fingerprint="cache-fp-2",
                request_fingerprint="req-fp-2",
                previous_route_hash="route-c",
                new_route_hash="route-d",
                verdict=RouteMissVerdict.CLEAN,
            )
        )
        events = store.recent_route_events(limit=10)
        assert len(events) == 2
        newest = events[0]
        assert newest.verdict is RouteMissVerdict.CLEAN
        assert newest.previous_route_hash == "route-c"
        oldest = events[1]
        assert oldest.verdict is RouteMissVerdict.ROUTE_INSTABILITY
        assert oldest.gateway == "openrouter"
        assert oldest.upstream_provider == "provider-b"
        assert oldest.endpoint == ENDPOINT
        assert oldest.region == "us-west"
        assert oldest.deployment == "edge-b"
        assert oldest.id is not None

        stats = store.route_intel_stats()
        assert isinstance(stats, RouteIntelStats)
        assert stats.route_switches == 2
        assert stats.instability_verdicts == 1
        assert stats.short_ttl_verdicts == 0
        assert stats.last_switch_at == T0 + timedelta(seconds=60)

        # last-for-session with a "since" window (fresh-evidence guard)
        found = store.last_route_event_for_session(
            "sess-hash-1", since=T0.timestamp()
        )
        assert found is not None and found.verdict is RouteMissVerdict.ROUTE_INSTABILITY
        # a window AFTER the event excludes it (stale evidence)
        assert (
            store.last_route_event_for_session(
                "sess-hash-1", since=(T0 + timedelta(seconds=1)).timestamp()
            )
            is None
        )
        assert store.last_route_event_for_session("nope") is None
    finally:
        store.close()
