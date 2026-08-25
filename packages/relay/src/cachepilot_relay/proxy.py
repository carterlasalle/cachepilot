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

import json
import logging
from collections.abc import Mapping

import httpx
from cachepilot_core.adapters import CacheProviderAdapter, OpenAICompatibleAdapter
from cachepilot_core.route_affinity import AffinityConfig
from cachepilot_core.snapshots import SnapshotStore
from cachepilot_core.telemetry import Outcome
from cachepilot_core.usage import TokenUsage
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from cachepilot_relay.config import RelayConfig
from cachepilot_relay.lease_controller import LeaseController, LeaseRequestContext
from cachepilot_relay.observation import (
    RequestObserver,
    extract_route_identity,
    strip_correlation_headers,
)
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

    def __init__(
        self,
        config: RelayConfig,
        client: httpx.AsyncClient,
        *,
        adapter: CacheProviderAdapter | None = None,
        lease_controller: LeaseController | None = None,
    ) -> None:
        self.config = config
        self._client = client
        #: The provider adapter (PRD §34) powers warm-building, route
        #: affinity application (``can_pin_route`` / ``apply_route_affinity``)
        #: and outcome classification. Injectable for tests; the generic
        #: OpenAI-compatible adapter reports no pinning capability, so route
        #: affinity never activates without a capable adapter.
        self._adapter = adapter or OpenAICompatibleAdapter()
        self.observer = (
            RequestObserver(
                db_path=config.telemetry_db_path,
                enabled=config.observation_enabled,
                route_intel_enabled=config.route_intel_enabled,
                churn_detection_enabled=config.churn_detection_enabled,
            )
            if config.observation_enabled
            else None
        )
        # Phase 5/6: the lease controller shares the observer's telemetry
        # store and turns the observed request + X-CachePilot-Targets header
        # into lease lifecycle events (PRD §132, §148). Phase 6 adds the
        # memory-only snapshot store (PRD §30) and the HTTP warm executor
        # (transport + adapter, PRD §147) — warm requests are sent DIRECTLY
        # to the upstream and never re-enter this proxy's
        # forwarding/observation path. Phase 9 adds the economic route
        # affinity wiring (PRD §73-74). Fail-open: a controller problem never
        # breaks forwarding (AGENTS.md invariant 9).
        self.lease_controller = lease_controller
        if self.lease_controller is None and config.observation_enabled:
            self.lease_controller = LeaseController(
                settings=config.lease_settings,
                store=self.observer.store if self.observer is not None else None,
                enabled=config.observation_enabled,
                snapshot_store=SnapshotStore(),
                # PRD §31: the warm resends exactly the headers this dialect
                # declares replay-safe.
                replay_headers=self._adapter.replay_headers,
                warm_executor=HttpWarmExecutor(
                    self._client,
                    self._adapter,
                    # P07: the warm's cost is estimated from the configured
                    # pricing snapshot (PRD §65 priority 2) so warm costs are
                    # visible (invariant 4) and the economic gate sees them.
                    pricing=config.lease_settings.pricing,
                ),
                affinity_config=AffinityConfig(
                    enabled=config.route_affinity_enabled,
                    safety_margin=config.route_affinity_safety_margin_usd,
                ),
                affinity_extra_cost_usd=config.route_affinity_extra_cost_usd,
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
        # P09 (PRD §72.4, §73-74): when the lease holds an active, economic
        # route affinity and the adapter can pin, the forwarded body carries
        # the pin. Fail-open: any affinity error leaves the body verbatim.
        forward_body = self._apply_affinity(request, body, lease_ctx)
        upstream_request = self._client.build_request(
            request.method, url, headers=headers, content=forward_body
        )
        try:
            upstream = await self._client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            logger.warning("upstream request failed for %s %s: %s", request.method, url, exc)
            outcome = self._observe_failure(request, url, forward_body)
            # §148: a failed call must never be treated as a cache refresh.
            self._lease_end(lease_ctx, outcome or Outcome.FAILED)
            # PRD §93 relay failure isolation: no response was ever received,
            # so there is no upstream status/body/headers to mirror. A timeout
            # is a distinct condition from an unreachable upstream and clients
            # retry the two differently, so it gets its own status. The body
            # names the condition only — never the exception text, which
            # carries the upstream URL (and therefore any query-string
            # credential, AGENTS.md rule 10).
            timed_out = isinstance(exc, httpx.TimeoutException)
            return Response(
                json.dumps(
                    {
                        "error": {
                            "type": "upstream_timeout" if timed_out else "upstream_unreachable",
                            "message": (
                                "the upstream provider did not respond in time"
                                if timed_out
                                else "the upstream provider could not be reached"
                            ),
                        }
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
                status_code=504 if timed_out else 502,
                media_type="application/json",
            )
        response_headers = strip_hop_by_hop(upstream.headers)
        # aiter_raw() keeps the body byte-exact (no transparent decompression).
        if should_stream(upstream):
            response_headers.pop("content-length", None)
            outcome = self._observe_streaming(request, url, forward_body, upstream)
            self._lease_end(
                lease_ctx,
                outcome or Outcome.SUCCESS_UNVERIFIED,
                route_hash=extract_route_identity(url, upstream.headers).route_hash(),
            )
            return StreamingResponse(
                upstream.aiter_raw(),
                status_code=upstream.status_code,
                headers=response_headers,
            )
        response_body = b"".join([chunk async for chunk in upstream.aiter_raw()])
        outcome, usage = self._observe_bounded(request, url, forward_body, response_body, upstream)
        self._lease_end(
            lease_ctx,
            outcome or Outcome.SUCCESS_UNVERIFIED,
            usage=usage,
            route_hash=extract_route_identity(url, upstream.headers).route_hash(),
        )
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

    def _lease_end(
        self,
        ctx: LeaseRequestContext | None,
        outcome: Outcome,
        usage: TokenUsage | None = None,
        route_hash: str | None = None,
    ) -> None:
        if ctx is None or self.lease_controller is None:
            return
        self.lease_controller.on_request_end(ctx, outcome, usage=usage, route_hash=route_hash)

    # -- route affinity (P09: PRD §72.4, §73-74; fail open) ------------------

    def _apply_affinity(self, request: Request, body: bytes, lease_ctx) -> bytes:
        """Apply an active economic route affinity to the forwarded body.

        Only when affinity is enabled, the adapter reports ``can_pin_route()``
        and the lease's registry holds an unexpired, generation-valid pin.
        The adapter's :meth:`apply_route_affinity` returns the modified
        :class:`~cachepilot_core.adapters.PhysicalRequest` (or the same object
        when the user's global routing must not be overwritten — PRD §74).
        Fail-open: any error returns the body verbatim (AGENTS.md invariant 9).
        """
        if (
            lease_ctx is None
            or self.lease_controller is None
            or not self.config.route_affinity_enabled
        ):
            return body
        if not self._adapter.can_pin_route():
            return body
        try:
            route = self.lease_controller.active_affinity_route(lease_ctx.lease_id)
        except Exception:
            logger.warning("route affinity lookup failed (traffic unaffected)", exc_info=True)
            return body
        if route is None:
            return body
        try:
            parsed = json.loads(body) if body else None
            if not isinstance(parsed, dict):
                return body
            modified = self._adapter.apply_route_affinity(parsed, route)
            if modified is parsed:
                return body
            return json.dumps(modified, separators=(",", ":")).encode("utf-8")
        except Exception:
            logger.warning("route affinity application failed (traffic unaffected)", exc_info=True)
            return body

    # -- observation (fail open: never breaks forwarding) -------------------

    def _observe_bounded(
        self,
        request: Request,
        url: str,
        request_body: bytes,
        response_body: bytes,
        upstream: httpx.Response,
    ) -> tuple[Outcome | None, TokenUsage]:
        if self.observer is None:
            return None, TokenUsage()
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
            return None, TokenUsage()

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
