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
| any other `GET` | 404; `POST`/writes are refused with 405 (the backend is read-only) |

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
after the whole read session, and confirms writes are refused (405).

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
  plugin.
- If port 8788 is taken, pass `--port` and update the Vite proxy in
  `dashboard/vite.config.ts`.
