#!/usr/bin/env python3
"""E2E test-hygiene guard + teardown for CachePilot E2E ticks (E2E-011).

Reusable, stdlib-only (Python 3.12) helper that stops a single E2E tick from
leaking its ephemeral test services into the next run:

  * ``pre_run_guard()`` -- FAILS (or auto-cleans) the tick when any process is
    already listening on the reserved 908x test-only port range, so Run N+1 can
    never silently bind-test a stale process left by Run N.
  * ``teardown()``      -- kills the spawned test PIDs (mock upstream,
    ``cachepilotd --listen`` relay, dashboard backend ``server.py``) and then
    re-verifies via ``ss``/``ps`` that NO process remains listening on 908x, so
    every subsequent run binds fresh current-build services.

The 908x range is RESERVED TEST-ONLY (README "Test hygiene" + tasks.md E2E-011);
no product service may bind it.

Usage as a library::

    import sys; sys.path.insert(0, "e2e-output")
    import hygiene
    hygiene.pre_run_guard()              # fail hard if a stale listener on 908x
    p1 = subprocess.Popen([... mock_upstream ...])
    p2 = subprocess.Popen([... cachepilotd --listen ...])
    ... run the tick ...
    hygiene.teardown(p1.pid, p2.pid)     # kill + post-verify 908x clean

Usage as a CLI::

    python e2e-output/hygiene.py scan                # list 908x occupants
    python e2e-output/hygiene.py pre-run             # fail (exit 1) if occupied
    python e2e-output/hygiene.py pre-run --clean     # auto-kill stale occupants
    python e2e-output/hygiene.py teardown 1234 5678 # kill pids + verify clean
    python e2e-output/hygiene.py self-test           # live guard+teardown proof
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

# Reserved ephemeral TEST-ONLY range (>= TEST_PORT_START <= TEST_PORT_END).
# Nothing outside test code may bind these; see README "Test hygiene".
TEST_PORT_START = 9080
TEST_PORT_END = 9089

# cmdline fragments used only to *label* an ``ss``-detected occupant as an E2E
# test service (never to discover one -- discovery is purely listen-state based).
_E2E_CMD_TOKENS = ("mock_upstream.py", "cachepilotd", "dashboard/backend/server.py")

_PORT_TOKEN = re.compile(r":(\d{2,5})\b")
_PID_TOKEN = re.compile(r"pid=(\d+)")


@dataclass
class Occupant:
    """A process currently listening on the reserved 908x range."""

    port: int
    pid: str | None = None
    cmdline: str = ""

    @property
    def is_e2e(self) -> bool:
        return any(tok in self.cmdline for tok in _E2E_CMD_TOKENS)


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False).stdout
    except FileNotFoundError:
        sys.stderr.write(
            f"hygiene: required command not found: {' '.join(argv)}\n"
        )
        return ""


def _cmdlines(pids: list[str]) -> dict[str, str]:
    if not pids:
        return {}
    out = _run(["ps", "-o", "pid=,command=", "-p", ",".join(pids)])
    result: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            result[parts[0]] = parts[1]
    return result


def find_occupants() -> list[Occupant]:
    """Return every process currently LISTENING on the reserved 908x range."""
    out = _run(["ss", "-tlnp"])
    seats: dict[int, Occupant] = {}
    order: list[int] = []
    for line in out.splitlines():
        if not line.lstrip().startswith("LISTEN"):
            continue
        in_range = [
            int(p)
            for p in _PORT_TOKEN.findall(line)
            if TEST_PORT_START <= int(p) <= TEST_PORT_END
        ]
        if not in_range:
            continue
        port = in_range[0]
        if port not in seats:
            pid = _PID_TOKEN.search(line)
            seats[port] = Occupant(port=port, pid=pid.group(1) if pid else None)
            order.append(port)
    cmd = _cmdlines([o.pid for o in seats.values() if o.pid])
    for port in order:
        occ = seats[port]
        if occ.pid:
            occ.cmdline = cmd.get(occ.pid, "")
    return [seats[p] for p in order]


def _kill_pid(pid: int | str, sig: int) -> bool:
    try:
        os.kill(int(pid), sig)
        return True
    except (ProcessLookupError, PermissionError, ValueError):
        return False


def _alive(pid: int | str) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill_until_clean() -> list[Occupant]:
    """Kill every 908x occupant, escalating TERM -> KILL, until the range is free.

    Re-surveys a few times so a child daemon that survives its parent's death
    (a common leak shape) is still caught: anything listening on 908x is killed.
    Returns the (expectedly empty) list of occupants still listening.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for occ in find_occupants():
            if occ.pid:
                _kill_pid(occ.pid, sig)
        time.sleep(0.3 if sig == signal.SIGTERM else 0.2)
    return find_occupants()


