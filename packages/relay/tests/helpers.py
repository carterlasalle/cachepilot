"""Shared offline harness: in-process uvicorn servers on ephemeral ports.

All relay tests run the upstream fake provider and the relay in-process on
``port=0`` (OS-assigned ephemeral ports — never the hardcoded 8787) so
nothing depends on free ports or real network access.
"""

from __future__ import annotations

import asyncio
from typing import Self

import httpx
import uvicorn
from cachepilot_relay.config import RelayConfig
from cachepilot_relay.server import RelayServer


class RunningServer:
    """A uvicorn server bound to an ephemeral port, started in-process."""

    def __init__(self, server: uvicorn.Server, task: asyncio.Task, base_url: str) -> None:
        self.server = server
        self._task = task
        self.base_url = base_url

    @property
    def port(self) -> int:
        return self.server.servers[0].sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.server.should_exit = True
        await asyncio.wait_for(self._task, 15)


async def start_server(app, *, host: str = "127.0.0.1", port: int = 0) -> RunningServer:
    """Start ``app`` on an ephemeral port; returns once it accepts traffic."""
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    )
    task = asyncio.create_task(server.serve())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15.0
    while not server.started:
        if loop.time() > deadline:
            raise TimeoutError("test server did not start within 15s")
        await asyncio.sleep(0.005)
    bound_port = server.servers[0].sockets[0].getsockname()[1]
    return RunningServer(server, task, f"http://{host}:{bound_port}")


class DifferentialHarness:
    """Fake upstream + relay on ephemeral ports with a shared httpx client.

    ``compare`` sends the IDENTICAL request directly to the upstream and via
    the relay, then asserts the responses match: same status code, same body
    bytes, same headers where relevant (RFC 7230 hop-by-hop excluded — the
    relay must strip those per its spec).
    """

    def __init__(self, upstream_app, *, relay_kwargs: dict | None = None) -> None:
        self._upstream_app = upstream_app
        # P03 golden tests run the relay in pure pass-through mode: observation
        # defaults OFF so no telemetry store is opened/written during the
        # byte-identity comparisons. Phase 4 observation tests opt in with an
        # explicit tmp telemetry_db_path.
        self._relay_kwargs = {"observation_enabled": False, **(relay_kwargs or {})}
        self.upstream: RunningServer | None = None
        self.relay: RunningServer | RelayServer | None = None
        self.client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        self.upstream = await start_server(self._upstream_app)
        # The relay runs through the real production class (RelayServer), so
        # the tests exercise its exact uvicorn wiring — including the
        # transparent date/server header policy. ``relay_kwargs`` lets
        # Phase 4 observation tests point the telemetry store at a tmp path.
        self.relay = RelayServer(
            RelayConfig(upstream=self.upstream.base_url, listen="127.0.0.1:0", **self._relay_kwargs)
        )
        await self.relay.start()
        self.client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *exc_info) -> None:
        assert self.client is not None
        await self.client.aclose()
        assert self.relay is not None and self.upstream is not None
        await self.relay.stop()
        await self.upstream.stop()

    async def send(self, base_url: str, method: str, path: str, **kwargs) -> httpx.Response:
        assert self.client is not None
        return await asyncio.wait_for(self.client.request(method, base_url + path, **kwargs), 30.0)

    async def compare(self, method: str, path: str, **kwargs) -> tuple[httpx.Response, httpx.Response]:
        """Send one identical request both ways; assert relayed == direct."""
        assert self.upstream is not None and self.relay is not None
        direct = await self.send(self.upstream.base_url, method, path, **kwargs)
        relayed = await self.send(self.relay.base_url, method, path, **kwargs)
        assert relayed.status_code == direct.status_code
        assert relayed.content == direct.content
        assert relevant_headers(relayed) == relevant_headers(direct)
        return direct, relayed


def relevant_headers(response: httpx.Response) -> dict[str, str]:
    """Response headers minus hop-by-hop fields (RFC 7230 §6.1).

    ``date`` is excluded too: every hop's server regenerates it, so the
    relayed value is inherently the relay's timestamp, not the upstream's.
    """
    from cachepilot_relay.proxy import HOP_BY_HOP_HEADERS

    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "date"
    }
