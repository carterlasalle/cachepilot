#!/usr/bin/env bash
# E2E-001 Run 13 -- focused 2nd pass: correct/complete evidence for E2E-002
# (relay readouts), E2E-003/007 (uniform JSON 405 on DASHBOARD /api), E2E-005,
# E2E-006, E2E-010, IPv6/external-bind, CLEAN teardown.
set -u
cd /home/hermes/cachepilot || exit 1
export PATH="$PWD/.venv/bin:$PATH"
source e2e-output/hygiene.sh
LOG=/tmp/r13_pass2.log
: > "$LOG"
echo "[r13-pass2] start $(date -u +%FT%TZ)" | tee -a "$LOG"
e2e_guard_pre_run --clean || { echo "PRE-RUN GUARD FAILED"; exit 1; }
e2e_wrap
trap - ERR; set +E

./.venv/bin/python e2e-output/run13/seed.py /tmp/r13-telemetry.db >>"$LOG" 2>&1
e2e_spawn python e2e-output/run13/mock_upstream.py 9081; sleep 0.8
e2e_spawn cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081; sleep 0.8
e2e_spawn python dashboard/backend/server.py --db /tmp/r13-telemetry.db --port 9083; sleep 0.9
head -c 300 /dev/urandom > /tmp/r13-corrupt.db
e2e_spawn python dashboard/backend/server.py --db /tmp/r13-corrupt.db --port 9086; sleep 0.9

R=http://127.0.0.1:9082; B=http://127.0.0.1:9083

echo "===== P2.A E2E-002 relay readout (live) =====" | tee -a "$LOG"
echo "[healthy 9082]" | tee -a "$LOG"
CACHEPILOT_RELAY_LISTEN=127.0.0.1:9082 uv run --no-sync cachepilot status --db /tmp/r13-telemetry.db 2>&1 | grep -iE "relay|reach|healthy|ok" | tee -a "$LOG"
echo "[unreachable 9998]" | tee -a "$LOG"
CACHEPILOT_RELAY_LISTEN=127.0.0.1:9998 uv run --no-sync cachepilot status --db /tmp/r13-telemetry.db 2>&1 | grep -iE "Relay|reach|unreach|down" | tee -a "$LOG"
echo "[occupied 9091] (socket held)" | tee -a "$LOG"
python3 -c "import socket,time; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('127.0.0.1',9091)); s.listen(1); time.sleep(12)" & OCP=$!; sleep 0.5
CACHEPILOT_RELAY_LISTEN=127.0.0.1:9091 uv run --no-sync cachepilot status --db /tmp/r13-telemetry.db 2>&1 | grep -iE "Relay|occup|another|in use|not" | tee -a "$LOG"
kill $OCP 2>/dev/null; wait $OCP 2>/dev/null

echo "===== P2 E2E-003/007 uniform JSON 405 on DASHBOARD /api/status =====" | tee -a "$LOG"
for m in POST PUT DELETE PATCH OPTIONS TRACE; do
  hdr=$(curl -s -D - -o /tmp/r13_405.json -X "$m" "$B/api/status" 2>/dev/null)
  code=$(echo "$hdr" | head -1 | awk '{print $2}')
  ctype=$(echo "$hdr" | grep -i '^content-type' | tr -d '\r' | sed 's/^[Cc]ontent-[Tt]ype: //')
  echo "  $m /api/status -> HTTP ${code:-000} ctype=${ctype:-?} body=$(head -c 60 /tmp/r13_405.json 2>/dev/null | tr -d '\n')" | tee -a "$LOG"
done

echo "===== P2: E2E-010 HEAD mirrors GET on dashboard =====" | tee -a "$LOG"
for p in /api/health /api/leases / /assets/index-*.js; do
  g=$(curl -s -I "$B$p" 2>/dev/null); g_code=$(echo "$g"|head -1|awk '{print $2}')
  g_cl=$(echo "$g"|grep -i '^content-length'|tr -d '\r'|awk '{print $2}')
  dB=$(curl -s -X HEAD -o /dev/null -w "%{size_download}" -I "$B$p" 2>/dev/null)
  echo "  HEAD $p -> HTTP ${g_code:-?} CL=${g_cl:-?} body=${dB:-?}" | tee -a "$LOG"
done

echo "===== P2: E2E-005 churn-vs-switches (exact lines) =====" | tee -a "$LOG"
echo "[status]" | tee -a "$LOG"
uv run --no-sync cachepilot status --db /tmp/r13-telemetry.db 2>&1 | grep -iE "churn|switch|event" | tee -a "$LOG"
echo "[routes]" | tee -a "$LOG"
uv run --no-sync cachepilot routes --db /tmp/r13-telemetry.db 2>&1 | grep -iE "switch" | tee -a "$LOG"

echo "===== P2: relay control HEAD/body & external-bind refusal =====" | tee -a "$LOG"
curl -s -o /dev/null -w "relay /cachepilot/health -> %{http_code}\n" "$R/cachepilot/health" | tee -a "$LOG"

echo "===== P2 finished, teardown =====" | tee -a "$LOG"
e2e_teardown
echo "post-teardown:" | tee -a "$LOG"
./.venv/bin/python e2e-output/hygiene.py scan
exit 0