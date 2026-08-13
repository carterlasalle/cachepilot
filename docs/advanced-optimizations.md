# Advanced Optimizations (Phase 11) — Measurement-First Runbook

Phase 11 (PRD §138) implements the four optimization candidates as
**DETECT / measurement-first** capabilities. The phase title says it all:
*"Only after measurement."* Nothing in this phase reorders tools, rewrites
prompts, or changes warm decisions — every candidate is a measurement view
over the existing telemetry (PRD §24 layered topology, §25 churn detector,
§57-58 TTL, §99 survival probability, §82 schema).

## 1. Survival model — P(cache survives | age)

**Module:** `packages/core/src/cachepilot_core/survival.py`

Moves from ONE deterministic estimated TTL (PRD §57) toward an empirical
survival probability (PRD §99): provider caches can be evicted before their
nominal TTL, so learned data should model `P(cache survives | age)`.

- Non-parametric Kaplan-Meier-style estimator over **CLEAN**
  `ttl_observations` (the PRD §56 `clean` flag), keyed per route profile key
  (PRD §82: provider / model / api_mode / endpoint_hash / route_hash).
- `CONFIRMED_HIT` at idle age A = right-censored observation ("survived to
  age A"); `MISS_REBUILT` at idle age A = death event ("died at age A").
  `SUCCESS_UNVERIFIED` / `FAILED` are never survival evidence (AGENTS.md
  invariant 3 — HTTP 200 ≠ cache hit).
- `SurvivalCurve.survival_at(t)` returns the product-limit estimate at age t;
  beyond the observed horizon it returns None — an honest unknown, never a
  fabricated probability.
- The P11 schema migration adds route-identity columns
  (`provider` / `model` / `api_mode` / `endpoint_hash`) to `ttl_observations`
  (idempotent ALTER, same pattern as P10's churn columns). Pre-P11 rows keep
  NULL identity and are excluded from per-profile curves — never
  mis-attributed. The learner (`TTLLearner`) persists the identity on every
  new observation.

**Display:** `cachepilot ttl` shows per profile `P(survive at TTL)` (at the
profile's estimated TTL) and the median survival age.

**Guards:** the survival curve is a diagnostic layer ALONGSIDE the PRD §59 TTL
override hierarchy — it does not feed `TTLResolver` or any warm decision. If
it ever does, that wiring must be gated behind an explicit
`CACHEPILOT_*` config flag (default off) and proven semantically safe first.

## 2. Cross-request prefix topology

**Module:** `packages/core/src/cachepilot_core/topology.py`
**CLI:** `cachepilot topology [--limit N] [--db PATH]`

Determines which prefix layer (PRD §24: static system prefix / dynamic
system suffix / tool schemas / historical conversation / recent conversation
tail, plus the identity layers route / model / cache key) provides the most
economic value, by measuring over stored events:

- **Per-layer stability** — change frequency across consecutive
  `request_events` for the same session/cache fingerprint. Flat layers
  (tool schemas, route, model, cache key) are exact from the stored hashes;
  the four layered sub-layers are attributed from the stored churn events'
  `first_divergent_layer` (the exact layered hashes are memory-only per PRD
  §30). Attribution gaps are counted and disclosed — never silently
  assumed unchanged.
- **Per-layer economic value** — estimated reusable prefix tokens lost per
  layer, summed from classified churn events
  (`estimated_prefix_loss_tokens` attributed to their
  `first_divergent_layer`). Un-attributable losses are reported separately.
- **Prefix stability** — the share of consecutive pairs that kept the cache
  fingerprint.
- Offline-testable path: `topology_from_snapshots` aggregates exact layered
  flags over `LayeredHashes` sequences (used by the unit tests / fake
  provider); `topology_from_store` derives the stored view.

## 3. Volatile prompt isolation

**Module:** `packages/core/src/cachepilot_core/churn.py` (classification)

`split_system_layers` already finds the volatile boundary (PRD §24/§25). P11
sharpens the classification of churn confined to the system layers — the
diagnosis is no longer the generic "system prompt changed":

- `system_suffix_churn (volatile value in dynamic system suffix)` — the
  divergence is CONFINED to the dynamic system suffix (static prefix stable,
  no other layer moved): the volatile value after the boundary destroyed the
  reusable prefix (confidence 0.85).
- `volatile_value_in_prefix (volatile value inside static system prefix)` —
  a prefix-only divergence whose window looks volatile (unix epoch seconds,
  hex blobs, labelled ids such as `run abc123`): a dynamic value the
  boundary heuristic did not recognise (confidence 0.85). Plain prefix moves
  keep `changing memory prefixes (static system prefix moved)` (0.90).
- The generic suffix cause `volatile value inserted into prompt prefix`
  (PRD §24 example) still applies when the suffix moved together with other
  layers (isolation not proven).

The new causes surface automatically in `cachepilot churn` (most common
causes) and `cachepilot explain-miss` (likely cause). DETECT-only — the
classification never rewrites or canonicalizes anything (PRD §25). The
classification runs under the existing `CACHEPILOT_CHURN_DETECTION_ENABLED`
gate (default true; disabled ⇒ zero churn events recorded, PRD §164).

## 4. Stable tool ordering (measurement only)

**Modules:** `identity.py` (`tools_set_hash`), `topology.py` (per-route stats)

Counts how often the SAME tool set arrives in a different order:

- `tools_hash` (cache identity) is order-sensitive — a permutation changes
  it; the new order-independent `tools_set_hash` (sorted, deduplicated
  member digests) stays stable across permutations.
- The relay persists both on `request_events` (P11 schema migration adds the
  `tools_set_hash` column; fingerprints exclude it — cache identity and
  stored fingerprint values are unchanged).
- `cachepilot topology` reports per route: decidable pairs, tool-set changes,
  order permutations and ordering stability %.
- NO automatic tool reordering is implemented — PRD §138 requires proof of
  semantic safety before any reordering; this phase only measures.

## Config flags

There are **no new behavior-enabling flags**: all four capabilities are
DETECT/measurement-only and require no runtime enablement. The relevant
existing flags:

| Flag | Default | Effect |
|---|---|---|
| `CACHEPILOT_CHURN_DETECTION_ENABLED` | true | Gates the churn detector that feeds volatile-isolation classification (PRD §164) |
| `CACHEPILOT_RELAY_OBSERVATION_ENABLED` | true | Master observation switch (PRD §84) |
| `CACHEPILOT_TELEMETRY_DB` | `~/.hermes/cachepilot/cachepilot.db` | Telemetry database path (PRD §81) |

Anything that would reorder tools or rewrite prompts (or feed survival into
warm decisions) must be introduced behind an explicit `CACHEPILOT_*` config
flag, default OFF, with proof of semantic safety — none of that exists in
this phase.

## Schema changes (P11)

Both migrations are idempotent ALTERs on connect (the P10 pattern in
`storage.py`); fresh databases create the full shape in `_SCHEMA`:

- `request_events.tools_set_hash TEXT` (nullable) — tool-ordering stability.
- `ttl_observations.provider / model / api_mode / endpoint_hash TEXT`
  (nullable) — per-profile survival attribution.

## Quality gate

```bash
cd /home/hermes/cachepilot
uv run ruff check .
uv run --group dev pytest -x --tb=short
uvx mypy --python-executable .venv/bin/python --follow-imports=skip \
  packages/core/src/cachepilot_core/survival.py \
  packages/core/src/cachepilot_core/topology.py \
  packages/cli/src/cachepilot_cli/main.py
```
