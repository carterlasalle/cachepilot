# CachePilot Architecture

## Phase 4 — Physical Request Observation

Observation turns the relay's view of the physical HTTP request/response
pair into structured telemetry, without changing pass-through behaviour
(PRD §131; the P03 golden differential suite stays byte-identical).

### Flow

```
Hermes (plugin)          relay (cachepilotd)                provider
    │ llm_request middleware               │                      │
    │ injects X-CachePilot-* headers       │                      │
    ├──────────────────────────────────────▶                      │
    │                      strips correlation headers (PRD §29)   │
    │                      buffers + hashes request body          │
    │                      computes request/cache fingerprints    │
    │                      extracts route identity (PRD §71)      │
    │                      forwards verbatim ────────────────────▶│
    │                      parses usage read-only (bounded only)  │
    │                      classifies outcome (PRD §68-70)        │
    │                      writes request_events / churn_events   │
    │◀───────────────────── response byte-identical ──────────────│
    │
    └── cachepilot status / costs (CLI) reads the SQLite store
```

### Components

- **Correlation IDs** (`packages/hermes-plugin`): the `llm_request`
  middleware merges `X-CachePilot-Session` (cached per process),
  `X-CachePilot-Request` (per call) and `X-CachePilot-Turn`
  (deterministic per session/request pair) into the provider request's
  `headers` mapping when one exists. Fail-open: no headers mapping ⇒ pass
  through unchanged; existing header values are never clobbered.
  Gated by `CACHEPILOT_CORRELATION_HEADERS` (default true).
- **Header stripping** (`cachepilot_relay.observation`): the relay removes
  the three correlation headers before forwarding — they never reach the
  upstream and never affect provider cache identity (PRD §29).
- **Fingerprints**: `CanonicalRequest` is built from the physical request
  (provider from the upstream host, model/api_mode from body + path,
  auth_scope as a stable hash of the Authorization header or
  `relay-default`, prompt/system/tools hashes only) and both fingerprints
  are computed with `cachepilot_core` (PRD §22-23; AGENTS.md invariants 7-8).
- **Route identity** (`RouteIdentity`): gateway (upstream host),
  upstream_provider/region/deployment from response headers
  (`x-provider`, `x-served-by`, ...) when present, endpoint from the URL.
  Only observable fields are populated; its hash is stored as `route_hash`.
- **Usage + outcome** (`cachepilot_core.telemetry`): bounded responses are
  parsed read-only with `UsageNormalizer` and classified
  CONFIRMED_HIT / MISS_REBUILT / SUCCESS_UNVERIFIED / FAILED per PRD §68-70.
  Streaming responses record SUCCESS_UNVERIFIED with zero usage — the stream
  is never consumed (streaming usage parsing is deferred). Non-2xx/transport
  errors record FAILED.
- **Storage** (`cachepilot_core.storage`): SQLite (WAL, safe fallback) at
  `~/.hermes/cachepilot/cachepilot.db` (`CACHEPILOT_TELEMETRY_DB`
  overrides). Tables: `request_events` (hashes, usage, cost, outcome,
  request_kind) and `churn_events` (per-session fingerprint transitions
  with change flags). Only hashes/timestamps/usage/prices/route identities/
  outcomes are persisted (AGENTS.md invariant 10).
- **CLI** (`packages/cli`): `cachepilot status` (relay health via TCP
  probe, plugin state, cache health: hit %, per-outcome counts, churn and
  route changes), `cachepilot leases` (honest Phase 5 placeholder) and
  `cachepilot costs` (recorded-cost-only totals — never "money saved",
  PRD §79).
- **Fail open** (AGENTS.md invariant 9): every observation/storage error —
  including an unusable telemetry path at startup — logs a warning and
  leaves forwarding untouched. `CACHEPILOT_RELAY_OBSERVATION_ENABLED=false`
  makes the relay pure Phase 3 pass-through.
