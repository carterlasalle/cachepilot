# CachePilot for Hermes

**Cost-aware KV-cache lease optimization, nonblocking long-task runtime, cache intelligence, and provider-aware request replay — for stock Hermes Agent. No fork.**

![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![uv workspace](https://img.shields.io/badge/uv-workspace-8a2be2)
![License](https://img.shields.io/badge/license-Apache--2.0-green)
![Phases P00-P12](https://img.shields.io/badge/phases-P00--P12%20complete-2ea44f)

The cheapest LLM call is the one Hermes never needs to make. The second-cheapest is a request whose expensive prefix is already cached. CachePilot sits between Hermes and the provider so neither does expensive work unnecessarily — without forking or monkey-patching Hermes.

**Docs:** [PRD (169-section spec)](docs/PRD.md) · [Architecture](docs/architecture.md) · [Provider adapters](docs/provider-adapters.md) · [Cache economics](docs/cache-economics.md) · [Threat model](docs/threat-model.md) · [Hermes integration](docs/hermes-integration.md) · [Dashboard runbook](docs/dashboard.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

---

## What it does

| Capability | Description |
|---|---|
| **Long-task runtime** | Deterministically classifies terminal commands (pytest, docker build, cargo build…) and auto-backgrounds long ones with `notify_on_complete`. The LLM sleeps; completion notification wakes it exactly once. Zero LLM polling. |
| **Cache lease manager** | Tracks the physical provider cache a session depends on while background work is alive — TTL, confidence, warm deadlines, generation counters, per-identity locks. Real requests always win. |
| **Economic controller** | Warms a cache ONLY when `expected_avoidable_loss × resume_probability > next_warm_cost + margin` and the next warm fits the remaining budget. Stops warming (`ECONOMIC_STOP`) when irrational. Never a blind "warm while alive" watchdog. |
| **Cache intelligence** | Fingerprints requests at two levels (full request vs cache identity), learns real provider TTLs from evidence per route, detects cache churn (timestamps, router moves, tool-schema drift) with layered diagnosis, and explains misses. |
| **Provider-aware replay** | `cachepilotd` localhost relay (127.0.0.1:8787) observes the exact wire request, reproduces cache-equivalent warms with output bounding (`max_tokens=1`), and verifies hits via telemetry — never trusts HTTP 200. |
| **Observability CLI** | `cachepilot status / leases / costs / ttl / routes / churn / explain-miss / topology` reads a SQLite telemetry store. Honest by construction: empty databases say so, "money saved" is never claimed with incomplete cost data. |
| **Optional UI dashboard** | `dashboard/` — a yarn-managed React/TypeScript dashboard (live leases, cache topology, cost graph, TTL learning, route changes, miss explanation) over a read-only JSON backend. Never a core dependency; absent = no effect on the product. |

## Architecture

```
┌──────────────────────────────────────────────┐
│            Stock Hermes Agent                │
│  (no fork, no monkey patches, no CI-patch)   │
│                                              │
│  llm_request / llm_execution / tool_request  │
│  / tool_execution middleware + 8 lifecycle   │
│  hooks — documented plugin surfaces only     │
└──────────────────────┬───────────────────────┘
                       │ X-CachePilot-Session / -Request
                       │ / -Turn / -Targets correlation headers
                       ▼
┌──────────────────────────────────────────────┐
│        cachepilotd  (localhost relay)        │
│  127.0.0.1:8787 · wildcard bind refused      │
│                                              │
│  verbatim forwarding (RFC 7230 hop-by-hop    │
│  stripping) · correlation-header stripping · │
│  request/cache fingerprints · usage +        │
│  outcome classification · lease scheduler ·  │
│  economic warm replay (bounded, verified)    │
│  · SQLite WAL telemetry (hashes only)        │
└──────────────────────┬───────────────────────┘
                       │ verbatim upstream traffic
                       ▼
    OpenAI / Anthropic / OpenRouter / DeepSeek / compatible providers
```

See [docs/architecture.md](docs/architecture.md) for the full component walkthrough and [docs/hermes-integration.md](docs/hermes-integration.md) for how the plugin hooks stock Hermes.

## Quick start

```bash
uv sync --group dev      # install workspace + dev deps (pytest, ruff)
uv run pytest            # full suite (packages/core, hermes-plugin, relay, cli)
uv run ruff check .      # lint gate
uvx mypy --python-executable .venv/bin/python --follow-imports=skip <file>  # type gate (per-module)
```

Run the relay against a provider, then install the plugin into Hermes (details in [docs/hermes-integration.md](docs/hermes-integration.md)):

```bash
uv run cachepilotd --upstream https://api.openai.com/v1   # or CACHEPILOT_UPSTREAM
```

## CLI overview

`cachepilot` reads the telemetry store (`--db`, else `CACHEPILOT_TELEMETRY_DB`, else `~/.hermes/cachepilot/cachepilot.db`).

| Command | What it shows |
|---|---|
| `cachepilot status` | Relay health (HTTP probe of the relay control endpoint — healthy only when the relay itself answers), plugin state, cache health: hit %, per-outcome counts (CONFIRMED_HIT / MISS_REBUILT / SUCCESS_UNVERIFIED / FAILED), churn + route-change counts |
| `cachepilot leases` | Real lease rows from the store — targets, cache age, TTL, state |
| `cachepilot costs` | Recorded-cost-only totals (per provider) — net savings shown only when cost data are complete |
| `cachepilot ttl` | Route-keyed learned TTL profiles: estimate, lower/upper bounds, confidence, sample count |
| `cachepilot routes` | Observed route identities (gateway/upstream/endpoint/region/deployment) + instability stats |
| `cachepilot churn` | Per-layer change frequency + most common miss causes over churn events |
| `cachepilot explain-miss` | Explains the latest (or `--session`-scoped) miss: changed layers, likely cause, confidence, estimated prefix loss |
| `cachepilot topology` | Cross-request prefix topology: per-layer stability, attribution gaps, tool-ordering stability |

## Dashboard

An optional read-only UI (PRD §122/§139) lives in [`dashboard/`](dashboard/):
React + TypeScript, yarn-managed, with views for live leases, cache topology,
cost graph, TTL learning (incl. survival curves), route changes, churn and
miss explanation. A small read-only backend (`dashboard/backend/server.py`,
stdlib + `cachepilot_core`) serves the telemetry store as JSON — the same
query surface as the CLI, opened `mode=ro`, never modifying the DB and never
fabricating data (an empty DB renders empty states).

```bash
uv run python dashboard/backend/server.py   # backend on 127.0.0.1:8788
cd dashboard && yarn install && yarn dev    # frontend on http://127.0.0.1:5173
```

The dashboard is never a core dependency: it is not in the uv workspace, no
core package imports it, and deleting `dashboard/` changes nothing. See the
[Dashboard runbook](docs/dashboard.md) for the full API + view/empty-state
walkthrough.

## Test hygiene (E2E ephemeral test services)

The `908x` port range (9080-9089) is **reserved TEST-ONLY**. No product
service may bind it; every E2E tick uses it for ephemeral mock upstream,
relay (`cachepilotd --listen`), and dashboard backend (`server.py`) processes
that must NEVER leak across runs.

To keep a tick from leaking a test service into the next run (E2E-011) and
to stop a stale process from silently becoming the next run's "live" target,
use [`e2e-output/hygiene.py`](e2e-output/hygiene.py) (stdlib-only) and/or
source the shell wrapper [`e2e-output/hygiene.sh`](e2e-output/hygiene.sh):

```bash
# Pre-run: fail the tick if any stale process already listens on 908x.
python e2e-output/hygiene.py pre-run                # exit 1 if occupied
python e2e-output/hygiene.py pre-run --clean        # auto-kill stale occupants

# Post-run: kill the spawned test PIDs and verify 908x is clean via ss/ps.
python e2e-output/hygiene.py teardown <pid1> <pid2>

# Or source the shell helper: spawn every service via e2e_spawn, wrap with
# e2e_wrap, and let trap-based teardown fire on EXIT/INT/ERR.
source e2e-output/hygiene.sh
e2e_guard_pre_run && e2e_wrap
e2e_spawn python e2e-output/runN/mock_upstream.py 9081
e2e_spawn uv run cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081
e2e_teardown   # kill spawned + re-verify 908x clean

python e2e-output/hygiene.py self-test              # live guard+teardown proof
```

Every E2E tick must: (1) run the pre-run guard before spawning anything,
(2) wrap its spawned services in trap-based teardown, and (3) after the tick
re-verify via `ss`/`ps` that no process remains on `908x`. See
[docs/e2e-testing.md](docs/e2e-testing.md) for the full runbook.

## Configuration

All settings are `CACHEPILOT_*` environment variables, read at startup; malformed values fall back to defaults (fail open for traffic). Full inventory in the docs above; the essentials:

| Variable | Default | Meaning |
|---|---|---|
| `CACHEPILOT_ENABLED` | `true` | Plugin master switch |
| `CACHEPILOT_UPSTREAM` | — | Provider base URL for `cachepilotd` (required) |
| `CACHEPILOT_RELAY_LISTEN` | `127.0.0.1:8787` | Relay bind address (wildcard binds refused) |
| `CACHEPILOT_RELAY_OBSERVATION_ENABLED` | `true` | `false` = pure pass-through relay |
| `CACHEPILOT_TELEMETRY_DB` | `~/.hermes/cachepilot/cachepilot.db` | SQLite telemetry store |
| `CACHEPILOT_LEASE_DRY_RUN` | `true` | `false` = warm requests actually sent |
| `CACHEPILOT_ECONOMICS_ENABLED` | `true` | `false` = restore plain watchdog warming |
| `CACHEPILOT_ECONOMICS_BUDGET_RATIO` | `0.70` | Fraction of expected value spendable on warms |
| `CACHEPILOT_ECONOMICS_RESUME_PROBABILITY` | `0.95` | P(resume) for `notify_on_complete` targets |
| `CACHEPILOT_ECONOMICS_DETACHED_RESUME_PROBABILITY` | `0.20` | P(resume) for detached targets |
| `CACHEPILOT_TTL_FORCE_SECONDS` | — | Tier-1 TTL override |
| `CACHEPILOT_ROUTE_AFFINITY` | `false` | Economic route pinning (opt-in, adapter-gated) |
| `CACHEPILOT_CHURN_DETECTION_ENABLED` | `true` | P10 churn classification switch |

## Security posture

- Relay binds `127.0.0.1` only; wildcard binds (`0.0.0.0` / `::`) are refused unless explicitly allowed
- Never persists API keys, auth headers, raw prompts, tool output, or raw schemas — only hashes, timestamps, usage, prices, route identities, outcomes (PRD §83). Request snapshots are memory-only and die on relay restart
- Fail open for normal traffic (forwarding never depends on CachePilot); fail closed for warming (uncertain warm = skip)
- Warm circuit breaker: 2 consecutive unverified warm misses stop warming that lease until new cache evidence arrives
- Correlation headers are stripped before requests reach the upstream — they never touch provider cache identity
- Stock Hermes stays upstream: zero source modifications, zero monkey patches (CI-asserted)

See [SECURITY.md](SECURITY.md) and [docs/threat-model.md](docs/threat-model.md).

## Phase status

Spec: [docs/PRD.md](docs/PRD.md) (§127-139). One PR per phase, each judged by the GitReins Tier-2 evaluator.

| Phase | Status |
|---|---|
| P00 Research harness (fake provider, fingerprints, economics) | ✅ complete |
| P01 Hermes plugin skeleton | ✅ complete |
| P02 Long-task runtime | ✅ complete |
| P03 Relay pass-through (`cachepilotd`) | ✅ complete |
| P04 Physical request observation | ✅ complete |
| P05 Lease manager | ✅ complete |
| P06 Cache warming | ✅ complete |
| P07 Economic controller | ✅ complete |
| P08 TTL learning | ✅ complete |
| P09 Route intelligence | ✅ complete |
| P10 Churn intelligence | ✅ complete |
| P11 Advanced optimizations (only after measurement) | ✅ complete |
| P12 Optional UI dashboard (never a core dependency) | ✅ complete |

## License

[Apache-2.0](LICENSE)
