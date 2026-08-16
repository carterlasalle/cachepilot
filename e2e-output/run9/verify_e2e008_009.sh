#!/usr/bin/env bash
# E2E-001 Run 9 — verify E2E-008 (corrupt) + E2E-009 (wrong-schema) honest-empty contract
set -u
cd /home/hermes/cachepilot || exit 1
printf 'this is not a sqlite database' > /tmp/r9-corrupt.db
uv run python e2e-output/run9/make_wrongschema.py

echo "=== E2E-008: corrupt/non-SQLite DB ==="
for cmd in status churn explain-miss leases costs; do
  out=$(uv run cachepilot "$cmd" --db /tmp/r9-corrupt.db 2>&1); code=$?
  tb=$(printf '%s' "$out" | grep -c Traceback)
  note=$(printf '%s' "$out" | grep -icE 'corrupt|empty store' || true)
  echo "$cmd: exit=$code traceback=$tb notice=$note"
done

echo "=== E2E-009: wrong-schema SQLite DB ==="
for cmd in status churn leases routes topology explain-miss costs ttl; do
  out=$(uv run cachepilot "$cmd" --db /tmp/r9-wrong.db 2>&1); code=$?
  tb=$(printf '%s' "$out" | grep -c Traceback)
  nst=$(printf '%s' "$out" | grep -icE 'no such table|expected telemetry schema|empty store' || true)
  leak=$(printf '%s' "$out" | grep -qi 'no such table' && echo LEAK || echo clean)
  echo "$cmd: exit=$code traceback=$tb schema_notice=$nst $leak"
done
echo "DONE"