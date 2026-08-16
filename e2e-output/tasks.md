# CachePilot — E2E Tick Findings (E2E-001)

Run: 2026-08-13 · Worker: Step 3.7 Flash (CLI/API, no browser) · Repo: cachepilot @ 42311f1

All findings below were reproduced live against a fresh deploy (uv workspace
sync, yarn install/build, seeded temp telemetry DB, real relay + mock upstream).
None are fabricated; every task row carries reproduction evidence.

> **TEST-HYGIENE CONVENTION (E2E-011):** the `908x` ephemeral port range
> (9080-9089) is **reserved TEST-ONLY**. Every E2E tick MUST (1) run the
> pre-run guard (`python e2e-output/hygiene.py pre-run` / `--clean`) before
> spawning anything, (2) wrap spawned services in trap-based teardown
> (`source e2e-output/hygiene.sh` → `e2e_wrap`/`e2e_spawn`), and (3) verify
> with `ss`/`ps` after the tick that NO process remains on `908x`. Any tick
> that leaves a `908x` listener behind (or that proceeds while a stale
> listener holds the range) is a hygiene failure. Runbook:
> `docs/e2e-testing.md`.

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

---

## RUN 5 — CLI/API variant (2026-08-15)

Run date: 2026-08-15 · repo `main` @ `7a8abda` (Run 5 tick) · CLI/API E2E
variant (no browser). Fresh deploy: `uv sync --group dev` clean, `.venv/bin`
present, `yarn install && yarn build` → **43 modules transformed** (index.html
0.55 kB, JS 162.59 kB, CSS 5.48 kB) green, `dist/` emitted. Backend:
`.venv/bin/python dashboard/backend/server.py`. Relay: real `cachepilotd`
(127.0.0.1:8795 → mock upstream 127.0.0.1:9991). Seeded + empty + corrupt
temp telemetry DBs under `/tmp/cp-e2e5/`.

### Re-verification of prior findings

| ID | Severity | Result | Evidence (live) |
|----|----------|--------|-----------------|
| E2E-002 | MEDIUM | **FIXED** | With `hermes-webui` squatting 8787 and `mcp serve` on 8788, `cachepilot status` → `Relay: occupied by another service` (never `healthy`, prior R2 false positive). Pointed at a real relay (`CACHEPILOT_RELAY_LISTEN=127.0.0.1:8795`): relay control `GET /cachepilot/health` answered `{"service":"cachepilot-relay","status":"ok"}` and CLI+API both read `Relay: healthy`. Both daemons refuse an occupied default with the actionable error: `cachepilotd: ... 127.0.0.1:8787 is already in use — another process owns it (on a stock Hermes host this is typically hermes-webui, HERMES_WEBUI_PORT default 8787). Pick a free address with --listen HOST:PORT or CACHEPILOT_RELAY_LISTEN.`; `[dashboard] error: listen address 127.0.0.1:8788 is already in use — another process owns it (on a stock Hermes host this is typically hermes_cli's 'mcp serve'). Pick a free port with --port PORT and update the /api proxy target in dashboard/vite.config.ts.` (exit 2, tested on 8787/8788/8791/8790 all occupied). Relay pass-through live-verified: GET `/hello` → mock upstream body byte-identical; hop-by-hop/correlation handling intact. |
| E2E-003 | LOW | **FIXED** | Live server: `POST`/`PUT`/`DELETE`/`PATCH /api/leases` all → **405 `application/json; charset=utf-8`** `{"error": "the dashboard backend is read-only (GET only)"}`. No 501/HTML. |
| E2E-004 | LOW | **FIXED** | `cachepilot status/leases/costs/ttl/routes/churn/explain-miss/topology --db /tmp/cp-e2e5/clean-cli/missing.db` → each prints the read-only stderr notice naming the path ("CLI reads are read-only; the relay creates the DB on first write") + honest empty output, **exit 0**, and **neither the file nor its parent directory is created** (`ls` confirms the dir does not exist). Dashboard on a missing `--db` renders honest empty state (`/api/status` hit_rate null + `providers: []`, `/api/leases` `[]`, `/api/costs` `0.0`). |
| E2E-005 | LOW | **FIXED** | Same seeded DB: `cachepilot status` shows `route-change churn events 0` **with the footnote** "= churn events attributed to a route change (churn_events.route_changed); all observed switches: cachepilot routes", while `cachepilot routes` shows `route switches 1`. API mirrors: `/api/status` `route_changes: 0` vs `/api/routes` `stats.route_switches: 1`. Labels now disambiguate the sources. |
| E2E-006 | MEDIUM | **FIXED** | `dashboard/src/styles.css` has `@media (max-width: 768px)` (line 414) collapsing `.sidebar`→row sticky top nav and reflowing to single column; desktop ≥768px untouched (media query is the only breakpoint). `yarn build` green. (Browser render not re-run in this CLI/API variant; code/build state confirms the fix per Run 4.) |
| E2E-007 | LOW | **FIXED** | Live server: `OPTIONS`/`TRACE`/`HEAD /api/leases` now all **405 JSON**; `HEAD` omits the body but carries the JSON content-type + Content-Length (HTTP HEAD semantics). `smoke_test.py` 405 loop covers all 7 methods (HEAD asserts empty body) → **SMOKE TEST PASSED**. |

### New finding

| ID | Severity | Component | Summary |
|----|----------|-----------|---------|
| E2E-008 | LOW | CLI (`cachepilot_cli`) + dashboard backend (`server.py`) | A present-but-corrupt / non-SQLite telemetry DB file makes every CLI read command crash with an unhandled `sqlite3.DatabaseError: file is not a database` traceback (exit 1), and the dashboard `/api/*` endpoints return HTTP 500 JSON — both contradicting the documented contract that "a corrupt file, or an unreadable database is an honest empty store" (server.py:142-143, docs/dashboard.md). |

## E2E-008 — LOW — Corrupt/unreadable telemetry DB: CLI traceback + dashboard 500, not the documented honest-empty-store

