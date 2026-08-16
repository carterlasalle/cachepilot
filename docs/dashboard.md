# CachePilot Dashboard — Runbook

The optional observability dashboard (PRD §122 "Dashboard", §139 "Phase 12 —
Optional UI"). React/TypeScript frontend, yarn-managed, backed by a small
read-only HTTP server that serves the telemetry store's data as JSON.

**The dashboard is optional and never a core dependency.** `core`,
`hermes-plugin`, `relay` and `cli` never import dashboard code, and the uv
workspace does not include `dashboard/` — the core product is byte-identical
with or without it (PRD §139). Removing `dashboard/` changes nothing.

## Layout

```text
dashboard/
├── package.json          # yarn-managed frontend (REQUIRED: yarn, never npm/pnpm)
├── yarn.lock             # committed; node_modules/ is gitignored
├── .yarnrc.yml           # node-modules linker (classic layout)
├── index.html            # Vite entry
├── vite.config.ts        # dev server 127.0.0.1:5173, /api proxy → 127.0.0.1:8788
├── src/                  # React + TypeScript views
├── backend/
│   ├── server.py         # read-only telemetry JSON backend (stdlib http.server)
│   └── smoke_test.py     # self-contained backend verification (not in pytest)
└── dist/                 # yarn build output (gitignored)
```

## Prerequisites

- Python 3.12 + `uv` (repo root workspace — the backend imports
  `cachepilot_core` from the workspace venv)
- Node ≥ 18 + yarn (any yarn ≥ 1.22 works; developed against yarn 4)

## 1. Install frontend dependencies

```bash
cd dashboard
yarn install        # creates node_modules/ + yarn.lock (yarn.lock is committed)
```

## 2. Start the read-only backend

From the repo root (so `uv` resolves the workspace venv that contains
`cachepilot_core`):

```bash
uv run python dashboard/backend/server.py
```

Options: `--db PATH` (telemetry DB; default `CACHEPILOT_TELEMETRY_DB`, else
`~/.hermes/cachepilot/cachepilot.db`), `--host`, `--port` (default
`127.0.0.1:8788`).

Read-only guarantees:

- The store is opened with SQLite's `mode=ro` URI and the schema
  creation/migration statements are skipped entirely — the database file is
  never modified (the smoke test proves it byte-identical).
- SQLite's standard WAL journal bookkeeping may create empty `-wal`/`-shm`
  sidecar files next to a cleanly-closed WAL database — the same artifacts
  any read-only connection (including the CLI's reads) leaves behind. They
  are journal files, not the database.
- The `cachepilot` CLI opens the telemetry database the same read-only way
  (`mode=ro`, no schema work): a missing `--db` / `CACHEPILOT_TELEMETRY_DB`
  path is never created — the CLI prints a notice naming the path and
  renders the honest empty state (the relay creates the DB on its first
  write).
- A present-but-corrupt or non-SQLite store is treated exactly like a
  missing one (E2E-008), as is a VALID SQLite file whose schema lacks the
  expected CachePilot telemetry tables (E2E-009). Both openers (the CLI's
  `open_read_only_store` and the dashboard's `open_store`) run an up-front
  read-only probe on a scratch `mode=ro` connection before returning a
  store: `PRAGMA quick_check`, then `sqlite_master` to confirm the expected
  telemetry tables are present. If that raises `sqlite3.Error`, or any
  expected table is absent, the file is corrupt/not-SQLite/wrong-schema and
  is handled as an honest empty store — the CLI prints a stderr notice
  naming the path and exits 0 with no traceback, and every dashboard
  endpoint returns HTTP 200 with its empty state (zeros / empty lists),
  never a 500.
- Endpoints expose the SAME query surface as the `cachepilot` CLI
  (`status`, `leases`, `costs`, `ttl`, `routes`, `churn`, `explain-miss`,
  `topology`). Nothing is invented: a missing/empty DB renders empty states,
  never fabricated numbers (AGENTS.md invariants 3/10).

## 3. Start the frontend

```bash
cd dashboard
yarn dev            # http://127.0.0.1:5173 (proxies /api → 127.0.0.1:8788)
```

Production mode (no dev server): `yarn build`, then the backend serves
`dist/` at `http://127.0.0.1:8788/` directly.

## API

| Endpoint | Data (all read-only, mirror the CLI) |
|---|---|
| `GET /api/status` | Cache-health aggregates (requests, hit rate, per-outcome counts, churn/route counts), relay + plugin state, per-provider summary |
| `GET /api/leases` | Live lease snapshots (state, targets, cache age, TTL, warm cost) |
| `GET /api/costs` | Recorded-cost-only totals per provider + cumulative series (never "money saved") |
| `GET /api/ttl` | Route-keyed TTL profiles + per-profile survival curve (P(survive), median) |
| `GET /api/routes` | Observed route identities + instability stats (verdicts, switches) |
| `GET /api/churn` | Per-layer change frequency + most common likely causes |
| `GET /api/miss?session=` | Latest (or session-scoped) miss explanation: stable/changed layers, cause, confidence, prefix loss |
| `GET /api/topology` | Cross-request prefix topology + tool-ordering stability |
| `GET /api/health` | `{"ok": true}` connectivity probe |
| `HEAD <any-uri>` | Mirrors the matching `GET` exactly — same status + headers (incl. `Content-Length`), but zero response-body bytes (RFC 9110 §9.3.2) |
| any other `GET` | 404; every write / non-`GET` method (`POST`/`PUT`/`DELETE`/`PATCH`/`OPTIONS`/`TRACE`) is refused with the same machine-readable JSON 405 (the backend is read-only) |

The route counters are deliberately different measurements (PRD §25 vs §72):
`GET /api/status` `route_changes` counts churn events with `route_changed=1`
(churn-attributed), while `GET /api/routes` `route_switches` counts all
`route_events` rows (observed switches). The CLI mirrors this as
"route-change churn events" (`cachepilot status`) vs "route switches"
(`cachepilot routes`); the JSON field names are unchanged.

## Views

| View | Shows | Empty state |
|---|---|---|
| Overview | Hit rate, per-outcome counts, churn/route counts, relay + plugin state, per-provider table | "No provider telemetry recorded yet" |
| Live leases | Lease table polling every 5s: state badge, targets, cache age, TTL, confidence, warm count | "No active leases recorded yet" |
| Cache topology | Per-layer change frequency bars + stability %, tool-ordering stability per route | "No consecutive request pairs recorded yet" |
| Cost graph | Recorded cost per provider + cumulative line over the most recent 200 rows | "No recorded costs yet" |
| TTL learning | Profile cards: TTL estimate, bounds, confidence, samples + survival curve | "No TTL profiles yet" |
| Route changes | Instability stats + route-identity event table with verdicts | "No observed route changes yet" |
| Churn | Per-layer change frequency, top causes, recent events | "No churn events" |
| Miss explanation | Stable/changed layers, likely cause, confidence, estimated prefix loss | "No churn events recorded — nothing to explain" |

Every empty state is a real empty store, never a fabricated number. Unknowns
render as `n/a` / `unknown` exactly like the CLI.

## Verification

The backend ships a self-contained smoke test (not part of the pytest suite,
so the optional dashboard never affects the core quality gate):

```bash
uv run python dashboard/backend/smoke_test.py
```

It seeds a temp DB via `TelemetryStore`, serves every endpoint, asserts the
populated + empty-store responses, proves the DB file is byte-identical
after the whole read session, confirms every write / non-`GET` method
(`POST`/`PUT`/`DELETE`/`PATCH`/`OPTIONS`/`TRACE`) is refused with the
machine-readable JSON 405, and asserts `HEAD` mirrors `GET` (same status +
headers, zero body) for API and static/SPA resources per RFC 9110 §9.3.2
(E2E-010).

Frontend gate:

```bash
cd dashboard
yarn build          # tsc --noEmit && vite build
```

## Notes

- `dashboard/` is deliberately NOT in `tool.uv.workspace.members`; it has no
  Python packaging of its own and needs no `__init__.py`.
- The backend only depends on the workspace `cachepilot_core` (plus the
  Python standard library). It does not import `cachepilot_relay` or the
  plugin (the relay probe's control path is mirrored here and pinned by the
  smoke test, which probes a REAL relay).
- If port 8788 is taken, pass `--port` and update the Vite proxy (see
  "Stock-Hermes hosts" below).

## Stock-Hermes hosts: default-port overlap (E2E-002)

CachePilot's two default ports are the same ones stock Hermes companion
processes use, so on a stock Hermes host BOTH defaults may already be taken:

| CachePilot default | Stock-Hermes occupant |
|---|---|
| Relay (`cachepilotd`) `127.0.0.1:8787` (PRD §26) | `hermes-webui` — `HERMES_WEBUI_PORT` default 8787 (`hermes-webui/api/config.py:51`) |
| Dashboard backend `127.0.0.1:8788` | `hermes_cli`'s `mcp serve` companion |

Both servers detect the collision at startup and fail with a clear,
actionable error naming the occupied address and the override — never a
bare `OSError: Address already in use`:

```text
cachepilotd: error: listen address 127.0.0.1:8787 is already in use — another process owns it ...
[dashboard] error: listen address 127.0.0.1:8788 is already in use — another process owns it ...
```

Overrides:

- **Relay**: `cachepilotd --listen 127.0.0.1:<free-port>` or
  `CACHEPILOT_RELAY_LISTEN=127.0.0.1:<free-port>`; Hermes' provider base URL
  must point at the same address.
- **Dashboard backend**: `uv run python dashboard/backend/server.py --port <free-port>`
  AND update the `/api` proxy target in `dashboard/vite.config.ts` (the dev
  server proxies to `127.0.0.1:8788` by default; production `dist/` is
  served same-origin by the backend, so only the dev flow needs the proxy
  change).

## Relay health field

`GET /api/status` → `relay` (and `cachepilot status` → `Relay:`) is an HTTP
probe of the relay's local control endpoint — `GET /cachepilot/health`, a
path the relay answers itself and never forwards upstream (a narrow PRD §27
deviation reserved for liveness). 'healthy' is only reported when that
endpoint answers with its distinctive body, so the readout reflects actual
CachePilot relay presence, not "anything listening on the port":

| Value | Meaning |
|---|---|
| `healthy` | The CachePilot relay is running on the listen address (control endpoint answered) |
| `occupied by another service` | An HTTP server answered, but it is NOT the CachePilot relay (e.g. `hermes-webui` on 8787, or the MCP server on 8788) |
| `unreachable` | Nothing answered: port closed, a listener that never speaks HTTP, or an invalid `CACHEPILOT_RELAY_LISTEN` |

A bare TCP connect no longer counts as healthy: with no `cachepilotd`
running but `hermes-webui` on 8787, the readout is `occupied by another
service`, never `healthy`.
