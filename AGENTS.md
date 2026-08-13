# CachePilot — Agent Rules

## Mission

Cost-aware KV-cache lease optimization, nonblocking long-task runtime, cache
intelligence, and provider-aware request replay for **stock upstream
NousResearch/hermes-agent** — no Hermes fork, no monkey-patching.

The primary design principle: **optimize the physical LLM request path
without waking the LLM unnecessarily.**

- The cheapest LLM call is the one Hermes never makes.
- The second-cheapest is a request whose expensive prefix is already cached.
- The optimizer must be below the model: automatic, deterministic,
  infrastructure — never an MCP tool, skill, or prompt instruction the model
  chooses to invoke.

## Non-Negotiable Architecture Rules

1. **No Hermes fork. No monkey patches. No recurring patch maintenance.**
   Integration is limited to documented plugin surfaces: `llm_request` /
   `llm_execution` / `tool_request` / `tool_execution` middleware and
   lifecycle hooks. Do NOT import private AIAgent internals.
2. **No LLM polling.** No periodic agent turn exists solely to ask whether
   background work is still running. Local process monitoring is fine; LLM
   monitoring is not. Anti-pattern: "check again in 30 seconds" turns.
3. **HTTP 200 ≠ cache hit.** Always distinguish CONFIRMED_HIT /
   MISS_REBUILT / SUCCESS_UNVERIFIED / FAILED. Never label a warm
   "successful" when only "request physically completed" is known.
4. **Warm costs are visible.** Session cost = ordinary + warm. Net savings
   = avoided cold cost − warm costs − cache-write penalties. Never hide
   warm usage; never claim "money saved" when cost data are incomplete.
5. **Warming must be economic, not a watchdog.** WARM iff
   `expected_avoidable_loss > expected_next_warm_cost + safety_margin`.
   A 3-hour compile must NOT trigger 36 refreshes just because the job
   exists. `warm forever while process alive` is forbidden.
6. **Real requests win.** A natural Hermes request refreshes the cache and
   cancels any scheduled warm. Never race a real request; per-identity
   locks, generation counters, `real_request_active` checks.
7. **Cache identity is physical, not session.** Identity includes provider,
   model, api_mode, endpoint, auth_scope, route, prompt/cache key, system
   hash, tools hash. `session_id` alone is NEVER cache identity.
8. **Two fingerprints.** `request_fingerprint` (full canonical request) vs
   `cache_fingerprint` (only prefix-cache-relevant fields; excludes
   max_tokens/stream/timeouts). A warm differs only in safe output-bounding
   fields.
9. **Fail open for traffic, fail closed for warming.** Normal provider
   requests always forward even if CachePilot breaks. Uncertain warm =
   skip. Warm circuit breaker after 2 consecutive misses; relay circuit
   breaker after 3 consecutive relay-attributable failures.
10. **Never persist secrets or prompts by default.** No API keys, no auth
    headers, no raw prompts/history/tool output. Persist only hashes,
    timestamps, usage, prices, route identities, outcomes. Snapshots are
    memory-only and die on relay restart.

## Guard Hierarchy (when optimizing)

1. Eliminate unnecessary model calls (long-task manager first — biggest win)
2. Preserve prefix stability (churn detection)
3. Reuse natural model calls as cache activity
4. Warm only when needed
5. Warm only when economics are positive
6. Improve route affinity only when economically useful
7. Learn provider behavior from evidence (TTL learning)
8. Automate more only after measurement

## Required Technology

- Python 3.12, `uv` workspace (packages/core, hermes-plugin, relay, cli)
- Pydantic v2 schemas, asyncio, SQLite (WAL) telemetry
- `yarn` for any future dashboard (NOT npm/pnpm) — never a core dependency
- Provider adapters via a common base (no 12 near-identical adapters)

## Definition of Done (per PR)

- stock Hermes unchanged (CI test asserts zero source modifications)
- code + tests + docs updated
- `uv run ruff check .`, `uv run mypy`, `uv run pytest` pass
- fake-provider integration suite covers the behavior (never "200 = success")
- race tests for warm-vs-real, warm-vs-complete, model-switch invalidation
- economics + fingerprint logic unit-tested offline
- no architectural invariant weakened

## Absolute Anti-Patterns (never implement)

- LLM heartbeat prompts / "check again in N seconds" agent turns
- warm-forever-while-alive watchdogs
- HTTP 200 = cache confirmed
- hidden warm costs
- provider-wide TTL when route/model behavior differs
- session_id = cache identity
- arbitrary OpenAI-compatible fields on every provider
- raw prompt persistence by default
- Hermes core monkey patches / forks

## Work Procedure

1. Read `docs/PRD.md` (the full spec — 169 sections) and `docs/` runbooks.
2. Identify the owning module (core/, plugin/, relay/, cli/).
3. Phases are sequential (Phase 0 research harness → Phase 12 UI); work in
   order, one PR per phase, each judged.
4. Run the full quality gate before committing; bridge every commit to a
   `gitreins task complete` so the Tier-2 judge evaluates real code.
5. Report CI health each tick.

## Key Files

- `docs/PRD.md` — the complete PRD/architecture/technical specification
- `docs/architecture.md`, `docs/provider-adapters.md`,
  `docs/cache-economics.md`, `docs/threat-model.md`,
  `docs/hermes-integration.md` — current runbooks (updated per phase)
- `CONTRIBUTING.md` — dev setup, quality gate, GitReins task lifecycle
- `SECURITY.md` — vulnerability reporting + security posture
- `AGENTS.md` — this file
