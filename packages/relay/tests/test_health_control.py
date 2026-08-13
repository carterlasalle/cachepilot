"""E2E-002: relay control endpoint + startup occupant detection.

The relay's pass-through stays byte-identical for every upstream path, but
GET on one reserved local path (``RELAY_HEALTH_PATH``) is answered by the
relay itself so the CLI/dashboard relay probe can tell the relay from ANY
other process on the port. This file pins that contract and the
occupant-detection failure mode (a bare ``OSError: Address already in use``
is not actionable).
"""

from __future__ import annotations

import asyncio
import socket

import pytest
from cachepilot_relay.config import RELAY_HEALTH_PATH, RelayConfig
from cachepilot_relay.server import (
    RELAY_HEALTH_BODY,
    ListenAddressInUseError,
    RelayServer,
    main,
)
from helpers import DifferentialHarness
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse


def build_upstream_app() -> Starlette:
    """Tiny echo upstream that records every path it receives."""

    seen: list[str] = []

    async def echo(request: Request) -> JSONResponse:
        seen.append(request.url.path)
        return JSONResponse({"method": request.method, "path": request.url.path})

    app = Starlette()
    app.add_route("/{path:path}", echo, methods=["GET", "POST"])
    app.state.seen = seen
    return app


async def _scenario_control_endpoint_answers_locally() -> None:
    upstream = build_upstream_app()
    async with DifferentialHarness(upstream) as harness:
        assert harness.relay is not None
        response = await harness.send(harness.relay.base_url, "GET", RELAY_HEALTH_PATH)
        assert response.status_code == 200
        assert response.json() == RELAY_HEALTH_BODY
        assert response.headers["content-type"].startswith("application/json")
        # the control endpoint is answered LOCALLY: the upstream never saw it
        assert upstream.state.seen == []


def test_control_endpoint_answers_distinctively_and_is_not_forwarded():
    asyncio.run(_scenario_control_endpoint_answers_locally())


async def _scenario_pass_through_unaffected_by_control_route() -> None:
    upstream = build_upstream_app()
    async with DifferentialHarness(upstream) as harness:
        direct, relayed = await harness.compare("GET", "/v1/echo?q=1")
        assert relayed.status_code == direct.status_code == 200
        assert relayed.content == direct.content
        # the relayed echo saw the path arrive at the upstream through the relay
        assert relayed.json()["path"] == "/v1/echo"
        # the control interception is NARROW: only GET on the control path is
        # answered locally; other methods still pass through (PRD §27 holds
        # for everything but the probe method on the one reserved path)
        assert harness.relay is not None
        post = await harness.send(harness.relay.base_url, "POST", RELAY_HEALTH_PATH)
        assert post.status_code == 200
        assert post.json()["path"] == RELAY_HEALTH_PATH
        # GET on the control path never reaches the upstream (answered locally)
        await harness.send(harness.relay.base_url, "GET", RELAY_HEALTH_PATH)
        # compare() hit the upstream twice (direct + via relay); the POST was
        # forwarded; the control GET was not
        assert upstream.state.seen == ["/v1/echo", "/v1/echo", RELAY_HEALTH_PATH]


def test_pass_through_unaffected_by_control_route():
    asyncio.run(_scenario_pass_through_unaffected_by_control_route())


def test_start_refuses_occupied_listen_address():
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        port = squatter.getsockname()[1]
        config = RelayConfig(
            upstream="http://127.0.0.1:1",
            listen=f"127.0.0.1:{port}",
            observation_enabled=False,
        )
        server = RelayServer(config)
        with pytest.raises(ListenAddressInUseError, match="already in use"):
            asyncio.run(server.start())


def test_main_reports_occupied_listen_with_clear_actionable_error(capsys):
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        port = squatter.getsockname()[1]
        exit_code = main(["--listen", f"127.0.0.1:{port}", "--upstream", "http://127.0.0.1:1"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "already in use" in err
    assert "--listen" in err
    assert "CACHEPILOT_RELAY_LISTEN" in err


def test_control_endpoint_body_is_the_probe_marker():
    # The CLI/dashboard probes key on exactly this marker; pin it here so a
    # body change cannot silently break the 'healthy' readout elsewhere.
    assert RELAY_HEALTH_BODY == {"service": "cachepilot-relay", "status": "ok"}
