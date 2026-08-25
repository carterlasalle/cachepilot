"""Provider adapter layer — PRD §34-36 (Phase 6).

Each provider gets ONE adapter that knows its wire dialect: how to derive
cache identity, how to bound a warm request, how to parse usage and how to
classify the cache outcome honestly (AGENTS.md invariant 3: HTTP 200 ≠ cache
hit). Capabilities are declared per adapter (PRD §35) — **no assumptions
based solely on "OpenAI-compatible"**: different compatible providers behave
differently.

Phase 6 ships exactly ONE verified adapter: :class:`OpenAICompatibleAdapter`
(PRD §133). Later phases add OpenRouter, DeepSeek, Anthropic and OpenAI
adapters from the same base.

Type notes (PRD §34's abstract ``PhysicalRequest`` / ``PhysicalResponse``):

- ``PhysicalRequest`` is the raw JSON request body — what the relay's
  memory-only snapshot store holds (PRD §30) and what
  :meth:`CacheProviderAdapter.build_warm_request` replays.
- ``PhysicalResponse`` is the provider's HTTP response (``httpx.Response``).
- Identity methods (``canonical_cache_identity`` / ``cache_fingerprint``)
  need the transport facts (provider, endpoint, auth scope, route) that the
  raw body alone does not carry; they accept the codebase's canonical view
  (:class:`~cachepilot_core.identity.CanonicalRequest`, hashes only) and
  raise ``TypeError`` for a raw body so a caller can never silently derive a
  wrong identity.

The warm path never touches identity methods: the relay already computed the
lease's cache fingerprint from the physical request (PRD §22-23) before the
snapshot was stored.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol

import httpx

from cachepilot_core.fingerprint import cache_fingerprint as _cache_fingerprint
from cachepilot_core.identity import CacheIdentity, CanonicalRequest
from cachepilot_core.snapshots import RequestSnapshot
from cachepilot_core.telemetry import (
    Outcome,
    classify_outcome,
    usage_has_cache_telemetry,
)
from cachepilot_core.usage import TokenUsage, UsageNormalizer

logger = logging.getLogger("cachepilot_core.adapters")

#: The physical request as an adapter sees it: the raw JSON request body
#: (the snapshot). Identity methods additionally accept the canonical view.
type PhysicalRequest = Mapping[str, Any]
type PhysicalResponse = httpx.Response


@dataclass(frozen=True)
class TTLHint:
    """Provider-supplied cache TTL hint (PRD §34 ``ttl_hint``).

    Phase 8 (TTL learning) consumes these; an adapter that cannot supply a
    trustworthy hint returns ``None`` and the learned estimator takes over.
    """

    ttl_s: float
    confidence: float
    source: str


@dataclass(frozen=True)
class CacheCapabilities:
    """What one provider's cache behaves like — PRD §35, exactly.

    ``read_refreshes_ttl`` is a trinary (``yes``/``no``/``unknown``): many
    providers do not document whether a cache *read* extends the entry's
    TTL. ``unknown`` is the honest default and keeps TTL learning (P08) from
    assuming.
    """

    supports_cache_telemetry: bool
    supports_cache_write_telemetry: bool
    supports_prompt_cache_key: bool
    supports_explicit_cache_control: bool
    supports_output_bound: bool
    supports_stream_cancel: bool
    read_refreshes_ttl: Literal["yes", "no", "unknown"]
    route_identity_available: bool
    route_affinity_available: bool


class CacheProviderAdapter(Protocol):
    """Provider adapter interface — EXACTLY PRD §34's method signatures.

    ``build_warm_request`` returns ``None`` when a bounded warm cannot be
    built with certainty (fail closed for warming, AGENTS.md invariant 9) —
    the PRD's abstract signature models PRD §31's stream-cancel fallback,
    which only adapters that can *verify* it may use; this phase's
    OpenAI-compatible adapter cannot, so skip is expressed as ``None``.
    """

    capabilities: CacheCapabilities

    def canonical_cache_identity(
        self,
        request: PhysicalRequest,
        response: PhysicalResponse | None,
    ) -> CacheIdentity: ...

    def cache_fingerprint(
        self,
        request: PhysicalRequest,
    ) -> str: ...

    def build_warm_request(
        self,
        original: PhysicalRequest,
    ) -> PhysicalRequest | None: ...

    def parse_usage(
        self,
        response: PhysicalResponse,
    ) -> TokenUsage: ...

    def classify_cache_result(
        self,
        usage: TokenUsage,
        response: PhysicalResponse,
    ) -> Outcome: ...

    def extract_route_identity(
        self,
        response: PhysicalResponse,
    ) -> str | None: ...

    def ttl_hint(
        self,
        request: PhysicalRequest,
    ) -> TTLHint | None: ...

    def can_pin_route(self) -> bool: ...

    def apply_route_affinity(
        self,
        request: PhysicalRequest,
        route: str,
    ) -> PhysicalRequest: ...


@dataclass
class WarmResult:
    """Result of one warm execution (PRD §147).

    ``outcome`` is ``None`` when the adapter declined to build a bounded
    warm (uncertain → skip; nothing was sent and nothing was paid for).
    Generated content is deliberately NOT part of this result: the warm's
    output is discarded (PRD §32) — only usage/outcome/cost matter.
    """

    outcome: Outcome | None
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: Decimal = field(default_factory=Decimal)


class WarmExecutor(Protocol):
    """The injectable warm executor — transport + adapter (PRD §147).

    The lease manager calls ``execute(snapshot)`` for a due warm; the
    implementation owns the provider transport (e.g. httpx) and returns the
    parsed usage, the honestly-classified outcome and the cost. The manager
    never touches the network itself (offline-testable core).
    """

    async def execute(self, snapshot: RequestSnapshot) -> WarmResult: ...


class OpenAICompatibleAdapter:
    """The ONE verified Phase 6 adapter: generic OpenAI-compatible dialect.

    Covers the OpenAI chat/completions wire shape: ``model``, ``messages``,
    ``tools``, ``max_tokens`` / ``max_completion_tokens`` /
    ``max_output_tokens``, and ``usage.prompt_tokens_details.cached_tokens``
    cache telemetry. It does NOT assume every "OpenAI-compatible" provider
    behaves identically (PRD §35) — capabilities below are the documented
    conservative set, and identity/route methods stay conservative.

    Tool policy (PRD §33): tools participate in the provider's cached prefix
    (they feed ``tools_hash``, part of physical cache identity, AGENTS.md
    invariant 7), so a warm REPLAYS them unchanged. ``tool_choice`` is NOT
    forced to ``none``: its cache-identity role is provider-specific and
    unverified for the generic dialect, and PRD §33 says *if uncertain, do
    not mutate tool choice*. Any tool output a warm response might carry is
    discarded by the executor — the relay never executes tools (PRD §32).
    """

    capabilities = CacheCapabilities(
        # prompt_tokens_details.cached_tokens is standard in this dialect.
        supports_cache_telemetry=True,
        # The OpenAI dialect does not report cache writes (that is
        # Anthropic's cache_creation_input_tokens); misses are only
        # inferable from cached_tokens == 0.
        supports_cache_write_telemetry=False,
        # Automatic prefix caching keyed by the request prefix.
        supports_prompt_cache_key=True,
        # No explicit cache_control field in this dialect.
        supports_explicit_cache_control=False,
        # max_tokens / max_completion_tokens / max_output_tokens.
        supports_output_bound=True,
        # A stream-cancel warm strategy is NOT verifiable for the generic
        # dialect → unbounded requests are skipped, never stream-cancelled.
        supports_stream_cancel=False,
        # OpenAI does not document whether a read refreshes the TTL.
        read_refreshes_ttl="unknown",
        # No standard route identity across the compatible family; the
        # relay's observation layer extracts route identity from response
        # headers independently of the adapter.
        route_identity_available=False,
        # No route-affinity mechanism in the generic dialect.
        route_affinity_available=False,
    )

    _OUTPUT_BOUND_FIELDS = (
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
    )

    def __init__(self) -> None:
        self._normalizer = UsageNormalizer()

    # -- identity (PRD §34) -------------------------------------------------

    def canonical_cache_identity(
        self,
        request: PhysicalRequest,
        response: PhysicalResponse | None,
    ) -> CacheIdentity:
        """Cache identity from the canonical physical request (PRD §22).

        The raw body alone cannot provide provider/endpoint/auth-scope/route
        (those are transport facts the relay captured when it built the
        canonical request), so a raw body is rejected rather than silently
        hashed into a wrong identity.
        """
        canonical = self._require_canonical(request, "canonical_cache_identity")
        return CacheIdentity(
            provider=canonical.provider,
            model=canonical.model,
            api_mode=canonical.api_mode,
            endpoint=canonical.endpoint,
            auth_scope=canonical.auth_scope,
            route=canonical.route,
            prompt_key=canonical.prompt_key,
            system_hash=canonical.system_hash,
            tools_hash=canonical.tools_hash,
        )

    def cache_fingerprint(self, request: PhysicalRequest) -> str:
        """The authoritative cache fingerprint (PRD §23) of a canonical request."""
        return _cache_fingerprint(self._require_canonical(request, "cache_fingerprint"))

    # -- warm building (PRD §31, §33) ---------------------------------------

    def build_warm_request(self, original: PhysicalRequest) -> PhysicalRequest | None:
        """Bounded cache-equivalent replay (PRD §31).

        Deep-copies the snapshot, forces ``stream`` off when the original
        carried it, then sets the FIRST present output-bound field to ``1``
        (``max_tokens``, else ``max_completion_tokens``, else
        ``max_output_tokens``). Nothing else is invented or mutated — a
        provider field the original did not support is never added, and
        ``stream`` is never introduced into a request that lacked it.

        Disabling ``stream`` does not change cache identity: ``stream`` is one
        of the output-bounding fields deliberately excluded from
        :data:`~cachepilot_core.fingerprint.CACHE_IDENTITY_FIELDS` (PRD §23,
        invariant 8), so the warm still replays the same physical cache
        identity. It is required for verification: an SSE body cannot be parsed
        for usage, so a streamed warm can only ever be classified
        SUCCESS_UNVERIFIED — spend with no evidence, and two of those open the
        §94 circuit breaker.

        Returns ``None`` (skip, fail closed) when no output-bound field
        exists: the stream-cancel fallback of PRD §31 is NOT used because
        this adapter cannot verify it (``supports_stream_cancel`` is False).
        """
        if not isinstance(original, Mapping):
            raise TypeError(
                "build_warm_request requires the raw request body snapshot "
                "(the relay's SnapshotStore holds it); a CanonicalRequest "
                "carries only hashes and cannot be replayed"
            )
        warm = copy.deepcopy(dict(original))
        if "stream" in warm:
            warm["stream"] = False
        for bound_field in self._OUTPUT_BOUND_FIELDS:
            if bound_field in warm:
                warm[bound_field] = 1
                return warm
        logger.info(
            "warm skipped: request has no output-bound field (%s) and the "
            "adapter has no verified stream-cancel strategy (fail closed)",
            ", ".join(self._OUTPUT_BOUND_FIELDS),
        )
        return None

    # -- response parsing (PRD §34, §68-70) ----------------------------------

    def parse_usage(self, response: PhysicalResponse) -> TokenUsage:
        """Normalize the response's usage payload (OpenAI dialect)."""
        payload = self._payload(response)
        return self._normalizer.normalize((payload or {}).get("usage", payload), provider="openai")

    def classify_cache_result(
        self,
        usage: TokenUsage,
        response: PhysicalResponse,
    ) -> Outcome:
        """Honest outcome classification — invariant 3 (PRD §68-70)."""
        payload = self._payload(response)
        telemetry_present = usage_has_cache_telemetry((payload or {}).get("usage", payload))
        return classify_outcome(
            status_code=response.status_code,
            telemetry_present=telemetry_present,
            cache_read_tokens=usage.cache_read_tokens,
        )

    # -- route affinity (PRD §34, §71-73; Phase 9) ---------------------------

    def extract_route_identity(self, response: PhysicalResponse) -> str | None:
        """No standard route identity in the generic dialect (capability False)."""
        return None

    def ttl_hint(self, request: PhysicalRequest) -> TTLHint | None:
        """No trustworthy provider TTL hint; Phase 8 learns it from evidence."""
        return None

    def can_pin_route(self) -> bool:
        return False

    def apply_route_affinity(
        self,
        request: PhysicalRequest,
        route: str,
    ) -> PhysicalRequest:
        """No affinity mechanism in this dialect — request unchanged (Phase 9)."""
        return request

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _payload(response: PhysicalResponse) -> dict[str, Any] | None:
        try:
            parsed = response.json()
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _require_canonical(request: PhysicalRequest, method: str) -> CanonicalRequest:
        if isinstance(request, CanonicalRequest):
            return request
        raise TypeError(
            f"{method} needs the canonical physical request "
            "(CanonicalRequest — the relay's physical identity view); the raw "
            "request body lacks the transport facts (provider, endpoint, "
            "auth scope, route) that cache identity requires"
        )
