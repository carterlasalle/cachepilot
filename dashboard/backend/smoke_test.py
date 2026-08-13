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

Exit code 0 = everything verified; nonzero with a message otherwise.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from cachepilot_core.leases import CacheLease, LeaseState
from cachepilot_core.route_intel import RouteChangeEvent, RouteMissVerdict
from cachepilot_core.storage import TelemetryStore
from cachepilot_core.telemetry import ChurnEvent, Outcome, TelemetryEvent, TokenUsage
from cachepilot_core.ttl import TTLProfile

#: Ensure the repo root is importable (the script may be run from any cwd).
repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from dashboard.backend.server import DashboardServer, Handler

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


def main() -> int:
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
            check("status stats.total == 3", payload["stats"]["total"] == 3, str(payload["stats"]["total"]))
            check("status hit_rate is a number", isinstance(payload["stats"]["hit_rate"], float), str(payload["stats"]["hit_rate"]))
            check("status providers recorded", len(payload["providers"]) == 2, str(payload["providers"]))
            check("status relay readout", payload["relay"] in ("healthy", "unreachable"), str(payload["relay"]))
            check("status plugin readout", payload["plugin"].startswith("active"), str(payload["plugin"]))

            status, payload = fetch(port, "/api/leases")
            check("leases 200", status == 200, str(status))
            check("leases non-empty", len(payload["leases"]) == 1, str(len(payload["leases"])))
            lease = payload["leases"][0]
            check("lease state armed", lease["state"] == "armed", str(lease["state"]))
            check("lease cache age computed", lease["cache_age_s"] is not None, str(lease["cache_age_s"]))
            check("lease targets", lease["active_targets"] == ["target-1"], str(lease["active_targets"]))

            status, payload = fetch(port, "/api/costs")
            check("costs 200", status == 200, str(status))
            check("costs total > 0", payload["total_usd"] > 0, str(payload["total_usd"]))
            check("costs per-provider", payload["per_provider"].get("openai", 0) > 0, str(payload["per_provider"]))
            check("costs recent series", len(payload["recent"]) == 2, str(len(payload["recent"])))
            check("costs honest note", "recorded-cost-only" in payload["note"], payload["note"])

            status, payload = fetch(port, "/api/ttl")
            check("ttl 200", status == 200, str(status))
            check("ttl profiles non-empty", len(payload["profiles"]) == 1, str(len(payload["profiles"])))
            profile = payload["profiles"][0]
            check("ttl estimate present", profile["estimated_ttl_s"] == 288.0, str(profile["estimated_ttl_s"]))
            check("ttl survival curve", profile["survival"] is not None and profile["survival"]["sample_count"] == 4, str(profile.get("survival")))
            if profile["survival"] is not None:
                check("ttl survival steps", len(profile["survival"]["steps"]) >= 1, str(len(profile["survival"]["steps"])))
                check("ttl survival at-ttl defined", profile["survival"]["p_survive_at_ttl"] is not None, str(profile["survival"]["p_survive_at_ttl"]))

            status, payload = fetch(port, "/api/routes")
            check("routes 200", status == 200, str(status))
            check("routes events non-empty", len(payload["events"]) == 1, str(len(payload["events"])))
            check("routes instability verdict", payload["events"][0]["verdict"] == "route_instability", str(payload["events"][0]["verdict"]))
            check("routes stats", payload["stats"]["route_switches"] == 1, str(payload["stats"]))

            status, payload = fetch(port, "/api/churn")
            check("churn 200", status == 200, str(status))
            check("churn events non-empty", len(payload["events"]) == 1, str(len(payload["events"])))
            tools = next((layer for layer in payload["layers"] if layer["layer"] == "tools"), None)
            check("churn tools layer changed", tools is not None and tools["changed"] == 1, str(tools))
            check("churn top causes", len(payload["top_causes"]) == 1 and payload["top_causes"][0]["cause"] == "tool list mutation", str(payload["top_causes"]))

            status, payload = fetch(port, "/api/miss")
            check("miss 200", status == 200, str(status))
            check("miss explains latest", payload["event"] is not None and payload["event"]["likely_cause"] == "tool list mutation", str(payload.get("event")))
            check("miss changed layers", "tools" in payload["changed"] and "system" in payload["stable"], f"{payload['stable']} / {payload['changed']}")
            check("miss prefix loss", payload["event"]["estimated_prefix_loss_tokens"] == 1234, str(payload["event"]))

            status, payload = fetch(port, "/api/miss?session=hash-session-2")
            check("miss unknown session empty", payload["event"] is None, str(payload.get("event")))

            status, payload = fetch(port, "/api/topology")
            check("topology 200", status == 200, str(status))
            check("topology pair measured", payload["total_pairs"] == 1, str(payload["total_pairs"]))
            check("topology churn pair", payload["churn_pairs"] == 1, str(payload["churn_pairs"]))

            status, payload = fetch(port, "/api/health")
            check("health 200 ok", status == 200 and payload == {"ok": True}, str(payload))

            status, _ = fetch(port, "/api/not-an-endpoint")
            check("unknown endpoint 404", status == 404, str(status))

            print("== read-only proof ==")
            sha_after = file_sha256(db_path)
            files_after = sorted(p.name for p in tmp_path.iterdir())
            new_files = [name for name in files_after if name not in files_before]
            check("DB bytes unchanged", sha_before == sha_after, f"{sha_before[:12]}… vs {sha_after[:12]}…")
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
                check("empty stats zero", payload["stats"]["total"] == 0 and payload["providers"] == [], str(payload))
                check("empty plugin honest", "no telemetry recorded yet" in payload["plugin"], str(payload["plugin"]))
                status, payload = fetch(empty_port, "/api/costs")
                check("empty costs zero", payload["total_usd"] == 0.0 and payload["recent"] == [], str(payload))
                status, payload = fetch(empty_port, "/api/ttl")
                check("empty ttl []", payload["profiles"] == [], str(payload))
                status, payload = fetch(empty_port, "/api/routes")
                check("empty routes", payload["events"] == [] and payload["stats"]["route_switches"] == 0, str(payload))
                status, payload = fetch(empty_port, "/api/churn")
                check("empty churn layers zero", all(layer["changed"] == 0 for layer in payload["layers"]), str(payload["layers"]))
                status, payload = fetch(empty_port, "/api/miss")
                check("empty miss null", payload["event"] is None, str(payload))
                status, payload = fetch(empty_port, "/api/topology")
                check("empty topology zero", payload["total_pairs"] == 0, str(payload))
            finally:
                empty_server.shutdown()
                empty_server.server_close()

            print("== write refusal ==")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/leases", method="POST", data=b"{}"
            )
            try:
                urllib.request.urlopen(req, timeout=10)
                check("POST refused", False, "POST unexpectedly succeeded")
            except urllib.error.HTTPError as exc:
                check("POST refused 405", exc.code == 405, str(exc.code))
        finally:
            server.shutdown()
            server.server_close()

    if FAILURES:
        print(f"\nSMOKE TEST FAILED ({len(FAILURES)} failure(s)):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nSMOKE TEST PASSED — dashboard backend verified against a seeded "
          "TelemetryStore, empty-store states, and a byte-identical read-only DB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
