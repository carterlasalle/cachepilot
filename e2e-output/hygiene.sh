#!/usr/bin/env bash
# e2e-output/hygiene.sh -- sourced by CachePilot E2E ticks (E2E-011).
#
# Wrap every spawned ephemeral test service in trap-based teardown so a crash,
# Ctrl-C, or a `set -e` abort can never leak a process across tick runs
# (run-9 leaked its mock upstream + relay ~4h; run-11's fresh binds then failed
# "Address already in use" -- see e2e-output/tasks.md E2E-011).
#
# Usage (source it, then spawn every test service with e2e_spawn):
#
#   source e2e-output/hygiene.sh
#   e2e_guard_pre_run                       # fail fast if any stale 908x listener
#   e2e_wrap                                # install EXIT/INT/TERM/ERR teardown
#   e2e_spawn python e2e-output/runN/mock_upstream.py 9081
#   e2e_spawn uv run cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081
#   ... run the tick ...
#   e2e_teardown                            # kill spawned + verify 908x clean
#
# The 908x range is RESERVED TEST-ONLY (README "Test hygiene" + tasks.md).

E2E_HYGIENE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_HYGIENE_PY="$E2E_HYGIENE_DIR/hygiene.py"
E2E_REPO_ROOT="$(cd "$E2E_HYGIENE_DIR/.." && pwd)"
if [ -x "$E2E_REPO_ROOT/.venv/bin/python" ]; then
  E2E_HYGIENE_PYTHON="$E2E_REPO_ROOT/.venv/bin/python"
  E2E_HYGIENE_EXEC=("$E2E_HYGIENE_PYTHON")
elif command -v python3 >/dev/null 2>&1; then
  E2E_HYGIENE_EXEC=(python3)
elif command -v uv >/dev/null 2>&1; then
  E2E_HYGIENE_EXEC=(uv run python)
else
  E2E_HYGIENE_EXEC=(python)
fi

# Fail (or with --clean, auto-kill) if a stale process already listens on 908x.
e2e_guard_pre_run() {
  "${E2E_HYGIENE_EXEC[@]}" "$E2E_HYGIENE_PY" pre-run "$@" || return $?
}

# Kill every spawned test PID and re-verify 908x is clean via ss/ps.
e2e_teardown() {
  local rc=0
  if [ ${#E2E_SPAWNED_PIDS[@]} -gt 0 ]; then
    "${E2E_HYGIENE_EXEC[@]}" "$E2E_HYGIENE_PY" teardown "${E2E_SPAWNED_PIDS[@]}" || rc=$?
    E2E_SPAWNED_PIDS=()
  else
    "${E2E_HYGIENE_EXEC[@]}" "$E2E_HYGIENE_PY" pre-run --clean || rc=$?
  fi
  return "$rc"
}

# Spawn a test service in the background and remember its PID for teardown.
e2e_spawn() {
  "$@" &
  E2E_SPAWNED_PIDS+=("$!")
}

# Install trap-based teardown so a crash/Ctrl-C/set -e abort cannot leak
# a spawned service across runs.
e2e_wrap() {
  trap 'e2e_teardown' EXIT
  trap 'e2e_teardown; exit 1' INT TERM
  set -E
  trap 'e2e_teardown' ERR
}