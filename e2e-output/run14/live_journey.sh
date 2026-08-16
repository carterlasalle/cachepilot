#!/usr/bin/env bash
# E2E-001 Run 14 -- FULL live CLI/API user journey + re-verification of ALL prior
# findings E2E-002..E2E-011, with guaranteed teardown (e2e-output/hygiene.sh),
# plus a fresh edge-probe batch hunting for a new defect.
# Spawns current-build services only after hygiene guard proves 908x clean.
set -u
cd /home/hermes/cachepilot || exit 1
export PATH="$PWD/.venv/bin:$PATH"
source e2e-output/hygiene.sh
LOG=/tmp/r14_journey.log
: > "$LOG"
echo "[r14] journey start $(date -u +%FT%TZ)" | tee -a "$LOG"

# --- guard: fail fast if any stale 908x listener ---
e2e_guard_pre_run --clean || { echo "PRE-RUN GUARD FAILED"; exit 1; }
echo "[r14] pre-run hygiene guard passed (908x clean)" | tee -a "$LOG"

# --- install EXIT/INT/TERM teardown, but DISABLE the ERR trap: the body
#     legitimately runs failing commands (curl 000 on health checks, occupied
#     daemons exiting 2, greps that miss) which must NOT tear down mid-run.
e2e_spawn_init() { :; }
e2e_wrap
trap - ERR            # remove the ERR autoteardown e2e_wrap set
set +E

# --- seed the telemetry DB (reuse smoke_test seed_store) ---
./.venv/bin/python e2e-output/run14/seed.py /tmp/r14-telemetry.db >>"$LOG" 2>&1
SEED_SHA_BEFORE=$(sha256sum /tmp/r14-telemetry.db | cut -d' ' -f1)
echo "[r14] seeded telemetry DB sha24=$SEED_SHA_BEFORE" | tee -a "$LOG"

# --- spawn mock upstream + relay pass-through pair ---
e2e_spawn python e2e-output/run14/mock_upstream.py 9081
sleep 0.8
e2e_spawn cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081
sleep 0.8

# --- failing upstream (503) + relay 9097 for byte-identical forward ---
e2e_spawn python e2e-output/run14/upstream_503.py 9092
sleep 0.6
e2e_spawn cachepilotd --listen 127.0.0.1:9097 --upstream http://127.0.0.1:9092
sleep 0.8

# --- dashboard backend on seeded DB + corrupt + wrong-schema + nonexistent ---
e2e_spawn python dashboard/backend/server.py --db /tmp/r14-telemetry.db --port 9083
sleep 0.8
head -c 300 /dev/urandom > /tmp/r14-corrupt.db
e2e_spawn python dashboard/backend/server.py --db /tmp/r14-corrupt.db --port 9086
sleep 0.8
./.venv/bin/python e2e-output/run14/make_wrongschema.py >>"$LOG" 2>&1
e2e_spawn python dashboard/backend/server.py --db /tmp/r14-wrong.db --port 9087
sleep 0.8
# nonexistent --db + nonexistent parent dir (E2E-004: never creates)
rm -rf /tmp/r14-nodir
e2e_spawn python dashboard/backend/server.py --db /tmp/r14-nodir/missing.db --port 9088
sleep 0.8

echo "=== STARTED ===" | tee -a "$LOG"
ss -tlnp 2>/dev/null | grep -E "9081|9082|9097|9092|908[368]" | awk '{print $4, $6}' | tee -a "$LOG"

R=http://127.0.0.1:9082
R503=http://127.0.0.1:9097
B=http://127.0.0.1:9083

echo "===== LIVE USER JOURNEY =====" | tee -a "$LOG"

echo "-- A. relay control GET /cachepilot/health (distinctive JSON) --" | tee -a "$LOG"
curl -s "$R/cachepilot/health" | tee -a "$LOG"; echo >>"$LOG"

echo "-- B. relay GET pass-through byte-identical --" | tee -a "$LOG"
curl -s -D /tmp/r14_gh.txt -o /tmp/r14_gbody.txt "$R/upstream/resource"
grep -i 'X-Upstream-Marker' /tmp/r14_gh.txt >>"$LOG" 2>&1 && echo "  marker present" || echo "  NO MARKER"
echo "  relayGET_body=$(cat /tmp/r14_gbody.txt)" | tee -a "$LOG"

