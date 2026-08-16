# E2E Testing Runbook — ephemeral service hygiene (E2E-011)

CachePilot E2E ticks spin up a small fleet of live test services, verify a
user journey against them, tear them down, and continue. The #1 way those
ticks go wrong is **process leakage**: a test service from run N keeps running
for hours, so run N+1's fresh bind fails (`Address already in use`) and its
"live" evidence is — silently — gathered against a stale process from an
earlier run.

This page documents the hygiene contract that prevents that. It is
infrastructure, not product code: nothing in `src/`, `packages/`, or
`dashboard/backend/server.py` logic is changed by it.

## Reserved port range

The `908x` range (ports **9080-9089**) is **reserved TEST-ONLY**. Every E2E
tick uses it for its ephemeral services:

| Service | Typical bind |
|---|---|
| mock upstream | `python e2e-output/run<N>/mock_upstream.py 9081` |
| relay | `uv run cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081` |
| dashboard backend | `.venv/bin/python dashboard/backend/server.py --port 9083` (+ 908x for corrupt/wrong-schema replay) |

No product service may bind `908x`. If the range is occupied at the start of
a tick, something leaked and the tick must stop and clean (not proceed).

## The reusable helper

[`e2e-output/hygiene.py`](../e2e-output/hygiene.py) (stdlib-only, no deps)
exposes two first-class operations, plus a runnable `self-test`:

- `pre_run_guard(clean=False)` — scans `ss -tlnp` for any listener on `908x`.
  Free range → exit 0. Occupied → prints a clear, actionable message (port,
  pid, whether it looks like an E2E service) and either **fails the tick**
  (exit 1) or, with `clean=True`, **auto-kills** the stale listener and
  re-verifies.
- `teardown(*pids)` — sends `TERM` to every spawned PID, then `KILL` any
  survivor, then kills any remaining `908x` occupant and post-verifies via
  `ss`/`ps`. Exit 0 = spawned services dead AND 908x clean.
- `self-test` — starts a fake listener on the range, proves the guard fails it
  and that `--clean`/`teardown` free it. Live proof the helper works.

CLI:

```bash
python e2e-output/hygiene.py scan                 # list 908x occupants
python e2e-output/hygiene.py pre-run              # fail (exit 1) if occupied
python e2e-output/hygiene.py pre-run --clean      # auto-kill stale occupants
python e2e-output/hygiene.py teardown 1234 5678   # kill pids + verify clean
python e2e-output/hygiene.py self-test            # live guard+teardown proof
```

## Required tick hygiene (the contract)

Every E2E tick MUST do all three, in order:

1. **Pre-run guard.** Before spawning anything, run the guard and stop if a
   stale process already listens on `908x`. Forbidden to proceed and
   "bind-test a stale service". Use `--clean` if you'd rather auto-clean than
   abort.

   ```bash
   python e2e-output/hygiene.py pre-run        # abort (exit 1) if occupied
   # or auto-clean:
   python e2e-output/hygiene.py pre-run --clean
   ```

2. **Trap-based teardown.** Wrap every spawned service so a crash, Ctrl-C, or a
   `set -e` abort cannot leak it. Source `e2e-output/hygiene.sh` and spawn via
   `e2e_spawn` + install traps with `e2e_wrap`:

   ```bash
   source e2e-output/hygiene.sh
   e2e_guard_pre_run && e2e_wrap
   e2e_spawn python e2e-output/runN/mock_upstream.py 9081
   e2e_spawn uv run cachepilotd --listen 127.0.0.1:9082 --upstream http://127.0.0.1:9081
   # ... run the tick ...
   e2e_teardown          # kill spawned + verify 908x clean (on EXIT/INT/ERR too)
   ```

   (Or, programmatically: start each service with `subprocess.Popen`, keep the
   PIDs, and call `hygiene.teardown(p1.pid, p2.pid)` in a `finally`/trap.)

3. **Post-run verification.** After the tick, re-verify no process remains on
   `908x`. The helper does this inside `teardown`; independently it is:

   ```bash
   ss -tlnp | grep -E ':908[0-9]'    # must print nothing
   ps -ef | grep -E 'mock_upstream.cachepilotd --listen|dashboard/backend/server.py'
   ```

Run `python e2e-output/hygiene.py self-test` at any time to confirm the guard
and teardown behave as documented.

## Why this exists (E2E-011 reproduction)

Run 9 (2026-08-15, 21:59-22:00) started its mock upstream
(`e2e-output/run9/mock_upstream.py 9081`) and relay
(`cachepilotd --listen 127.0.0.1:9082`) and then claimed "All services were
killed after the tick". They were **not**: both stayed alive for ~4 hours
(pids 2955253 / 2955310) across the E2E-010 fix tick and 10 idle ticks. Run 11
then failed its fresh binds (`Address already in use`) and its initial relay
evidence ran against those stale survivors. The two processes were
functionally identical to a fresh instance, so the final evidence was valid —
but the leak contaminated verification integrity and wasted debugging time.
This runbook + the hygiene helper close that gap.