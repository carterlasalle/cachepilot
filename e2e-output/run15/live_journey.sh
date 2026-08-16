#!/usr/bin/env bash
# E2E-001 Run 15 -- FULL live CLI/API user journey + re-verification of ALL prior
# findings E2E-002..E2E-011, with guaranteed teardown (e2e-output/hygiene.sh),
# plus a fresh edge-probe batch hunting for a new defect.
# Spawns current-build services only after hygiene guard proves 908x clean.
set -u
cd /home/hermes/cachepilot || exit 1
export PATH="$PWD/.venv/bin:$PATH"
source e2e-output/hygiene.sh
LOG=/tmp/r15_journey.log
: > "$LOG"
echo "[r15] journey start $(date -u +%FT%TZ)" | tee -a "$LOG"

# --- guard: fail fast if any stale 908x listener ---
e2e_guard_pre_run --clean || { echo "PRE-RUN GUARD FAILED"; exit 1; }
echo "[r15] pre-run hygiene guard passed (908x clean)" | tee -a "$LOG"

# --- install EXIT/INT/TERM teardown, but DISABLE the ERR trap: the body
#     legitimately runs failing commands (curl 000 on health checks, occupied
#     daemons exiting 2, greps that miss) which must NOT tear down mid-run.
e2e_spawn_init() { :; }
e2e_wrap
trap - ERR            # remove the ERR autoteardown e2e_wrap set
set +E

# --- seed the telemetry DB (reuse smoke_test seed_store) ---
./.venv/bin/python e2e-output/run15/seed.py /tmp/r15-telemetry.db >>"$LOG" 2>&1
SEED_SHA_BEFORE=$(sha256sum /tmp/r15-telemetry.db | cut -d' ' -f1)
echo "[r15] seeded telemetry DB sha=$SEED_SHA_BEFORE" | tee -a "$LOG"

# --- spawn mock upstream + relay pass-through pair ---
e2e_spawn python e2e-output/run15/mock_upstream.py 9081
sleep 0.8
e2e_spawn cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081
sleep 0.8

# --- failing upstream (503) + relay 9097 for byte-identical forward ---
e2e_spawn python e2e-output/run15/upstream_503.py 9092
sleep 0.6
e2e_spawn cachepilotd --listen 127.0.0.1:9097 --upstream http://127.0.0.1:9092
sleep 0.8

# --- dashboard backend on seeded DB + corrupt + wrong-schema + nonexistent ---
e2e_spawn python dashboard/backend/server.py --db /tmp/r15-telemetry.db --port 9083
sleep 0.8
head -c 300 /dev/urandom > /tmp/r15-corrupt.db
e2e_spawn python dashboard/backend/server.py --db /tmp/r15-corrupt.db --port 9086
sleep 0.8
./.venv/bin/python e2e-output/run15/make_wrongschema.py /tmp/r15-wrong.db >>"$LOG" 2>&1
e2e_spawn python dashboard/backend/server.py --db /tmp/r15-wrong.db --port 9087
sleep 0.8
# nonexistent --db + nonexistent parent dir (E2E-004: never creates)
rm -rf /tmp/r15-nodir
e2e_spawn python dashboard/backend/server.py --db /tmp/r15-nodir/missing.db --port 9088
sleep 0.8

echo "=== STARTED ===" | tee -a "$LOG"
ss -tlnp 2>/dev/null | grep -E "9081|9082|9097|9092|908[368]" | awk '{print $4, $6}' | tee -a "$LOG"

R=http://127.0.0.1:9082
R503=http://127.0.0.1:9097
U=http://127.0.0.1:9081
B=http://127.0.0.1:9083

echo "===== LIVE USER JOURNEY =====" | tee -a "$LOG"

echo "-- A. relay control GET /cachepilot/health (distinctive JSON) --" | tee -a "$LOG"
curl -s "$R/cachepilot/health" | tee -a "$LOG"; echo >>"$LOG"

