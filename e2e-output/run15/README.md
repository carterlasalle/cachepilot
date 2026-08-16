# Run 15 — CLI/API E2E tick (E2E-001-R15) — test artifacts

Live verification notes for the 2026-08-16 CLI/API E2E run. Full evidence in
`e2e-output/report.md` (Run 15) + `e2e-output/tasks.md` (RUN 15). **Zero new
findings this run — all prior E2E-002..E2E-011 re-verified FIXED, edge probe
batch clean, no E2E-012 filed.** The E2E-011 test-hygiene guard/teardown
(`e2e-output/hygiene.sh` → `e2e_guard_pre_run`/`e2e_wrap`/`e2e_spawn`/
`e2e_teardown`) was used live on every spawned service: 908x range clean
pre-run, every service trap-killed, no listener leaked post-run.

## Fixtures

- `mock_upstream.py` — byte-echo upstream (port from argv; default 9081), adds
  `x-upstream-marker: mock`. Reused from run14.
- `upstream_503.py` — always 503 (Content-Length 0) upstream for byte-identical
  forward probe. Reused from run14.
- `seed.py` — seeds a telemetry DB via the exact `seed_store()` from
  `dashboard/backend/smoke_test.py`. Telemetry DB kept in /tmp, not committed.
- `make_wrongschema.py` — writes an unrelated-schema (wrong) SQLite DB. Path
  via argv (default `/tmp/r15-wrong.db`) so it matches the `--db` the dashboard
  spawns.
- `live_journey.sh` — full live user journey + re-verification of E2E-002..E2E-011
  + edge-probe batch (sources hygiene.sh; `trap - ERR` after spawn so expected
  fail-fast commands can't self-teardown mid-run; hygiene self-test runs LAST
  because its `--clean` step kills all 908x occupants).
- `relay_readout.sh` — focused capture of the exact E2E-002 relay-readout
  strings (`Relay: healthy` / `Relay: unreachable`).

## Byte-identical proof (stronger than earlier display-only bodies)

Pass-through bodies are compared with `cmp` against the **direct** upstream
response on the same path, not just shown:

- GET `/upstream/resource` — direct=32B relay=32B, `cmp` equal → YES + marker.
- POST `/upstream/posts` (body `{"payload":"echo-me-15"}`) — direct=24B
  relay=24B, `cmp` equal → YES + marker.
- 503 via relay 9097→9092 — direct HTTP 503 body 0B, relay HTTP 503 body 0B,
  byte-identical YES.
- control `GET /cachepilot/health` — intercepted, 200
  `{"service":"cachepilot-relay","status":"ok"}` (distinctive, 44B).

## Services used (ephemeral, all killed after the tick)

- mock upstream: `python e2e-output/run15/mock_upstream.py 9081`
- relay: `cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081`
  (+ 503 relay 9097 → 9092)
- dashboard backend 9083 on seeded DB + 9086 (corrupt) + 9087 (wrong-schema)
  + 9088 (nonexistent `--db` path).

## Quality gate (all green)

```
uv sync --group dev                    → Resolved 33 packages / Checked 32, OK
.venv/bin/python -m pytest -x -q       → 488 passed in 77.98s
.venv/bin/python -m ruff check src/ packages/ dashboard/backend/ e2e-output/hygiene.py
                                       → All checks passed! (exit 0)
uvx mypy --python-executable .venv/bin/python --native-parser
     --python-version 3.12 --follow-imports=skip src packages → Success: no issues in 74 files
cd dashboard && yarn build             → ✓ 43 modules transformed, built in 1.97s
.venv/bin/python dashboard/backend/smoke_test.py
                                       → SMOKE TEST PASSED, 144 PASS lines, exit 0
```

> Note 1 — mypy: the task's bare spelling
> `uvx mypy --native-parser --python-version 3.12 --follow-imports=skip src packages`
> FAILS (73 `import-not-found` errors for `starlette.*`) because `uvx` runs
> mypy in a throwaway env without the project's installed deps. With
> `--python-executable .venv/bin/python` (the standard invocation used by all
> prior runs) it is `Success, no issues found in 74 source files`. The failure
> is an env-resolution artifact of the bare command, not a code defect.
>
> Note 2 — smoke test: `e2e-output/smoke_test.py` does not exist; the smoke
> test lives at `dashboard/backend/smoke_test.py` (path used by all prior runs
> and by gitreins/AGENTS docs).

## Live user journey + re-verification summary

Relay 9082→9081: control `GET /cachepilot/health` → 200 distinctive JSON; GET +
POST pass-through **byte-identical** via `cmp` (`x-upstream-marker: mock`);
upstream 503 forwarded byte-identical via relay 9097→9092. Dashboard 9083 →
all 9 `/api/*` real seeded JSON (`/api/status` stats.total=3,
`/api/leases` populated, `/api/costs` total_usd=0.00033, ttl/churn/routes/
topology/miss all non-empty); all 8 CLI reads consistent; seeded DB sha
`c44d5c28…` byte-stable (read-only proven).

All prior E2E-002..E2E-011 re-verified FIXED with live evidence: relay readouts
`Relay: healthy`(9082) / `Relay: unreachable`(9998 closed) /
`Relay: unreachable`(9091 foreign) + startup occupant detection exit 2 on both
daemons; uniform JSON 405 on all 6 non-GET methods + HEAD-mirrors-GET 200/0-body
(incl. real asset `/assets/index-NcpMpYl1.js` CL=162662); missing `--db` never
created exit 0 honest empty; churn-vs-switches disambiguated (`route-change
churn events 0` footnote vs `route switches 1`); corrupt + wrong-schema →
all 8 CLI honest-empty exit 0 no traceback + dashboard 200 `{"leases":[]}`;
320px mobile via code (`styles.css:414`) + built bundle
(`@media (max-width: 768px){`).

## Edge-probe batch (new-defect hunt) — all clean

Hostile params `limit=-5|abc|999999&offset=-1` → 200 no crash; hostile
`session=../../etc/passwd|%00|a%20b|999999|null` → 200 honest
`{"event": null, ...}` JSON; 404s correct (`/api/not-an-endpoint`,
`/api/leases/`, `/api//leases`); static traversal `/../../etc/passwd`,
`/%2e%2e/etc/passwd`, `/..%2f..%2fetc/passwd` → 200 SPA `index.html` (547B),
no path disclosure; HTTP/1.0 `/api/health` → `HTTP/1.0 200 OK` +
`Content-Type: application/json; charset=utf-8` + `Content-Length: 12`; relay
control-path encodings → query-string `/cachepilot/health?x=1` still
intercepted (44B distinctive JSON), trailing-slash `/cachepilot/health/`,
double-slash `/cachepilot//health`, case `/CachePilot/health` all forwarded
(32B mock body — no interception leak); `--db` empty-file / directory-as-db /
`/dev/null` → honest-empty exit 0, no traceback.

## New finding

**None.** Zero-findings tick; all prior E2E-002..E2E-011 re-verified FIXED with
live command evidence; edge-probe batch clean. No E2E-012 filed. The
test-artifact observations above (mypy bare-`uvx` env resolution + smoke-test
path) are recorded for future ticks, not product defects.