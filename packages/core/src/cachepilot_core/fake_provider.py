"""Deterministic fake LLM provider — offline cache simulator (PRD §109, §127).

Simulates, with zero network:

- a KV prefix cache: ``cache[fingerprint] = expires_at``
- TTL expiry
- ``cache_read_tokens`` (hit) vs ``cache_write_tokens`` (miss) — a request
  arriving before expiration reads the prefix, otherwise the prefix is written
- variable but seeded/deterministic latency
- route identity
- a pricing table with cost estimation

The simulator is a plain in-memory object: it never creates an ``httpx``
client or touches the network. :func:`provider_result_to_http_response`
builds an in-memory ``httpx.Response`` so later relay phases can consume the
fake provider through the same interface as a real upstream.
"""

from __future__ import annotations

import hashlib
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from pydantic import BaseModel, Field

from cachepilot_core.fingerprint import cache_fingerprint, request_fingerprint
from cachepilot_core.identity import CanonicalRequest
from cachepilot_core.pricing import PricingTable, estimate_cost, estimate_resume_costs
from cachepilot_core.usage import TokenUsage


def _default_pricing() -> PricingTable:
    # Mirrors the PRD §62 shape: cached reads ~10% of cold input; a write costs
    # about input + 10% (PRD §160: writes are tracked separately and can be
    # expensive). Fallback snapshot only — never authority (PRD §66).
    return PricingTable(
        input_per_mtok=Decimal("0.80"),
        output_per_mtok=Decimal("2.40"),
        cache_read_per_mtok=Decimal("0.08"),
        cache_write_per_mtok=Decimal("0.88"),
    )


class FakeProviderConfig(BaseModel):
    """Configuration for the deterministic fake provider."""

    provider: str = "fake-provider"
    route: str = "fake-route-1"
    #: Deployment identity echoed as ``x-served-by`` (PRD §71 deployment
    #: field) — lets tests simulate OpenRouter-style route switches by
    #: changing it between requests.
    deployment: str = "edge-fake-1"
    ttl_s: float = Field(default=300.0, gt=0, description="Simulated cache TTL in seconds.")
    prefix_tokens: int = Field(default=4000, ge=1, description="Simulated prefix size.")
    latency_base_ms: float = Field(default=100.0, ge=0)
    latency_jitter_ms: float = Field(default=50.0, ge=0, description="Deterministic jitter range.")
    seed: int = Field(default=0, description="RNG seed — makes latency reproducible.")
    pricing: PricingTable = Field(default_factory=_default_pricing)
    refresh_on_hit: bool = Field(default=False, description="Extend TTL when a hit occurs.")
    completion_tokens: int | None = Field(default=None, description="Fixed completion size (None = derive from fingerprint).")
    completion_min: int = Field(default=50, ge=1)
    completion_max: int = Field(default=500, ge=1)


class FakeProviderResult(BaseModel):
    """Outcome of one fake completion."""

    cache_fingerprint: str
    request_fingerprint: str
    provider: str
    model: str
    usage: TokenUsage
    latency_ms: float
    route: str
    deployment: str
    cache_hit: bool
    expires_at: datetime | None