echo "-- B. relay GET pass-through BYTE-IDENTICAL (direct vs relay cmp) --" | tee -a "$LOG"
curl -s -D /tmp/r15_gh.txt -o /tmp/r15_gdirect.txt "$U/upstream/resource"
curl -s -D /tmp/r15_grh.txt -o /tmp/r15_grelay.txt "$R/upstream/resource"
cmp -s /tmp/r15_gdirect.txt /tmp/r15_grelay.txt && echo "  GET byte-identical=YES (direct=$(wc -c </tmp/r15_gdirect.txt)B relay=$(wc -c </tmp/r15_grelay.txt)B)" | tee -a "$LOG" || echo "  GET byte-identical=NO" | tee -a "$LOG"
grep -i 'X-Upstream-Marker' /tmp/r15_grh.txt | tr -d '\r' >>"$LOG" && echo "  relay GET marker present" | tee -a "$LOG" || echo "  NO MARKER" | tee -a "$LOG"
echo "  relayGET_body=$(cat /tmp/r15_grelay.txt)" | tee -a "$LOG"

echo "-- C. relay POST pass-through BYTE-IDENTICAL (direct vs relay cmp) --" | tee -a "$LOG"
curl -s -o /tmp/r15_pdirect.txt -X POST -d '{"payload":"echo-me-15"}' "$U/upstream/posts"
curl -s -D /tmp/r15_prh.txt -o /tmp/r15_prelay.txt -X POST -d '{"payload":"echo-me-15"}' "$R/upstream/posts"
cmp -s /tmp/r15_pdirect.txt /tmp/r15_prelay.txt && echo "  POST byte-identical=YES (direct=$(wc -c </tmp/r15_pdirect.txt)B relay=$(wc -c </tmp/r15_prelay.txt)B)" | tee -a "$LOG" || echo "  POST byte-identical=NO" | tee -a "$LOG"
grep -i 'X-Upstream-Marker' /tmp/r15_prh.txt | tr -d '\r' >>"$LOG" && echo "  relay POST marker present" | tee -a "$LOG" || echo "  NO MARKER" | tee -a "$LOG"
echo "  relayPOST_body=$(cat /tmp/r15_prelay.txt)" | tee -a "$LOG"

echo "-- D. upstream 503 forwarded byte-identical via relay 9097 -> 9092 --" | tee -a "$LOG"
DIRECT_CODE=$(curl -s -o /tmp/r15_d503.txt -w "%{http_code}" "http://127.0.0.1:9092/downstream")
RELAY_CODE=$(curl -s -o /tmp/r15_r503.txt -w "%{http_code}" "$R503/downstream")
DIRE_BODY=$(wc -c </tmp/r15_d503.txt); REL_BODY=$(wc -c </tmp/r15_r503.txt)
echo "  direct 9092 -> HTTP $DIRECT_CODE body=${DIRE_BODY}B ; relay 9097 -> HTTP $RELAY_CODE body=${REL_BODY}B ; byte-identical=$([ "$DIRECT_CODE" = "$RELAY_CODE" ] && [ "$DIRE_BODY" = "$REL_BODY" ] && echo YES || echo NO)" | tee -a "$LOG"

echo "-- E. dashboard /api/* GET endpoints (seeded, real JSON) --" | tee -a "$LOG"
for ep in health status leases costs ttl churn routes topology miss; do
  code=$(curl -s -o /tmp/r15_api_json -w "%{http_code}" "$B/api/$ep")
  size=$(wc -c < /tmp/r15_api_json)
  echo "  /api/$ep -> HTTP $code ${size}B $(head -c 40 /tmp/r15_api_json | tr -d '\n')" | tee -a "$LOG"
done

echo "-- F. all 8 CLI read commands consistent --" | tee -a "$LOG"
for cmd in status leases costs ttl churn explain-miss routes topology; do
  out=$(uv run --no-sync cachepilot "$cmd" --db /tmp/r15-telemetry.db 2>&1 | head -1)
  echo "  cachepilot $cmd -> $out" | tee -a "$LOG"
