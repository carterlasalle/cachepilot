"""Outcome classification + telemetry models (PRD §68-70, §80; AGENTS.md invariant 3)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from cachepilot_core.telemetry import (
    CacheHealthStats,
    Outcome,
    TelemetryEvent,
    classify_outcome,
    usage_has_cache_telemetry,
)
from cachepilot_core.usage import TokenUsage
from pydantic import ValidationError

# -- classify_outcome (PRD §68-70) -------------------------------------------


def test_confirmed_hit_requires_positive_cache_read_tokens():
    outcome = classify_outcome(status_code=200, telemetry_present=True, cache_read_tokens=4000)
    assert outcome is Outcome.CONFIRMED_HIT


def test_miss_rebuilt_when_telemetry_shows_zero_reads():
    outcome = classify_outcome(status_code=200, telemetry_present=True, cache_read_tokens=0)
    assert outcome is Outcome.MISS_REBUILT


def test_success_unverified_without_trustworthy_telemetry():
    # Provider returned 200 but hid cache telemetry entirely → unverified.
    assert classify_outcome(status_code=200, telemetry_present=False, cache_read_tokens=0) is (
        Outcome.SUCCESS_UNVERIFIED
    )


def test_success_unverified_with_no_usage_at_all():
    assert classify_outcome(status_code=200, telemetry_present=False, cache_read_tokens=0) is (
        Outcome.SUCCESS_UNVERIFIED
    )


@pytest.mark.parametrize("status_code", [400, 401, 429, 500, 502, 503])
def test_non_2xx_is_always_failed(status_code):
    # Even a response carrying cache telemetry is FAILED — the request did
    # not complete (HTTP 200 ≠ cache hit, invariant 3).
    assert (
        classify_outcome(
            status_code=status_code, telemetry_present=True, cache_read_tokens=4000
        )
        is Outcome.FAILED
    )


def test_redirect_is_not_a_hit():
    assert classify_outcome(status_code=302, telemetry_present=True, cache_read_tokens=100) is (
        Outcome.FAILED
    )


# -- usage_has_cache_telemetry (PRD §70 "trustworthy telemetry") --------------


def test_openai_cached_tokens_count_as_telemetry():
    assert usage_has_cache_telemetry({"prompt_tokens_details": {"cached_tokens": 0}})


def test_anthropic_cache_fields_count_as_telemetry():
    assert usage_has_cache_telemetry({"cache_read_input_tokens": 100})
    assert usage_has_cache_telemetry({"cache_creation_input_tokens": 100})


def test_generic_cache_read_tokens_count_as_telemetry():
    assert usage_has_cache_telemetry({"cache_read_tokens": 42})


def test_plain_usage_without_cache_fields_is_not_telemetry():
    assert not usage_has_cache_telemetry({"prompt_tokens": 10, "completion_tokens": 5})


def test_empty_or_missing_usage_is_not_telemetry():
    assert not usage_has_cache_telemetry({})
    assert not usage_has_cache_telemetry(None)
    assert not usage_has_cache_telemetry([1, 2, 3])


# -- TelemetryEvent model -----------------------------------------------------


def test_telemetry_event_defaults():
    event = TelemetryEvent(
        request_fingerprint="fp-req",
        cache_fingerprint="fp-cache",
        provider="openai",
        model="gpt-4",
        outcome=Outcome.CONFIRMED_HIT,
    )
    assert event.usage == TokenUsage()
    assert event.request_kind == "normal"
    assert event.session_hash is None
    assert event.timestamp is not None
    assert event.route_hash is None


def test_telemetry_event_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        TelemetryEvent(
            request_fingerprint="a",
            cache_fingerprint="b",
            provider="p",
            model="m",
            outcome=Outcome.FAILED,
            raw_prompt="should never exist",
        )


def test_telemetry_event_rejects_bad_request_kind():
    with pytest.raises(ValidationError):
        TelemetryEvent(
            request_fingerprint="a",
            cache_fingerprint="b",
            provider="p",
            model="m",
            outcome=Outcome.FAILED,
            request_kind="sneaky",
        )


# -- CacheHealthStats aggregation ---------------------------------------------


def test_stats_record_accumulates_outcomes():
    stats = CacheHealthStats()
    stats.record(Outcome.CONFIRMED_HIT)
    stats.record(Outcome.CONFIRMED_HIT)
    stats.record(Outcome.MISS_REBUILT)
    stats.record(Outcome.SUCCESS_UNVERIFIED)
    stats.record(Outcome.FAILED)
    assert stats.total == 5
    assert stats.confirmed_hits == 2
    assert stats.misses == 1
    assert stats.unverified == 1
    assert stats.failed == 1


def test_hit_rate_is_hits_over_telemetry_observed():
    stats = CacheHealthStats.from_outcomes(
        [Outcome.CONFIRMED_HIT, Outcome.CONFIRMED_HIT, Outcome.MISS_REBUILT]
    )
    assert stats.telemetry_observed == 3
    assert stats.hit_rate == pytest.approx(2 / 3)
    assert stats.total == 3


def test_hit_rate_is_none_without_telemetry():
    stats = CacheHealthStats.from_outcomes([Outcome.SUCCESS_UNVERIFIED, Outcome.FAILED])
    assert stats.telemetry_observed == 0
    assert stats.hit_rate is None


def test_from_outcomes_empty():
    stats = CacheHealthStats.from_outcomes([])
    assert stats.total == 0
    assert stats.hit_rate is None


def test_stats_accept_explicit_churn_and_route_counts():
    stats = CacheHealthStats(churn_events=4, route_changes=2)
    assert stats.churn_events == 4
    assert stats.route_changes == 2
    # Churn/route counters are independent of outcome aggregation.
    stats.record(Outcome.CONFIRMED_HIT)
    assert stats.total == 1
    assert stats.churn_events == 4


def test_event_cost_round_trips_as_decimal():
    event = TelemetryEvent(
        request_fingerprint="a",
        cache_fingerprint="b",
        provider="openrouter",
        model="m",
        outcome=Outcome.SUCCESS_UNVERIFIED,
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, cost=Decimal("0.0032")),
    )
    assert event.usage.cost == Decimal("0.0032")
