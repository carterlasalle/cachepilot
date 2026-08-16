#!/usr/bin/env bash
# Focused E2E-002 relay-readout evidence capture (Run 15).
set -u
cd /home/hermes/cachepilot || exit 1
export PATH="$PWD/.venv/bin:$PATH"
source e2e-output/hygiene.sh
LOG=/tmp/r15_relayreadout.log; : > "$LOG"
e2e_guard_pre_run --clean || exit 1
e2e_wrap; trap - ERR; set +E
./.venv/bin/python e2e-output/run15/seed.py /tmp/r15-telemetry.db >>"$LOG" 2>&1
e2e_spawn python e2e-output/run15/mock_upstream.py 9081; sleep 0.7
e2e_spawn cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081; sleep 0.8
echo "=== healthy relay (9082) ===" | tee -a "$LOG"
CACHEPILOT_RELAY_LISTEN=127.0.0.1:9082 uv run --no-sync cachepilot status --db /tmp/r15-telemetry.db 2>&1 | grep -iE "relay *: *|relay |reachable" | tee -a "$LOG"
echo "=== unreachable (9998 closed) ===" | tee -a "$LOG"
CACHEPILOT_RELAY_LISTEN=127.0.0.1:9998 uv run --no-sync cachepilot status --db /tmp/r15-telemetry.db 2>&1 | grep -iE "relay *: *|unreach|down|fail" | tee -a "$LOG"
echo "=== occupied (9091 foreign listener) ===" | tee -a "$LOG"
python3 -c "import socket,time; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('127.0.0.1',9091)); s.listen(1); time.sleep(8)" &
OCP=$!
sleep 0.5
CACHEPILOT_RELAY_LISTEN=127.0.0.1:9091 uv run --no-sync cachepilot status --db /tmp/r15-telemetry.db 2>&1 | grep -iE "relay *: *|occup|another|in use|not " | tee -a "$LOG"
kill $OCP 2>/dev/null; wait $OCP 2>/dev/null
e2e_teardown
echo "post-teardown:" | tee -a "$LOG"
./.venv/bin/python e2e-output/hygiene.py scan
exit 0