done
SEED_SHA_AFTER=$(sha256sum /tmp/r15-telemetry.db | cut -d' ' -f1)
echo "  seeded DB sha BEFORE=$SEED_SHA_BEFORE AFTER=$SEED_SHA_AFTER read-only=$([ "$SEED_SHA_BEFORE" = "$SEED_SHA_AFTER" ] && echo YES || echo NO)" | tee -a "$LOG"

echo "===== RE-VERIFICATION OF PRIOR FINDINGS E2E-002..E2E-011 =====" | tee -a "$LOG"

echo "-- E2E-002 relay readout healthy/unreachable/occupied --" | tee -a "$LOG"
echo -n "    healthy(9082): " | tee -a "$LOG"
CACHEPILOT_RELAY_LISTEN=127.0.0.1:9082 uv run --no-sync cachepilot status --db /tmp/r15-telemetry.db 2>&1 | grep -iE "Relay *:|healthy|unreach" | head -1 >>"$LOG"
echo -n "    unreachable(9998 closed): " | tee -a "$LOG"
CACHEPILOT_RELAY_LISTEN=127.0.0.1:9998 uv run --no-sync cachepilot status --db /tmp/r15-telemetry.db 2>&1 | grep -iE "Relay *:|healthy|unreach" | head -1 >>"$LOG"
echo -n "    occupied(9091 foreign): " | tee -a "$LOG"
python3 -c "import socket,time,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('127.0.0.1',9091)); s.listen(1); time.sleep(14)" &
OCPQ=$!
sleep 0.5
CACHEPILOT_RELAY_LISTEN=127.0.0.1:9091 uv run --no-sync cachepilot status --db /tmp/r15-telemetry.db 2>&1 | grep -iE "Relay *:|healthy|unreach" | head -1 | tee -a "$LOG"
kill $OCPQ 2>/dev/null; wait $OCPQ 2>/dev/null

echo "-- E2E-002 startup occupant detection exit 2 on BOTH daemons --" | tee -a "$LOG"
python3 -c "import socket,time,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('127.0.0.1',9089)); s.listen(1); sys.stdout.flush(); time.sleep(10)" &
OCA=$!; sleep 0.5
cachepilotd --listen 127.0.0.1:9089 --upstream http://127.0.0.1:9081 >/tmp/r15_relay_occ.out 2>&1; RC=$?
echo "  cachepilotd occupied exit=$RC err=$(head -1 /tmp/r15_relay_occ.out | tr -d '\n')" | tee -a "$LOG"
kill $OCA 2>/dev/null; wait $OCA 2>/dev/null
python3 -c "import socket,time,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('127.0.0.1',9084)); s.listen(1); sys.stdout.flush(); time.sleep(10)" &
OCB=$!; sleep 0.5
python dashboard/backend/server.py --db /tmp/r15-telemetry.db --port 9084 >/tmp/r15_dash_occ.out 2>&1; RC2=$?
echo "  server.py occupied exit=$RC2 err=$(grep -oE 'already in use|Address in use|in use' /tmp/r15_dash_occ.out | head -1)" | tee -a "$LOG"
kill $OCB 2>/dev/null; wait $OCB 2>/dev/null

echo "-- E2E-003/007 uniform JSON 405 on /api/status for POST/PUT/DELETE/PATCH/OPTIONS/TRACE --" | tee -a "$LOG"
for m in POST PUT DELETE PATCH OPTIONS TRACE; do
  hdr=$(curl -s -D - -o /tmp/r15_405.json -X "$m" "$B/api/status" 2>/dev/null)
  code=$(echo "$hdr" | head -1 | awk '{print $2}')
  ctype=$(echo "$hdr" | grep -i '^content-type' | tr -d '\r' | sed 's/^[Cc]ontent-[Tt]ype: //')
  echo "  $m /api/status -> HTTP ${code:-000} ctype=${ctype:-?} body=$(cat /tmp/r15_405.json 2>/dev/null | tr -d '\n')" | tee -a "$LOG"
