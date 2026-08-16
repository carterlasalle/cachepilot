# Run 13 — CLI/API E2E tick (E2E-001-R13) — test artifacts

Live verification notes for the 2026-08-16 CLI/API E2E run. Full evidence in
`e2e-output/report.md` (Run 13) + `e2e-output/tasks.md` (RUN 13). **Zero new
findings this run — everything re-verified FIXED, edge probes clean.** The
E2E-011 test-hygiene guard/teardown (`e2e-output/hygiene.sh` →
`e2e_guard_pre_run`/`e2e_wrap`/`e2e_spawn`/`e2e_teardown`) was used live on
every spawned service and its premise holds: 908x range clean pre-run, every
service trap-killed, no listener leaked post-run.

## Fixtures

- `mock_upstream.py` — byte-echo upstream (port from argv; default 9081), adds
  `x-upstream-marker: mock`. Copied from run12.
- `upstream_503.py` — always 503 (Content-Length 0) upstream for byte-identical
  forward probe.
- `seed.py` — seeds a telemetry DB via the exact `seed_store()` from
  `dashboard/backend/smoke_test.py`. Telemetry DB kept in /tmp, not committed
  (run7/9/11/12 precedent).
- `make_wrongschema.py` — writes an unrelated-schema (wrong) SQLite DB.
- `live_journey.sh` — full live user journey + re-verification of
  E2E-002..E2E-011 (sources hygiene.sh; `trap - ERR` after spawn so the body's
  expected fail-fast commands can't self-teardown mid-run).
- `pass2.sh` / `pass3.sh` — focused re-verification passes (correct IQ
  endpoint for the 405, live relay readouts, byte-identical pass-through,
  real-asset HEAD mirror, teardown verification).

## Services used (ephemeral, all killed after the tick)

- mock upstream: `python e2e-output/run13/mock_upstream.py 9081`
- relay: `cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081`
  (+ 503 relay 9097 → 9092)
- dashboard backend 9083 on seeded DB + 9086 (corrupt) + 9087 (wrong-schema)
  + 9088 (nonexistent --db path)

## Quality gate (all green)

```
uv sync --group dev                    → Resolved 33 packages / Checked 32, OK
uv run pytest -x -q                    → 488 passed in 54.52s
./.venv/bin/ruff check src/ packages/ dashboard/backend/  → All checks passed! (exit 0)
uvx mypy --python-executable .venv/bin/python --native-parser
     --python-version 3.12 --follow-imports=skip src packages → Success: no issues in 74 files
cd dashboard && yarn build             → 43 modules transformed, built in 1.77s
./.venv/bin/python -m smoke_test        → SMOKE TEST PASSED, exit 0
```

## Live user journey + re-verification summary

Relay 9082→9081: control `GET /cachepilot/health` → 200
`{"service":"cachepilot-relay","status":"ok"}` (CL 44); GET + POST pass-through
**byte-identical** (`x-upstream-marker: mock`); real JS asset HEAD → 200/0-body.
Dashboard 9083 → all 9 `/api/*` real seeded JSON; all 8 CLI reads consistent;
seeded DB byte-stable.

All prior E2E-002..E2E-011 re-verified FIXED with live evidence: relay
readouts + startup occupant detection exit 2 on both daemons; uniform JSON 405
(+ HEAD mirrors GET 200/0-body); missing --db never created exit 0 honest
empty; churn-vs-switches disambiguated; corrupt + wrong-schema honest-empty
exit 0 + dashboard 200 empty JSON; 320px mobile via code/build state.

## New finding

**None.** Zero-findings tick; no E2E-012 filed.

> Note: in the first journey pass the relay-facing write probes (E2E-003/007)
> were against the relay socket, which is correct-only forwarder — the uniform
> JSON 405 contract lives on `dashboard/backend/server.py` `/api/*`, verified
> in pass2 against 9083. This tick's finding table uses the dashboard evidence.