# Run 14 — CLI/API E2E tick (E2E-001-R14) — test artifacts

Live verification notes for the 2026-08-16 CLI/API E2E run. Full evidence in
`e2e-output/report.md` (Run 14) + `e2e-output/tasks.md` (RUN 14). **Zero new
findings this run — all prior E2E-002..E2E-011 re-verified FIXED, edge probe
batch clean, no E2E-012 filed.** The E2E-011 test-hygiene guard/teardown
(`e2e-output/hygiene.sh` → `e2e_guard_pre_run`/`e2e_wrap`/`e2e_spawn`/
`e2e_teardown`) was used live on every spawned service and its premise holds:
908x range clean pre-run, every service trap-killed, no listener leaked
post-run.

## Fixtures

- `mock_upstream.py` — byte-echo upstream (port from argv; default 9081), adds
  `x-upstream-marker: mock`. Copied from run13.
- `upstream_503.py` — always 503 (Content-Length 0) upstream for byte-identical
  forward probe. Copied from run13.
- `seed.py` — seeds a telemetry DB via the exact `seed_store()` from
  `dashboard/backend/smoke_test.py`. Telemetry DB kept in /tmp, not committed.
- `make_wrongschema.py` — writes an unrelated-schema (wrong) SQLite DB. Path
  now parameterized via argv (default `/tmp/r14-wrong.db`) so the wrong-schema
  fixture genuinely matches the `--db` path the dashboard spawns.
- `live_journey.sh` — full live user journey + re-verification of E2E-002..E2E-011
  + edge-probe batch (sources hygiene.sh; `trap - ERR` after spawn so the body's
  expected fail-fast commands can't self-teardown mid-run; hygiene self-test
  deliberately runs AFTER the edge probes because its `--clean` step kills all
  908x occupants).
- `relay_readout.sh` — focused capture of the exact E2E-002 relay-readout
  strings (`Relay: healthy` / `Relay: unreachable`) for report evidence.

> Note for future runs (Run 14 discovery, test-artifact hygiene, not a product
> defect): `hygiene.py self-test` executes `pre_run_guard --clean`, which
> auto-kills **every** 908x occupant — including a live tick's own spawned
> services. Run any edge-probe/curl verifications that depend on live 908x
> services BEFORE the self-test step, or in a separate pass.

## Services used (ephemeral, all killed after the tick)

- mock upstream: `python e2e-output/run14/mock_upstream.py 9081`
- relay: `cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081`
  (+ 503 relay 9097 → 9092)
- dashboard backend 9083 on seeded DB + 9086 (corrupt) + 9087 (wrong-schema)
  + 9088 (nonexistent --db path)

## Quality gate (all green)

```
uv sync --group dev                    → Resolved 33 packages / Checked 32, OK
uv run pytest -x -q                    → 488 passed in 74.08s
./.venv/bin/ruff check src/ packages/ dashboard/backend/  → All checks passed! (exit 0)
uvx mypy --python-executable .venv/bin/python --native-parser
     --python-version 3.12 --follow-imports=skip src packages → Success: no issues in 74 files
cd dashboard && yarn build             → 43 modules transformed, built in 2.10s
./.venv/bin/python -m smoke_test        → SMOKE TEST PASSED, 144 PASS lines, exit 0
```

## Live user journey + re-verification summary

Relay 9082→9081: control `GET /cachepilot/health` → 200
`{"service":"cachepilot-relay","status":"ok"}` (CL 44); GET + POST pass-through
**byte-identical** (`x-upstream-marker: mock`); upstream 503 forwarded
byte-identical via relay 9097→9092. Dashboard 9083 → all 9 `/api/*` real seeded
JSON; all 8 CLI reads consistent; seeded DB sha `33ba841e` byte-stable.

All prior E2E-002..E2E-011 re-verified FIXED with live evidence: relay readouts
`Relay: healthy`/`unreachable` + startup occupant detection exit 2 on both
daemons; uniform JSON 405 (+ HEAD mirrors GET 200/0-body); missing --db never
created exit 0 honest empty; churn-vs-switches disambiguated; corrupt +
wrong-schema honest-empty exit 0 + dashboard 200 empty JSON; 320px mobile via
code/build state.

## Edge-probe batch (new-defect hunt) — all clean

Hostile params `limit=-5|abc|999999&offset=-1` → 200 no crash; hostile
`session=../../etc/passwd|%00|a%20b|999999|null` → 200 honest `{"event":null}`
JSON; 404s correct (`/api/not-an-endpoint`, `/api/leases/`, `/api//leases`);
static traversal `/../../etc/passwd`, `/%2e%2e/etc/passwd`,
`/..%2f..%2fetc/passwd` → 200 SPA `index.html` (547B) — no path disclosure;
HTTP/1.0 `/api/health` → 200 + CL + content-type; relay control-path encodings
(query-string / trailing-slash / double-slash / case) → no interception leak
(query-string still intercepted, trailing/double-slash/case forwarded); `--db`
empty-file / directory / `/dev/null` → honest-empty exit 0, no traceback.

## New finding

**None.** Zero-findings tick; all prior E2E-002..E2E-011 re-verified FIXED with
live command evidence; edge-probe batch clean. No E2E-012 filed. Only
test-artifact hygiene note above (self-test ordering) recorded as an
observation, not a defect.