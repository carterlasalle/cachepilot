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
| P12 | Optional UI dashboard (yarn/React) — live leases, cache topology, cost graph, TTL learning, route changes, miss explanation; NEVER a core dependency | Low | 4±1 | P07-P10 | ++dashboard, ++yarn | DS-V4-Flash | Low | Kimi-K3 |
| CI-001 | GitHub Actions CI missing — PRD §140 mandates ruff/mypy/pytest/coverage/dependency-audit/race-tests + Hermes current-main & latest-release compatibility matrix; repo has zero workflows (`gh run list` empty, no `.github/`) | High | 2±1 | — | ++ci, ++terminal | DS-V4-Flash | Low | Kimi-K3 |

## Completed

| ID | Task | Pri | Cpx | Commit | Model |
|----|------|-----|-----|--------|-------|
| P11 | Advanced optimizations (only after measurement) — empirical survival model P(cache survives \| age) from clean ttl_observations (PRD §99, KM estimator, honest horizon), cross-request prefix topology measurement view (per-layer stability/change frequency/est. prefix-token loss + per-route tool-schema ordering stability via tools_set_hash, `cachepilot topology`), volatile prompt isolation (system-suffix churn classified as volatile-value cause, DETECT-only), tool ordering measured NOT reordered; storage migration + relay observation feed; docs/advanced-optimizations.md (judge PASS 24f3f6cb) | Low | 5±1 | 2df919d | DS-V4-Flash |
| DOCS-000 | Documentation gate — Launchpad-style README (badges/nav/diagram/tables, no stale claims), CONTRIBUTING.md, SECURITY.md, LICENSE (Apache-2.0), docs/cache-economics.md + threat-model.md + hermes-integration.md runbooks, AGENTS.md Key Files touch-up (judge PASS 88cab1cf) | High | 3±1 | 6f624ee | DS-V4-Flash |
| P10 | Churn intelligence — layered prefix hashing (§24), system/tools/cache-key/route/history-boundary diff classification (§25, DETECT-only), likely cause + confidence, `cachepilot churn` + `explain-miss` CLIs, CACHEPILOT_CHURN_DETECTION_ENABLED flag (§164), churn_events schema migration (judge PASS 9241ec69) | Medium | 3±1 | 2bc4986 | DS-V4-Flash |
| P00 | Research harness — fake provider (KV cache/TTL/usage/pricing simulator), canonical request repr, usage normalization, cache fingerprint, economic calculator; all offline-testable | Critical | 5±1 | 4d8017c | DS-V4-Flash |
| P01 | Hermes plugin skeleton — manifest, middleware registration, lifecycle hooks, structured debug logs; CI test asserts stock Hermes unchanged (judge PASS ddfc8a00) | Critical | 3±1 | f423a54 (+2795b4b) | DS-V4-Flash |
| P02 | Long-task runtime — terminal long-task classifier (deterministic), auto-background promotion, completion notifications, subagent target tracking (refcount), command duration history; NO warming; benchmark polling reduction vs stock (judge PASS c85a2dad) | Critical | 5±1 | 4ff3a49 | DS-V4-Flash |
| P03 | Relay pass-through — cachepilotd (127.0.0.1:8787), 100% pass-through, 0 cache modification; golden differential tests: same response/stream/tools/errors, byte-identical bodies via aiter_raw, RFC 7230 hop-by-hop stripping, wildcard-bind refused (judge PASS 310803ad) | High | 4±1 | ce49ad1 | DS-V4-Flash |
| P04 | Physical request observation — correlation IDs (X-CachePilot-Session/Request/Turn, stripped before upstream), request+cache fingerprints from the physical HTTP request, usage parsing, cache telemetry (CONFIRMED_HIT/MISS_REBUILT/SUCCESS_UNVERIFIED/FAILED), route extraction, SQLite WAL telemetry store, `cachepilot status/leases/costs` CLI (judge PASS d48f7976) | High | 4±1 | c53eb82 | DS-V4-Flash |
| P05 | Lease manager — CacheLease/LeaseState (PRD §49), arm/invalidate/complete, generation counter, normal-request-reset, per-cache-identity locks, §53 deadline + §54 jitter scheduler, DRY-RUN only (`WOULD WARM IN 47s`, no network warm), leases table + real `cachepilot leases` listing, X-CachePilot-Targets header bridging plugin targets → relay, race tests (judge PASS a7f7f161) | High | 5±1 | b90d98b | DS-V4-Flash |
| P06 | Cache warming — bounded cache-equivalent replay: adapters.py (CacheCapabilities, CacheProviderAdapter base, OpenAICompatibleAdapter — max_tokens=1-family bounding, never invents fields), memory-only request snapshots (PRD §30), real warm executor via injectable transport (dry_run stays default), content discarded, warm_count/warm_cost_usd recorded, last_cache_touch_at only on verified touch, 2-miss warm circuit breaker, tool_choice policy §33, docs/provider-adapters.md, fake-provider + race tests (judge PASS 78483b62) | High | 5±1 | 8d6d81d | DS-V4-Flash |
| P07 | Economic controller — real economic gate replaces placeholder (PRD §134): estimated cold/cached resume costs populated from pricing (PRD §65 resolver), next-warm cost predictor, §63 resume probability (0.95 notify_on_complete / 0.20 detached / 0.0 no targets), budget_ratio 0.70 warm budget, ECONOMIC_STOP on budget exhaustion, CACHEPILOT_ECONOMICS_* config, WarmDecision explainability (PRD §145), economics.enabled=false restores watchdog; lease-level PRD §103 tests (judge PASS 6c486f48) | Critical | 4±1 | 96a35fa | DS-V4-Flash |
| P08 | TTL learning — route-aware learned bounds (lower/upper/estimate/confidence), observational refinement, `cachepilot ttl`; route changes must not corrupt bounds. TTLProfile per PRD §55, estimate §57, confidence §58, override hierarchy §59 (force > learned ≥0.7 > adapter hint > default), provider_profiles + ttl_observations tables §82, relay observation feed, lease TTL resolution wired (judge PASS 4a29492b) | High | 4±1 | c4d9b47 | DS-V4-Flash |
| P09 | Route intelligence — RouteIdentity model (PRD §71: gateway/upstream/endpoint/region/deployment, only observable fields), UC-5 router-miss analysis (ROUTE_INSTABILITY vs SHORT_TTL vs CLEAN, route-change events, instability miss never reaches TTL bounds), optional economic route affinity (§73-74: lease-scoped/temporary/reversible, savings-vs-cost gate, CACHEPILOT_ROUTE_AFFINITY default off, applied only when adapter can_pin_route), `cachepilot routes` CLI with instability stats (judge PASS 720baed0) | Medium | 4±1 | 445c440 | DS-V4-Flash |
| P-BOOT | Bootstrap — repo, AGENTS.md, docs/PRD.md (full spec), task board, gitreins, hilo, scheduler registration | Critical | 2±1 | — | DS-V4-Flash |

## [ ] NEVER-DONE — Run coding-hermes-never-done 14-point audit

Load coding-hermes-never-done skill. Run ALL checks: spec alignment, doc
coverage, test gaps, package upgrades, pitfall hunt, performance audit,
endpoint verification, CI/CD health, DuckBrain sync, code quality,
middle-out wiring, usability smoke test, E2E testing, GitReins judge.
Create a task for EVERY gap found. This task is never complete — the audit
always finds something.
