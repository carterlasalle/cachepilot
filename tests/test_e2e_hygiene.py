"""Tests for the E2E test-hygiene guard/teardown helper (E2E-011).

Covers the reusable ``e2e-output/hygiene.py``: the pre-run guard that fails on
a stale 908x listener, the auto-clean path, and teardown that kills and then
re-verifies no 908x listener remains. Uses real local bind/kill -- the 908x
range is reserved test-only, so it is expected to be free in CI/lab.
"""

import os
import subprocess
import sys
import time

import pytest

_E2E_OUTPUT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "e2e-output"))
if _E2E_OUTPUT not in sys.path:
    sys.path.insert(0, _E2E_OUTPUT)

import hygiene

_FAKE_SERVICE = (
    "import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,"
    "socket.SO_REUSEADDR,1); s.bind(('127.0.0.1', %d)); s.listen(5);"
    "import time; time.sleep(300)"
)


@pytest.fixture()
def fake_listener():
    """Start a real listener on the reserved 908x range and clean up after."""
    port = hygiene.TEST_PORT_START
    proc = subprocess.Popen([sys.executable, "-c", _FAKE_SERVICE % port])
    try:
        for _ in range(100):
            if hygiene.find_occupants():
                break
            time.sleep(0.05)
        yield port, proc
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        hygiene.teardown(proc.pid)


def test_reserved_range_is_908x():
    assert hygiene.TEST_PORT_START == 9080
    assert hygiene.TEST_PORT_END == 9089


def test_find_occupants_detects_reserved_range_listener(fake_listener):
    port, _proc = fake_listener
    assert any(o.port == port for o in hygiene.find_occupants())


def test_pre_run_guard_returns_nonzero_when_occupied(fake_listener):
    _port, _proc = fake_listener
    assert hygiene.pre_run_guard() != 0


def test_pre_run_guard_clean_kills_occupant(fake_listener):
    _port, proc = fake_listener
    assert hygiene.pre_run_guard(clean=True) == 0
    assert hygiene.find_occupants() == []
    proc.wait(timeout=10)


def test_teardown_kills_and_verifies_clean(fake_listener):
    _port, proc = fake_listener
    assert hygiene.teardown(proc.pid) == 0
    assert hygiene.find_occupants() == []
    proc.wait(timeout=10)


def test_self_test_live_guard_and_teardown():
    assert hygiene.main(["self-test"]) == 0