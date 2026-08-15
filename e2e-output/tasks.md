# CachePilot — E2E Tick Findings (E2E-001)

Run: 2026-08-13 · Worker: Step 3.7 Flash (CLI/API, no browser) · Repo: cachepilot @ 42311f1

All findings below were reproduced live against a fresh deploy (uv workspace
sync, yarn install/build, seeded temp telemetry DB, real relay + mock upstream).
None are fabricated; every task row carries reproduction evidence.

## Findings

| ID | Severity | Component | Summary |
|----|----------|-----------|---------|
| E2E-002 | MEDIUM | relay / dashboard backend / deploy | Default ports 8787 + 8788 collide with stock-Hermes companion processes (hermes-webui, Hermes MCP server) on this host; out-of-box flows unusable, relay probe reports false "healthy" — **no longer reproduces (fixed in 8057569)** |
| E2E-003 | LOW | dashboard backend | PUT/DELETE return 501 + HTML body, not the documented 405 JSON — write refusal works but the error contract is wrong and non-JSON — **no longer reproduces (fixed in c976378)** |
| E2E-004 | LOW | CLI (TelemetryStore.connect) | Every CLI read command silently CREATES an empty telemetry DB at a missing `--db`/`CACHEPILOT_TELEMETRY_DB` path (mkdir + CREATE TABLE); a typo'd path yields a stray ~84 KB file instead of an error — **no longer reproduces (fixed in 93d7a32)** |
| E2E-005 | LOW | CLI + dashboard API | "Route changes" counters disagree across surfaces on the same DB — **no longer reproduces (fixed in ba93d89)** |
| E2E-006 | MEDIUM | dashboard / responsive UI | At 320px viewport the fixed 230px sidebar clips dashboard content — **no longer reproduces (fixed in 1095a97)** |
| E2E-007 | LOW | dashboard backend | Write-refusal JSON 405 does not extend to HEAD/OPTIONS/TRACE — they still return 501 + text/html (incomplete continuation of the E2E-003 fix); smoke test only asserts the 4 write methods |

---

## E2E-002 — MEDIUM — Default ports collide with stock Hermes companions; relay probe gives false "healthy"

- **Component**: `cachepilotd` (PRD §26 default `127.0.0.1:8787`), dashboard backend (default `127.0.0.1:8788`), `vite.config.ts` proxy (→ 8788), CLI/API relay TCP probe.
- **Reproduction** (all live, this host):
  1. `ss -tlnp` → `127.0.0.1:8788` owned by `hermes_cli.main mcp serve` (pid 2798298, 1 day old); `0.0.0.0:8787` owned by `hermes-webui/server.py` (pid 2678866, 4 days old).
  2. `uv run python dashboard/backend/server.py --db <tmp> --port 8788` → `OSError: [Errno 98] Address already in use`.
  3. `curl http://127.0.0.1:8788/api/health` (while squatted) → `{"error":"unauthorized"}` HTTP 401 — the MCP server's own response, not the dashboard's.
  4. `uv run cachepilotd --listen 0.0.0.0:8787` refused correctly (wildcard policy) but `127.0.0.1:8787` is also taken by hermes-webui; `--listen 127.0.0.1:8790` was required.
  5. `cd dashboard && yarn dev` → `/api` proxy forwards to 8788, which is the MCP server: `curl http://127.0.0.1:5173/api/health` → `{"error":"unauthorized"}` HTTP 401. The documented dev flow cannot reach the CachePilot backend without editing `vite.config.ts`.
  6. With NO cachepilotd running, both `cachepilot status` and `GET /api/status` report `Relay: healthy` — the TCP probe (server.py:159, main.py:227) succeeds against hermes-webui listening on 8787. The probe is honest-by-construction ("never asserting more than a TCP connect proves", documented), but the practical readout is a false positive whenever ANY process owns 8787.