done

echo "-- E2E-010 HEAD mirrors GET (200 / 0 body / content-length mirrors, incl. real asset) --" | tee -a "$LOG"
ASSET_NAME=$(basename "$(ls dashboard/dist/assets/index-*.js | head -1)")
for p in /api/health /api/leases / "/assets/$ASSET_NAME"; do
  g_code=$(curl -s -I "$B$p" 2>/dev/null | head -1 | awk '{print $2}')
  g_cl=$(curl -s -I "$B$p" 2>/dev/null | grep -i '^content-length' | tr -d '\r' | awk '{print $2}')
  h_body=$(curl -s -X HEAD -o /dev/null -w "%{size_download}" "$B$p" 2>/dev/null)
  echo "  HEAD $p -> HTTP ${g_code:-?} CL=${g_cl:-?} download_body=${h_body:-?} (must be 200 / 0B)" | tee -a "$LOG"
done

echo "-- E2E-004 missing --db never created, exit 0 honest empty --" | tee -a "$LOG"
rm -rf /tmp/r15-noclidb
uv run --no-sync cachepilot status --db /tmp/r15-noclidb/nope.db >/tmp/r15_nodb.out 2>&1; RC=$?
echo "  exit=$RC file_created=$([ -e /tmp/r15-noclidb/nope.db ] && echo YES || echo NO) dir_created=$([ -d /tmp/r15-noclidb ] && echo YES || echo NO)" | tee -a "$LOG"
echo "  output=$(head -1 /tmp/r15_nodb.out | tr -d '\n')" | tee -a "$LOG"

echo "-- E2E-005 churn-vs-switches disambiguated --" | tee -a "$LOG"
echo -n "  status churn lines: " | tee -a "$LOG"
uv run --no-sync cachepilot status --db /tmp/r15-telemetry.db 2>&1 | grep -iE "churn" | head -2 | tr '\n' ' | ' >>"$LOG"; echo >>"$LOG"
echo -n "  routes switches: " | tee -a "$LOG"
uv run --no-sync cachepilot routes --db /tmp/r15-telemetry.db 2>&1 | grep -iE "switch" | head -1 | tr -d '\n' | tee -a "$LOG"; echo >>"$LOG"

echo "-- E2E-008/009 corrupt + wrong-schema: 8 CLI read commands exit 0 no traceback --" | tee -a "$LOG"
for bad in /tmp/r15-corrupt.db /tmp/r15-wrong.db; do
  for cmd in status leases costs ttl churn explain-miss routes topology; do
    out=$(uv run --no-sync cachepilot "$cmd" --db "$bad" 2>&1); rc=$?
    tb=$(echo "$out" | grep -ci Traceback); nst=$(echo "$out" | grep -ci "no such table")
    echo "  $(basename "$bad") $cmd exit=$rc tb=$tb nst=$nst" | tee -a "$LOG"
  done
done

echo "-- E2E-008/009 dashboard corrupt (9086) + wrong-schema (9087) 200 empty JSON --" | tee -a "$LOG"
for port in 9086 9087; do
  code=$(curl -s -o /tmp/r15_bad_api.json -w "%{http_code}" "http://127.0.0.1:$port/api/leases")
  echo "  :$port/api/leases -> HTTP $code body=$(cat /tmp/r15_bad_api.json | tr -d '\n')" | tee -a "$LOG"
done

echo "-- E2E-006 320px mobile via code/build --" | tee -a "$LOG"
grep -n "max-width: 768px" dashboard/src/styles.css | head -1 >>"$LOG"
grep -oE "@media ?\(max-width: ?768px\)\{" dashboard/dist/assets/index-*.css | head -c 80; echo >>"$LOG"

