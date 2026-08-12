# CachePilot for Hermes

**Cost-aware KV cache lease optimization, nonblocking long-task runtime, cache intelligence, and provider-aware request replay — for stock Hermes Agent. No fork.**

The cheapest LLM call is the one Hermes never needs to make. The second-cheapest is a request whose expensive prefix is already cached. CachePilot sits between Hermes and the provider to make sure neither does expensive work unnecessarily.

> **Status:** Bootstrap complete — Phase 0 (research harness) is the first build task. See [docs/PRD.md](docs/PRD.md) (the full 169-section spec) and [.coding-hermes/tasks.md](.coding-hermes/tasks.md).

## What it does

| Capability | Description |
|---|---|
| **Long-task runtime** | Detects long commands (pytest, docker build, cargo build…) and auto-backgrounds them; LLM sleeps, completion notification wakes it exactly once. Zero LLM polling. |
| **Cache lease manager** | Tracks the physical provider cache a session depends on while background work is alive — TTL, confidence, warm deadlines, generation counters. |
| **Economic controller** | Warms a cache ONLY when `expected_avoidable_loss > warm_cost + margin`. Stops warming when irrational. Never a blind "warm while alive" watchdog. |
| **Cache intelligence** | Fingerprints requests at two levels (full request vs cache-identity), learns real provider TTLs from evidence, detects cache churn (timestamps, router moves, tool-schema drift), explains misses. |
| **Provider-aware replay** | `cachepilotd` localhost relay (127.0.0.1:8787) observes the exact wire request, reproduces cache-equivalent warms with output bounding (`max_tokens=1`), verifies hits via telemetry — never trusts HTTP 200. |

## Architecture

```
Stock Hermes Agent ── plugin middleware/hooks ──► CachePilot plugin
                                                       │
                                                       ▼
                                               cachepilotd (localhost relay)
                                                       │
                                              OpenAI / Anthropic / OpenRouter /
                                              DeepSeek / compatible providers
```

## Quick start (not yet — Phase 0 pending)

```bash
uv sync --group dev
uv run pytest
```

## Phases

[P00–P12 task board](.coding-hermes/tasks.md): research harness → plugin skeleton → long-task runtime → relay pass-through → request observation → lease manager → warming → economics → TTL learning → route intelligence → churn intelligence → advanced optimizations → optional UI.

## Security posture

- Relay binds `127.0.0.1` only; control plane via Unix socket (0600)
- Never persists API keys, auth headers, raw prompts, or tool output — only hashes, usage, prices, outcomes
- Fail open for normal traffic, fail closed for warming
- Stock Hermes stays upstream: zero source modifications, zero monkey patches

## Docs

- [PRD (full spec)](docs/PRD.md) — mission, economics formula, 169 sections
- [AGENTS.md](AGENTS.md) — agent rules, invariants, anti-patterns

## License

Apache-2.0 (pending — license file added at first content commit).
