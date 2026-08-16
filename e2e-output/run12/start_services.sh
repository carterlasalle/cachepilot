#!/usr/bin/env bash
# E2E-001 Run 12 -- live CLI/API user journey with guaranteed teardown.
# Sources e2e-output/hygiene.sh (E2E-011) so every spawned service is trap-killed.
set -u
cd /home/hermes/cachepilot || exit 1
export PATH="$PWD/.venv/bin:$PATH"
# shellcheck source=/dev/null
source e2e-output/hygiene.sh

# --- guard: fail fast if any stale 908x listener ---
e2e_guard_pre_run --clean || { echo "PRE-RUN GUARD FAILED"; exit 1; }

# --- wire up trap-based teardown ---
e2e_wrap
e2e_spawn python e2e-output/run12/mock_upstream.py 9081
sleep 0.7

# relay 1: 9082 -> mock 9081 (pass-through)
e2e_spawn cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081
sleep 0.7

# failing upstream (503) on 9092 + relay 9097 -> 9092 (503 probe)
e2e_spawn python e2e-output/run12/upstream_503.py 9092
sleep 0.5
e2e_spawn cachepilotd --listen 127.0.0.1:9097 --upstream http://127.0.0.1:9092
sleep 0.7

# dashboard backend on seeded DB (9083) + corrupt (9086) + wrong-schema (9087) + nonexistent (9088)
e2e_spawn python dashboard/backend/server.py --db /tmp/r12-telemetry.db --port 9083
sleep 0.7
head -c 300 /dev/urandom > /tmp/r12-corrupt.db
e2e_spawn python dashboard/backend/server.py --db /tmp/r12-corrupt.db --port 9086
sleep 0.7
python e2e-output/run12/make_wrongschema.py
e2e_spawn python dashboard/backend/server.py --db /tmp/r12-wrong.db --port 9087
sleep 0.7
e2e_spawn python dashboard/backend/server.py --db /tmp/r12-nodir/missing.db --port 9088
sleep 0.7

echo "=== STARTED. Services on 9081/9082/9092/9097/9083/9086/9087/9088 ==="
ss -tlnp 2>/dev/null | grep -E "9081|9082|9097|9092|908[368]" | awk '{print $4, $6}'
echo "RUN12_READY"
# main() never returns -- caller stops here; teardown via EXIT trap keeps running
while :; do sleep 60; done