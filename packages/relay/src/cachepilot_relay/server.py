"""cachepilotd — CachePilot local provider relay (PRD §26, Phase 3).

A small local HTTP server that forwards every request verbatim to a configured
upstream provider and returns the response unchanged (100% pass-through, 0
cache modification, PRD §130). Binds ``127.0.0.1:8787`` by default and refuses
wildcard binds (``0.0.0.0`` / ``::``) unless explicitly allowed (PRD §26).
Graceful shutdown on SIGINT/SIGTERM.

CLI::

    cachepilotd --upstream https://api.openai.com/v1 [--listen 127.0.0.1:8787]

The upstream may also come from ``CACHEPILOT_UPSTREAM``; an explicit
``--upstream`` flag always wins.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Sequence
from contextlib import asynccontextmanager

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response

from cachepilot_relay.config import (
    DEFAULT_LISTEN,
    ENV_LISTEN,
    ENV_UPSTREAM,
    RelayConfig,
    parse_listen,
)
from cachepilot_relay.proxy import RelayProxy

logger = logging.getLogger("cachepilot_relay")

#: Every standard method is forwarded (PRD §27: forward EVERY request).
_ALL_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT", "TRACE")


def create_app(config: RelayConfig) -> Starlette:
    """Build the relay ASGI app; the upstream client lives for the app lifetime."""

    @asynccontextmanager
    async def lifespan(app: Starlette):
        client = httpx.AsyncClient(timeout=None, follow_redirects=False)
        app.state.proxy = RelayProxy(config, client)
        try:
            yield
        finally:
            await client.aclose()

    app = Starlette(lifespan=lifespan)

    async def relay(request: Request) -> Response:
        started = time.monotonic()
        response = await app.state.proxy.forward(request)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        logger.info(
            "relay method=%s path=%s status=%d duration_ms=%.1f",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    app.add_route("/{path:path}", relay, methods=list(_ALL_METHODS))
    return app


class RelayServer:
    """uvicorn-backed relay server, controllable from code and from tests.

    Tests start it on ``port=0`` and read :attr:`port` / :attr:`base_url` so
    no hardcoded port is ever bound; the default-address policy (PRD §26) is
    tested separately in ``test_bind.py``.
    """

    def __init__(self, config: RelayConfig, *, app: Starlette | None = None) -> None:
        self.config = config
        host, port = parse_listen(config.listen)
        uvicorn_config = uvicorn.Config(
            app if app is not None else create_app(config),
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
            # The relay is transparent: it does not stamp its own date/server
            # headers, so the upstream's values pass through unchanged
            # (uvicorn's serve() still handles SIGINT/SIGTERM gracefully).
            date_header=False,
            server_header=False,
        )
        self._server = uvicorn.Server(uvicorn_config)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Bind and start accepting; raises TimeoutError if startup stalls."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._server.serve())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15.0
        while not self._server.started:
            if loop.time() > deadline:
                raise TimeoutError("cachepilotd relay did not start within 15s")
            await asyncio.sleep(0.005)

    @property
    def port(self) -> int:
        """The actually-bound port (useful with ``listen`` port 0)."""
        return self._server.servers[0].sockets[0].getsockname()[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def request_stop(self) -> None:
        """Ask the serve loop to exit; the running request completes first."""
        self._server.should_exit = True

    async def wait(self) -> None:
        """Wait for the serve loop (and graceful shutdown) to finish."""
        assert self._task is not None, "relay server was never started"
        await self._task

    async def stop(self) -> None:
        self.request_stop()
        await self.wait()


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point (``cachepilotd``). Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="cachepilotd",
        description=(
            "CachePilot local provider relay — 100% pass-through, 0 cache "
            "modification (PRD Phase 3)."
        ),
    )
    parser.add_argument(
        "--listen",
        default=None,
        metavar="HOST:PORT",
        help=f"listen address (default {DEFAULT_LISTEN}; env {ENV_LISTEN})",
    )
    parser.add_argument(
        "--upstream",
        default=None,
        metavar="URL",
        help=f"upstream provider base URL (required; env {ENV_UPSTREAM})",
    )
    parser.add_argument(
        "--allow-external-bind",
        action="store_true",
        default=None,
        help="allow binding a wildcard address (0.0.0.0/::); refused by default",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="minimum log level on stderr (default INFO)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # The relay's own logs are the signal; httpx's per-request lines are noise.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        config = RelayConfig.from_env(
            listen=args.listen,
            upstream=args.upstream,
            allow_external_bind=args.allow_external_bind,
        )
    except ValueError as exc:
        print(f"cachepilotd: error: {exc}", file=sys.stderr)
        return 2

    try:
        asyncio.run(_serve(config))
    except KeyboardInterrupt:
        pass
    return 0


async def _serve(config: RelayConfig) -> None:
    server = RelayServer(config)
    await server.start()
    logger.info("cachepilotd relay listening on %s, upstream %s", config.listen, config.upstream)
    # uvicorn's serve() handles SIGINT/SIGTERM natively: both set should_exit,
    # the serve loop finishes the in-flight request and shuts down gracefully.
    await server.wait()
    logger.info("cachepilotd relay stopped")