echo "-- EDGE PROBE BATCH (new-defect hunt) --" | tee -a "$LOG"

echo "  E1 hostile params /api/leases?limit=-5|abc|999999 offset=-1 -> " | tee -a "$LOG"
curl -s -o /dev/null -w "     HTTP %{http_code} size=%{size_download}\n" "$B/api/leases?limit=-5|abc|999999&offset=-1" | tee -a "$LOG"
echo "  E2 hostile sessions /api/miss?session=... -> " | tee -a "$LOG"
for s in "../../etc/passwd" "%00" "a%20b" "999999" "null"; do
  curl -s -o /tmp/r15_miss -w "     session=$s HTTP %{http_code} size=%{size_download} " "$B/api/miss?session=$s" >>"$LOG"
  head -c 30 /tmp/r15_miss | tr -d '\n' >>"$LOG"; echo >>"$LOG"
done
echo "  E3 404 handling -> " | tee -a "$LOG"
for p in /api/not-an-endpoint /api/leases/ /api//leases; do
  curl -s -o /dev/null -w "     $p HTTP %{http_code} size=%{size_download}\n" "$B$p" | tee -a "$LOG"
done
echo "  E4 static traversal -> " | tee -a "$LOG"
for p in "/../../etc/passwd" "/%2e%2e/etc/passwd" "/..%2f..%2fetc/passwd"; do
  curl -s -o /tmp/r15_tr -w "     $p HTTP %{http_code} size=%{size_download} " "$B$p" >>"$LOG"
  head -c 25 /tmp/r15_tr | tr -d '\n' >>"$LOG"; echo >>"$LOG"
done
echo "  E5 HTTP/1.0 /api/health -> " | tee -a "$LOG"
curl -s --http1.0 -D - -o /tmp/r15_h10 "$B/api/health" | tr -d '\r' | grep -iE "^HTTP|^Content-Length|^Content-Type" | tee -a "$LOG"
echo "  E6 relay control-path encodings -> " | tee -a "$LOG"
for p in "/cachepilot/health?x=1" "/cachepilot/health/" "/cachepilot//health" "/CachePilot/health"; do
  curl -s -o /tmp/r15_cp -w "     $p HTTP %{http_code} size=%{size_download} body=" "$R$p" >>"$LOG"
  head -c 30 /tmp/r15_cp | tr -d '\n' >>"$LOG"; echo >>"$LOG"
done
echo "  E7 --db edge cases: empty file / directory / /dev/null -> " | tee -a "$LOG"
: > /tmp/r15-empty.db
uv run --no-sync cachepilot status --db /tmp/r15-empty.db >/tmp/r15_emp.out 2>&1; rc=$?
echo "     empty-file exit=$rc tb=$(grep -ci traceback /tmp/r15_emp.out)" | tee -a "$LOG"
mkdir -p /tmp/r15-dbdir
uv run --no-sync cachepilot status --db /tmp/r15-dbdir >/tmp/r15_dir.out 2>&1; rc=$?
echo "     directory-as-db exit=$rc tb=$(grep -ci traceback /tmp/r15_dir.out)" | tee -a "$LOG"
uv run --no-sync cachepilot status --db /dev/null >/tmp/r15_dev.out 2>&1; rc=$?
echo "     /dev/null-as-db exit=$rc tb=$(grep -ci traceback /tmp/r15_dev.out)" | tee -a "$LOG"

echo "-- E2E-011 hygiene self-test + post-run teardown verification --" | tee -a "$LOG"
./.venv/bin/python e2e-output/hygiene.py self-test >>"$LOG" 2>&1
echo "  self-test exit=$?" | tee -a "$LOG"

echo "===== JOURNEY COMPLETE =====" | tee -a "$LOG"
echo "===== explicit teardown =====" | tee -a "$LOG"
e2e_teardown
echo "===== post-teardown 908x scan =====" | tee -a "$LOG"
./.venv/bin/python e2e-output/hygiene.py scan
exit 0