echo "-- C. relay POST pass-through byte-identical (echo body) --" | tee -a "$LOG"
curl -s -D /tmp/r14_ph.txt -o /tmp/r14_pbody.txt -X POST -d '{"payload":"echo-me-14"}' "$R/upstream/posts"
grep -i 'X-Upstream-Marker' /tmp/r14_ph.txt >>"$LOG" 2>&1 && echo "   marker present" || true
echo "  relayPOST_body=$(cat /tmp/r14_pbody.txt)" | tee -a "$LOG"

echo "-- D. upstream 503 forwarded byte-identical via relay 9097 -> 9092 --" | tee -a "$LOG"
curl -s -o /dev/null -w "  forward503 status=%{http_code}\n" "$R503/downstream" | tee -a "$LOG"

echo "-- E. dashboard /api/* GET endpoints (seeded, real JSON) --" | tee -a "$LOG"
for ep in health status leases costs ttl churn routes topology miss; do
  code=$(curl -s -o /tmp/r14_api_json -w "%{http_code}" "$B/api/$ep")
  size=$(wc -c < /tmp/r14_api_json)
  echo "  /api/$ep -> HTTP $code ${size}B $(head -c 40 /tmp/r14_api_json | tr -d '\n')" | tee -a "$LOG"
done

echo "-- F. all 8 CLI read commands consistent --" | tee -a "$LOG"
for cmd in status leases costs ttl churn explain-miss routes topology; do
  out=$(uv run --no-sync cachepilot "$cmd" --db /tmp/r14-telemetry.db 2>&1 | head -1)
  echo "  cachepilot $cmd -> $out" | tee -a "$LOG"
done
SEED_SHA_AFTER=$(sha256sum /tmp/r14-telemetry.db | cut -d' ' -f1)
echo "  seeded DB sha24 BEFORE=$SEED_SHA_BEFORE AFTER=$SEED_SHA_AFTER read-only=$([ "$SEED_SHA_BEFORE" = "$SEED_SHA_AFTER" ] && echo YES || echo NO)" | tee -a "$LOG"

echo "===== RE-VERIFICATION OF PRIOR FINDINGS E2E-002..E2E-011 =====" | tee -a "$LOG"

echo "-- E2E-002 relay readout healthy/unreachable/occupied via CACHEPILOT_RELAY_LISTEN --" | tee -a "$LOG"
echo -n "    healthy: " | tee -a "$LOG"
CACHEPILOT_RELAY_LISTEN=127.0.0.1:9082 uv run --no-sync cachepilot status --db /tmp/r14-telemetry.db 2>&1 | grep -iE "relay|healthy|ok" | head -1 >>"$LOG"
echo -n "    unreachable: " | tee -a "$LOG"
CACHEPILOT_RELAY_LISTEN=127.0.0.1:9998 uv run --no-sync cachepilot status --db /tmp/r14-telemetry.db 2>&1 | grep -iE "relay|unreach|down|fail" | head -1 >>"$LOG"
echo -n "    occupied: " | tee -a "$LOG"
python3 -c "import socket,time,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('127.0.0.1',9091)); s.listen(1); time.sleep(14)" &
OCPQ=$!
sleep 0.5
CACHEPILOT_RELAY_LISTEN=127.0.0.1:9091 uv run --no-sync cachepilot status --db /tmp/r14-telemetry.db 2>&1 | grep -iE "relay|occup|" | head -1 | tee -a "$LOG"
kill $OCPQ 2>/dev/null; wait $OCPQ 2>/dev/null

echo "-- E2E-002 startup occupant detection exit 2 on BOTH daemons --" | tee -a "$LOG"
python3 -c "import socket,time,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('127.0.0.1',9089)); s.listen(1); sys.stdout.flush(); time.sleep(10)" &
OCA=$!; sleep 0.5
cachepilotd --listen 127.0.0.1:9089 --upstream http://127.0.0.1:9081 >/tmp/r14_relay_occ.out 2>&1; RC=$?
echo "  cachepilotd occupied exit=$RC ($(head -1 /tmp/r14_relay_occ.out | tr -d '\n'))" | tee -a "$LOG"
kill $OCA 2>/dev/null; wait $OCA 2>/dev/null
python3 -c "import socket,time,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('127.0.0.1',9084)); s.listen(1); sys.stdout.flush(); time.sleep(10)" &
OCB=$!; sleep 0.5
python dashboard/backend/server.py --db /tmp/r14-telemetry.db --port 9084 >/tmp/r14_dash_occ.out 2>&1; RC2=$?
echo "  server.py occupied exit=$RC2 ($(grep -oE 'already in use' /tmp/r14_dash_occ.out | head -1))" | tee -a "$LOG"
kill $OCB 2>/dev/null; wait $OCB 2>/dev/null

