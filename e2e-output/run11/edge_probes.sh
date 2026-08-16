#!/usr/bin/env bash
# E2E-001 Run 11 — hunt: probe novel CLI/API contract edges for a new gap
set -u
cd /home/hermes/cachepilot || exit 1
B=http://127.0.0.1:9083   # seeded dashboard
R=http://127.0.0.1:9082   # relay

echo "===== 1) Accept: text/plain on /api/status (must stay JSON) ====="
curl -s -H 'Accept: text/plain' $B/api/status | head -c 60; echo ""

echo "===== 2) weird limit/offset params ====="
for q in "limit=-5" "limit=abc" "limit=999999" "offset=-1"; do
  code=$(curl -s -o /tmp/r11q.json -w "%{http_code}" "$B/api/leases?$q")
  echo "leases?$q -> HTTP $code body=$(head -c 50 /tmp/r11q.json)"
done

echo "===== 3) /api/miss with hostile session values ====="
for s in "999999" "null" "./../../etc/passwd" "%00" "a%20b"; do
  code=$(curl -s -o /tmp/r11m.json -w "%{http_code}" "$B/api/miss?session=$s")
  echo "miss?session=$s -> HTTP $code body=$(head -c 55 /tmp/r11m.json)"
done

echo "===== 4) unknown /api endpoint + trailing slash ====="
curl -s -o /dev/null -w "not-an-endpoint -> %{http_code}\n" $B/api/not-an-endpoint
curl -s -o /dev/null -w "api/leases/ (trailing slash) -> %{http_code}\n" $B/api/leases/
curl -s -o /dev/null -w "api (no slash) -> %{http_code}\n" $B/api

echo "===== 5) HTTP/1.0 vs 1.1 content-length on /api/health ====="
curl -s --http1.0 -o /dev/null -D - $B/api/health 2>&1 | grep -iE '^HTTP/|content-length|content-type'

echo "===== 6) relay: control path with query string + weird encodings ====="
curl -s -o /dev/null -w "GET /cachepilot/health?x=1 -> %{http_code}\n" "$R/cachepilot/health?x=1"
curl -s -o /dev/null -w "GET /cachepilot/health/ -> %{http_code}\n" "$R/cachepilot/health/"
curl -s -o /dev/null -w "GET //cachepilot/health -> %{http_code}\n" "$R//cachepilot/health"

echo "===== 7) dashboard: static asset + SPA fallback + traversal ====="
curl -s -o /dev/null -w "GET /assets/index-*.js -> %{http_code}\n" $B/assets/$(ls /home/hermes/cachepilot/dashboard/dist/assets/ | grep '\.js$' | head -1)
curl -s -o /dev/null -w "GET /unknown/spa/deep -> %{http_code}\n" $B/unknown/spa/deep
curl -s -o /dev/null -w "GET /../etc/passwd -> %{http_code}\n" $B/../etc/passwd
curl -s -o /dev/null -w "GET %2e%2e/etc/passwd -> %{http_code}\n" "$B/%2e%2e/etc/passwd"

echo "===== 8) CLI: --db is a DIRECTORY ====="
mkdir -p /tmp/r11-dir.db
uv run --no-sync cachepilot status --db /tmp/r11-dir.db >/tmp/r11dir.out 2>&1; echo "exit=$?"
grep -c Traceback /tmp/r11dir.out | sed 's/^/traceback=/'; head -2 /tmp/r11dir.out

echo "===== 9) CLI: --db is /dev/null ====="
uv run --no-sync cachepilot status --db /dev/null >/tmp/r11null.out 2>&1; echo "exit=$?"
grep -c Traceback /tmp/r11null.out | sed 's/^/traceback=/'; head -2 /tmp/r11null.out