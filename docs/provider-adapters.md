# Provider Adapters — Cache Warming (PRD §34-36, Phase 6)

This runbook describes the provider adapter layer that makes warm requests
provider-safe: `packages/core/src/cachepilot_core/adapters.py`, the
memory-only snapshot store (`snapshots.py`, PRD §30), and how the relay
executes a warm (`HttpWarmExecutor`).

## Why adapters exist

Every provider has its own cache wire dialect: which output fields may be
bounded, how cache reads/writes appear in usage, whether a read refreshes
the TTL, whether routes are observable. One near-identical adapter per
provider is forbidden (PRD §36); a shared base declares the interface and
each adapter declares its **capabilities** honestly (PRD §35) — never assume
behaviour from the label "OpenAI-compatible".

## The interface (PRD §34)

`CacheProviderAdapter` (a `Protocol`) — exact method signatures:

| Method | Purpose |
|---|---|
| `canonical_cache_identity(request, response) -> CacheIdentity` | Physical cache identity (PRD §22) |
| `cache_fingerprint(request) -> str` | Prefix-cache fingerprint (PRD §23) |
| `build_warm_request(original) -> request \| None` | Bounded cache-equivalent replay (PRD §31) |
| `parse_usage(response) -> TokenUsage` | Usage normalization |
| `classify_cache_result(usage, response) -> Outcome` | Honest CONFIRMED_HIT / MISS_REBUILT / SUCCESS_UNVERIFIED / FAILED (PRD §68-70) |
| `extract_route_identity(response) -> str \| None` | Route identity when observable (PRD §71) |
| `ttl_hint(request) -> TTLHint \| None` | Provider TTL hint (P08 learning consumes these) |
| `can_pin_route() -> bool` | Route pinning support (P09) |
| `apply_route_affinity(request, route) -> request` | Affinity application (P09) |

Type notes:

- `PhysicalRequest` is the raw JSON request body — exactly what the relay's
  memory-only snapshot store holds (PRD §30) and what `build_warm_request`
  replays. Identity methods additionally accept the codebase's canonical
  view (`CanonicalRequest`, hashes only): the raw body alone lacks the
  transport facts (provider, endpoint, auth scope, route) that cache
  identity requires, so those methods raise `TypeError` on a raw body rather
  than silently deriving a wrong identity.
- `build_warm_request` returns `None` for "cannot build a bounded warm with
  certainty". The PRD's abstract signature models a stream-cancel fallback
  (PRD §31); only an adapter that can *verify* stream cancel may use it.
  `None` is the fail-closed expression of "uncertain warm = skip"
  (AGENTS.md invariant 9).

## CacheCapabilities (PRD §35)

```python
@dataclass(frozen=True)
class CacheCapabilities:
    supports_cache_telemetry: bool
    supports_cache_write_telemetry: bool
    supports_prompt_cache_key: bool
    supports_explicit_cache_control: bool
    supports_output_bound: bool
    supports_stream_cancel: bool
    read_refreshes_ttl: Literal["yes", "no", "unknown"]
    route_identity_available: bool
    route_affinity_available: bool
```

`read_refreshes_ttl` is deliberately trinary: most providers do not document
whether a cache *read* extends the entry's TTL, and "unknown" keeps TTL
learning (P08) from assuming.

## The OpenAI-compatible adapter (Phase 6's only adapter, PRD §133)

Covers the OpenAI chat/completions wire shape (`model`, `messages`,
`tools`, `max_tokens` family, `usage.prompt_tokens_details.cached_tokens`).

Capabilities (conservative, documented):

- `supports_cache_telemetry=True` — `cached_tokens` is standard.
- `supports_cache_write_telemetry=False` — this dialect does not report
  cache writes (that is Anthropic's `cache_creation_input_tokens`).
- `supports_prompt_cache_key=True`, `supports_explicit_cache_control=False`.
- `supports_output_bound=True`; `supports_stream_cancel=False` — unbounded
  requests are **skipped**, never stream-cancelled.
- `read_refreshes_ttl="unknown"`.
- `route_identity_available=False`, `route_affinity_available=False` — the
  generic dialect has no standard route identity/affinity mechanism.

## Warm-request bounding (PRD §31)

`build_warm_request` deep-copies the snapshot, then sets the **first
present** output-bound field to `1`:

1. `max_tokens` — else
2. `max_completion_tokens` — else
3. `max_output_tokens` — else
4. **skip** (return `None`): the adapter never invents a field the provider
   did not support, and the stream-cancel fallback is unverified here.

Everything else — model, messages, tools, temperature, stream, metadata —
is preserved byte-for-byte, because the warm must remain **cache-equivalent**
(only safe output-bounding fields may differ; PRD §23).

### Tool policy (PRD §33)

- **Tools are replayed unchanged**: they participate in the provider's
  cached prefix (they feed `tools_hash`, part of physical cache identity).
- **`tool_choice` is NOT mutated**: its cache-identity role is
  provider-specific and unverified for the generic dialect. PRD §33: *if
  uncertain, do not mutate tool choice*.
- Any tool output a warm response might carry is **discarded** by the warm
  executor — the relay never executes tools (PRD §32).

## Warm safety

- The relay's `HttpWarmExecutor` sends the warm **directly** to the upstream
  with a plain httpx call: it never re-enters the observation/forwarding
  path (no recursive lease tracking, no re-observation, no tool execution).
- Generated content is discarded; only usage/outcome/cost are returned.
- A bounded per-request timeout (30s default) keeps a hung upstream from
  stalling the scheduler; transport errors classify as FAILED.
- Warm costs are always recorded on the lease (`warm_count`,
  `warm_cost_usd` — invariant 4), and `last_cache_touch_at` advances only on
  a **verified** cache touch: CONFIRMED_HIT refreshes; MISS_REBUILT
  refreshes only when write telemetry proves this request rebuilt the
  cache; SUCCESS_UNVERIFIED and FAILED never refresh (invariant 3).
- The warm circuit breaker (PRD §94) stops warming a lease after 2
  consecutive warm outcomes that did not verify a cache touch, until a
  normal request produces new cache evidence.

## Snapshots (PRD §30)

The relay keeps the last cache-producing request body per cache identity
**in memory only** (`SnapshotStore`). Nothing in the snapshot is ever
persisted: prompts, conversation history, API keys, authorization headers,
tool arguments. A freshly constructed controller has no snapshots — leases
become non-warmable (`SKIPPED_UNSUPPORTED`) until the next successful
request. That is acceptable because provider caches are ephemeral anyway.

## Adding an adapter (later phases)

1. Subclass/implement `CacheProviderAdapter` with the exact signatures.
2. Declare honest `CacheCapabilities` — no assumptions from the label.
3. Implement `build_warm_request` bounding + tool policy per PRD §31/§33;
   return `None` when uncertain.
4. Add integration tests against the fake provider (never "200 = success").

Phase 6 ships exactly one verified adapter; OpenRouter, DeepSeek, Anthropic
and OpenAI adapters follow in later phases (PRD §133).