- **Expected**: fresh deploy on a stock-Hermes host binds its defaults and the relay readout reflects relay presence. `docs/dashboard.md` documents the 8788 workaround ("If port 8788 is taken, pass --port and update the Vite proxy") but nothing documents the 8787 collision with `HERMES_WEBUI_PORT` (default 8787, `hermes-webui/api/config.py:51`), and the status view cannot distinguish "cachepilotd up" from "anything listening on 8787".
- **Actual**: both default ports are unusable on this host; dev proxy points at a foreign server (401); relay readout is always "healthy" with no relay running.
- **Suggested fix direction** (no code changed in this tick): pick non-colliding defaults or detect occupants on startup with a clear error; have the relay probe require an HTTP confirmation (e.g. `/health` pass-through response) instead of TCP-connect-only; document the 8787/HERMES_WEBUI_PORT overlap.

## E2E-003 — LOW — Dashboard backend: PUT/DELETE/PATCH → 501 HTML, not documented 405 JSON

- **Component**: `dashboard/backend/server.py` `Handler` (only `do_GET` + `do_POST` are implemented).
- **Reproduction** (live, seeded DB server on 8789):
  - `curl -X PUT -d '{}' http://127.0.0.1:8789/api/leases` → HTTP 501, HTML error page `Unsupported method ('PUT').`
  - `curl -X DELETE http://127.0.0.1:8789/api/leases` → HTTP 501, HTML.
  - `curl -X POST -d '{}' http://127.0.0.1:8789/api/leases` → HTTP 405 `{"error": "the dashboard backend is read-only (GET only)"}` ✓ (the only write path the smoke test covers).
- **Expected**: `docs/dashboard.md` API table: "`POST`/writes are refused with 405 (the backend is read-only)". A JSON 405 for every write method.
- **Actual**: only POST is 405+JSON; PUT/DELETE (and PATCH) fall through to `BaseHTTPRequestHandler`'s 501 with an HTML body. Read-only integrity is NOT violated (all writes refused), but the documented error contract is inconsistent and machine-unfriendly (non-JSON).
- Note: `smoke_test.py` only asserts POST 405, which is why this escaped the 52-check gate.

## E2E-004 — LOW — CLI read commands silently create an empty telemetry DB on a missing path

- **Component**: `cachepilot_core.storage.TelemetryStore.connect` (storage.py:722-741) — `mkdir(parents=True)` + `sqlite3.connect` (creates file) + `executescript(_SCHEMA)` + migrations, used by every CLI command.
- **Reproduction** (live):
  1. `rm -f /tmp/cachepilot-e2e/missing-cli.db*`
  2. `uv run cachepilot status --db /tmp/cachepilot-e2e/missing-cli.db` → prints "no telemetry recorded yet" (exit 0) AND creates `/tmp/cachepilot-e2e/missing-cli.db` (84 KB, full schema).
- **Expected**: a read-only observability command on a nonexistent path either errors ("no such database") or documents that it bootstraps an empty store. The dashboard backend deliberately solves this (`ReadOnlyTelemetryStore` mode=ro, skips schema) — the CLI has no equivalent.
- **Actual**: a typo'd `--db` path (or stale `CACHEPILOT_TELEMETRY_DB`) silently materializes a stray empty DB and reports an honest-looking "no telemetry recorded yet", masking the mistake. Undocumented side effect (dashboard.md documents only the WAL sidecar artifacts of reads, not file creation).
- **Suggested fix direction**: add a read-only open mode to `TelemetryStore` (mirror `ReadOnlyTelemetryStore`) for CLI reads, or print a notice when a missing DB is created.

## E2E-005 — LOW — Conflicting "route changes" counters across CLI surfaces (and API)

- **Component**: `cachepilot status` (`CacheHealthStats.route_changes` = `SELECT COUNT(*) FROM churn_events WHERE route_changed = 1`, storage.py:899-901) vs `cachepilot routes` (`RouteIntelStats.route_switches` = count of `route_events` rows) — mirrored by `GET /api/status` vs `GET /api/routes`.
- **Reproduction** (live, seeded DB with 1 churn event `route_changed=False` and 1 route event `route-1 → route-2` verdict `route_instability`):
  - `cachepilot status` → `route changes       0`
  - `cachepilot routes` → `route switches        1`
  - `GET /api/status` → `"route_changes": 0`; `GET /api/routes` → `"stats": {"route_switches": 1, ...}` (same DB, same request stream).