- **Component**: `packages/cli/src/cachepilot_cli/churn.py:open_read_only_store`
  (returns a live store for ANY existing file — only missing files are caught)
  + `dashboard/backend/server.py:open_store` / `ReadOnlyTelemetryStore.connect`.
  Every `cachepilot <cmd>` read command and all 9 `/api/*` GET endpoints.
- **Reproduction** (live):
  1. `echo "not a database" > /tmp/cp-e2e5/text.db` and
     `head -c 2000 /dev/urandom > /tmp/cp-e2e5/corrupt.db`.
  2. `cachepilot status --db <corrupt.db>` → **raw Python traceback**,
     `sqlite3.DatabaseError: file is not a database`, **exit 1** (reproduced for
     `status`, `leases`, `costs`, `ttl`). No graceful message, no honest empty
     state — the traceback leaks the CLI entry point and internal call stack.
  3. `.venv/bin/python dashboard/backend/server.py --port 8796 --db <corrupt.db>`
     then `GET /api/status` and `GET /api/leases` → **HTTP 500**
     `{"error": "DatabaseError: file is not a database"}`.
- **Expected**: the never-fabricate read posture treats a corrupt/unreadable
  database like a missing one. `server.py:141-143` states: "a missing file, a
  corrupt file, or an unreadable database is an honest empty store, not an
  error page full of invented numbers"; docs/dashboard.md line 66-67 promises
  the CLI "renders the honest empty state". A read-only observability command
  should degrade to an honest empty state (or a clean, decidable error), never
  a raw `sqlite3` traceback or a 500.
- **Actual**: the CLI crashes with an unhandled traceback (exit 1) and the
  dashboard answers 500 — neither is the documented contract. The dashboard
  `_api` does fail open (a bad store never kills the request thread) and the
  500 body is JSON+serializable, so severity is LOW and read-only integrity is
  NOT violated; but the CLI behaviour is a usability defect (a corrupt DB is a
  common failure after an interrupted write or a stale/foreign file at the
  `--db` path).
- **Suggested fix direction** (NOT changed this tick — architectural, multiple
  files): in `open_read_only_store` and the dashboard's `open_store`, probe the
  file (e.g. `PRAGMA quick_check` / `SELECT count(*) FROM sqlite_master` on a
  scratch read-only connection) and treat a `sqlite3.DatabaseError` as the
  same "no readable telemetry" path as a missing file → honest empty state;
  have the CLI wrap store reads so a `DatabaseError` degrades to the documented
  empty output + a stderr notice instead of a traceback. Extend `smoke_test.py`
  with a corrupt-DB case asserting the honest-empty contract.

### RUN 5 zero-finding areas / gates

- Quality gate: **482 pytest passed in 43.37s**; `ruff check src/ packages/ dashboard/backend/` → "All checks passed!"; mypy authoritative CI invocation → **Success: no issues found in 74 source files**. `uv sync --group dev` clean.
- Frontend gate: `yarn install` + `yarn build` → **43 modules transformed**, success, `dist/` emitted.
- `dashboard/backend/smoke_test.py` → **SMOKE TEST PASSED** (seeded + empty store + relay probe + startup occupant detection + 7-method 405 contract + byte-identical read-only DB).
- All 8 CLI commands on the seeded DB return real, mutually consistent data: `status` (3 requests, hit rate 50.0% = 1/2, 1 CHIT / 1 MISS / 1 UNVERIFIED), `leases` (LEASE lease-00…  ARMED, 76s cache age, 300s TTL), `costs` ($0.000330 total, openai), `ttl` (288s estimate, 120-600s bounds, conf 0.85, 12 samples, survival P(survive)=1.00 n=4, median 300s), `routes` (1 switch, 1 route_instability verdict), `churn` (tools+cache key changed, cause "tool list mutation"), `explain-miss` (stable/changed layers, cause, conf 0.82, ~1234 tokens, first divergent byte), `topology` (2 sessions, 1 pair, tool schemas stability 0.0%, ~1,234 tokens).
- All 8 CLI commands on a MISSING DB: honest empty states, exit 0, no file/dir created (E2E-004). On the seeded DB all 9 `/api/*` endpoints return real JSON matching docs/dashboard.md schemas; empty-store endpoints return honest zeros/`[]`/null hit_rate; unknown `/api/*` → 404 JSON; static SPA serving (`/`, `/some/route`) serves `index.html`; `../package.json` traversal → 404 (root-escaped paths blocked), `/..%2f..%2fetc/passwd` → not leaked (served app HTML, candidate never escapes `dist/` root).
- Edge inputs clean: `/api/miss?session=zzz` → `{"event": null, "stable": [], "changed": []}`; invalid `CACHEPILOT_RELAY_LISTEN` → `Relay: unreachable (invalid CACHEPILOT_RELAY_LISTEN='...')`; unknown CLI subcommand → argparse error. Relay pass-through works (GET/POST forwarded to upstream; 501 from the GET-only mock upstream is the stub, not the relay).
- Browser/visual at 320/768/1280px and live console: NOT re-run this tick (CLI/API variant; browser follow-up lives with the Luna variant as in prior runs).

---

## RUN 7 — CLI/API variant (2026-08-16)

Run date: 2026-08-16 · repo `main` (E2E-001-R7 tick) · CLI/API E2E variant
(no browser). Fresh deploy: `uv sync --group dev` clean, **482 pytest passed
(40.34s)**, `ruff check src/ packages/ dashboard/backend/` → "All checks
passed!", `yarn build` → **43 modules transformed** (dist emitted),
`dashboard/backend/smoke_test.py` → **SMOKE TEST PASSED**. Live relay
(`cachepilotd` 127.0.0.1:9082 → mock upstream 9081) + live dashboard backend
(9083) on a seeded temp telemetry DB under `e2e-output/run7/`. Ports 8787/8788
are squatted by foreign processes on this host (hermes-webui, mcp serve), used
as the E2E-002 occupancy live-check.

### Re-verification of prior findings — ALL FIXED

