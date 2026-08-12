"""Cost estimation and cost resolution — PRD §60, §65, §66, §160.

Cost resolution priority (PRD §65):

1. provider-returned monetary usage
2. provider usage × live pricing metadata
3. configured price override
4. unknown

If cost is unknown, savings must never be claimed (AGENTS.md invariant 4).
Pricing tables are never treated as permanent authority (PRD §66); fallback
snapshots carry an ``as_of`` timestamp, and live/provider-supplied usage wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field

from cachepilot_core.usage import TokenUsage

_PER_MTK = Decimal(1_000_000)


class PricingTable(BaseModel):
    """Price per 1M tokens for a route/model. Fallback snapshots only — see PRD §66."""

    input_per_mtok: Decimal = Field(..., ge=0)
    output_per_mtok: Decimal = Field(..., ge=0)
    cache_read_per_mtok: Decimal = Field(..., ge=0)
    cache_write_per_mtok: Decimal = Field(..., ge=0)
    as_of: datetime | None = Field(default=None, description="Snapshot timestamp — never authority.")


def estimate_cost(usage: TokenUsage, pricing: PricingTable) -> Decimal:
    """Estimate the monetary cost of a request from its token usage.

    Uncached input = ``prompt - cache_read - cache_write`` (floored at zero);
    cached portions are billed at their own rates. Cache writes are tracked
    separately because they can be expensive (PRD §160).
    """
    uncached = max(usage.prompt_tokens - usage.cache_read_tokens - usage.cache_write_tokens, 0)
    raw = (
        Decimal(uncached) * pricing.input_per_mtok
        + Decimal(usage.cache_read_tokens) * pricing.cache_read_per_mtok
        + Decimal(usage.cache_write_tokens) * pricing.cache_write_per_mtok
        + Decimal(usage.completion_tokens) * pricing.output_per_mtok
    )
    return raw / _PER_MTK


def estimate_resume_costs(
    prefix_tokens: int,
    pricing: PricingTable,
    completion_tokens: int = 0,
) -> tuple[Decimal, Decimal]:
    """Return ``(cold_resume_cost, cached_resume_cost)`` for a prefix.

    A cold resume must write the prefix into the cache (``cache_write``); a
    cached resume is served from cache (``cache_read``). The difference is the
    avoidable loss at the heart of the economic controller (PRD §60).
    """
    cold = TokenUsage(
        prompt_tokens=prefix_tokens,
        completion_tokens=completion_tokens,
        cache_write_tokens=prefix_tokens,
    )
    cached = TokenUsage(
        prompt_tokens=prefix_tokens,
        completion_tokens=completion_tokens,
        cache_read_tokens=prefix_tokens,
    )
    return estimate_cost(cold, pricing), estimate_cost(cached, pricing)


class CostSource(str, Enum):
    PROVIDER_RETURNED = "provider_returned"
    LIVE_PRICING = "live_pricing"
    CONFIG_OVERRIDE = "config_override"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CostResolution:
    """Result of resolving a request's cost (PRD §65)."""

    amount: Decimal | None
    source: CostSource

    @property
    def is_known(self) -> bool:
        return self.amount is not None and self.source is not CostSource.UNKNOWN


class CostResolver:
    """Resolves cost with the PRD §65 priority order."""

    def resolve(
        self,
        usage: TokenUsage,
        pricing: PricingTable | None = None,
        override: Decimal | None = None,
    ) -> CostResolution:
        if usage.cost is not None:
            return CostResolution(usage.cost, CostSource.PROVIDER_RETURNED)
        if pricing is not None:
            return CostResolution(estimate_cost(usage, pricing), CostSource.LIVE_PRICING)
        if override is not None:
            return CostResolution(override, CostSource.CONFIG_OVERRIDE)
        return CostResolution(None, CostSource.UNKNOWN)
