"""Usage normalization — canonical TokenUsage from provider payloads (PRD §109, §160)."""

from datetime import UTC
from decimal import Decimal

import pytest
from cachepilot_core.usage import TokenUsage, UsageNormalizer
from pydantic import ValidationError

normalizer = UsageNormalizer()


def test_openai_dialect():
    payload = {
        "usage": {
            "prompt_tokens": 4200,
            "completion_tokens": 300,
            "prompt_tokens_details": {"cached_tokens": 4000},
        }
    }
    usage = normalizer.normalize(payload, provider="openai")
    assert usage.prompt_tokens == 4200
    assert usage.completion_tokens == 300
    assert usage.cache_read_tokens == 4000
    assert usage.cache_write_tokens == 0
    assert usage.cost is None


def test_anthropic_dialect():
    """Anthropic input_tokens excludes cached portions — prompt is the sum."""
    payload = {
        "usage": {
            "input_tokens": 200,
            "output_tokens": 150,
            "cache_read_input_tokens": 4000,
            "cache_creation_input_tokens": 4200,
        }
    }
    usage = normalizer.normalize(payload, provider="anthropic")
    assert usage.prompt_tokens == 200 + 4000 + 4200
    assert usage.completion_tokens == 150
    assert usage.cache_read_tokens == 4000
    assert usage.cache_write_tokens == 4200


def test_openrouter_monetary_cost_captured():
    payload = {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.0032}}
    usage = normalizer.normalize(payload, provider="openrouter")
    assert usage.cost == Decimal("0.0032")
    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 50


def test_unknown_provider_uses_generic_dialect():
    payload = {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    usage = normalizer.normalize(payload, provider="mystery-provider")
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 5
    assert usage.cache_read_tokens == 0
    assert usage.cache_write_tokens == 0


def test_empty_and_non_mapping_payloads_degrade_to_zero():
    assert normalizer.normalize(None) == TokenUsage()
    assert normalizer.normalize({}) == TokenUsage()
    assert normalizer.normalize("not-a-mapping") == TokenUsage()
    assert normalizer.normalize({"usage": "not-a-mapping"}) == TokenUsage()


def test_negative_token_counts_rejected():
    with pytest.raises(ValidationError):
        TokenUsage(prompt_tokens=-1)
    with pytest.raises(ValidationError):
        TokenUsage(cache_read_tokens=-5)


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        TokenUsage(prompt_tokens=1, bogus_field=2)


def test_wire_roundtrip_via_fake_provider_response():
    """F2B/B2F: fake provider -> httpx.Response -> normalizer recovers cache usage."""
    from datetime import datetime, timedelta

    from cachepilot_core.fake_provider import FakeProvider, provider_result_to_http_response
    from cachepilot_core.identity import ApiMode, CanonicalRequest

    provider = FakeProvider()
    request = CanonicalRequest.from_content(
        provider="openai",
        model="gpt-5.2",
        api_mode=ApiMode.CHAT,
        endpoint="https://api.openai.com/v1",
        auth_scope="default-profile",
        prompt_prefix="prefix",
        system="system",
    )
    t0 = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    miss = provider_result_to_http_response(provider.complete(request, t0))
    hit = provider_result_to_http_response(provider.complete(request, t0 + timedelta(seconds=10)))

    assert miss.status_code == 200
    assert normalizer.normalize(miss.json()["usage"], "openai").cache_write_tokens == provider.config.prefix_tokens
    assert normalizer.normalize(hit.json()["usage"], "openai").cache_read_tokens == provider.config.prefix_tokens
    assert hit.headers["x-cachepilot-cache-hit"] == "true"