- **Expected**: two surfaces that both look like "how many route changes happened" should agree, or the labels must disambiguate the sources (churn-attributed misses vs all observed switches).
- **Actual**: the numbers genuinely measure different tables (churn-flag vs route-events), but a user checking route instability sees `0` in status and `1` in routes with no explanation. Confusing; labels ("route changes" / "route switches") are too similar to signal the distinction.
- **Suggested fix direction**: rename the status field to `route-change churn events` (or add a footnote), or compute status's route changes from the same `route_events` source.

---

## Zero-finding areas (explicit)

The following were exercised with real evidence and produced NO findings:

- Quality gate: `uv sync --group dev` clean; `uv run ruff check .` → "All checks passed!"; `uv run pytest -q` → **463 passed in 36.85s**.
- Frontend gate: `yarn install` (yarn 4.18.0) + `yarn build` (`tsc --noEmit && vite build`) → success, `dist/` emitted (index.html 0.55 kB, JS 162.58 kB, CSS 4.67 kB).
- `dashboard/backend/smoke_test.py` → **PASSED, 52/52 checks** (53 PASS lines − 1 summary line), exit 0.
- All 9 dashboard endpoints live on a seeded temp DB: correct JSON shapes, real seeded data, honest empty states on a missing DB, `POST` 405, unknown GET 404, path-traversal attempts neutralized.
- All 8 CLI commands (`status leases costs ttl routes churn explain-miss topology`) against the seeded DB: real, mutually consistent output (totals, hit rate 50% = 1/2, $0.000330 costs, 288s TTL estimate, 1 switch, churn cause "tool list mutation", topology 1 pair); `--db` flag wins over env; `CACHEPILOT_ENABLED=false` → "inactive".
- Relay pass-through (real `cachepilotd` on 8790 → mock upstream on 8791): `/health` body byte-identical, POST bodies byte-identical, hop-by-hop + `X-CachePilot-*` correlation headers stripped, `Host` rewritten to upstream, 400 error JSON pass-through, SSE streamed identically; wildcard bind (`0.0.0.0`) refused with the documented error; observation recorded 6 live requests to the default telemetry DB without breaking pass-through (fail-open).
- Read-only proof: seeded temp DB SHA-256 byte-stable across live dashboard + CLI read passes; smoke test's byte-identical proof passed.
- Frontend headless: `yarn dev` (5173) serves HTML + all 15 TS/TSX modules transform (200); prod `dist/` served by the backend same-origin (index.html → JS/CSS bundles 200); frontend `types.ts`/`api.ts` contract matches backend payloads field-for-field.
- Console-level issues: NOT verifiable in this session — no browser tool (worker is the CLI/API variant, per board E2E-001 fallback "Step 3.7 Flash"). Browser/screenshot verification (Playwright, console errors, visual empty states) is a follow-up for the Luna variant.

---

## RUN 2 — browser/Luna variant

Run date: 2026-08-13 · seeded backend: `127.0.0.1:8792` · empty backend:
`127.0.0.1:8793`

Run 2 reproduced one new finding. Browser console and JavaScript errors were
clean in the browser session; seeded and empty states were checked visually;
screenshots are in `e2e-output/run2-screenshots/`.

| ID | Severity | Component | Summary |
|----|----------|-----------|---------|
| E2E-006 | MEDIUM | dashboard / responsive UI | At 320px viewport the fixed 230px sidebar leaves only 90px for main content; dashboard cards continue off-screen with no mobile navigation or reflow, making the dashboard unusable on a phone-width viewport |

## E2E-006 — MEDIUM — 320px mobile viewport clips dashboard content

- **Component**: dashboard frontend layout (`.sidebar` + `.main` responsive
  layout), all views.
- **Reproduction** (live browser evidence):
  1. Start the built dashboard backend on a free port with a nonexistent DB:
     `uv run python dashboard/backend/server.py --db <missing> --port 8793`.
  2. Open `http://127.0.0.1:8793/` at a 320×800 viewport.
  3. The captured `empty-overview-320-800-settled.png` shows the fixed sidebar
     occupying x=0..229 and the main area beginning at x=230, leaving only 90px
     visible. Overview cards begin at x=254 and continue past the viewport;
     the right side of the cards and the rest of the dashboard are inaccessible
     without horizontal scrolling.
