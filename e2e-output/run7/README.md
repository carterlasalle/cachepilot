# Run 7 — CLI/API E2E tick (E2E-001-R7) — test artifacts

Live verification notes for the 2026-08-16 CLI/API E2E run. Full evidence in
`e2e-output/report.md` + `e2e-output/tasks.md`; the finding filed as
`E2E-009` on `.coding-hermes/tasks.md`.

## Services used (ephemeral ports, all killed after the tick)

- mock upstream: `python3 e2e-output/run7/mock_upstream.py` → 127.0.0.1:9081
- relay: `uv run cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081`
- dashboard backend (seeded DB): `uv run python dashboard/backend/server.py --db e2e-output/run7/telemetry.db --port 9083` (+ 9084/9085 for relay-readout env variants, 9086 corrupt-DB, 9087 wrong-schema-DB)
- fixture seed: the exact `seed_store()` logic from `dashboard/backend/smoke_test.py`
  written into `e2e-output/run7/telemetry.db` (deleted post-tick — binary artifact).

## E2E-009 reproduction (the new finding)

A VALID SQLite file with an unrelated schema passes the `_is_readable_sqlite`
probe (`PRAGMA quick_check` is integrity-only) and then crashes the read path:

```
python -c "import sqlite3;c=sqlite3.connect('/tmp/cp-e2e-wrongschema.db');c.execute('CREATE TABLE unrelated (id INTEGER PRIMARY KEY, name TEXT)');c.commit()"
# probe: dashboard.backend.server._is_readable_sqlite(Path(p)) -> True
uv run cachepilot churn --db /tmp/cp-e2e-wrongschema.db
#   -> Traceback ... sqlite3.OperationalError: no such table: churn_events, exit 1
#   (reproduced for ALL 8 read commands)
uv run python dashboard/backend/server.py --port 9087 --db /tmp/cp-e2e-wrongschema.db
curl http://127.0.0.1:9087/api/status
#   -> HTTP 500 {"error":"OperationalError: no such table: request_events"}
#   (all 9 endpoints 500)
```