# Run 11 — CLI/API E2E tick (E2E-001-R11) — test artifacts

Live verification notes for the 2026-08-16 CLI/API E2E run. Full evidence in
`e2e-output/report.md` + `e2e-output/tasks.md`. **1 new finding this run —
E2E-011 (LOW, test-hygiene): run-9's mock upstream + relay were still alive
~4h after run 9 claimed "all services killed", so this run's fresh binds
failed and the initial relay footprint ran against the stale survivors
(re-verified fresh; leaks killed).** Two non-defect observations also
recorded below.

## Services used (ephemeral ports, all killed after the tick)

- mock upstream: `python e2e-output/run11/mock_upstream.py 9081` →
  127.0.0.1:9081 (byte-echo + `x-upstream-marker: mock`; no do_HEAD → 501)
- failing upstream (503 probe): `python -c ...` on 9092
- relay: `uv run cachepilotd --listen 127.0.0.1:9082 --upstream
  http://127.0.0.1:9081` (+ a second relay on 9097 → 9092 for the 503
  pass-through check)
- dashboard backend (seeded DB): `uv run python dashboard/backend/server.py
  --db /tmp/r11-telemetry.db --port 9083` (+ 9086 corrupt-DB, 9087
  wrong-schema-DB, 9088 nonexistent-DB)
- fixture seed: `e2e-output/run11/seed.py` calls the exact `seed_store()` from
  `dashboard/backend/smoke_test.py`. The telemetry DB is a binary artifact in
  /tmp and was NOT committed (matches run7/run9 precedent).

## Quality gate (all green)

```
uv sync --group dev                    → Resolved 33 packages, OK
uv run pytest -x -q                    → 482 passed in 39.82s
./.venv/bin/ruff check src/ packages/ dashboard/backend/  → All checks passed! (exit 0)
uvx mypy --python-executable .venv/bin/python --native-parser
     --python-version 3.12 --follow-imports=skip src packages → Success: no issues in 74 files
cd dashboard && yarn build             → 43 modules transformed, built in 2.00s
./.venv/bin/python -m smoke_test        → SMOKE TEST PASSED, 144 PASS lines, exit 0
```

## Observations (recorded, NOT defects)

1. **`uv run ruff check src/` prints `[]` instead of "All checks passed!"** —
   exit code 0 and zero diagnostics in every variant; `uv run --group dev ruff
   check .` (the exact CI invocation) and `./.venv/bin/ruff check src/ ...`
   both print "All checks passed!". Same binary (0.16.2), same config
   (`output_format = full`), only the `uv run` + path-only combination shows
   JSON `[]`. Cosmetic tooling quirk; gate verdict unaffected.

2. **Relay HEAD /cachepilot/health is answered LOCALLY as a HEAD-mirror, not
   passed through.** Starlette auto-adds HEAD to GET routes
   (`starlette/routing.py`, `Route.__init__`) and suppresses the body
   (`responses.py` `send_header_only`), so HEAD on the control path → 200 +
   `content-length: 44` (the health body length) + 0 body bytes. This is the
   RFC 9110 §9.3.2-correct mirror (consistent with the E2E-010 principle the
   dashboard was fixed to) — it is NOT the 501 the mock upstream would answer,
   and no consumer breaks (the relay probe uses GET). POST/OPTIONS/HEAD-other-
   paths all still pass through (mock 501/echo prove forwarding). The pinned
   test docs say "only GET … intercepted" — HEAD is the narrow exception to
   the letter of that phrase, but the behavior is HTTP-correct.

## New finding — E2E-011 (LOW, test-hygiene) — filed on the board

**E2E ticks leak ephemeral test services across runs.** This run's fresh
binds on 9081/9082 failed (`Address already in use`) because run-9's mock
upstream + relay were still alive (~4h later; pids 2955253/2955310, started
Sat Aug 15 21:59:59/22:00:01 — run-9's report claimed "All services were
killed after the tick"). The initial relay footprint ran against those stale
survivors (same installed binary/source → functionally identical, and
re-verified fresh this tick); leaks were killed during this run. Fix
direction (test-hygiene): trap-based teardown + post-run ss/ps clean-check +
pre-run stale-process guard; 908x reserved test-only. Full detail in
`e2e-output/tasks.md` + `.coding-hermes/tasks.md` (Active).

## Services used (ephemeral ports) — all killed this tick

mock_upstream (9081), relay (9082), second relay (9097→9092, 503 probe),
failing upstream (9092), dashboards 9083/9086/9087/9088 — plus the leaked
run-9 processes — all terminated; `ss -tlnp` confirms the 908x range is
free. Seeded DB `/tmp/r11-telemetry.db` (binary artifact) not committed.