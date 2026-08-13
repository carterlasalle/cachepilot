"""P11 survival model — Kaplan-Meier-style P(cache survives | age) (PRD §99, §138).

Unit coverage of the empirical survival estimator: product-limit semantics
(CONFIRMED_HIT = censored 'survived to age A', MISS_REBUILT = death 'died at
age A'), monotonic non-increasing survival, honesty (no observations / beyond
the observed horizon ⇒ None, never fabricated), median survival, and the
per-route-profile store integration (CLEAN observations only — PRD §56).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cachepilot_core.storage import TelemetryStore
from cachepilot_core.survival import (
    SURVIVAL_OUTCOMES,
    SurvivalCurve,
    SurvivalObservation,
    SurvivalStep,
    curve_from_observations,
    curve_from_profile,
    estimate_survival,
)
from cachepilot_core.telemetry import Outcome
from cachepilot_core.ttl import TTLProfile, endpoint_hash

_T0 = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)
_ROUTE = "route-abc"
_ENDPOINT_HASH = endpoint_hash("https://fake-provider.invalid/v1")


def _obs(age: float, outcome: Outcome) -> SurvivalObservation:
    return SurvivalObservation(idle_age_s=age, outcome=outcome)


# -- estimator ----------------------------------------------------------------


def test_survival_outcomes_are_hit_and_miss_only():
    assert SURVIVAL_OUTCOMES == frozenset({Outcome.CONFIRMED_HIT, Outcome.MISS_REBUILT})


def test_estimate_survival_no_observations_is_empty():
    curve = estimate_survival([])
    assert curve.empty
    assert curve.steps == []
    assert curve.sample_count == 0
    assert curve.horizon_s is None
    assert curve.survival_at(10.0) is None
    assert curve.median_survival_s() is None


def test_estimate_survival_ignores_non_survival_outcomes_and_negative_ages():
    curve = estimate_survival(
        [
            _obs(100.0, Outcome.SUCCESS_UNVERIFIED),
            _obs(200.0, Outcome.FAILED),
            _obs(-5.0, Outcome.MISS_REBUILT),
        ]
    )
    assert curve.empty  # nothing usable — never fabricated


def test_estimate_survival_single_death():
    curve = estimate_survival([_obs(183.0, Outcome.MISS_REBUILT)])
    assert curve.sample_count == 1
    assert curve.steps == [SurvivalStep(age_s=183.0, survival=0.0, at_risk=1, events=1)]
    # before the death age the estimate is 1.0; at/after it 0.0
    assert curve.survival_at(100.0) == 1.0
    assert curve.survival_at(183.0) == 0.0
    assert curve.survival_at(500.0) is None  # beyond the observed horizon


def test_estimate_survival_single_hit_is_censored_not_a_death():
    curve = estimate_survival([_obs(183.0, Outcome.CONFIRMED_HIT)])
    assert curve.sample_count == 1
    assert curve.steps == []  # a hit never lowers survival (right-censored)
    assert curve.survival_at(100.0) == 1.0
    assert curve.survival_at(183.0) == 1.0
    assert curve.survival_at(200.0) is None  # beyond the horizon (183)


def test_estimate_survival_km_product_limit():
    """Two deaths at age 100: S = 1 - 2/5 = 0.6. The censored hits at 100 and
    300 leave the risk set, so the death at 500 has only 1 at risk:
    S = 0.6 * (1 - 1/1) = 0."""
    curve = estimate_survival(
        [
            _obs(100.0, Outcome.MISS_REBUILT),
            _obs(100.0, Outcome.MISS_REBUILT),
            _obs(100.0, Outcome.CONFIRMED_HIT),
            _obs(300.0, Outcome.CONFIRMED_HIT),
            _obs(500.0, Outcome.MISS_REBUILT),
        ]
    )
    assert curve.sample_count == 5
    assert curve.steps == [
        SurvivalStep(age_s=100.0, survival=0.6, at_risk=5, events=2),
        SurvivalStep(age_s=500.0, survival=0.0, at_risk=1, events=1),
    ]
    assert curve.horizon_s == 500.0
    assert curve.survival_at(0) == 1.0
    assert curve.survival_at(100.0) == 0.6
    assert curve.survival_at(300.0) == 0.6  # between steps — no death observed
    assert curve.survival_at(500.0) == 0.0
    assert curve.survival_at(501.0) is None  # beyond horizon — honest


def test_estimate_survival_censored_hits_leave_the_risk_set():
    """Censoring reduces at-risk for LATER deaths: 2 hits at 100 leave the
    risk set, then the death at 200 has only 1 at risk → S = 1 - 1/1 = 0."""
    curve = estimate_survival(
        [
            _obs(100.0, Outcome.CONFIRMED_HIT),
            _obs(100.0, Outcome.CONFIRMED_HIT),
            _obs(200.0, Outcome.MISS_REBUILT),
        ]
    )
    assert curve.steps == [SurvivalStep(age_s=200.0, survival=0.0, at_risk=1, events=1)]


def test_estimate_survival_median():
    curve = estimate_survival(
        [
            _obs(100.0, Outcome.MISS_REBUILT),
            _obs(100.0, Outcome.MISS_REBUILT),
            _obs(300.0, Outcome.CONFIRMED_HIT),
            _obs(400.0, Outcome.CONFIRMED_HIT),
        ]
    )
    assert curve.median_survival_s() == 100.0  # survival 0.5 at 100s
    never = estimate_survival([_obs(50.0, Outcome.CONFIRMED_HIT)])
    assert never.median_survival_s() is None  # no death observed — no median


def test_estimate_survival_deterministic():
    observations = [_obs(50.0, Outcome.CONFIRMED_HIT), _obs(120.0, Outcome.MISS_REBUILT)]
    assert estimate_survival(observations) == estimate_survival(list(reversed(observations)))
    assert estimate_survival(observations).model_dump() == estimate_survival(
        observations
    ).model_dump()


def test_survival_curve_carries_only_hashes_and_numbers():
    curve = estimate_survival([_obs(10.0, Outcome.MISS_REBUILT)])
    payload = curve.model_dump()
    for value in payload.values():
        if value is not None and not isinstance(value, list):
            assert not isinstance(value, str) or len(value) <= 64  # keys only


# -- store integration (PRD §56 clean filter + §82 profile key) ---------------


def _profile(**overrides) -> TTLProfile:
    base = {
        "provider": "fake-provider",
        "model": "gpt-5.2",
        "api_mode": "chat",
        "endpoint_hash": _ENDPOINT_HASH,
        "route_hash": _ROUTE,
        "lower_bound_s": 183.0,
        "upper_bound_s": 302.0,
        "estimated_ttl_s": 224.65,
        "confidence": 0.7,
        "sample_count": 5,
    }
    base.update(overrides)
    return TTLProfile(**base)


def _seed_observations(store: TelemetryStore) -> None:
    """3 clean hits at 100/200/300 and 1 clean miss at 250, plus an UNclean
    miss at 30 (must never enter the curve)."""
    for age, outcome, clean in (
        (30.0, Outcome.MISS_REBUILT, False),  # unclean — excluded
        (100.0, Outcome.CONFIRMED_HIT, True),
        (200.0, Outcome.CONFIRMED_HIT, True),
        (250.0, Outcome.MISS_REBUILT, True),
        (300.0, Outcome.CONFIRMED_HIT, True),
    ):
        store.record_ttl_observation(
            timestamp=_T0 + timedelta(seconds=int(age)),
            cache_fingerprint=f"fp-{int(age)}",
            route_hash=_ROUTE,
            idle_age_s=age,
            outcome=outcome,
            clean=clean,
            provider="fake-provider",
            model="gpt-5.2",
            api_mode="chat",
            endpoint_hash=_ENDPOINT_HASH,
        )


def test_curve_from_profile_uses_only_clean_observations(tmp_path):
    store = TelemetryStore(tmp_path / "t.db")
    try:
        _seed_observations(store)
        curve = curve_from_profile(store, _profile())
        # 4 clean observations (the unclean 30s miss never enters)
        assert curve.sample_count == 4
        assert curve.horizon_s == 300.0
        # KM over the clean obs: the hits at 100/200 left the risk set as
        # censored, so the death at 250 has 2 at risk → S(250) = 1 - 1/2 = 0.5
        assert curve.survival_at(250.0) == 0.5
        assert curve.survival_at(200.0) == 1.0
        assert curve.survival_at(300.0) == 0.5
        # the stored TTL 224.65s sits between 200 (hit) and 250 (death)
        assert curve.survival_at(224.65) == 1.0
    finally:
        store.close()


def test_curve_from_profile_keyed_by_route_identity(tmp_path):
    """A different route (or provider/model) must not leak observations in."""
    store = TelemetryStore(tmp_path / "t.db")
    try:
        _seed_observations(store)
        other = curve_from_profile(store, _profile(route_hash="route-other"))
        assert other.empty
        other_provider = curve_from_profile(store, _profile(provider="anthropic"))
        assert other_provider.empty
    finally:
        store.close()


def test_curve_from_profile_pre_p11_rows_are_never_misattributed(tmp_path):
    """Observations recorded WITHOUT the P11 identity columns (NULL) can never
    match a profile query, so legacy rows are excluded rather than mis-owned."""
    store = TelemetryStore(tmp_path / "t.db")
    try:
        store.record_ttl_observation(
            timestamp=_T0,
            cache_fingerprint="fp-legacy",
            route_hash=_ROUTE,
            idle_age_s=90.0,
            outcome=Outcome.MISS_REBUILT,
            clean=True,
        )  # no provider/model/... — a pre-P11-shaped row
        curve = curve_from_profile(store, _profile())
        assert curve.empty
        curve = curve_from_observations("legacy", store.clean_observations_for_profile(
            provider="fake-provider",
            model="gpt-5.2",
            api_mode="chat",
            endpoint_hash=_ENDPOINT_HASH,
            route_hash=_ROUTE,
        ))
        assert curve.empty
    finally:
        store.close()


def test_curve_from_observations_filters_clean_and_age():
    from cachepilot_core.ttl import StoredTTLObservation

    rows = [
        StoredTTLObservation(
            id=1,
            timestamp=_T0,
            cache_fingerprint="a",
            route_hash=_ROUTE,
            idle_age_s=10.0,
            outcome=Outcome.CONFIRMED_HIT,
            clean=False,
        ),
        StoredTTLObservation(
            id=2,
            timestamp=_T0,
            cache_fingerprint="b",
            route_hash=_ROUTE,
            idle_age_s=20.0,
            outcome=Outcome.MISS_REBUILT,
            clean=True,
        ),
        StoredTTLObservation(
            id=3,
            timestamp=_T0,
            cache_fingerprint="c",
            route_hash=_ROUTE,
            idle_age_s=None,
            outcome=Outcome.CONFIRMED_HIT,
            clean=True,
        ),
    ]
    curve = curve_from_observations(_ROUTE, rows)
    assert curve.sample_count == 1  # only the clean row with an idle age
    assert curve.steps == [SurvivalStep(age_s=20.0, survival=0.0, at_risk=1, events=1)]
    assert curve.profile_key == _ROUTE


def test_survival_curve_model_extra_forbidden():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SurvivalCurve(profile_key="k", bogus=True)
    with pytest.raises(TypeError):
        SurvivalStep(age_s=1.0, survival=0.5, at_risk=1, events=1, bogus=1)
