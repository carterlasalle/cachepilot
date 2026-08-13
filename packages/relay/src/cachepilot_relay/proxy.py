"""Pass-through proxy — PRD §27 data plane, Phase 3 + Phase 4 observation.

Forwards every request verbatim (method, path, query, headers, body) to the
configured upstream and returns the upstream response unchanged: same status,
same body bytes, same relevant headers, same streaming behaviour — SSE and
chunked responses are streamed so chunks flush as they arrive.

Phase 3 guarantees (unchanged): zero cache modification — no ``X-*`` headers
are added, the body is never rewritten, and the only header surgery is
hop-by-hop stripping per RFC 7230 §6.1 (``Connection``, ``Keep-Alive``,
``Transfer-Encoding``, ``TE``, ``Trailer``, ``Upgrade``, ``Proxy-*``) plus
any header nominated by the incoming ``Connection`` field, dropping the
client's ``Host`` so the upstream sees its own address (standard reverse-
proxy rewrite), and — Phase 4 — removing the relay-internal correlation
headers (PRD §29) that must never reach the upstream.

Phase 4 observation (PRD §27 steps 2-6, §131) is read-only and fail-open:
the request body is buffered once (needed for fingerprinting) and the
response body is parsed without being modified. Any observation error logs
a warning and never breaks forwarding (AGENTS.md invariant 9).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

import httpx
from cachepilot_core.adapters import OpenAICompatibleAdapter
from cachepilot_core.snapshots import SnapshotStore
from cachepilot_core.telemetry import Outcome
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from cachepilot_relay.config import RelayConfig
from cachepilot_relay.lease_controller import LeaseController, LeaseRequestContext
from cachepilot_relay.observation import RequestObserver, strip_correlation_headers
from cachepilot_relay.warm_executor import HttpWarmExecutor

logger = logging.getLogger("cachepilot_relay.proxy")

#: RFC 7230 §6.1 hop-by-hop headers (+ the non-standard Proxy-Connection).
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def strip_hop_by_hop(headers: Mapping[str, str]) -> dict[str, str]:
    """Return ``headers`` without hop-by-hop fields (RFC 7230 §6.1).

    In addition to the fixed hop-by-hop set, any header nominated by the
    ``Connection`` field is removed.
    """
    connection_tokens = {
        token.strip().lower()
        for token in headers.get("connection", "").split(",")
        if token.strip()
    }
    banned = HOP_BY_HOP_HEADERS | connection_tokens
    return {key: value for key, value in headers.items() if key.lower() not in banned}


def build_upstream_url(base: str, path: str, query: str = "") -> str:
    """Join an incoming ``path``/``query`` onto an upstream ``base``.

    The base prefix is preserved (``https://host/api/v1`` + ``/chat`` →
    ``https://host/api/v1/chat``), unlike ``urllib.parse.urljoin`` which would
    discard the base path for absolute request paths.
    """
    prefix = base.rstrip("/")
    request_path = path.lstrip("/") if path else ""
    url = f"{prefix}/{request_path}" if request_path else prefix
    return f"{url}?{query}" if query else url


def should_stream(upstream: httpx.Response) -> bool:
    """True when the upstream body must be relayed as a live stream.

    SSE responses (``text/event-stream``) and chunked responses (no
    ``Content-Length``) are streamed so chunks flush as they arrive; bounded
    responses are buffered so ``Content-Length`` survives the hop.
    """
    content_type = upstream.headers.get("content-type", "")
    return content_type.startswith("text/event-stream") or "content-length" not in upstream.headers


class RelayProxy:
    """Forward one incoming request to the upstream, verbatim + observed."""

    def __init__(self, config: RelayConfig, client: httpx.AsyncClient) -> None:
        self.config = config
        self._client = client
        self.observer = (
            RequestObserver(
                db_path=config.telemetry_db_path,
                enabled=config.observation_enabled,
            )
            if config.observation_enabled
            else None
        )
        # Phase 5/6: the lease controller shares the observer's telemetry
        # store and turns the observed request + X-CachePilot-Targets header
        # into lease lifecycle events (PRD §132, §148). Phase 6 adds the
        # memory-only snapshot store (PRD §30) and the HTTP warm executor
        # (transport + OpenAI-compatible adapter, PRD §147) — warm requests
        # are sent DIRECTLY to the upstream and never re-enter this proxy's
        # forwarding/observation path. Fail-open: a controller problem never
        # breaks forwarding (AGENTS.md invariant 9).
        self.lease_controller = (
            LeaseController(
                settings=config.lease_settings,
                store=self.observer.store if self.observer is not None else None,
                enabled=config.observation_enabled,
                snapshot_store=SnapshotStore(),
                warm_executor=HttpWarmExecutor(self._client, OpenAICompatibleAdapter()),
            )
            if config.observation_enabled
            else None
        )

    def close(self) -> None:
        """Release the telemetry store (safe when observation is disabled)."""
        if self.observer is not None:
            self.observer.close()

    async def start_lease_scheduler(self) -> None:
        """Start the background lease scheduler task (PRD §132)."""
        if self.lease_controller is not None:
            await self.lease_controller.start()

    async def stop_lease_scheduler(self) -> None:
        """Cancel the scheduler task (safe when never started)."""
        if self.lease_controller is not None:
            await self.lease_controller.stop()

    async def forward(self, request: Request) -> Response:
        url = build_upstream_url(self.config.upstream, request.url.path, request.url.query)
        headers = strip_hop_by_hop(request.headers)
        # The client's Host names the relay, not the upstream; httpx rebuilds
        # Host from the upstream URL (standard reverse-proxy rewrite).
        headers.pop("host", None)
        # Correlation IDs are relay-internal (PRD §29): stripped before the
        # upstream ever sees them so they can never affect cache identity.
        strip_correlation_headers(headers)
        # The request body is buffered once: forwarded verbatim AND hashed
        # for the fingerprints (observation is read-only over the bytes).
        body = await request.body()
        lease_ctx = self._lease_start(request, url, body)
        upstream_request = self._client.build_request(
            request.method, url, headers=headers, content=body
        )
        try:
            upstream = await self._client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            logger.warning("upstream request failed for %s %s: %s", request.method, url, exc)
            outcome = self._observe_failure(request, url, body)
            # §148: a failed call must never be treated as a cache refresh.
            self._lease_end(lease_ctx, outcome or Outcome.FAILED)
            return Response(b"", status_code=502)
        response_headers = strip_hop_by_hop(upstream.headers)
        # aiter_raw() keeps the body byte-exact (no transparent decompression).
        if should_stream(upstream):
            response_headers.pop("content-length", None)
            outcome = self._observe_streaming(request, url, body, upstream)
            self._lease_end(lease_ctx, outcome or Outcome.SUCCESS_UNVERIFIED)
            return StreamingResponse(
                upstream.aiter_raw(),
                status_code=upstream.status_code,
                headers=response_headers,
            )
        response_body = b"".join([chunk async for chunk in upstream.aiter_raw()])
        outcome = self._observe_bounded(request, url, body, response_body, upstream)
        self._lease_end(lease_ctx, outcome or Outcome.SUCCESS_UNVERIFIED)
        return Response(response_body, status_code=upstream.status_code, headers=response_headers)

    # -- lease lifecycle (fail open: never breaks forwarding) ---------------

    def _lease_start(
        self,
        request: Request,
        url: str,
        body: bytes,
    ) -> LeaseRequestContext | None:
        if self.lease_controller is None:
            return None
        return self.lease_controller.on_request_start(
            body=body,
            path=request.url.path,
            upstream_url=url,
            request_headers=request.headers,
            session_header=request.headers.get("x-cachepilot-session"),
            targets_header=request.headers.get("x-cachepilot-targets"),
        )

    def _lease_end(self, ctx: LeaseRequestContext | None, outcome: Outcome) -> None:
        if ctx is None or self.lease_controller is None:
            return
        self.lease_controller.on_request_end(ctx, outcome)

    # -- observation (fail open: never breaks forwarding) -------------------

    def _observe_bounded(
        self,
        request: Request,
        url: str,
        request_body: bytes,
        response_body: bytes,
        upstream: httpx.Response,
    ) -> Outcome | None:
        if self.observer is None:
            return None
        try:
            # request_body is the buffered client body (fingerprints);
            # response_body is the upstream's buffered body (usage parse).
            return self.observer.observe_bounded(
                request_body=request_body,
                response_body=response_body,
                path=request.url.path,
                upstream_url=url,
                status_code=upstream.status_code,
                response_headers=upstream.headers,
                session_header=request.headers.get("x-cachepilot-session"),
                auth_headers=request.headers,
            )
        except Exception as exc:  # noqa: BLE001 — fail open (AGENTS.md invariant 9)
            logger.warning("telemetry observation failed (traffic unaffected): %s", exc)
            return None

    def _observe_streaming(
        self,
        request: Request,
        url: str,
        body: bytes,
        upstream: httpx.Response,
    ) -> Outcome | None:
        if self.observer is None:
            return None
        try:
            return self.observer.observe_streaming(
                body,
                path=request.url.path,
                upstream_url=url,
                status_code=upstream.status_code,
                response_headers=upstream.headers,
                session_header=request.headers.get("x-cachepilot-session"),
                auth_headers=request.headers,
            )
        except Exception as exc:  # noqa: BLE001 — fail open (AGENTS.md invariant 9)
            logger.warning("telemetry observation failed (traffic unaffected): %s", exc)
            return None

    def _observe_failure(
        self,
        request: Request,
        url: str,
        body: bytes,
    ) -> Outcome | None:
        if self.observer is None:
            return None
        try:
            return self.observer.observe_failure(
                body,
                path=request.url.path,
                upstream_url=url,
                session_header=request.headers.get("x-cachepilot-session"),
                auth_headers=request.headers,
            )
        except Exception as exc:  # noqa: BLE001 — fail open (AGENTS.md invariant 9)
            logger.warning("telemetry observation failed (traffic unaffected): %s", exc)
            return None
