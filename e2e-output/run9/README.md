# Run 9 — CLI/API E2E tick (E2E-001-R9) — test artifacts

Live verification notes for the 2026-08-16 CLI/API E2E run. Full evidence in
`e2e-output/report.md` + `e2e-output/tasks.md`; the new finding filed as
`E2E-010` on `.coding-hermes/tasks.md`.

## Services used (ephemeral ports, all killed after the tick)

- mock upstream: `python e2e-output/run9/mock_upstream.py 9081` → 127.0.0.1:9081
- relay: `uv run cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081`
- dashboard backend (seeded DB): `uv run python dashboard/backend/server.py
  --db <seeded> --port 9083` (+ 9086 corrupt-DB, 9087 wrong-schema-DB)
- fixture seed: `e2e-output/run9/seed.py <db>` calls the exact
  `seed_store()` from `dashboard/backend/smoke_test.py`. The telemetry DB is a
  binary artifact and was NOT committed (matches run7 precedent).

## Quality gate (all green)

- `uv sync --group dev` clean
- `uv run pytest -q` → **482 passed in 41.53s**
- `ruff check src/ packages/ dashboard/backend/` → All checks passed!
- mypy (CI invocation: `--follow-imports=skip src packages`) → Success, 74 files
- `cd dashboard && yarn build` → **43 modules transformed**, built in 1.98s
- `uv run python dashboard/backend/smoke_test.py` → **SMOKE TEST PASSED**

## E2E-010 reproduction (the new finding)

Every dashboard resource that GET-returns 200 answers `HEAD` with **405
Method Not Allowed + `application/json` read-only refusal**, while the raw
TCP probe / GET works fine. HTTP semantics (RFC 9110 §9.3.2) require HEAD to
mirror GET (same status + headers, no body):

```
GET  /                 -> 200 text/html          HEAD /                 -> 405 JSON "read-only"
GET  /leases (SPA)     -> 200                    HEAD /leases           -> 405 JSON
GET  /assets/index-*.js-> 200 text/javascript    HEAD /assets/index-*.js -> 405 JSON
GET  /api/health       -> 200 {"ok":true}        HEAD /api/health       -> 405 JSON
GET  /api/leases       -> 200 [...]              HEAD /api/leases       -> 405 JSON
```

Repro: `curl -s -I http://127.0.0.1:9083/` → `HTTP/1.0 405` with
`Content-Type: application/json; charset=utf-8` and `Content-Length: 58`
(the read-only refusal body is announced but not sent, HEAD semantics).

Expected: HEAD on any GET-200 resource returns 200 with the same headers GET
would send (no body) — so a `curl -I`/HEAD-based liveness or cache check can
confirm the dashboard is serving.

Actual: HEAD is routed to the write-refusal path (`_write_refused`), so the
dashboard reports 405 for a read-only probe on a resource it is actively
serving. The write-refusal 405 contract (E2E-003/E2E-007) was correct for
mutating methods (POST/PUT/DELETE/PATCH/TRACE) but over-broadened to HEAD.

Also observed (not a bug, recorded): the relay's default telemetry store
`~/.hermes/cachepilot/cachepilot.db` was written by the live relay
(WAL mtime matched the last forwarded request) while `cachepilotd` on 9082
passed traffic through to the mock upstream — observation stays fail-open.