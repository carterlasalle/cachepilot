"""PRD §109 — deterministic fake provider cache simulator (offline integration)."""

from datetime import UTC, datetime, timedelta

from cachepilot_core.fake_provider import (
    FakeProvider,
    FakeProviderConfig,
    provider_result_to_http_response,
)
from cachepilot_core.identity import ApiMode, CanonicalRequest

T0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def make_request(**overrides):
    fields = {
        "provider": "openai",
        "model": "gpt-5.2",
        "api_mode": ApiMode.CHAT,
        "endpoint": "https://api.openai.com/v1",
        "auth_scope": "default-profile",
        "route": "route-a",
        "prompt_prefix": "You are a helpful assistant.",
        "system": "system prompt",
        "tools": [{"name": "get_weather"}],
    }
    fields.update(overrides)
    return CanonicalRequest.from_content(**fields)


def test_miss_writes_then_hit_reads():
    """PRD §109: before expiration -> cache_read_tokens; else cache_write_tokens."""
    provider = FakeProvider()
    request = make_request()
    prefix = provider.config.prefix_tokens

    miss = provider.complete(request, T0)
    assert miss.cache_hit is False
    assert miss.usage.cache_write_tokens == prefix
    assert miss.usage.cache_read_tokens == 0

    hit = provider.complete(request, T0 + timedelta(seconds=10))
    assert hit.cache_hit is True
    assert hit.usage.cache_read_tokens == prefix
    assert hit.usage.cache_write_tokens == 0


def test_ttl_expiry_goes_back_to_miss():
    provider = FakeProvider()
    request = make_request()
    assert provider.complete(request, T0).cache_hit is False
    before_expiry = provider.complete(request, T0 + timedelta(seconds=299))
    assert before_expiry.cache_hit is True
    after_expiry = provider.complete(request, T0 + timedelta(seconds=301))
    assert after_expiry.cache_hit is False
    assert after_expiry.usage.cache_write_tokens == provider.config.prefix_tokens


def test_distinct_requests_do_not_false_hit():
    provider = FakeProvider()
    provider.complete(make_request(system="system A"), T0)
    other = provider.complete(make_request(system="system B"), T0 + timedelta(seconds=5))
    assert other.cache_hit is False  # different cache fingerprint -> separate entry


def test_route_is_part_of_cache_identity():
    provider = FakeProvider()
    provider.complete(make_request(route="route-a"), T0)
    rerouted = provider.complete(make_request(route="route-b"), T0 + timedelta(seconds=5))
    assert rerouted.cache_hit is False
    assert rerouted.route == provider.config.route == "fake-route-1"


def test_latency_is_variable_and_deterministic():
    first = FakeProvider()
    second = FakeProvider()  # same seed -> identical latency sequence
    request = make_request()
    latencies_a = [first.complete(request, T0 + timedelta(seconds=i)).latency_ms for i in range(6)]
    latencies_b = [second.complete(request, T0 + timedelta(seconds=i)).latency_ms for i in range(6)]
    assert latencies_a == latencies_b
    assert len(set(latencies_a)) > 1  # variable, not constant
    assert all(l >= first.config.latency_base_ms for l in latencies_a)


def test_cache_map_shape():
    """cache[fingerprint] = expires_at, keyed by the cache fingerprint."""
    provider = FakeProvider()
    request = make_request()
    provider.complete(request, T0)
    from cachepilot_core.fingerprint import cache_fingerprint

    assert set(provider.cache) == {cache_fingerprint(request)}
    assert provider.cache[cache_fingerprint(request)] == T0 + timedelta(seconds=provider.config.ttl_s)


def test_is_cached_helper():
    provider = FakeProvider()
    request = make_request()
    assert not provider.is_cached(request, T0)
    provider.complete(request, T0)
    assert provider.is_cached(request, T0 + timedelta(seconds=60))
    assert not provider.is_cached(request, T0 + timedelta(seconds=301))


def test_cost_of_hit_vs_miss():
    provider = FakeProvider()
    request = make_request()
    miss = provider.complete(request, T0)
    hit = provider.complete(request, T0 + timedelta(seconds=1))
    assert provider.cost_of(miss) > provider.cost_of(hit)


def test_resume_costs_derive_from_pricing():
    provider = FakeProvider(FakeProviderConfig(completion_tokens=0))
    cold, cached = provider.resume_costs()
    # 4000 tokens at write 0.88/M vs read 0.08/M
    assert cold == provider.cost_of(provider.complete(make_request(), T0))
    assert cold > cached


def test_refresh_on_hit_extends_ttl():
    provider = FakeProvider(FakeProviderConfig(refresh_on_hit=True))
    request = make_request()
    provider.complete(request, T0)
    refreshed = provider.complete(request, T0 + timedelta(seconds=60))
    assert refreshed.cache_hit is True
    assert refreshed.expires_at == T0 + timedelta(seconds=60 + provider.config.ttl_s)


def test_completion_tokens_deterministic_and_configurable():
    request = make_request()
    provider = FakeProvider()
    assert provider.complete(request, T0).usage.completion_tokens == provider.complete(
        request, T0
    ).usage.completion_tokens
    fixed = FakeProvider(FakeProviderConfig(completion_tokens=42))
    assert fixed.complete(request, T0).usage.completion_tokens == 42


def test_http_response_is_offline_and_in_memory():
    provider = FakeProvider()
    request = make_request()
    response = provider_result_to_http_response(provider.complete(request, T0))
    assert response.status_code == 200
    assert response.request is not None  # purely in-memory request/response pair
    assert response.headers["x-cachepilot-cache-hit"] == "false"
    assert response.json()["usage"]["prompt_tokens"] == provider.config.prefix_tokens
    # no network: httpx.Response construction performs zero I/O by design