echo "-- E2E-003/007 uniform JSON 405 on DASHBOARD /api/status for POST/PUT/DELETE/PATCH/OPTIONS/TRACE --" | tee -a "$LOG"
for m in POST PUT DELETE PATCH OPTIONS TRACE; do
  hdr=$(curl -s -D - -o /tmp/r14_405.json -X "$m" "$B/api/status" 2>/dev/null)
  code=$(echo "$hdr" | head -1 | awk '{print $2}')
  ctype=$(echo "$hdr" | grep -i '^content-type' | tr -d '\r' | sed 's/^[Cc]ontent-[Tt]ype: //')
  echo "  $m /api/status -> HTTP ${code:-000} ctype=${ctype:-?} body=$(cat /tmp/r14_405.json 2>/dev/null | tr -d '\n')" | tee -a "$LOG"
done

echo "-- E2E-010 HEAD mirrors GET (200 / 0 body / content-length mirrors) --" | tee -a "$LOG"
for p in /api/health /api/leases / /assets/index-*.js; do
  g=$(curl -s -I "$B$p" 2>/dev/null)
  g_code=$(echo "$g" | head -1 | awk '{print $2}')
  g_cl=$(echo "$g" | grep -i '^content-length' | tr -d '\r' | awk '{print $2}')
  h_bodysize=$(curl -s -X HEAD -o /dev/null -w "%{size_download}" -I "$B$p" 2>/dev/null)
  echo "  HEAD $p -> HTTP ${g_code:-?} CL=${g_cl:-?} download_body=${h_bodysize:-?} (must be 200 / 0B)" | tee -a "$LOG"
done

echo "-- E2E-004 missing --db never created, exit 0 honest empty --" | tee -a "$LOG"
rm -rf /tmp/r14-noclidb
uv run --no-sync cachepilot status --db /tmp/r14-noclidb/nope.db >/tmp/r14_nodb.out 2>&1; RC=$?
echo "  exit=$RC file_created=$([ -e /tmp/r14-noclidb/nope.db ] && echo YES || echo NO) dir_created=$([ -d /tmp/r14-noclidb ] && echo YES || echo NO)" | tee -a "$LOG"
echo "  output=$(head -1 /tmp/r14_nodb.out | tr -d '\n')" | tee -a "$LOG"

echo "-- E2E-005 churn-vs-switches disambiguated --" | tee -a "$LOG"
echo -n "  status churn/footnote: " | tee -a "$LOG"
uv run --no-sync cachepilot status --db /tmp/r14-telemetry.db 2>&1 | grep -iE "churn" | head -2 | tr '\n' ' | ' >>"$LOG"; echo >>"$LOG"
echo -n "  routes switches: " | tee -a "$LOG"
uv run --no-sync cachepilot routes --db /tmp/r14-telemetry.db 2>&1 | grep -iE "switch" | head -1 | tr -d '\n' | tee -a "$LOG"; echo >>"$LOG"

echo "-- E2E-008/009 corrupt + wrong-schema: 8 CLI read commands exit 0 no traceback --" | tee -a "$LOG"
for bad in /tmp/r14-corrupt.db /tmp/r14-wrong.db; do
  for cmd in status leases costs ttl churn explain-miss routes topology; do
    out=$(uv run --no-sync cachepilot "$cmd" --db "$bad" 2>&1); rc=$?
    tb=$(echo "$out" | grep -ci Traceback); nst=$(echo "$out" | grep -ci "no such table")
    echo "  $bad $(basename $bad) $cmd exit=$rc tb=$tb nst=$nst" | tee -a "$LOG"
  done
done

echo "-- E2E-008/009 dashboard corrupt (9086) + wrong-schema (9087) 200 empty JSON --" | tee -a "$LOG"
for port in 9086 9087; do
  code=$(curl -s -o /tmp/r14_bad_api.json -w "%{http_code}" "http://127.0.0.1:$port/api/leases")
  echo "  :$port/api/leases -> HTTP $code body=$(cat /tmp/r14_bad_api.json | tr -d '\n')" | tee -a "$LOG"
done

