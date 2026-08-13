# CachePilot — Task Board

> Foreman: deepseek-v4-flash @ openrouter | DuckBrain: cachepilot

Cost-aware KV-cache lease optimization + nonblocking long-task runtime for
stock Hermes Agent. Spec-complete: `docs/PRD.md` (169 sections) is the
authoritative spec. Implementation follows the PRD's phase sequence
(§127–139), one PR = one phase. Read `AGENTS.md` + the relevant `docs/`
before each phase. Follow AGENTS.md invariants + PR scope rules; bridge
every commit to a `gitreins task complete`.

## Active

| ID | Task | Pri | Cpx | Deps | Tags | Model | Reasoning | Fallback |
|----|------|-----|-----|------|------|-------|-----------|----------|
| P05 | Lease manager — arm/invalidate/complete, generation counter, normal-request-reset, scheduler; DRY-RUN only (`WOULD WARM IN 47s`), validated on real workloads | High | 5±1 | P02+P04 | ++lease, ++scheduler | DS-V4-Flash | High | DS-V4-Pro |
| P06 | Cache warming — bounded cache-equivalent replay (max_tokens=1 output bounding, content discarded), ONE verified OpenAI-compatible adapter first | High | 5±1 | P05 | +++warm, ++adapter | DS-V4-Flash | High | DS-V4-Pro |
| P07 | Economic controller — cost estimation, warm budget (budget_ratio 0.70), expected savings, ECONOMIC_STOP; turns KV watchdog into KV optimizer | Critical | 4±1 | P06 | +++economics, ++pricing | DS-V4-Flash | High | DS-V4-Pro |
| P08 | TTL learning — route-aware learned bounds (lower/upper/estimate/confidence), observational refinement, `cachepilot ttl`; route changes must not corrupt bounds | High | 4±1 | P04+P07 | ++ttl, ++learning | DS-V4-Flash | Medium | Kimi-K3 |
| P09 | Route intelligence — route identity (gateway/upstream/endpoint/region), router-miss analysis, optional economic route affinity (never blind stickiness) | Medium | 4±1 | P08 | ++routing | DS-V4-Flash | Medium | Kimi-K3 |
| P10 | Churn intelligence — system/tools/cache-key/route/history-boundary diff classification, `cachepilot explain-miss`; DETECT only in P0, no auto-rewrite | Medium | 3±1 | P04 | ++churn, ++diagnostics | DS-V4-Flash | Medium | Kimi-K3 |
| P11 | Advanced optimizations (only after measurement) — stable tool ordering, volatile prompt isolation, cross-request prefix topology, survival model P(cache survives | age) | Low | 5±1 | P08-P10 | ++optimize, ++probabilistic | DS-V4-Flash | Low | Kimi-K3 |
| P12 | Optional UI dashboard (yarn/React) — live leases, cache topology, cost graph, TTL learning, route changes, miss explanation; NEVER a core dependency | Low | 4±1 | P07-P10 | ++dashboard, ++yarn | DS-V4-Flash | Low | Kimi-K3 |

## Completed

| ID | Task | Pri | Cpx | Commit | Model |
|----|------|-----|-----|--------|-------|
| P00 | Research harness — fake provider (KV cache/TTL/usage/pricing simulator), canonical request repr, usage normalization, cache fingerprint, economic calculator; all offline-testable | Critical | 5±1 | 4d8017c | DS-V4-Flash |
| P01 | Hermes plugin skeleton — manifest, middleware registration, lifecycle hooks, structured debug logs; CI test asserts stock Hermes unchanged (judge PASS ddfc8a00) | Critical | 3±1 | f423a54 (+2795b4b) | DS-V4-Flash |
| P02 | Long-task runtime — terminal long-task classifier (deterministic), auto-background promotion, completion notifications, subagent target tracking (refcount), command duration history; NO warming; benchmark polling reduction vs stock (judge PASS c85a2dad) | Critical | 5±1 | 4ff3a49 | DS-V4-Flash |
| P03 | Relay pass-through — cachepilotd (127.0.0.1:8787), 100% pass-through, 0 cache modification; golden differential tests: same response/stream/tools/errors, byte-identical bodies via aiter_raw, RFC 7230 hop-by-hop stripping, wildcard-bind refused (judge PASS 310803ad) | High | 4±1 | ce49ad1 | DS-V4-Flash |
| P04 | Physical request observation — correlation IDs (X-CachePilot-Session/Request/Turn, stripped before upstream), request+cache fingerprints from the physical HTTP request, usage parsing, cache telemetry (CONFIRMED_HIT/MISS_REBUILT/SUCCESS_UNVERIFIED/FAILED), route extraction, SQLite WAL telemetry store, `cachepilot status/leases/costs` CLI (judge PASS d48f7976) | High | 4±1 | c53eb82 | DS-V4-Flash |
| P-BOOT | Bootstrap — repo, AGENTS.md, docs/PRD.md (full spec), task board, gitreins, hilo, scheduler registration | Critical | 2±1 | — | DS-V4-Flash |

## [ ] DOCS-000 — Documentation gate (completion-phase — do near the end, BEFORE E2E-001 / NEVER-DONE idle)

Final documentation pass per the fleet docs gate. NOT a bootstrap task — this
runs when implementation is substantially complete (after P10+), so the docs
describe what was actually built:

- Launchpad-style README: title + badges + nav + diagram + tables (skimmable)
- CONTRIBUTING.md, SECURITY.md, AGENTS.md current
- docs/ runbooks: architecture.md, provider-adapters.md, cache-economics.md,
  threat-model.md, hermes-integration.md
- GitHub repo description + 3-8 topics via gh repo edit
- License file (Apache-2.0, per user decision for fleet repos)

A stub/empty README does NOT satisfy this gate. Only when every required doc
exists and meets the content bar may the project proceed to E2E verify / idle.

## [ ] NEVER-DONE — Run coding-hermes-never-done 14-point audit

Load coding-hermes-never-done skill. Run ALL checks: spec alignment, doc
coverage, test gaps, package upgrades, pitfall hunt, performance audit,
endpoint verification, CI/CD health, DuckBrain sync, code quality,
middle-out wiring, usability smoke test, E2E testing, GitReins judge.
Create a task for EVERY gap found. This task is never complete — the audit
always finds something.
