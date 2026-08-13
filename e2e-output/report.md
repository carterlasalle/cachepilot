# CachePilot — E2E Testing Tick Report (E2E-001)

Date: 2026-08-13 · Worker: Step 3.7 Flash (CLI/API variant — no browser tool) · Repo: cachepilot @ 42311f1 (clean, main)

Scope: fresh deploy + full user journey — quality gate, dashboard build,
smoke test, live dashboard backend, live CLI, live relay pass-through,
headless frontend, body-level response verification on every endpoint.

Verdict: **4 real findings (1 MEDIUM, 3 LOW) — no zero-findings tick.** All
core invariants held: no fabricated numbers, HTTP-200≠cache-hit discipline
preserved in every surface, dashboard read-only (DB byte-stable), relay 100%
pass-through. Findings are filed in `e2e-output/tasks.md` for the board.

---

## 1. Quality gate (deploy/build) — PASS

```
uv sync --group dev            → Resolved 33 packages, OK
uv run ruff check .            → All checks passed!
uv run pytest -q               → 463 passed in 36.85s
```

Full suite green (includes P00–P12 unit/integration/race tests). No pre-existing failures.

## 2. Dashboard build — PASS

```
cd dashboard
yarn install                   → yarn 4.18.0, done in 0.96s
yarn build                     → tsc --noEmit && vite build: ✓ built in 1.84s
  dist/index.html                    0.55 kB │ gzip: 0.34 kB
  dist/assets/index-wV7zBj42.js    162.58 kB │ gzip: 51.82 kB
  dist/assets/index-CfIeM9Qw.css     4.67 kB │ gzip: 1.47 kB
```

TypeScript strict-pass (tsc --noEmit) before Vite emit. `dist/` is gitignored; nothing to fix.

## 3. Dashboard backend smoke test — PASS (52/52)

```
uv run python dashboard/backend/smoke_test.py
→ SMOKE TEST PASSED — dashboard backend verified against a seeded
  TelemetryStore, empty-store states, and a byte-identical read-only DB.
  (52 check lines, 0 FAIL; exit 0)
```

Covers: populated endpoints, empty-store states, DB SHA-256 byte-identical,
no new files beyond SQLite WAL sidecars, unknown endpoint 404, POST 405.

## 4. Live dashboard backend — PASS, with 1 finding (E2E-003)

Started on a seeded temp DB (seed via `TelemetryStore`, same fixture as the
smoke test) at `127.0.0.1:8789` (default 8788 was squatted — finding E2E-002).

Body-level verification (not just HTTP codes) of every endpoint:

| Endpoint | HTTP | Body verified |
|---|---|---|
| GET /api/health | 200 | `{"ok": true}` |
| GET /api/status | 200 | stats.total=3, hit_rate=0.5, confirmed_hits=1, misses=1, unverified=1, relay="healthy", plugin="active", providers=[anthropic{1, null}, openai{2, 0.00033}] |
| GET /api/leases | 200 | 1 lease, state="armed", cache_age_s=126.5, active_targets=["target-1"], warm_cost_usd=1e-05, ttl 300.0 |
| GET /api/costs | 200 | total_usd=0.00033, per_provider={openai: 0.00033}, recent=2 cost points, honest "recorded-cost-only" note (invariant 4) |
| GET /api/ttl | 200 | 1 profile, estimated_ttl_s=288.0, bounds 120/600, confidence 0.85, samples 12, survival curve sample_count=4, p_survive_at_ttl=1.0, median 300.0 |
| GET /api/routes | 200 | 1 event verdict="route_instability" (route-1→route-2), stats route_switches=1, instability_verdicts=1 |
| GET /api/churn | 200 | 1 event, tools layer changed=1/1, top_causes=[{"tool list mutation", 1}] |
| GET /api/miss | 200 | latest event explained: likely_cause="tool list mutation", confidence 0.82, prefix loss 1234, stable=[system,history,route,model], changed=[tools,cache key] |
| GET /api/miss?session=hash-session-1 | 200 | session-scoped explanation returned |
| GET /api/topology | 200 | sessions=2, total_pairs=1, churn_pairs=1, prefix_stability_pct=0.0, tool schemas layer changed 1/1 (est. 1,234 tokens), 8 layers, tool_ordering=[] |
| POST /api/leases | 405 | `{"error": "the dashboard backend is read-only (GET only)"}` |
| PUT /api/leases | **501** | HTML error page — **finding E2E-003** |
| DELETE /api/leases | **501** | HTML error page — **finding E2E-003** |
| GET /api/not-an-endpoint | 404 | `{"error": "unknown endpoint: ..."}` |
| GET / (with dist/) | 200 | index.html → assets/index-*.js (200, text/javascript) + *.css (200, text/css) |
| GET /leases (SPA fallback) | 200 | index.html |
| GET path-traversal attempts | 200/404 | neutralized — no file disclosure (resolved candidates confined to dist/) |

Empty-store verification (server on a nonexistent DB path): every endpoint
returns honest zeros/empty lists/null — `{"leases": []}`, total=0, `event:
null`, `{"profiles": []}` — never fabricated numbers. DB file SHA-256 stable
across live dashboard + CLI reads (38fa4c54… before == after).

## 5. Live CLI — PASS, with 2 findings (E2E-004, E2E-005)

