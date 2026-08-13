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

## Phase 10 — Churn Intelligence

Phase 10 (PRD §137) turns the existing churn booleans into a diagnosis: which
layer of the prompt topology destroyed a reusable prefix, where, and why
(PRD §24-25, §75). DETECT ONLY — automatic canonicalization belongs to a
later phase (PRD §25).

### Layered prefix hashing (`cachepilot_core.churn`)

PRD §24 topology, one hash per layer, computed from request content:

```text
static system prefix | dynamic system suffix | tool schemas |
historical conversation | recent conversation tail
```

- `request_content_from_payload(payload, ...)` extracts the layered view the
  same way the relay's canonical path does (top-level `system` or
  system-role messages; non-system messages; `tools`/`functions`), so the
  flat hashes agree with the stored `system_hash` / `tools_hash` /
  `history_hash` (P08/P09 semantics unchanged — the TTL learner's §56
  clean-check keeps using `history_hash` equality).
- `split_system_layers` draws the static/dynamic boundary at the FIRST
  volatile region (timestamp / date / clock time / UUID — the PRD §25 churn
  vocabulary). Everything before it is presumed static.
- `LayeredHashes` carries hashes ONLY (sha-256 hex digests + identity
  fields). No prompt content ever reaches storage (AGENTS.md invariant 10).

### Diff classification (`classify` / `classify_hashes`)

`classify(previous, current)` compares two `RequestContent` views and returns
a `ChurnClassification`: the six ChurnEvent-aligned booleans (system / tools /
history / route / cache key / model), layered attribution
(`system_prefix_changed`, `history_tail_changed`, ... — `None` = not
computable), a `DivergenceHint` (first divergent byte as offset + PRD §24
layer + a BOUNDED in-memory snippet), an estimated reusable-prefix loss in
tokens (~4 chars/token heuristic over the common prefix), a human-readable
likely cause and a confidence 0..1. `classify_hashes` is the hash-only path
(no content ⇒ no offset/loss — never fabricated).

Cause vocabulary (priority: route, then model, then the earliest changed
content layer): `router affinity loss` (0.92 — the PRD §75 example),
`provider failover (model switched)`, `volatile value inserted into prompt
prefix`, `changing memory prefixes (static system prefix moved)`,
`tool list mutation`, `history-boundary churn (recent conversation tail
moved)`, `conversation history rewritten (compression/truncation)`,
`prompt cache key mutation`. Confidence = base − 0.10 × (extra changed
layers), floor 0.50. `changed_frequency` renders PRD §25 aggregates
(`changed 11/12 requests`).

Invariant-10 posture: only the numeric offset + layer name of the divergence
hint are persisted; the PRD §25 content window exists in-memory only and
must never be written to the store.

### Relay wiring (`cachepilot_relay.observation`)

- `_record_churn` keeps the pre-P10 boolean computation byte-for-byte (the
  P08 TTL learner and P09 router-miss analysis depend on those exact
  hash-equality semantics) and enriches the row with the classifier's
  `likely_cause` / `confidence` / `estimated_prefix_loss_tokens` /
  `first_divergent_offset` / `first_divergent_layer`.
- The classifier runs ONLY on fingerprint transitions (never per request)
  and is fail-open (an error logs and degrades to the hash-only fallback —
  traffic unaffected).
- A bounded memory-only cache of the last request body per session (PRD §30
  — dies on relay restart, ≤1 MiB per body, ≤32 sessions) powers the
  content-level classification; after a restart the first transition falls
  back to hash-only attribution.
- `churn_events` gained five nullable columns (idempotent ALTER migration
  for pre-P10 databases); fresh databases create the full shape.

### CLIs (`cachepilot churn`, `cachepilot explain-miss`)

- `cachepilot churn` (PRD §76): per-layer change frequency over the recorded
  churn events + the most common likely causes. Empty DB: `no churn events`.
- `cachepilot explain-miss` (PRD §75, §137): explains the LATEST churn event
  (or `--session <hash>` scoped) — Stable/Changed layers, likely cause,
  confidence, estimated prefix loss, first divergent byte. Rows recorded
  before P10 show `n/a` — honest unknowns, never guesses.

### Feature flag (PRD §164)

`cache.churn_detection.enabled` ↔ `CACHEPILOT_CHURN_DETECTION_ENABLED`
(default true, independent of observation/route-intel). Disabled ⇒ the
observer records ZERO churn events; request telemetry is unaffected.