def pre_run_guard(clean: bool = False) -> int:
    """Fail (or auto-clean) the tick when a stale process is already on 908x.

    Returns 0 when the range is free (or was cleaned), non-zero otherwise.
    With ``clean=True`` stale occupants are killed before returning; otherwise a
    stale occupant prints an actionable error and returns 1.
    """
    occupants = find_occupants()
    if not occupants:
        print(
            "hygiene pre-run: OK -- no listener on reserved 908x range.",
            flush=True,
        )
        return 0
    print("hygiene pre-run: STALE TEST PROCESS DETECTED on 908x:", flush=True)
    for occ in occupants:
        kind = "E2E test service" if occ.is_e2e else "unexpected/unknown process"
        print(
            f"  port {occ.port}  pid={occ.pid or '?'}  {kind}  "
            f"cmd={occ.cmdline or 'n/a'}",
            flush=True,
        )
    if clean:
        print("hygiene pre-run: auto-cleaning stale occupants...", flush=True)
        left = _kill_until_clean()
        if not left:
            print("hygiene pre-run: range cleaned.", flush=True)
            return 0
        print("hygiene pre-run: FAIL -- could not free:", flush=True)
        for occ in left:
            print(f"  port {occ.port}  pid={occ.pid or '?'}  {occ.cmdline or 'n/a'}", flush=True)
        return 1
    print(
        "hygiene pre-run: refusing to run the tick. Free 908x before proceeding "
        "(e.g. `python e2e-output/hygiene.py pre-run --clean`, or kill the PID "
        "listed above).",
        flush=True,
    )
    return 1


def teardown(*pids: int, grace: float = 1.0) -> int:
    """Kill the spawned test PIDs, then verify NO 908x listener remains.

    Sends TERM to every given PID, waits ``grace`` seconds, KILLs any that
    survived, then kills-and-re-checks every remaining 908x occupant.
    Returns 0 when the range ends clean, non-zero otherwise.
    """
    for pid in pids:
        _kill_pid(pid, signal.SIGTERM)
    time.sleep(grace)
    for pid in pids:
        if _alive(pid):
            _kill_pid(pid, signal.SIGKILL)
    time.sleep(0.3)
    occupants = _kill_until_clean()
    if not occupants:
        print(
            "hygiene teardown: OK -- spawned services dead, 908x clean.",
            flush=True,
        )
        return 0
    print("hygiene teardown: FAIL -- still listening on 908x:", flush=True)
    for occ in occupants:
        print(f"  port {occ.port}  pid={occ.pid or '?'}  cmd={occ.cmdline or 'n/a'}", flush=True)
    return 1


_FAKE_SERVICE = (
    "import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,"
    "socket.SO_REUSEADDR,1); s.bind(('127.0.0.1', %d)); s.listen(5);"
    "import time; time.sleep(300)"
)


def _self_test() -> int:
    """Live demonstration: guard catches a stale listener; teardown frees it."""
    port = TEST_PORT_START
    proc = subprocess.Popen([sys.executable, "-c", _FAKE_SERVICE % port])
    try:
        for _ in range(100):
            if find_occupants():
                break
            time.sleep(0.1)
        print(
            f"== pre_run_guard (expect FAIL while fake listener holds port {port}) =="
        )
        rc = pre_run_guard()
        print(f"pre_run_guard exit={rc} (expected 1)\n")
        if rc != 1:
            return 1
        print("== pre_run_guard --clean (auto-clean the stale listener) ==")
        rc = pre_run_guard(clean=True)
        print(f"pre_run_guard --clean exit={rc} (expected 0)\n")
        if rc != 0 or find_occupants():
            return 1
        print("== teardown (kill a spawned PID + verify clean) ==")
        proc2 = subprocess.Popen([sys.executable, "-c", _FAKE_SERVICE % port])
        for _ in range(100):
            if find_occupants():
                break
            time.sleep(0.1)
        rc = teardown(proc2.pid)
        proc2.wait(timeout=10)
        print(f"teardown exit={rc} (expected 0)\n")
        return 0 if rc == 0 and not find_occupants() else 1
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "scan":
        occupants = find_occupants()
        for occ in occupants:
            print(f"port {occ.port}  pid={occ.pid or '?'}  {occ.cmdline or 'n/a'}")
        return 1 if occupants else 0
    if cmd == "pre-run":
        return pre_run_guard(clean="--clean" in argv)
    if cmd == "teardown":
        pids = [int(a) for a in argv[1:] if a.isdigit()]
        if not pids:
            sys.stderr.write("hygiene: teardown requires at least one PID\n")
            return 2
        return teardown(*pids)
    if cmd == "self-test":
        return _self_test()
    sys.stderr.write(f"hygiene: unknown command {cmd!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())