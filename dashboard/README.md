# CachePilot Dashboard

Optional read-only observability UI for the CachePilot telemetry store
(PRD §122/§139). React + TypeScript frontend, yarn-managed, backed by a
read-only Python JSON server. **Never a core dependency** — the core product
does not import this directory.

```bash
# 1. backend (from the repo root — uses the workspace venv)
uv run python dashboard/backend/server.py          # 127.0.0.1:8788

# 2. frontend (new terminal)
yarn install && yarn dev                            # http://127.0.0.1:5173
```

For everything — API surface, views, empty-state behavior, verification —
see [`docs/dashboard.md`](../docs/dashboard.md).
