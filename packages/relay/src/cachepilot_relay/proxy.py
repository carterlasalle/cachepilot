"""Pass-through proxy — PRD §27 data plane, Phase 3 (100% pass-through).

Forwards every request verbatim (method, path, query, headers, body) to the
configured upstream and returns the upstream response unchanged: same status,
same body bytes, same relevant headers, same streaming behaviour — SSE and
chunked responses are streamed so chunks flush as they arrive.

Zero cache modification (AGENTS.md rules 4 and 10): no ``X-*`` headers are
added, the body is never rewritten, and nothing is persisted. The only header
surgery is hop-by-hop stripping per RFC 7230 §6.1 (``Connection``,
``Keep-Alive``, ``Transfer-Encoding``, ``TE``, ``Trailer``, ``Upgrade``,
``Proxy-*``) plus any header nominated by the incoming ``Connection`` field,
and dropping the client's ``Host`` so the upstream sees its own address
(standard reverse-proxy rewrite, required for correct forwarding).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from cachepilot_relay.config import RelayConfig

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
    """Forward one incoming request to the upstream, verbatim."""

    def __init__(self, config: RelayConfig, client: httpx.AsyncClient) -> None:
        self.config = config
        self._client = client

    async def forward(self, request: Request) -> Response:
        url = build_upstream_url(self.config.upstream, request.url.path, request.url.query)
        headers = strip_hop_by_hop(request.headers)
        # The client's Host names the relay, not the upstream; httpx rebuilds
        # Host from the upstream URL (standard reverse-proxy rewrite).
        headers.pop("host", None)
        upstream_request = self._client.build_request(
            request.method, url, headers=headers, content=request.stream()
        )
        try:
            upstream = await self._client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            logger.warning("upstream request failed for %s %s: %s", request.method, url, exc)
            return Response(b"", status_code=502)
        response_headers = strip_hop_by_hop(upstream.headers)
        # aiter_raw() keeps the body byte-exact (no transparent decompression).
        if should_stream(upstream):
            response_headers.pop("content-length", None)
            return StreamingResponse(
                upstream.aiter_raw(),
                status_code=upstream.status_code,
                headers=response_headers,
            )
        body = b"".join([chunk async for chunk in upstream.aiter_raw()])
        return Response(body, status_code=upstream.status_code, headers=response_headers)