- **Expected**: at the documented responsive breakpoint, navigation and the
  main dashboard reflow or collapse so a 320px viewport can access the complete
  empty state and view controls without content being clipped off-screen.
- **Actual**: sidebar remains desktop-width and main content retains its
  desktop minimum geometry. The screenshot displays only the left edge of the
  first cards; no mobile menu or horizontal-scroll affordance exists.
- **Evidence**: `e2e-output/run2-screenshots/empty-overview-320-800-settled.png`,
  plus 768px and 1280px comparison screenshots. This is a visual usability
  finding, not a console error.
- **Suggested fix direction**: add a mobile breakpoint that collapses the
  sidebar (drawer or horizontal nav) and makes card/table grids and topbar
  responsive; preserve readable empty-state text at 320px.

## RUN 2 explicit zero-finding areas

- Browser console: zero application console messages and zero JS errors in the
  seeded and empty browser sessions.
- Seeded status and empty Overview: real values and honest zeros/`n/a`; empty
  state visibly rendered with contrast.
- Live lease polling: displayed 5s cadence and cache age changed 198s → 203s
  after a 6-second wait.
- Desktop visual checks: no clipping/overflow observed at 768px or 1280px.
- Read-only contract: PUT/PATCH/DELETE/POST all 405 JSON; all nine GETs 200;
  DB SHA-256 unchanged.
- Quality/build/smoke: pytest 482 passed, ruff passed, yarn build passed,
  smoke test 52/52 passed.

---

## RUN 3 — CLI/API variant (2026-08-15)

Re-verified all five prior findings against a fresh deploy (`ff55ff5`):
**E2E-002, E2E-003, E2E-004, E2E-005, E2E-006 no longer reproduce (fixed).**
One new finding was filed (E2E-007). Full evidence in `e2e-output/report.md`.

## E2E-007 — LOW — Dashboard backend: HEAD/OPTIONS/TRACE return 501 + text/html, not the documented 405 JSON

- **Component**: `dashboard/backend/server.py` `Handler` — adds `do_POST`,
  `do_PUT`, `do_DELETE`, `do_PATCH` (the E2E-003 fix) but NOT `do_HEAD`,
  `do_OPTIONS`, `do_TRACE`.
- **Reproduction** (live, seeded + empty-store servers on 8794/8797):
  - `curl -X HEAD /api/leases` → **501** `text/html;charset=utf-8`
  - `curl -X OPTIONS /api/leases` → **501** `text/html;charset=utf-8`
  - `curl -X TRACE /api/leases` → **501** `text/html;charset=utf-8`
  - contrast: `curl -X PUT/DELETE/PATCH/POST /api/leases` → **405**
    `application/json; charset=utf-8` `{"error":"the dashboard backend is read-only (GET only)"}`
- **Expected**: `docs/dashboard.md` `line 96` documents "`POST`/writes are
  refused with 405 (the backend is read-only)" — a consistent JSON 405
  (or, for the read-only GET-contract, at least a JSON error) for any
  unimplemented method, as with the fixed write methods.
- **Actual**: only the four write methods are 405 JSON; HEAD (a read-ish method
  many HTTP clients use for liveness/`curl -I`), OPTIONS (CORS preflight), and
  TRACE fall through to `BaseHTTPRequestHandler`'s generic **501 + HTML body**.
  Read-only integrity is NOT violated (all still refused), but the error
  contract remains inconsistent and non-JSON for these methods — the same
  defect class E2E-003 fixed, extended incompletely.
- **Note**: `smoke_test.py` asserts 405 only for `("POST","PUT","DELETE","PATCH")`
  (line 554), so this gap is untested by the gate.
- **Suggested fix direction**: implement `do_HEAD` (honor GET semantics, omit
  body) and route `OPTIONS`/`TRACE`/any unknown method through `_write_refused()`
  (405 JSON), and extend the smoke test's 405 loop to include an exhaustive
  method set (HEAD/OPTIONS/TRACE) so the contract is uniformly machine-readable.
