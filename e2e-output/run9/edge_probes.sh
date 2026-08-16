#!/usr/bin/env bash
# E2E-001 Run 9 — hunt: probe novel CLI/API contract edges for a new gap
set -u
cd /home/hermes/cachepilot || exit 1

echo "===== 1) empty-string CACHEPILOT_TELEMETRY_DB ====="
pushd /tmp >/dev/null
CACHEPILOT_TELEMETRY_DB="" uv run --project /home/hermes/cachepilot cachepilot status >/tmp/r9_envempty.out 2>/tmp/r9_envempty.err
echo "exit=$? stdout_tail=$(tail -1 /tmp/r9_envempty.out) stderr=[$(cat /tmp/r9_envempty.err | head -1)]"
ls -la /tmp/cachepilot.db 2>/dev/null || echo "no /tmp/cachepilot.db created"
popd >/dev/null

echo "===== 2) ~ in --db (path expansion) ====="
rm -f /tmp/r9-tilde.db
uv run cachepilot status --db /tmp/r9-tilde.db >/dev/null 2>&1
echo "exit=$? (expect honest empty)"

echo "===== 3) HEAD on control static / (SPA) ====="
curl -s -X HEAD -o /dev/null -D - http://127.0.0.1:9083/ 2>&1 | grep -iE '^HTTP/'

echo "===== 4) /api/status Accept: text/plain (should still be JSON) ====="
curl -s -H 'Accept: text/plain' http://127.0.0.1:9083/api/status | head -c 80; echo ""

echo "===== 5) huge/negative limit query params /api/leases?x=1 ====="
curl -s -o /tmp/r9_q.json -w "leases?bogus=%{http_code}\n" "http://127.0.0.1:9083/api/leases?limit=-5"

echo "===== 6) /api/miss with invalid session types ====="
for s in "999999" "null" "./../../etc/passwd"; do
  code=$(curl -s -o /tmp/r9m.json -w "%{http_code}" "http://127.0.0.1:9083/api/miss?session=$s")
  echo "miss?session=$s -> $code body=$(head -c 60 /tmp/r9m.json)"
done

echo "===== 7) protocol HTTP/1.0 vs 1.1 presence of content-length ====="
curl -s --http1.0 -o /dev/null -D - http://127.0.0.1:9083/api/health 2>&1 | grep -iE '^HTTP/|content'