Seeded temp DB via TelemetryStore; `cachepilot <cmd> --db /tmp/cachepilot-e2e/telemetry.db`:

| Command | Verified output |
|---|---|
| status | CachePilot 0.1.0, Relay: healthy, plugin: active, requests 3, hit rate 50.0% (1/2 with telemetry), CONFIRMED_HIT 1, MISS_REBUILT 1, SUCCESS_UNVERIFIED 1, churn events 1, most-recent churn line |
| leases | `lease-00 1 152s 300s ARMED` (cache age consistent with seeded touch −60s) |
| costs | total recorded $0.000330, by provider openai $0.000330, recorded-cost-only note |
| ttl | openai \| gpt-4o-mini \| chat: endpoint/route short hashes, estimated 288s, bounds 120/600, confidence 0.85, samples 12, survival P=1.00 (n=4), median 300s |
| routes | verdict=route_instability, route-1… → route-2…, gateway/upstream/endpoint/region, deployment n/a, switches 1, last switch ts, instability verdicts 1, short-TTL 0 |
| churn | per-layer frequency (tools/cache key changed 1/1), top cause "tool list mutation" ×1 |
| explain-miss | session, cache key aaaa…→bbbb…, Stable 4 / Changed 2 layers, cause + confidence 0.82 + ~1234 tokens + first divergent offset 4096 'tool schemas' |
| topology | sessions 2, pairs 1, churn 1 (stability 0.0%), per-layer table (tool schemas 0.0% ~1,234 tokens), tool-ordering "no decidable tool-set pairs" |

CLI/API cross-checks: `--db` flag beats `CACHEPILOT_TELEMETRY_DB`; `CACHEPILOT_ENABLED=false` → `Hermes plugin: inactive (CACHEPILOT_ENABLED=false)`; unknown session → honest "nothing to explain".

Findings: CLI read on a missing path silently creates an empty DB (E2E-004);
`status` "route changes 0" vs `routes` "route switches 1" on the same DB (E2E-005).

## 6. Live relay pass-through — PASS

`cachepilotd --listen 127.0.0.1:8790 --upstream http://127.0.0.1:8791` (mock
upstream with request logging; defaults 8787/8788 squatted — E2E-002).

| Check | Result |
|---|---|
| GET /health direct vs via relay | body `{"ok": true, "upstream": "mock"}` byte-identical, 200, upstream headers pass through (x-upstream-marker) |
| POST /v1/chat/completions | body byte-identical at upstream (79 B / 32 B), response echoed byte-for-byte |
| Hop-by-hop stripping | `Connection: X-Strip-Me` + `X-Strip-Me` header NOT present upstream (RFC 7230 §6.1) |
| Correlation headers | `X-CachePilot-Session` / `X-CachePilot-Request` NOT present upstream (PRD §29) |
| Host rewrite | upstream saw `Host: 127.0.0.1:8791` (its own address), not 8790 |
| Error pass-through | GET /error → HTTP 400 + JSON body unchanged |
| SSE streaming | /stream → `data: one/two/[DONE]` identical bytes direct vs via relay |
| No relay-added headers | uvicorn date/server headers off; upstream's own date/server passed through |
| Wildcard bind | `--listen 0.0.0.0:8787` refused with documented pydantic error (exit 2) |
| Observation fail-open | 6 live requests recorded to default telemetry DB (5 SUCCESS_UNVERIFIED + 1 FAILED) while pass-through never broke |

## 7. Frontend (headless) — PASS, with evidence

- `yarn dev` on 5173: HTML served with react-refresh + @vite/client; entry `/src/main.tsx` and all 15 TS/TSX modules transform 200 (zero compile errors — consistent with tsc --noEmit).
- Production: backend serves `dist/` same-origin — index.html references built assets; JS (162.65 kB, text/javascript) and CSS (4.67 kB) both 200; SPA fallback for non-API paths.
- Frontend↔backend contract: `src/types.ts` + `src/api.ts` match the live backend payloads field-for-field for all 9 endpoints (including null unions for empty states).
- Browser-only verification (console errors, layout, visual empty states, 5s lease polling) is NOT verifiable without a browser tool — this session is the CLI/API worker variant; hand off to the Luna (browser/screenshots) variant per the board's fallback line. Note the one browser-reachable consequence of E2E-002: the dev proxy → 8788 serves a foreign 401, so the app in dev mode on this host would show an error state (fetch 401) until the port collision is resolved.

## 8. Findings → tasks

4 task rows filed in `e2e-output/tasks.md` (E2E-002..E2E-005). None were
fixed in this tick (mission: file, don't fix). Priorities for the next tick:

1. E2E-002 (MEDIUM) — port-collision + false "Relay: healthy" probe (operational on any stock-Hermes host).
2. E2E-003 (LOW) — 501 HTML vs documented 405 JSON on PUT/DELETE/PATCH.
3. E2E-004 (LOW) — CLI silently creates an empty DB on a missing path.
4. E2E-005 (LOW) — route-changes counter disagreement between status and routes.

## 9. Acceptance criteria

1. e2e-output/tasks.md + report.md committed with real findings — YES (4 findings, all reproduced live; no fabricated numbers).
2. smoke_test.py passes — YES (52/52).
3. yarn build succeeds — YES (tsc --noEmit + vite build, dist emitted).
4. Every endpoint exercised with body-level verification recorded — YES (§4 table; §5 CLI table; §6 relay table).
