"""Self-contained verification for the dashboard backend (PRD §122/§139).

Not part of the pytest suite (the dashboard is optional and self-contained —
``pyproject.toml`` testpaths do not include it). Run it with the workspace
venv so ``cachepilot_core`` resolves:

    uv run python dashboard/backend/smoke_test.py

What it verifies:

1. A telemetry DB seeded via ``TelemetryStore`` serves every dashboard
   endpoint (status / leases / costs / ttl / routes / churn / miss /
   topology / health) with real, non-empty data.
2. A missing DB renders EMPTY states (zeros and empty lists) — never
   fabricated numbers.
3. The backend is read-only: the DB file's SHA-256 is byte-identical before
   and after the whole read session, and no new files appear next to it.
4. Unknown endpoints 404 and writes 405 (GET-only backend).
5. E2E-002: the relay health probe is an HTTP confirmation of the relay's
   local control endpoint (healthy only against a REAL relay; a foreign HTTP
   server reads 'occupied by another service'; a closed port reads
   'unreachable'), and startup on an occupied port fails with a clear
   actionable error instead of a bare EADDRINUSE.

Exit code 0 = everything verified; nonzero with a message otherwise.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import uvicorn
from cachepilot_core.leases import CacheLease, LeaseState
from cachepilot_core.route_intel import RouteChangeEvent, RouteMissVerdict
from cachepilot_core.storage import TelemetryStore
from cachepilot_core.telemetry import ChurnEvent, Outcome, TelemetryEvent, TokenUsage
from cachepilot_core.ttl import TTLProfile
from cachepilot_relay.config import RelayConfig
from cachepilot_relay.server import create_app

#: Ensure the repo root is importable (the script may be run from any cwd).
repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from cachepilot_cli.main import main as cli_main

from dashboard.backend.server import DashboardServer, Handler
from dashboard.backend.server import main as server_main

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    suffix = f" — {detail}" if detail and condition else (f" — {detail}" if detail else "")
    print(f"  {status} {label}{suffix}")
    if not condition:
        FAILURES.append(f"{label}: {detail}")


def seed_store(db_path: Path) -> None:
    """Write a small, realistic telemetry fixture via the CLI-sanctioned path."""
    store = TelemetryStore(db_path)
    now = datetime.now(UTC)
    try:
        s1, s2 = "hash-session-1", "hash-session-2"
        fp_a, fp_b = "a" * 64, "b" * 64
        store.record_request(
            TelemetryEvent(
                request_fingerprint="req-fp-1",
                cache_fingerprint=fp_a,
                provider="openai",
                model="gpt-4o-mini",
                route_hash="route-1",
                usage=TokenUsage(
                    prompt_tokens=120,
                    completion_tokens=40,
                    cache_read_tokens=100,
                    cost=Decimal("0.000120"),
                ),
                outcome=Outcome.CONFIRMED_HIT,
                session_hash=s1,
                system_hash="sys-1",
                tools_hash="tools-1",
                history_hash="hist-1",
                timestamp=now,
            )
        )
        store.record_request(
            TelemetryEvent(
                request_fingerprint="req-fp-2",
                cache_fingerprint=fp_b,
                provider="openai",
                model="gpt-4o-mini",
                route_hash="route-1",
                usage=TokenUsage(
                    prompt_tokens=130,
                    completion_tokens=55,
                    cache_read_tokens=0,
                    cache_write_tokens=200,
                    cost=Decimal("0.000210"),
                ),
                outcome=Outcome.MISS_REBUILT,
                session_hash=s1,
                system_hash="sys-1",
                tools_hash="tools-2",
                history_hash="hist-1",
                timestamp=now,
            )
        )
        store.record_request(
            TelemetryEvent(
                request_fingerprint="req-fp-3",
                cache_fingerprint="c" * 64,
                provider="anthropic",
                model="claude-3-5-haiku",
                route_hash="route-2",
                outcome=Outcome.SUCCESS_UNVERIFIED,
                session_hash=s2,
                timestamp=now,
            )
        )
        store.record_churn(
            ChurnEvent(
                timestamp=now,
                session_hash=s1,
                previous_cache_fingerprint=fp_a,
                new_cache_fingerprint=fp_b,
                provider="openai",
                model="gpt-4o-mini",
                route_hash="route-1",
                system_changed=False,
                tools_changed=True,
                history_changed=False,
                route_changed=False,
                cache_key_changed=True,
                model_changed=False,
                likely_cause="tool list mutation",
                confidence=0.82,
                estimated_prefix_loss_tokens=1234,
                first_divergent_offset=4096,
                first_divergent_layer="tool schemas",
            )
        )
        store.record_lease(
            CacheLease(
                lease_id="lease-0001",
                session_id=s1,
                provider="openai",
                model="gpt-4o-mini",
                api_mode="chat",
                base_url="https://api.openai.com/v1",
                auth_scope_hash="auth-1",
                route_fingerprint="route-1",
                request_fingerprint="req-fp-1",
                cache_fingerprint=fp_a,
                system_fingerprint="sys-1",
                tools_fingerprint="tools-1",
                history_prefix_fingerprint="hist-1",
                last_real_request_at=time.time() - 120,
                last_cache_touch_at=time.time() - 60,
                last_confirmed_hit_at=time.time() - 60,
                estimated_ttl_s=300.0,
                ttl_confidence=0.9,
                active_targets={"target-1"},
                generation=2,
                warm_count=1,
                warm_cost_usd=0.000010,
                estimated_cold_resume_cost_usd=0.001000,
                estimated_cached_resume_cost_usd=0.000050,
                state=LeaseState.ARMED,
            )
        )
        profile = TTLProfile(
            provider="openai",
            model="gpt-4o-mini",
            api_mode="chat",
            endpoint_hash="ep-hash-1",
            route_hash="route-1",
            lower_bound_s=120.0,
            upper_bound_s=600.0,
            estimated_ttl_s=288.0,
            confidence=0.85,
            sample_count=12,
            updated_at=now,
        )
        store.upsert_profile(profile)
        for idle_age, outcome in (
            (60.0, Outcome.CONFIRMED_HIT),
            (120.0, Outcome.CONFIRMED_HIT),
            (180.0, Outcome.CONFIRMED_HIT),
            (300.0, Outcome.MISS_REBUILT),
        ):
            store.record_ttl_observation(
                timestamp=now,
                cache_fingerprint=fp_a,
                route_hash="route-1",
                idle_age_s=idle_age,
                outcome=outcome,
                clean=True,
                provider="openai",
                model="gpt-4o-mini",
                api_mode="chat",
                endpoint_hash="ep-hash-1",
            )
        store.record_route_event(
            RouteChangeEvent(
                timestamp=now,
                session_hash=s1,
                cache_fingerprint=fp_a,
                request_fingerprint="req-fp-1",
                previous_route_hash="route-1",
                new_route_hash="route-2",
                gateway="api.openai.com",
                upstream_provider="openai",
                endpoint="/v1/chat/completions",
                region="us-east-1",
                deployment=None,
                verdict=RouteMissVerdict.ROUTE_INSTABILITY,
            )
        )
    finally:
        store.close()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch(port: int, path: str) -> tuple[int, Any]:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


class _ForeignHttpHandler(BaseHTTPRequestHandler):
    """Minimal non-relay HTTP server (stands in for hermes-webui on 8787)."""

    def do_GET(self) -> None:
        body = b"<html><body>hermes web ui</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # quiet access log
        pass


def start_real_relay() -> tuple[Any, threading.Thread, int]:
    """Boot a REAL cachepilotd app (``create_app``) on an ephemeral port.

    Runs in a daemon thread via uvicorn's blocking ``run()``; the probe only
    ever touches the local control endpoint, so the upstream (a closed
    loopback port) is never reached. Returns (server, thread, bound_port).
    """
    config = RelayConfig(
        upstream="http://127.0.0.1:1",
        listen="127.0.0.1:0",
        observation_enabled=False,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(config),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15.0
    while not server.started:
        if time.time() > deadline:
            thread.join(5)
            raise RuntimeError("real relay did not start within 15s")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, thread, port


def relay_readout(port: int, listen: str) -> str:
    """Fetch /api/status with CACHEPILOT_RELAY_LISTEN set for one request."""
    os.environ["CACHEPILOT_RELAY_LISTEN"] = listen
    try:
        _, payload = fetch(port, "/api/status")
        return payload["relay"]
    finally:
        del os.environ["CACHEPILOT_RELAY_LISTEN"]


def main() -> int:
    # Deterministic relay probe baseline: point the probe at a closed
    # loopback port so the seeded-store readout below cannot accidentally
    # observe whatever happens to be squatting on 127.0.0.1:8787 (E2E-002).
    os.environ["CACHEPILOT_RELAY_LISTEN"] = "127.0.0.1:1"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "telemetry.db"
        print("== seeded DB ==")
        seed_store(db_path)
        sha_before = file_sha256(db_path)
        files_before = sorted(p.name for p in tmp_path.iterdir())

        server = DashboardServer(("127.0.0.1", 0), Handler, db_path=str(db_path))
        port = int(server.server_address[1])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            print("== populated store ==")
            status, payload = fetch(port, "/api/status")
            check("status 200", status == 200, str(status))
            check(
                "status stats.total == 3",
                payload["stats"]["total"] == 3,
                str(payload["stats"]["total"]),
            )
            check(
                "status hit_rate is a number",
                isinstance(payload["stats"]["hit_rate"], float),
                str(payload["stats"]["hit_rate"]),
            )
            check(
                "status providers recorded",
                len(payload["providers"]) == 2,
                str(payload["providers"]),
            )
            check(
                "status relay readout",
                payload["relay"] in ("healthy", "unreachable", "occupied by another service"),
                str(payload["relay"]),
            )
            check(
                "status plugin readout",
                payload["plugin"].startswith("active"),
                str(payload["plugin"]),
            )

            status, payload = fetch(port, "/api/leases")
            check("leases 200", status == 200, str(status))
            check("leases non-empty", len(payload["leases"]) == 1, str(len(payload["leases"])))
            lease = payload["leases"][0]
            check("lease state armed", lease["state"] == "armed", str(lease["state"]))
            check(
                "lease cache age computed",
                lease["cache_age_s"] is not None,
                str(lease["cache_age_s"]),
            )
            check(
                "lease targets",
                lease["active_targets"] == ["target-1"],
                str(lease["active_targets"]),
            )

            status, payload = fetch(port, "/api/costs")
            check("costs 200", status == 200, str(status))
            check("costs total > 0", payload["total_usd"] > 0, str(payload["total_usd"]))
            check(
                "costs per-provider",
                payload["per_provider"].get("openai", 0) > 0,
                str(payload["per_provider"]),
            )
            check("costs recent series", len(payload["recent"]) == 2, str(len(payload["recent"])))
            check("costs honest note", "recorded-cost-only" in payload["note"], payload["note"])

            status, payload = fetch(port, "/api/ttl")
            check("ttl 200", status == 200, str(status))
            check(
                "ttl profiles non-empty",
                len(payload["profiles"]) == 1,
                str(len(payload["profiles"])),
            )
            profile = payload["profiles"][0]
            check(
                "ttl estimate present",
                profile["estimated_ttl_s"] == 288.0,
                str(profile["estimated_ttl_s"]),
            )
            check(
                "ttl survival curve",
                profile["survival"] is not None and profile["survival"]["sample_count"] == 4,
                str(profile.get("survival")),
            )
            if profile["survival"] is not None:
                check(
                    "ttl survival steps",
                    len(profile["survival"]["steps"]) >= 1,
                    str(len(profile["survival"]["steps"])),
                )
                check(
                    "ttl survival at-ttl defined",
                    profile["survival"]["p_survive_at_ttl"] is not None,
                    str(profile["survival"]["p_survive_at_ttl"]),
                )

            status, payload = fetch(port, "/api/routes")
            check("routes 200", status == 200, str(status))
            check(
                "routes events non-empty", len(payload["events"]) == 1, str(len(payload["events"]))
            )
            check(
                "routes instability verdict",
                payload["events"][0]["verdict"] == "route_instability",
                str(payload["events"][0]["verdict"]),
            )
            check("routes stats", payload["stats"]["route_switches"] == 1, str(payload["stats"]))

            status, payload = fetch(port, "/api/churn")
            check("churn 200", status == 200, str(status))
            check(
                "churn events non-empty", len(payload["events"]) == 1, str(len(payload["events"]))
            )
            tools = next((layer for layer in payload["layers"] if layer["layer"] == "tools"), None)
            check(
                "churn tools layer changed", tools is not None and tools["changed"] == 1, str(tools)
            )
            check(
                "churn top causes",
                len(payload["top_causes"]) == 1
                and payload["top_causes"][0]["cause"] == "tool list mutation",
                str(payload["top_causes"]),
            )

            status, payload = fetch(port, "/api/miss")
            check("miss 200", status == 200, str(status))
            check(
                "miss explains latest",
                payload["event"] is not None
                and payload["event"]["likely_cause"] == "tool list mutation",
                str(payload.get("event")),
            )
            check(
                "miss changed layers",
                "tools" in payload["changed"] and "system" in payload["stable"],
                f"{payload['stable']} / {payload['changed']}",
            )
            check(
                "miss prefix loss",
                payload["event"]["estimated_prefix_loss_tokens"] == 1234,
                str(payload["event"]),
            )

            status, payload = fetch(port, "/api/miss?session=hash-session-2")
            check("miss unknown session empty", payload["event"] is None, str(payload.get("event")))

            status, payload = fetch(port, "/api/topology")
            check("topology 200", status == 200, str(status))
            check(
                "topology pair measured", payload["total_pairs"] == 1, str(payload["total_pairs"])
            )
            check("topology churn pair", payload["churn_pairs"] == 1, str(payload["churn_pairs"]))

            status, payload = fetch(port, "/api/health")
            check("health 200 ok", status == 200 and payload == {"ok": True}, str(payload))

            status, _ = fetch(port, "/api/not-an-endpoint")
            check("unknown endpoint 404", status == 404, str(status))

            print("== read-only proof ==")
            sha_after = file_sha256(db_path)
            files_after = sorted(p.name for p in tmp_path.iterdir())
            new_files = [name for name in files_after if name not in files_before]
            check(
                "DB bytes unchanged",
                sha_before == sha_after,
                f"{sha_before[:12]}… vs {sha_after[:12]}…",
            )
            # SQLite creates empty -wal/-shm journal sidecars when ANY
            # connection (read-only included — the CLI's reads do the same)
            # opens a WAL-mode database that was cleanly closed; they are
            # journal bookkeeping, never part of the database.
            sidecars = {f"{db_path.name}-wal", f"{db_path.name}-shm"}
            check(
                "no files created other than SQLite WAL sidecars",
                set(new_files) <= sidecars,
                str(new_files),
            )

            print("== empty store (missing DB) ==")
            empty_server = DashboardServer(
                ("127.0.0.1", 0), Handler, db_path=str(tmp_path / "missing.db")
            )
            empty_port = int(empty_server.server_address[1])
            empty_thread = threading.Thread(target=empty_server.serve_forever, daemon=True)
            empty_thread.start()
            try:
                status, payload = fetch(empty_port, "/api/leases")
                check("empty leases []", status == 200 and payload == {"leases": []}, str(payload))
                status, payload = fetch(empty_port, "/api/status")
                check(
                    "empty stats zero",
                    payload["stats"]["total"] == 0 and payload["providers"] == [],
                    str(payload),
                )
                check(
                    "empty plugin honest",
                    "no telemetry recorded yet" in payload["plugin"],
                    str(payload["plugin"]),
                )
                status, payload = fetch(empty_port, "/api/costs")
                check(
                    "empty costs zero",
                    payload["total_usd"] == 0.0 and payload["recent"] == [],
                    str(payload),
                )
                status, payload = fetch(empty_port, "/api/ttl")
                check("empty ttl []", payload["profiles"] == [], str(payload))
                status, payload = fetch(empty_port, "/api/routes")
                check(
                    "empty routes",
                    payload["events"] == [] and payload["stats"]["route_switches"] == 0,
                    str(payload),
                )
                status, payload = fetch(empty_port, "/api/churn")
                check(
                    "empty churn layers zero",
                    all(layer["changed"] == 0 for layer in payload["layers"]),
                    str(payload["layers"]),
                )
                status, payload = fetch(empty_port, "/api/miss")
                check("empty miss null", payload["event"] is None, str(payload))
                status, payload = fetch(empty_port, "/api/topology")
                check("empty topology zero", payload["total_pairs"] == 0, str(payload))
            finally:
                empty_server.shutdown()
                empty_server.server_close()

            print("== corrupt DB -> honest empty store (E2E-008) ==")
            # A present-but-corrupt / non-SQLite store file must behave EXACTLY
            # like a missing DB: no CLI traceback (exit 0, honest empty output,
            # stderr notice naming the path) and no dashboard HTTP 500.
            corrupt_path = tmp_path / "corrupt.db"
            corrupt_path.write_bytes(b"\x00\x01\xff not-sqlite-garbage " * 64)
            # (optional) PRAGMA quick_check on a real corrupt header raises
            # "file is not a database" — the probe the openers rely on.
            probe = sqlite3.connect(f"file:{corrupt_path}?mode=ro", uri=True)
            try:
                probe.execute("PRAGMA quick_check")
                check(
                    "corrupt probe quick_check raises",
                    False,
                    "quick_check unexpectedly succeeded on garbage bytes",
                )
            except sqlite3.DatabaseError as exc:
                check(
                    "corrupt probe quick_check raises file-is-not-a-database",
                    "file is not a database" in str(exc),
                    str(exc),
                )
            finally:
                probe.close()
            # CLI read commands exit 0 with honest empty output, no traceback,
            # and a stderr notice naming the path (every read command shares
            # open_read_only_store, so one command proves the shared opener).
            for cli_sub in ("status", "leases", "costs", "churn", "topology"):
                out_buf, err_buf = io.StringIO(), io.StringIO()
                with redirect_stdout(out_buf), redirect_stderr(err_buf):
                    cli_code = cli_main([cli_sub, "--db", str(corrupt_path)])
                cli_err = err_buf.getvalue()
                check(
                    f"corrupt CLI {cli_sub} exits 0",
                    cli_code == 0,
                    f"exit {cli_code}",
                )
                check(
                    f"corrupt CLI {cli_sub} stderr names path + corrupt",
                    str(corrupt_path) in cli_err and "not SQLite" in cli_err,
                    cli_err.strip(),
                )
                check(
                    f"corrupt CLI {cli_sub} no traceback",
                    "Traceback" not in cli_err and "sqlite3." not in cli_err,
                    cli_err.strip(),
                )
            # Dashboard /api/* endpoints return HTTP 200 empty-state JSON.
            corrupt_server = DashboardServer(
                ("127.0.0.1", 0), Handler, db_path=str(corrupt_path)
            )
            corrupt_port = int(corrupt_server.server_address[1])
            corrupt_thread = threading.Thread(
                target=corrupt_server.serve_forever, daemon=True
            )
            corrupt_thread.start()
            try:
                status, payload = fetch(corrupt_port, "/api/leases")
                check(
                    "corrupt leases 200 []",
                    status == 200 and payload == {"leases": []},
                    f"{status} {payload}",
                )
                status, payload = fetch(corrupt_port, "/api/status")
                check(
                    "corrupt stats 200 zero",
                    status == 200
                    and payload["stats"]["total"] == 0
                    and payload["providers"] == [],
                    f"{status} {payload}",
                )
                status, payload = fetch(corrupt_port, "/api/costs")
                check(
                    "corrupt costs 200 zero",
                    status == 200 and payload["total_usd"] == 0.0,
                    f"{status} {payload}",
                )
            finally:
                corrupt_server.shutdown()
                corrupt_server.server_close()

            print("== write refusal ==")
            refused_error = {"error": "the dashboard backend is read-only (GET only)"}
            # E2E-007: every non-GET method (POST/PUT/DELETE/PATCH/OPTIONS/
            # TRACE/HEAD) is refused with the same machine-readable read-only
            # JSON 405. HEAD is a GET without a response body, so it carries
            # no request data and its response omits the body (asserted empty
            # below) while still carrying the 405 status + JSON content-type.
            for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "HEAD"):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/leases",
                    method=method,
                    data=b"" if method == "HEAD" else b"{}",
                )
                try:
                    urllib.request.urlopen(req, timeout=10)
                    check(f"{method} refused", False, f"{method} unexpectedly succeeded")
                except urllib.error.HTTPError as exc:
                    body = exc.read().decode("utf-8")
                    check(f"{method} refused 405", exc.code == 405, str(exc.code))
                    content_type = exc.headers.get("Content-Type", "")
                    check(
                        f"{method} refused JSON content type",
                        content_type.startswith("application/json"),
                        content_type or "(no Content-Type header)",
                    )
                    if method == "HEAD":
                        check("HEAD omits response body", body == "", repr(body))
                    else:
                        try:
                            parsed_body = json.loads(body)
                        except json.JSONDecodeError:
                            parsed_body = None
                        check(
                            f"{method} refused documented error body",
                            parsed_body == refused_error,
                            body,
                        )

            print("== relay health probe (E2E-002) ==")
            # closed port -> unreachable (deterministic: loopback port 1)
            readout = relay_readout(port, "127.0.0.1:1")
            check("relay readout unreachable when port closed", readout == "unreachable", readout)
            # real relay -> healthy (probe hits the relay's local control
            # endpoint; the upstream is never contacted)
            relay_server, relay_thread, relay_port = start_real_relay()
            try:
                readout = relay_readout(port, f"127.0.0.1:{relay_port}")
                check("relay readout healthy against real relay", readout == "healthy", readout)
            finally:
                relay_server.should_exit = True
                relay_thread.join(15)
            # foreign HTTP server (hermes-webui stand-in) -> occupied, NEVER healthy
            foreign = ThreadingHTTPServer(("127.0.0.1", 0), _ForeignHttpHandler)
            foreign_thread = threading.Thread(target=foreign.serve_forever, daemon=True)
            foreign_thread.start()
            try:
                readout = relay_readout(port, f"127.0.0.1:{foreign.server_address[1]}")
                check(
                    "relay readout occupied by foreign HTTP server",
                    readout == "occupied by another service",
                    readout,
                )
                check(
                    "relay readout never healthy for foreign server", readout != "healthy", readout
                )
            finally:
                foreign.shutdown()
                foreign.server_close()
                foreign_thread.join(5)

            print("== startup occupant detection (E2E-002) ==")
            # the dashboard backend must fail with a CLEAR actionable error
            # when its listen address is taken, never a bare EADDRINUSE
            with socket.socket() as squatter:
                squatter.bind(("127.0.0.1", 0))
                squatter.listen(1)
                occupied_port = squatter.getsockname()[1]
                err_buf = io.StringIO()
                with redirect_stderr(err_buf):
                    exit_code = server_main(
                        ["--host", "127.0.0.1", "--port", str(occupied_port), "--db", str(db_path)]
                    )
                err_text = err_buf.getvalue()
                check("dashboard main exits 2 on occupied port", exit_code == 2, str(exit_code))
                check(
                    "dashboard error names the port and --port override",
                    "already in use" in err_text and "--port" in err_text,
                    err_text.strip(),
                )
                check(
                    "dashboard error names the vite proxy override",
                    "vite.config.ts" in err_text,
                    err_text.strip(),
                )
        finally:
            server.shutdown()
            server.server_close()

    if FAILURES:
        print(f"\nSMOKE TEST FAILED ({len(FAILURES)} failure(s)):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(
        "\nSMOKE TEST PASSED — dashboard backend verified against a seeded "
        "TelemetryStore, empty-store states, and a byte-identical read-only DB."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
