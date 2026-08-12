"""CachePilot core — Phase 0 research harness.

Offline-testable building blocks per PRD §127: canonical request identity
(§22), two fingerprints (§23), usage normalization (§109/§160), pricing and
cost resolution (§60/§65), the economic controller (§60-65), and the
deterministic fake provider cache simulator (§109).
"""

from cachepilot_core.economics import EconomicConfig, EconomicController, WarmAction, WarmDecision
from cachepilot_core.fake_provider import (
    FakeProvider,
    FakeProviderConfig,
    FakeProviderResult,
    provider_result_to_http_response,
)
from cachepilot_core.fingerprint import (
    CACHE_IDENTITY_FIELDS,
    cache_fingerprint,
    request_fingerprint,
)
from cachepilot_core.identity import ApiMode, CacheIdentity, CanonicalRequest, hash_content
from cachepilot_core.pricing import (
    CostResolution,
    CostResolver,
    CostSource,
    PricingTable,
    estimate_cost,
    estimate_resume_costs,
)
from cachepilot_core.usage import TokenUsage, UsageNormalizer

__version__ = "0.1.0"

__all__ = [
    "CACHE_IDENTITY_FIELDS",
    "ApiMode",
    "CacheIdentity",
    "CanonicalRequest",
    "CostResolution",
    "CostResolver",
    "CostSource",
    "EconomicConfig",
    "EconomicController",
    "FakeProvider",
    "FakeProviderConfig",
    "FakeProviderResult",
    "PricingTable",
    "TokenUsage",
    "UsageNormalizer",
    "WarmAction",
    "WarmDecision",
    "cache_fingerprint",
    "estimate_cost",
    "estimate_resume_costs",
    "hash_content",
    "provider_result_to_http_response",
    "request_fingerprint",
]