| ID | Severity | Result | Evidence (live) |
|----|----------|--------|-----------------|
| E2E-002 | MEDIUM | **FIXED** | `cachepilot status` with relay on 9082 → `Relay: healthy`; dashboard `/api/status` with `CACHEPILOT_RELAY_LISTEN=127.0.0.1:9082` → `healthy`, with `=127.0.0.1:9099` (closed) → `unreachable`, with default (8787 squatted foreign python) → `occupied by another service`. Relay control `GET /cachepilot/health` → `{"service":"cachepilot-relay","status":"ok"}`; `HEAD`/`POST /cachepilot/health` pass through upstream (narrow PRD §27 GET-only interception preserved). Startup occupant detection + actionable errors covered by smoke PASS. |
| E2E-003 | LOW | **FIXED** | `POST`/`PUT`/`DELETE`/`PATCH /api/leases` → **405 application/json; charset=utf-8** `{"error":"the dashboard backend is read-only (GET only)"}`. |
| E2E-004 | LOW | **FIXED** | All 8 `cachepilot <cmd> --db /tmp/...missing.db` → stderr read-only notice + honest empty output, **exit 0**, **no file created** (ls confirms). |
| E2E-005 | LOW | **FIXED** | Same seeded DB: `status` → `route-change churn events 0` + footnote; `routes` → `route switches 1`. Disambiguated. |
| E2E-006 | MEDIUM | **FIXED** | `dashboard/src/styles.css:414` `@media (max-width: 768px)` collapses `.app`→1fr, `.sidebar`→row sticky top nav, hides `.brand-sub`/`.sidebar-foot`, single-column stat-grid/bar-row/layer-groups. Code/build state confirms (CLI/API variant). |
| E2E-007 | LOW | **FIXED** | `OPTIONS`/`TRACE`/`HEAD /api/leases` → **405 JSON**; `HEAD` returns 405 + JSON content-type + Content-Length but **0 body bytes** (HTTP HEAD semantics). smoke 7-method loop green. |
| E2E-008 | LOW | **FIXED** | Corrupt/garbage DB: all CLI reads → stderr "corrupt or not SQLite" notice + honest empty, **exit 0**, **no traceback**; dashboard `/api/*` on corrupt DB → all **200 empty JSON** (verified on 9086: status total=0, leases [], costs 0.0, etc.). smoke corrupt-DB case green. |

### New finding

| ID | Severity | Component | Summary |
|----|----------|-----------|---------|
| E2E-009 | LOW | CLI (`cachepilot_cli`) + dashboard backend (`server.py`) | A **valid SQLite file with the wrong/unrelated schema** (passes `PRAGMA quick_check`) makes every CLI read command crash with an unhandled `sqlite3.OperationalError: no such table: ...` traceback (exit 1) and the dashboard `/api/*` endpoints return **HTTP 500** — a continuity gap in the E2E-008 honest-empty-store contract, which only handles non-SQLite/corrupt-garbage files. |

## E2E-009 — LOW — Wrong-schema SQLite DB: CLI traceback + dashboard 500 (E2E-008 honest-empty gap on the read path)

- **Component**: `packages/cli/src/cachepilot_cli/churn.py:_is_readable_sqlite` +
  `open_read_only_store`; `dashboard/backend/server.py` identical
  `_is_readable_sqlite` + `open_store`. The probe only runs `PRAGMA quick_check`
  (a file-INTEGRITY check) and does NOT validate that the expected CachePilot
  schema tables exist; the read-only openers (`TelemetryStore(read_only=True)`,
  `ReadOnlyTelemetryStore`) deliberately skip `conn.executescript(_SCHEMA)`, so
  a wrong-schema file has no self-heal path.
- **Reproduction** (live):
  1. Create a VALID SQLite DB with an unrelated table:
     `python -c "import sqlite3;c=sqlite3.connect('/tmp/cp-e2e-wrongschema.db');c.execute('CREATE TABLE unrelated (id INTEGER PRIMARY KEY, name TEXT)');c.commit()"`.
  2. `_is_readable_sqlite()` on this file → **True** (it is a valid SQLite DB).
  3. `cachepilot churn --db /tmp/cp-e2e-wrongschema.db` → **raw Python traceback**
     `sqlite3.OperationalError: no such table: churn_events`, **exit 1**.
     Reproduced for ALL 8 read commands (`status`, `leases`, `costs`, `ttl`,
     `routes`, `churn`, `explain-miss`, `topology`) — each crashes on its first
     query (`no such table: request_events` / `leases` / `provider_profiles` /
     `route_events` / `churn_events`).
  4. Dashboard backend on the same file then `GET /api/status`, `/api/leases`,
     `/api/costs`, ... → **HTTP 500**
     `{"error":"OperationalError: no such table: request_events"}` (per-endpoint
     table name varies). All 9 endpoints 500; none return the honest-empty 200.
