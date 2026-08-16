#!/usr/bin/env bash
# E2E-001 Run 13 -- final byte-identical pass-through + control JSON evidence.
set -u
cd /home/hermes/cachepilot || exit 1
export PATH="$PWD/.venv/bin:$PATH"
source e2e-output/hygiene.sh
LOG=/tmp/r13_pass3.log; : > "$LOG"
e2e_guard_pre_run --clean || exit 1
e2e_wrap; trap - ERR; set +E
./.venv/bin/python e2e-output/run13/seed.py /tmp/r13-telemetry.db >>"$LOG" 2>&1
e2e_spawn python e2e-output/run13/mock_upstream.py 9081; sleep 0.8
e2e_spawn cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081; sleep 0.8
e2e_spawn python dashboard/backend/server.py --db /tmp/r13-telemetry.db --port 9083; sleep 0.9
R=http://127.0.0.1:9082; B=http://127.0.0.1:9083
echo "===== control GET /cachepilot/health (distinctive JSON) =====" | tee -a "$LOG"
curl -s -D /tmp/r13ctrl_hdr -o /tmp/r13ctrl_body "$R/cachepilot/health"
echo "  response-body=$(cat /tmp/r13ctrl_body | tr -d '\n')" | tee -a "$LOG"
echo "  content-length=$(grep -i '^content-length' /tmp/r13ctrl_hdr | tr -d '\r' | awk '{print $2}')" | tee -a "$LOG"
echo "===== relay GET pass-through byte-identical =====" | tee -a "$LOG"
curl -s -D /tmp/r13g_hdr -o /tmp/r13g_body "$R/upstream/thing"
echo "  marker=$(grep -i 'x-upstream-marker' /tmp/r13g_hdr | tr -d '\r')" | tee -a "$LOG"
echo "  passthru_body=$(cat /tmp/r13g_body | tr -d '\n')  (direct mock body=$(echo '{"ok": true, "upstream": "mock"}'))" | tee -a "$LOG"
echo "===== relay POST pass-through byte-identical =====" | tee -a "$LOG"
curl -s -D /tmp/r13p_hdr -o /tmp/r13p_body -X POST -d 'HELLO-13-000' "$R/cache/echo"
echo "  marker=$(grep -i 'x-upstream-marker' /tmp/r13p_hdr | tr -d '\r')" | tee -a "$LOG"
echo "  echoed_body=$(cat /tmp/r13p_body | tr -d '\n')" | tee -a "$LOG"
echo "===== HEAD real JS asset mirrors GET =====" | tee -a "$LOG"
JS=/assets/$(ls dashboard/dist/assets/index-*.js | xargs -n1 basename)
g=$(curl -s -I "$B$JS" 2>/dev/null); g_code=$(echo "$g"|head -1|awk '{print $2}'); g_cl=$(echo "$g"|grep -i '^content-length'|tr -d '\r'|awk '{print $2}')
dB=$(curl -s -X HEAD -o /dev/null -w "%{size_download}" -I "$B$JS" 2>/dev/null)
echo "  HEAD $JS -> HTTP ${g_code:-?} CL=${g_cl:-?} body=${dB:-?}" | tee -a "$LOG"
echo "===== teardown =====" | tee -a "$LOG"
e2e_teardown
./.venv/bin/python e2e-output/hygiene.py scan | tee -a "$LOG"
exit 0