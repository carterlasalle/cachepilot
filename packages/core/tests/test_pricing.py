"""Cost estimation and cost resolution (PRD §60, §65, §66, §160)."""

from decimal import Decimal

import pytest
from cachepilot_core.pricing import (
    CostResolver,
    CostSource,
    PricingTable,
    estimate_cost,
    estimate_resume_costs,
)
from cachepilot_core.usage import TokenUsage
from pydantic import ValidationError

PRICING = PricingTable(
    input_per_mtok=Decimal("0.80"),
    output_per_mtok=Decimal("2.40"),
    cache_read_per_mtok=Decimal("0.08"),
    cache_write_per_mtok=Decimal("0.88"),
)

resolver = CostResolver()


def test_hit_is_cheaper_than_miss():
    miss = TokenUsage(prompt_tokens=4000, completion_tokens=120, cache_write_tokens=4000)
    hit = TokenUsage(prompt_tokens=4000, completion_tokens=120, cache_read_tokens=4000)
    assert estimate_cost(miss, PRICING) > estimate_cost(hit, PRICING)
    # PRD §60 example shape: cached prefix ~10% of cold prefix
    assert estimate_cost(hit, PRICING) < estimate_cost(miss, PRICING) / 5


def test_estimate_resume_costs_shapes():
    cold, cached = estimate_resume_costs(prefix_tokens=4000, pricing=PRICING)
    assert cold == Decimal("0.00352")  # 4000 * 0.88 / 1M
    assert cached == Decimal("0.00032")  # 4000 * 0.08 / 1M
    assert cold > cached


def test_cost_resolver_priority_provider_returned_wins():
    usage = TokenUsage(prompt_tokens=100, completion_tokens=50, cost=Decimal("0.0032"))
    resolution = resolver.resolve(usage, pricing=PRICING, override=Decimal("9.99"))
    assert resolution.source is CostSource.PROVIDER_RETURNED
    assert resolution.amount == Decimal("0.0032")
    assert resolution.is_known


def test_cost_resolver_priority_live_pricing():
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=0)
    resolution = resolver.resolve(usage, pricing=PRICING)
    assert resolution.source is CostSource.LIVE_PRICING
    assert resolution.amount == Decimal("0.00080")  # 1000 * 0.80 / 1M
    assert resolution.is_known


def test_cost_resolver_priority_config_override():
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=0)
    resolution = resolver.resolve(usage, pricing=None, override=Decimal("0.05"))
    assert resolution.source is CostSource.CONFIG_OVERRIDE
    assert resolution.amount == Decimal("0.05")


def test_cost_resolver_unknown_when_nothing_available():
    resolution = resolver.resolve(TokenUsage(prompt_tokens=1000))
    assert resolution.source is CostSource.UNKNOWN
    assert resolution.amount is None
    assert not resolution.is_known


def test_negative_prices_rejected():
    with pytest.raises(ValidationError):
        PricingTable(
            input_per_mtok=Decimal(-1),
            output_per_mtok=Decimal("2.40"),
            cache_read_per_mtok=Decimal("0.08"),
            cache_write_per_mtok=Decimal("0.88"),
        )