- **Expected**: identical to the E2E-008 contract — a present-but-unreadable
  telemetry path (a valid SQLite file that is not a CachePilot store: a foreign
  app's DB, a stale/different-schema file, tooling replaced the file) is an
  honest empty store. CLI: stderr notice naming the path + honest empty output,
  exit 0, no traceback. Dashboard: `/api/*` 200 empty JSON, no 500.
- **Actual**: `PRAGMA quick_check` only proves the file is a structurally valid
  SQLite DB — it says nothing about whether it has the CachePilot tables. The
  read-only openers skip schema creation (by design, to never modify the store),
  so the first real `SELECT` raises `OperationalError` and the read path crashes
  with a raw traceback (CLI) or a 500 (dashboard). Read-only integrity is NOT
  violated (nothing is written), severity LOW, but it is a genuine usability /
  honest-posture defect and a direct continuity gap of E2E-008's fix.
- **Suggested fix direction** (NOT changed this tick — tester): extend the
  `_is_readable_sqlite` probe to also confirm the expected schema is present,
  e.g. `SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN
  ('request_events', ...)` on the scratch read-only connection, and treat a
  schema mismatch exactly like the corrupt-file path (honest empty / 200). Also
  wrap CLI store reads (churn/main/topology handlers) so any `sqlite3.Error`
  during a read degrades to the documented honest-empty output + stderr notice
  instead of an unhandled traceback. Extend `smoke_test.py` with a
  valid-but-wrong-schema DB case asserting the honest-empty contract for both
  CLI and dashboard.

### RUN 7 zero-finding areas / gates

- Quality gate: **482 pytest passed (40.34s)**; `ruff check src/ packages/ dashboard/backend/` → "All checks passed!"; `uv sync --group dev` clean.
- Frontend gate: `yarn install` + `yarn build` → **43 modules transformed**, dist emitted.
- `dashboard/backend/smoke_test.py` → **SMOKE TEST PASSED** (seeded + empty + corrupt + relay probe + startup occupant detection + 7-method 405 + byte-identical read-only DB).
- Full CLI/API user journey on the seeded DB consistent: all 8 CLI commands return real, mutually consistent data (`status` 3 requests / 50.0% / churn 1; `leases` ARMED 118s/300s; `costs` $0.000330 openai; `ttl` 288s/120-600s/conf 0.85/survival P=1.00; `routes` 1 switch / route_instability; `churn` tools+cache key / "tool list mutation"; `explain-miss` cause+conf 0.82+~1234 tokens; `topology` 2 sessions / 1 pair / tool schemas 0.0%). All 9 `/api/*` GET endpoints return real JSON; write methods / HEAD/OPTIONS/TRACE all 405 JSON, HEAD body 0 bytes.
- Relay pass-through live-verified this tick: `GET /hello` and `POST /v1/chat/completions` forwarded to the mock upstream byte-identical; relay control `GET /cachepilot/health` intercepted (distinctive JSON) while `HEAD`/`POST /cachepilot/health` and non-control paths pass through unchanged.
- Browser/visual at 320/768/1280px and live console: NOT re-run this tick (CLI/API variant; browser follow-up lives with the Luna variant as in prior runs). E2E-006 verified by code/build state only.

---

## RUN 9 — 2026-08-16 — CLI/API variant (E2E-001-R9)

Run date: 2026-08-16 · repo `main` (E2E-001-R9 tick) · CLI/API E2E variant
(no browser). Fresh deploy + full CLI/API user journey against a live relay
and live dashboard backend; re-verify all prior findings (E2E-002..E2E-009);
hunt for new protocol/contract-level gaps on the read path. Test-only tick
— no source modified; only `e2e-output/report.md`, `e2e-output/tasks.md`,
`.coding-hermes/tasks.md`, and test fixtures under `e2e-output/run9/` changed.

**Verdict: 1 new LOW finding (E2E-010). All eight prior findings
E2E-002..E2E-009 re-verified as no-longer-reproducing (fixed).** No fabricated
numbers; the seeded store served honest, mutually consistent data on every
surface.

### Re-verification of prior findings — ALL FIXED

| ID | Severity | Result | Evidence (live) |
|----|----------|--------|-----------------|
| E2E-002 | MEDIUM | **FIXED** | `cachepilot status` with relay on 9082 → `Relay: healthy`; `CACHEPILOT_RELAY_LISTEN=127.0.0.1:9099` (closed) → `unreachable`; default (8787 squatted foreign python) → `occupied by another service` (never `healthy`). Relay control `GET /cachepilot/health` → `{"service":"cachepilot-relay","status":"ok"}`; `HEAD`/`POST /cachepilot/health` pass through upstream (narrow PRD §27). Startup occupant detection covered by smoke PASS. |
| E2E-003 | LOW | **FIXED** | `POST`/`PUT`/`DELETE`/`PATCH /api/leases` → **405 application/json; charset=utf-8** read-only refusal body. |
| E2E-004 | LOW | **FIXED** | All 8 `cachepilot <cmd> --db /tmp/r9-missing/telemetry.db` → honest stderr notice + empty output, **exit 0**, **no file/parent dir created**. |
| E2E-005 | LOW | **FIXED** | `status` → `route-change churn events 0` + footnote; `routes` → `route switches 1`. Disambiguated. |
| E2E-006 | MEDIUM | **FIXED** | `dashboard/src/styles.css:414` `@media (max-width:768px)` mobile collapse; code/build state confirms. |
| E2E-007 | LOW | **FIXED** | `OPTIONS`/`TRACE` → 405 JSON; `HEAD /api/leases` → 405 + JSON + `Content-Length: 58`, **0 body bytes**. |
| E2E-008 | LOW | **FIXED** | Corrupt DB → all CLI exit 0 no traceback + honest empty; all `/api/*` (9086) 200 empty JSON. |
| E2E-009 | LOW | **FIXED** | Wrong-schema SQLite DB → all 8 CLI exit 0 + stderr schema notice, no crash/leak; all `/api/*` (9087) 200 empty JSON (smoke 28 asserts). |

### New finding

| ID | Severity | Component | Summary |
|----|----------|-----------|---------|
| E2E-010 | LOW | dashboard backend (server.py `do_HEAD` → `_write_refused`) | **Every dashboard resource that returns 200 to GET answers `HEAD` with 405 + read-only JSON**, contradicting RFC 9110 §9.3.2 (HEAD must mirror GET: identical status + headers, no body). The E2E-003→E2E-007 uniform-405 fix was correct for mutating methods (POST/PUT/DELETE/PATCH/TRACE) but over-broadened to HEAD, a read-only method — so a `curl -I`/HEAD-based liveness check on `/`, `/api/health`, or any `/api/*` GET cannot confirm the dashboard is serving. |

## E2E-010 — LOW — Dashboard backend answers 405 to HEAD on every GET-200 resource (RFC 9110 §9.3.2 violation)

- **Component**: `dashboard/backend/server.py` `Handler.do_HEAD` → routes to
  `_write_refused(body=False)`. The body-suppression part is correct (HEAD
  omits the body) but the status/contract is wrong: HEAD is a read-only
  (GET-equivalent) method and must not be refused on a resource the backend
  is actively serving via GET.
- **Reproduction** (live, seeded dashboard on 9083):
  ```
  GET  /                    200 text/html         HEAD /                    405 application/json "read-only"
  GET  /leases (SPA)        200                    HEAD /leases              405 application/json
  GET  /assets/index-*.js   200 text/javascript   HEAD /assets/index-*.js    405 application/json
  GET  /api/health          200 {"ok":true}       HEAD /api/health          405 application/json
  GET  /api/leases          200 [...]             HEAD /api/leases          405 application/json
  ```
  `curl -s -I http://127.0.0.1:9083/` → `HTTP/1.0 405 Method Not Allowed`,
  `Content-Type: application/json; charset=utf-8`, `Content-Length: 58`
  (the read-only refusal body announced but not transmitted — HEAD semantics).
- **Expected**: RFC 9110 §9.3.2 — HEAD is identical to GET except the server
  MUST NOT send content. So HEAD on any GET-200 resource returns **200** with
  the same headers GET would send, no body. A `curl -I`/HEAD health check
  should confirm the dashboard is up and serving.
- **Actual**: HEAD is classified with the write-refusal methods → **405** on a
  read-only probe of a resource that GET-returns 200. Read-only integrity IS
  preserved (nothing written; seeded DB sha `636bbf30…` byte-stable across all
  GET + HEAD probes). Severity LOW: no data loss / no mutation; it is an
  HTTP-semantics contract defect and a practical gap for HEAD-based health /
  liveness monitoring (`curl -I`, some authed/proxy checkers).
- **Context / why it escaped prior runs**: the E2E-003 fix correctly made
  POST/PUT/DELETE/PATCH a machine-readable JSON 405, and E2E-007 (RUN 3)
  extended this to OPTIONS/TRACE/HEAD stating "HEAD omits the body but carries
  the JSON content-type" — that run asserted HEAD as **405**, treating it with
  the mutating bans. It did not check the GET-mirror semantic for HEAD, so the
  over-broadening was baked in and smoke_test.py only asserts HEAD → 405.
- **Suggested fix direction** (NOT changed this tick — tester): in `do_HEAD`,
  for any path that GET would serve with 200 (root/SPA fallback, static asset,
  `/api/*` GET endpoint), implement HEAD semantics by running the GET handler
  logic and suppressing the body (status 200, same headers, Content-Length of
  the would-be body, 0 body bytes). Keep 405 for truly-mutating methods
  (POST/PUT/DELETE/PATCH/TRACE) and for HEAD on an unknown/invalid route
  (`/api/not-an-endpoint` should stay 404/405 as appropriate). Extend
  `smoke_test.py` with a HEAD-on-read assertion (e.g. `/api/health` and `/`
  → 200, empty body) so the contract is gate-visible; update `docs/dashboard.md`
  (currently "every non-GET method (…/HEAD) is refused" at line 108/145).

### RUN 9 zero-finding areas / gates

- Quality gate: **482 pytest passed (41.53s)**; `ruff check src/ packages/ dashboard/backend/` → "All checks passed!"; mypy (CI invocation) → Success, 74 files; `uv sync --group dev` clean.
- Frontend gate: `cd dashboard && yarn build` → **43 modules transformed**, built in 1.98s, dist emitted.
- `dashboard/backend/smoke_test.py` → **SMOKE TEST PASSED** (seeded + empty + corrupt + wrong-schema + relay probe + startup occupant detection + 7-method 405 + byte-identical read-only DB).
- Full CLI/API user journey on the seeded DB consistent: all 8 CLI commands return real, mutually consistent data; all 9 `/api/*` GET endpoints return real JSON.
- Relay pass-through byte-identical live-verified: `GET /hello` and `POST` bodies byte-identical direct (9081) vs via relay (9082); upstream `x-upstream-marker: mock` preserved; control `GET /cachepilot/health` intercepted while `HEAD`/`POST /cachepilot/health` pass through upstream. Relay's default telemetry store (`~/.hermes/cachepilot/cachepilot.db`) written while relaying (observation fail-open preserved) — recorded, not a bug.
- Edge probes clean: empty-string `CACHEPILOT_TELEMETRY_DB` → honest default (`~/.hermes/cachepilot/cachepilot.db`) handle, no stray root file; `--db` on a directory path → honorable empty, exit 0; bogus/negative query params (`/api/leases?limit=-5`) → still 200 JSON; `/api/miss?session=` with `999999`/`null`/path-traversal → honest `{"event": null}`; `Accept: text/plain` → still JSON 200; HTTP/1.0 `/api/health` → 200 + Content-Length.
- Read-only proof: seeded DB sha `636bbf30…` byte-identical across all live reads + relay path.
- Browser/visual at 320/768/1280px and live console: NOT re-run this tick (CLI/API variant); E2E-006 verified by code/build state only.

### RUN 11 (2026-08-16) — 1 new finding (E2E-011); all nine prior FIXED

**Verdict: 1 new LOW finding (E2E-011, test-hygiene process leak).** All prior
E2E-002..E2E-010 re-verified FIXED with live command evidence (run11 artifacts
under `e2e-output/run11/`).

| ID | Severity | Result | Evidence (live, 9082/9083) |
|----|----------|--------|-----------------|
| E2E-002 | MEDIUM | **FIXED** | `CACHEPILOT_RELAY_LISTEN=127.0.0.1:9082` `status` → `Relay: healthy`; `=127.0.0.1:9099` (closed) → `unreachable`; `=127.0.0.1:9083` (foreign dashboard) → `occupied by another service`. Occupant detection: `cachepilotd` + `server.py` on squatted 9099 both **exit 2** with actionable errors naming port + override. Relay control `GET /cachepilot/health` intercepted; POST/OPTIONS + non-control HEAD pass through (mock 501/echo). |
| E2E-003 | LOW | **FIXED** | POST/PUT/DELETE/PATCH (/api/leases) → **405 application/json** read-only refusal. |
| E2E-004 | LOW | **FIXED** | `cachepilot status --db /tmp/r11-nodb/telemetry.db` → exit 0, stderr notice, honest empty; `ls` proves **no file / no parent dir created**. `--db` directory and `--db /dev/null` → same honest-empty exit 0. |
| E2E-005 | LOW | **FIXED** | `status` → `route-change churn events 0` + footnote; `routes` → `route switches 1`. |
| E2E-006 | MEDIUM | **FIXED** | `dashboard/src/styles.css:414` `@media (max-width:768px)` collapse; code/build state (yarn 43 modules built this tick). |
| E2E-007 | LOW | **FIXED** | OPTIONS/TRACE → 405 JSON; POST/PUT/DELETE/PATCH uniform JSON 405. |
| E2E-008 | LOW | **FIXED** | Corrupt DB → all 8 CLI exit 0 no traceback; all `/api/*` (9086) 200 empty JSON. |
| E2E-009 | LOW | **FIXED** | Wrong-schema SQLite → all 8 CLI exit 0, no traceback, **no "no such table" leak**; /api/* (9087) 200 empty JSON. |
| E2E-010 | LOW | **FIXED** | `curl -I /api/health`, `/`, `/leases` → **200, 0 body bytes**, headers mirror GET (RFC 9110 §9.3.2). |

**Gate + journey** (all green, live): `uv sync --group dev` clean; **482
pytest passed (-x -q, 39.82s)**; `ruff check src/ packages/ dashboard/backend/`
→ All checks passed!; mypy (CI invocation) → Success, 74 files; `yarn build` →
43 modules (2.00s); `smoke_test.py` → SMOKE TEST PASSED (144 PASS, exit 0);
relay 9082→9081 pass-through GET/POST **byte-identical** incl. an upstream
**503 forwarded byte-identical** (relay 9097→9092); dashboard 9083 all 9
`/api/*` real JSON; all 8 CLI read commands consistent.

**Edge probes clean** (no new defect): `Accept: text/plain` → JSON;
`/api/leases?limit=-5|abc|999999`, `offset=-1` → 200; `/api/miss?session=`
hostile values → honest `{event:null}`; `/api/not-an-endpoint` 404,
`/api/leases/` 404, `/api` 200 SPA; traversal `/etc/passwd` + `%2e%2e/…` →
index.html (no disclosure); relay control-path `?x=1|/|//` forwarded; `--db`
dir & `/dev/null` → honest empty exit 0; `CACHEPILOT_TELEMETRY_DB=""` → no
stray file; read-only sha `899f6d6b…` byte-stable.

**Observations (recorded, NOT defects):** (1) `uv run ruff check src/` prints
`[]` (JSON empty-findings) while `uv run --group dev ruff check .` (CI form),
`.venv/bin/ruff …`, and `--output-format full` all print "All checks passed!"
— same 0.16.2 binary + same config, exit 0 + zero findings in every variant
(cosmetic local quirk). (2) Relay answers `HEAD /cachepilot/health` locally
with a GET-mirror (200, CL 44, 0 body) via Starlette auto-HEAD on the GET
route — HTTP-correct per RFC 9110 §9.3.2 and consistent with the E2E-010
principle; POST/OPTIONS and non-control HEAD still pass through.

### New finding — RUN 11

| ID | Severity | Component | Summary |
|----|----------|-----------|---------|
| E2E-011 | LOW | E2E harness / run teardown hygiene | **E2E ticks leak their test services across runs** — run 9's mock upstream (`e2e-output/run9/mock_upstream.py 9081`, pid 2955253) and relay (`cachepilotd --listen 127.0.0.1:9082`, pid 2955310) were STILL ALIVE ~4h later (started Sat Aug 15 21:59:59 / 22:00:01, surviving the E2E-010 fix tick + 10 idle ticks), contradicting run-9's "All services were killed after the tick" claim. Run 11's fresh binds FAILED (`Address already in use`, exit 1/2) and its initial relay evidence ran against these stale run-9 survivors. Verified functionally identical to a fresh instance (same installed binary/source) and re-verified this tick against freshly-started services; leaks then killed. |

## E2E-011 — LOW — E2E ticks leak ephemeral test services across runs (run-9 mock + relay alive ~4h after "all killed")

- **Component**: E2E verification harness / run teardown procedure (not a
  product defect — no product code involved).
- **Reproduction** (live, this host, during Run 11):
  ```
  # 1. Run 11 starts its own mock upstream on 9081 -> bind FAILS (exit 1):
  python e2e-output/run11/mock_upstream.py 9081
  #   OSError: [Errno 98] Address already in use
  # 2. Run 11 starts its own relay on 9082 -> bind FAILS (exit 2):
  cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081
  #   cachepilotd: error: listen address 127.0.0.1:9082 is already in use ...
  # 3. The ports are held by RUN-9 survivors (checked via ss/ps):
  ss -tlnp   ->  127.0.0.1:9081  python  e2e-output/run9/mock_upstream.py 9081   (pid 2955253, started Sat Aug 15 21:59:59)
                 127.0.0.1:9082  cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081  (pid 2955310, started Sat Aug 15 22:00:00)
  ```
  run-9 `e2e-output/report.md` + `tasks.md` state "All services were killed
  after the tick" — contradicted by pids/alive-since times above.
- **Expected**: after every E2E tick every ephemeral test service is
  terminated and the host is verified clean, so Run N+1 always binds fresh
  current-build services.
- **Actual**: two test processes from run 9 survived ~4 hours across the
  E2E-010 fix tick and 10 subsequent idle ticks; Run 11 initially verified the
  relay footprint against these stale survivors (functionally equivalent —
  same installed binary/source — the evidence was re-confirmed this tick
  against freshly-started services, then the leaks were killed). Low severity:
  no product/data impact; it leaks ~2 small processes and contaminates
  verification integrity / wastes debugging time on "why did my bind fail".
- **Fix direction** (test-hygiene, not product code): (a) teardown in each E2E
  tick uses `trap`/`kill $PIDS` for every service it spawned and re-verifies
  with `ss`/`ps` that none remain; (b) add a pre-run guard that fails (or
  cleans) if any `e2e-output/*/mock_upstream.py` / `cachepilotd --listen` /
  `dashboard/backend/server.py` test process is already listening, so a stale
  process cannot silently become the "live" target; (c) treat the 908x
  ephemeral range as reserved test-only and document it.

### RUN 12 (2026-08-16) — no new finding; all ten prior FIXED

**Verdict: no new finding this run — a zero-findings tick.** All prior
E2E-002..E2E-011 re-verified FIXED with live command evidence (run12 artifacts
under e2e-output/run12/). Edge-probe batch clean. No E2E-012 filed.

| ID | Severity | Result | Evidence (live, 9082/9083) |
|----|----------|--------|-----------------|
| E2E-002 | MEDIUM | **FIXED** | healthy/unreachable/occupied readouts via CACHEPILOT_RELAY_LISTEN; cachepilotd on 9083 and server.py on 9097 both **exit 2** with actionable errors naming port + override. Relay control GET /cachepilot/health intercepted (distinct JSON); POST-on-control + OPTIONS + non-control HEAD pass through. |
| E2E-003 | LOW | **FIXED** | POST/PUT/DELETE/PATCH /api/health -> **405 application/json** read-only refusal. |
| E2E-004 | LOW | **FIXED** | status --db /tmp/r12-nodb/... exit 0, honest-empty notice, no file / no parent dir; --db dir + /dev/null honest-empty exit 0. |
| E2E-005 | LOW | **FIXED** | status route-change churn events 0 + footnote; routes route switches 1. |
| E2E-006 | MEDIUM | **FIXED** | styles.css:414 @media (max-width:768px) present in source AND built CSS (code/build). |
| E2E-007 | LOW | **FIXED** | OPTIONS/TRACE -> 405 JSON; uniform with POST/PUT/DELETE/PATCH. |
| E2E-008 | LOW | **FIXED** | Corrupt random-bytes DB -> all 8 CLI reads exit 0 no traceback; dashboard /api/* (9086) 200 empty JSON. |
| E2E-009 | LOW | **FIXED** | Wrong-schema (unrelated table) -> all 8 CLI reads exit 0, no traceback, no "no such table"; dashboard /api/* (9087) 200 empty JSON. |
| E2E-010 | LOW | **FIXED** | HEAD /api/health, /, /leases, /assets/*.js -> 200, 0 body, content-length mirrors GET. |
| E2E-011 | LOW | **FIXED** | hygiene.py self-test exit 0; guard + trap teardown used live on every spawned service; post-run ss confirms 908x clean, no leaker. |

**Gate + journey** (all green, live): uv sync --group dev clean; **488 pytest
passed (-x -q, 55.44s)**; ruff check src/ packages/ dashboard/backend/ -> All
checks passed!; mypy (uvx --native-parser --python-version 3.12
--follow-imports=skip src packages) -> Success, 74 files; yarn build -> 43
modules (1.75s); smoke_test.py -> SMOKE TEST PASSED (144 PASS, exit 0); relay
9082->9081 pass-through GET/POST byte-identical incl. upstream **503 forwarded
byte-identical** (relay 9097->9092); dashboard 9083 all 9 /api/* real JSON;
all 8 CLI read commands consistent; seeded DB sha e11c5b66e627... byte-stable
(read-only).

**Edge probes clean** (no new defect): Accept: text/plain -> JSON; /api/leases
limit=-5|abc|999999, offset=-1 -> 200; /api/miss hostile values -> honest
{event:null}; /api/not-an-endpoint 404, /api/leases/ 404, bare /api 200 SPA;
traversal %2e%2e/etc/passwd -> index.html (no disclosure); relay control-path
query-string/trailing-slash/double-slash answered 200 no interception leak;
CLI --db directory + /dev/null -> honest-empty exit 0.

**Observations (recorded, NOT defects):** none new this run — the two cosmetic
quirks seen in Run 11 (uv run ruff check src/ printing [], relay local
HEAD-mirror on the control path) are unchanged and documented; neither affects
the gate verdict.

### New finding — RUN 12

**None.** Zero-findings tick; all prior E2E-002..E2E-011 re-verified FIXED.
No E2E-012 task added to the board.

---

### RUN 13 (2026-08-16) — zero new finding; all prior E2E-002..E2E-011 FIXED

**Worker:** DS-V4-Flash (CLI/API, test-only tick — no src/ packages/
dashboard/backend/ logic modified). **Repo:** cachepilot @ main, clean.

**Fresh deploy (quality gate) — all green:** uv sync --group dev → 33 pkgs /
32 checked OK; **488 pytest (-x -q) 54.52s**; ruff check src/ packages/
dashboard/backend/ → All checks passed!; mypy (uvx --native-parser
--python-version 3.12 --follow-imports=skip src packages) → Success, 74 files;
dashboard yarn build → 43 modules (1.77s); dashboard/backend smoke_test.py →
SMOKE TEST PASSED (exit 0).

**Live user journey (fresh current-build services, E2E-011 hygiene used
throughout):** relay 9082→9081 mock upstream — control `GET /cachepilot/health`
→ 200 `{"service":"cachepilot-relay","status":"ok"}` (CL 44); relay GET
`/upstream/thing` **byte-identical** pass-through body
`{"ok": true, "upstream": "mock"}` + `x-upstream-marker: mock`; relay POST
`/cache/echo` body `HELLO-13-000` **byte-identical** echo + marker. Dashboard
backend 9083 on seeded telemetry DB — all 9 `/api/*` GET
(health/status/leases/costs/ttl/churn/routes/topology/miss) → real seeded
JSON. All 8 CLI read commands (status/leases/costs/ttl/churn/explain-miss/
routes/topology) consistent; seeded DB sha byte-stable before/after
(read-only proven).

**Re-verification of all prior findings (live evidence):** E2E-002 relay
readouts healthy(9082)/unreachable(9998)/occupied→unreachable(9091 foreign);
startup occupant detection → **both `cachepilotd` (9089) and
`dashboard/backend/server.py` (9084) exit 2** with actionable error naming
port + override. E2E-003/007 uniform **405 application/json; charset=utf-8**
for POST/PUT/DELETE/PATCH/OPTIONS/TRACE on /api/status (read-only refusal).
E2E-004 missing `--db` path → exit 0 honest-empty, **no file / no parent dir
created**. E2E-005 status `route-change churn events 0`+footnote vs routes
`route switches 1` (disambiguated). E2E-006 `styles.css:414 @media
(max-width: 768px)` + in built CSS bundle. E2E-008/009 corrupt (random bytes)
+ wrong-schema (unrelated table) → all 8 CLI read commands exit 0, no
traceback, no "no such table"; dashboard `/api/*` on 9086/9087 → 200
`{"leases":[]}` empty JSON. E2E-010 HEAD mirrors GET → 200, 0 body, CL
mirrors (incl. real asset `/assets/index-NcpMpYl1.js` CL=162662).

**Teardown / hygiene:** `e2e-output/hygiene.py self-test` exit 0; pre-run
guard + trap teardown used on every spawned service; post-run scan → **908x
clean, zero listeners leaked**.

### New finding — RUN 13

**None.** Zero-findings tick; all prior E2E-002..E2E-011 re-verified FIXED
with live command evidence. No E2E-012 task added to the board.

### RUN 14 (2026-08-16) — zero new finding; all prior E2E-002..E2E-011 FIXED

**Worker:** DS-V4-Flash (CLI/API, test-only tick — no src/ packages/
dashboard/backend/ logic modified). **Repo:** cachepilot @ main, clean.

**Fresh deploy (quality gate) — all green:** uv sync --group dev → 33 pkgs /
32 checked OK; **488 pytest (-x -q) 74.08s**; ruff check src/ packages/
dashboard/backend/ → All checks passed!; mypy (uvx --native-parser
--python-version 3.12 --follow-imports=skip src packages) → Success, 74 files;
dashboard yarn build → 43 modules (2.10s); dashboard/backend smoke_test.py →
SMOKE TEST PASSED (144 PASS, exit 0).

**Live user journey (fresh current-build services, E2E-011 hygiene used
throughout):** relay 9082→9081 mock upstream — control `GET /cachepilot/health`
→ 200 `{"service":"cachepilot-relay","status":"ok"}` (CL 44); relay GET
`/upstream/resource` **byte-identical** pass-through body
`{"ok": true, "upstream": "mock"}` + `x-upstream-marker: mock`; relay POST
`/upstream/posts` body `{"payload":"echo-me-14"}` **byte-identical** echo +
marker; upstream 503 forwarded **byte-identical** through relay 9097→9092.
Dashboard backend 9083 on seeded telemetry DB — all 9 `/api/*` GET
(health/status/leases/costs/ttl/churn/routes/topology/miss) → real seeded
JSON. All 8 CLI read commands (status/leases/costs/ttl/churn/explain-miss/
routes/topology) consistent; seeded DB sha `33ba841e` byte-stable before/after
(read-only proven).

**Re-verification of all prior findings (live evidence):** E2E-002 relay
readouts `Relay: healthy`(9082)/`unreachable`(9998 closed)/`unreachable`(9091
foreign); startup occupant detection → **both `cachepilotd` (9089) and
`dashboard/backend/server.py` (9084) exit 2** with actionable error naming
port + override. E2E-003/007 uniform **405 application/json; charset=utf-8**
for POST/PUT/DELETE/PATCH/OPTIONS/TRACE on /api/status (read-only refusal).
E2E-010 HEAD mirrors GET → 200, 0 body, CL mirrors (incl. real asset
`/assets/index-*.js` CL=547). E2E-004 missing `--db` path → exit 0
honest-empty, **no file / no parent dir created**. E2E-005 status `churn
events 1`+`route-change churn events 0` footnote vs routes `route switches 1`
(disambiguated). E2E-006 `styles.css:414 @media (max-width: 768px)` + in built
CSS bundle. E2E-008/009 corrupt (random bytes) + wrong-schema (unrelated
table) → all 8 CLI read commands exit 0, no traceback, no "no such table";
dashboard `/api/*` on 9086/9087 → 200 `{"leases":[]}` empty JSON. E2E-011
`hygiene.py self-test` exit 0 + guard/trap teardown + post-run **908x clean,
zero leaker**.

**Edge-probe batch (new-defect hunt) — all clean:** hostile params
`/api/leases?limit=-5|abc|999999&offset=-1` → 200 no crash; hostile
`/api/miss?session=../../etc/passwd|%00|a%20b|999999|null` → 200 honest
`{"event":null}` JSON; 404s correct (`/api/not-an-endpoint`,
`/api/leases/`, `/api//leases`); static traversal `/../../etc/passwd`,
`/%2e%2e/etc/passwd`, `/..%2f..%2fetc/passwd` → 200 SPA `index.html` (547B,
no path disclosure); HTTP/1.0 `/api/health` → 200 + CL + content-type; relay
control-path encodings (query-string / trailing-slash / double-slash / case) →
no interception leak; `--db` empty-file / directory / `/dev/null` → honest-empty
exit 0, no traceback.

**Teardown / hygiene:** pre-run guard + trap teardown used live on every
spawned service; `hygiene.py self-test` exit 0; post-run scan → **908x clean,
zero listeners leaked**.

### New finding — RUN 14

**None.** Zero-findings tick; all prior E2E-002..E2E-011 re-verified FIXED
with live command evidence; edge-probe batch clean. No E2E-012 filed. Only a
test-artifact observation recorded (not a defect): `hygiene.py self-test`'s
`--clean` step auto-kills all 908x occupants including a live tick's own
services, so verifications depending on live 908x services must run BEFORE
the self-test step (see run14/README.md).
