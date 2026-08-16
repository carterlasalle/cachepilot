# Run 12 — CLI/API E2E tick (E2E-001-R12) — test artifacts

Live verification notes for the 2026-08-16 CLI/API E2E run. Full evidence in
`e2e-output/report.md` + `e2e-output/tasks.md`. **No new findings this run —
everything re-verified FIXED, edge probes clean.** E2E-011 test-hygiene
guard/teardown (added in Run 11) was used live throughout and its premise
holds: the 908x range was clean pre-run, every spawned service was
trap/killed via `e2e_teardown`, and no listener leaked after the run.

## Services used (ephemeral ports, all killed after the tick)

- mock upstream: `python e2e-output/run12/mock_upstream.py 9081` →
  127.0.0.1:9081 (byte-echo + `x-upstream-marker: mock`)
- failing upstream (503 probe): `python e2e-output/run12/upstream_503.py 9092`
- relay: `cachepilotd --listen 127.0.0.1:9082 --upstream
  http://127.0.0.1:9081` (+ a second relay on 9097 → 9092 for the 503
  pass-through check)
- dashboard backend on a seeded telemetry DB: `python
  dashboard/backend/server.py --db /tmp/r12-telemetry.db --port 9083` (+ 9086
  corrupt-DB, 9087 wrong-schema-DB, 9088 nonexistent-DB)
- fixture seed: `e2e-output/run12/seed.py` calls the exact `seed_store()` from
  `dashboard/backend/smoke_test.py`. The telemetry DB is a binary artifact in
  /tmp and was NOT committed (matches run7/run9/run11 precedent).

All spawned via `e2e-output/hygiene.sh` (e2e_spawn/e2e_wrap/e2e_teardown —
E2E-011) so trap-based teardown guaranteed no leakage; post-run `ss` confirms
908x free.

## Quality gate (all green)

```
uv sync --group dev                    → Resolved 33 packages / Checked 32, OK
uv run pytest -x -q                    → 488 passed in 55.44s
./.venv/bin/ruff check src/ packages/ dashboard/backend/  → All checks passed! (exit 0)
uvx mypy --python-executable .venv/bin/python --native-parser
     --python-version 3.12 --follow-imports=skip src packages → Success: no issues in 74 files
cd dashboard && yarn build             → 43 modules transformed, built in 1.75s
./.venv/bin/python -m smoke_test        → SMOKE TEST PASSED, 144 PASS lines, exit 0
```

## Re-verification of all prior findings (regression)

- E2E-002 (relay readouts + startup occupant detection): healthy/unreachable/
  occupied all correct via `CACHEPILOT_RELAY_LISTEN`; both `cachepilotd` and
  `server.py` **exit 2** on an occupied port with an actionable error naming
  the port + override.
- E2E-003/007 (uniform JSON 405): POST/PUT/DELETE/PATCH/OPTIONS/TRACE on
  /api/health all → **405 application/json** read-only refusal.
- E2E-004 (missing `--db` never created): `status --db /tmp/r12-nodb/...` →
  exit 0, honest-empty notice, **no file and no parent dir** created; `--db`
  a directory and `--db /dev/null` → honest-empty exit 0.
- E2E-005 (churn vs switches disambiguated): `status` → `route-change churn
  events 0` + footnote; `routes` → `route switches 1`.
- E2E-006 (mobile collapse): `dashboard/src/styles.css:414` `@media
  (max-width: 768px)` present + present in built CSS bundle (code/build
  state; CLI/API variant).
- E2E-008/009 (corrupt + wrong-schema honest-empty): corrupt `/tmp/r12-
  corrupt.db` (random bytes) and wrong-schema `/tmp/r12-wrong.db` (unrelated
  table) → all 8 CLI read commands **exit 0, no traceback, no "no such
  table"**; dashboard `/api/*` on the corrupt (9086) and wrong-schema (9087)
  backends → **200 empty JSON**.
- E2E-010 (HEAD mirrors GET): HEAD `/api/health`, `/`, `/leases`,
  `/assets/index-*.js` → **200, 0 body bytes**, content-length mirrors GET
  (RFC 9110 §9.3.2).
- E2E-011 (test-hygiene guard/teardown): `e2e-output/hygiene.py self-test`
  exit 0; live guard caught nothing pre-run (range clean), teardown killed +
  verified clean after each probe batch; **no 908x listener leaked**.

## Live user journey (fresh current-build services)

- Relay 9082 → mock 9081: control `GET /cachepilot/health` intercepted with
  distinctive JSON `{"service":"cachepilot-relay","status":"ok"}`;
  GET/POST on non-control paths **byte-identical** pass-through
  (`x-upstream-marker: mock` present, body identical); upstream **503
  forwarded byte-identical** status through relay 9097→9092; POST-on-control
  and OPTIONS/HEAD non-control pass through (marker present).
- Dashboard backend 9083 on the seeded DB: all 9 `/api/*` GET endpoints
  (`health/status/leases/costs/ttl/churn/routes/topology/miss`) → real
  seeded JSON (non-empty, e.g. `status` total=3, leases populated).
- All 8 CLI read commands (`status/leases/costs/ttl/churn/explain-miss/
  routes/topology`) consistent with the seeded fixture; seeded DB sha
  `e11c5b66e627…` byte-stable before/after all reads (read-only proven).

## Edge probes (hunt for a new gap) — all clean, no new finding

- `Accept: text/plain` on /api/status → still JSON 200.
- `/api/leases?limit=-5|abc|999999`, `offset=-1` → 200 JSON, no crash.
- `/api/miss?session=999999|null|./../../etc/passwd|%00|a%20b` → honest
  `{"event": null, "stable": [], "changed": []}` 200.
- `/api/not-an-endpoint` → 404; `/api/leases/` → 404; bare `/api` → 200 SPA
  index.html (fallback, not an API route).
- HTTP/1.0 /api/health → 200 + Content-Length + Content-Type.
- traversal `%2e%2e/etc/passwd` → index.html (confined to dist/, no file
  disclosure).
- relay control path + query string / trailing slash / double slash →
  forwarded/answered 200, no interception leak.
- CLI `--db` a directory and `--db /dev/null` → honest-empty exit 0, no
  traceback.

## New finding

**None.** All prior E2E-002..E2E-011 re-verified FIXED; edge probes clean. No
E2E-012 task filed.