echo "-- E2E-006 320px mobile via code/build --" | tee -a "$LOG"
grep -n "max-width: 768px" dashboard/src/styles.css | head -1 >>"$LOG"
grep -oE "@media ?\(max-width:768px\)\{[^}]*\}" dashboard/dist/assets/index-*.css | head -c 80; echo >>"$LOG"

echo "-- EDGE PROBE BATCH (new-defect hunt) --" | tee -a "$LOG"

echo "  E1 hostile params /api/leases?limit=-5|abc|999999 offset=-1 -> " | tee -a "$LOG"
curl -s -o /dev/null -w "     HTTP %{http_code} size=%{size_download}\n" "$B/api/leases?limit=-5|abc|999999&offset=-1" | tee -a "$LOG"
echo "  E2 hostile sessions /api/miss?session=../../etc/passwd|%00|a%20b|999999 -> " | tee -a "$LOG"
for s in "../../etc/passwd" "%00" "a%20b" "999999" "null"; do
  curl -s -o /tmp/r14_miss -w "     session=$s HTTP %{http_code} size=%{size_download} " "$B/api/miss?session=$s" >>"$LOG"
  head -c 30 /tmp/r14_miss | tr -d '\n' >>"$LOG"; echo >>"$LOG"
done
echo "  E3 404 handling: /api/not-an-endpoint /api/leases/ double-slash -> " | tee -a "$LOG"
for p in /api/not-an-endpoint /api/leases/ /api//leases; do
  curl -s -o /dev/null -w "     $p HTTP %{http_code} size=%{size_download}\n" "$B$p" | tee -a "$LOG"
done
echo "  E4 traversal on static /../../etc/passwd %2e%2e/etc/passwd -> " | tee -a "$LOG"
for p in "/../../etc/passwd" "/%2e%2e/etc/passwd" "/..%2f..%2fetc/passwd"; do
  curl -s -o /tmp/r14_tr -w "     $p HTTP %{http_code} size=%{size_download} " "$B$p" >>"$LOG"
  head -c 25 /tmp/r14_tr | tr -d '\n' >>"$LOG"; echo >>"$LOG"
done
echo "  E5 HTTP/1.0 /api/health -> " | tee -a "$LOG"
curl -s --http1.0 -D - -o /tmp/r14_h10 "$B/api/health" | head -2 | tr -d '\r' | tee -a "$LOG"
echo "  E6 relay control-path encodings: query-string / trailing-slash / double-slash / case -> " | tee -a "$LOG"
for p in "/cachepilot/health?x=1" "/cachepilot/health/" "/cachepilot//health" "/CachePilot/health"; do
  curl -s -o /tmp/r14_cp -w "     $p HTTP %{http_code} size=%{size_download} body=" "$R$p" >>"$LOG"
  head -c 30 /tmp/r14_cp | tr -d '\n' >>"$LOG"; echo >>"$LOG"
done
echo "  E7 --db edge cases: empty file / directory / /dev/null -> " | tee -a "$LOG"
: > /tmp/r14-empty.db
uv run --no-sync cachepilot status --db /tmp/r14-empty.db >/tmp/r14_emp.out 2>&1; rc=$?
echo "     empty-file exit=$rc tb=$(grep -ci traceback /tmp/r14_emp.out)" | tee -a "$LOG"
mkdir -p /tmp/r14-dbdir
uv run --no-sync cachepilot status --db /tmp/r14-dbdir >/tmp/r14_dir.out 2>&1; rc=$?
echo "     directory-as-db exit=$rc tb=$(grep -ci traceback /tmp/r14_dir.out)" | tee -a "$LOG"
uv run --no-sync cachepilot status --db /dev/null >/tmp/r14_dev.out 2>&1; rc=$?
echo "     /dev/null-as-db exit=$rc tb=$(grep -ci traceback /tmp/r14_dev.out)" | tee -a "$LOG"

echo "-- E2E-011 hygiene self-test + post-run teardown verification --" | tee -a "$LOG"
./.venv/bin/python e2e-output/hygiene.py self-test >>"$LOG" 2>&1
echo "  self-test exit=$?" | tee -a "$LOG"

echo "===== JOURNEY COMPLETE =====" | tee -a "$LOG"
echo "===== explicit teardown =====" | tee -a "$LOG"
e2e_teardown
echo "===== post-teardown 908x scan =====" | tee -a "$LOG"
./.venv/bin/python e2e-output/hygiene.py scan
exit 0