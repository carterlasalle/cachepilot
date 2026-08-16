#!/usr/bin/env bash
# E2E-001 Run 12 -- edge-probe batch (hunt for a NEW contract gap).
# Sources e2e-output/hygiene.sh; starts the minimal probe stack then runs probes.
set -u
cd /home/hermes/cachepilot || exit 1
export PATH="$PWD/.venv/bin:$PATH"
source e2e-output/hygiene.sh
e2e_guard_pre_run --clean || { echo "GUARD FAILED"; exit 1; }
e2e_wrap

e2e_spawn python e2e-output/run12/mock_upstream.py 9081
sleep 0.7
e2e_spawn cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081
sleep 0.7
e2e_spawn python dashboard/backend/server.py --db /tmp/r12-telemetry.db --port 9083
sleep 1

B=http://127.0.0.1:9083
R=http://127.0.0.1:9082

echo "===== 1) Accept: text/plain on /api/status (must stay JSON) ====="
curl -s -H 'Accept: text/plain' $B/api/status | head -c 60; echo ""

echo "===== 2) weird limit/offset params on /api/leases ====="
for q in "limit=-5" "limit=abc" "limit=999999" "offset=-1"; do
  code=$(curl -s -o /tmp/r12q.json -w "%{http_code}" "$B/api/leases?$q"); echo "leases?$q -> HTTP $code body=$(head -c 40 /tmp/r12q.json)"
done

echo "===== 3) /api/miss hostile session values ====="
for s in "999999" "null" "./../../etc/passwd" "%00" "a%20b"; do
  code=$(curl -s -o /tmp/r12m.json -w "%{http_code}" "$B/api/miss?session=$s"); echo "miss?session=$s -> HTTP $code body=$(head -c 45 /tmp/r12m.json)"
done

echo "===== 4) unknown /api endpoint + trailing slash + bare /api ====="
curl -s -o /dev/null -w "not-an-endpoint -> %{http_code}\n" $B/api/not-an-endpoint
curl -s -o /dev/null -w "api/leases/ (trailing slash) -> %{http_code}\n" $B/api/leases/
curl -s -o /dev/null -w "api (no slash) -> %{http_code}\n" $B/api
curl -s -o /dev/null -w "%2e%2e/etc/passwd -> %{http_code}\n" "$B/%2e%2e/etc/passwd"

echo "===== 5) HTTP/1.0 vs 1.1 content-length on /api/health ====="
curl -s --http1.0 -o /dev/null -D - $B/api/health 2>&1 | grep -iE '^HTTP/|content-length|content-type'

echo "===== 6) relay: control path query string / trailing slash / double slash ====="
curl -s -o /dev/null -w "health?x=1 -> %{http_code}\n" "$R/cachepilot/health?x=1"
curl -s -o /dev/null -w "health/ -> %{http_code}\n" "$R/cachepilot/health/"
curl -s -o /dev/null -w "//cachepilot/health -> %{http_code}\n" "$R//cachepilot/health"

echo "===== 7) CLI: --db is a DIRECTORY ====="
mkdir -p /tmp/r12-dir.db
uv run --no-sync cachepilot status --db /tmp/r12-dir.db >/tmp/r12dir.out 2>&1; echo "exit=$?"
echo "traceback=$(grep -c Traceback /tmp/r12dir.out)"; head -1 /tmp/r12dir.out

echo "===== 8) CLI: --db is /dev/null ====="
uv run --no-sync cachepilot status --db /dev/null >/tmp/r12null.out 2>&1; echo "exit=$?"
echo "traceback=$(grep -c Traceback /tmp/r12null.out)"; head -1 /tmp/r12null.out

echo "===== PROBES DONE ====="