class FakeProvider:
    """Deterministic cache simulator. ``cache[fingerprint] = expires_at`` (PRD §109)."""

    def __init__(self, config: FakeProviderConfig | None = None) -> None:
        self.config = config or FakeProviderConfig()
        self.cache: dict[str, datetime] = {}
        self._rng = random.Random(self.config.seed)

    # -- queries ------------------------------------------------------------

    def is_cached(self, request: CanonicalRequest, now: datetime) -> bool:
        """True if the request's cache fingerprint is unexpired at ``now``."""
        expires_at = self.cache.get(cache_fingerprint(request))
        return expires_at is not None and self._ensure_aware(now) < expires_at

    # -- simulation ---------------------------------------------------------

    def complete(self, request: CanonicalRequest, now: datetime | None = None) -> FakeProviderResult:
        """Simulate one completion request.

        If the request's cache fingerprint is cached and unexpired, the prefix
        is served from cache (``cache_read_tokens``); otherwise the prefix is
        written to cache (``cache_write_tokens``) and the entry is created with
        ``expires_at = now + ttl`` (PRD §109).
        """
        now = self._ensure_aware(now or datetime.now(UTC))
        cache_fp = cache_fingerprint(request)
        request_fp = request_fingerprint(request)
        expires_at = self.cache.get(cache_fp)
        hit = expires_at is not None and now < expires_at

        latency_ms = self.config.latency_base_ms + self._rng.uniform(0.0, self.config.latency_jitter_ms)
        completion = self._completion_tokens(request_fp)

        if hit:
            usage = TokenUsage(
                prompt_tokens=self.config.prefix_tokens,
                completion_tokens=completion,
                cache_read_tokens=self.config.prefix_tokens,
            )
            if self.config.refresh_on_hit:
                expires_at = now + timedelta(seconds=self.config.ttl_s)
                self.cache[cache_fp] = expires_at
        else:
            usage = TokenUsage(
                prompt_tokens=self.config.prefix_tokens,
                completion_tokens=completion,
                cache_write_tokens=self.config.prefix_tokens,
            )
            expires_at = now + timedelta(seconds=self.config.ttl_s)
            self.cache[cache_fp] = expires_at

        return FakeProviderResult(
            cache_fingerprint=cache_fp,
            request_fingerprint=request_fp,
            provider=self.config.provider,
            model=request.model,
            usage=usage,
            latency_ms=latency_ms,
            route=self.config.route,
            deployment=self.config.deployment,
            cache_hit=hit,
            expires_at=expires_at,
        )

    # -- economics ----------------------------------------------------------

    def cost_of(self, result: FakeProviderResult | TokenUsage) -> Decimal:
        """Estimated monetary cost of a completion using the configured pricing."""
        usage = result.usage if isinstance(result, FakeProviderResult) else result
        return estimate_cost(usage, self.config.pricing)

    def resume_costs(self, completion_tokens: int = 0) -> tuple[Decimal, Decimal]:
        """``(cold_resume_cost, cached_resume_cost)`` for the simulated prefix."""
        return estimate_resume_costs(self.config.prefix_tokens, self.config.pricing, completion_tokens)

    # -- internals ----------------------------------------------------------

    def _completion_tokens(self, request_fp: str) -> int:
        if self.config.completion_tokens is not None:
            return self.config.completion_tokens
        digest = hashlib.sha256(request_fp.encode("utf-8")).digest()
        span = self.config.completion_max - self.config.completion_min
        return self.config.completion_min + (int.from_bytes(digest[:4], "big") % span)

    @staticmethod
    def _ensure_aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


def provider_result_to_http_response(result: FakeProviderResult) -> httpx.Response:
    """Build an in-memory ``httpx.Response`` (no network) from a fake result.

    The usage payload follows the OpenAI dialect so it round-trips through
    :class:`~cachepilot_core.usage.UsageNormalizer`; simulator internals are
    echoed in the ``x-cachepilot-*`` headers.
    """
    payload = {
        "id": f"fake-{result.request_fingerprint[:12]}",
        "object": "chat.completion",
        "model": result.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "fake completion"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
            "prompt_tokens_details": {"cached_tokens": result.usage.cache_read_tokens},
            "cache_creation_input_tokens": result.usage.cache_write_tokens,
        },
    }
    headers = {
        "x-cachepilot-cache-hit": str(result.cache_hit).lower(),
        "x-cachepilot-route": result.route,
        "x-cachepilot-latency-ms": f"{result.latency_ms:.3f}",
        "x-cachepilot-cache-fingerprint": result.cache_fingerprint,
        # PRD §71 route identity the relay's observation layer reads:
        # upstream provider label + deployment host (OpenRouter-style).
        "x-provider": result.provider,
        "x-served-by": result.deployment,
    }
    return httpx.Response(
        200,
        json=payload,
        headers=headers,
        request=httpx.Request("POST", "https://fake-provider.invalid/v1/chat/completions"),
    )
