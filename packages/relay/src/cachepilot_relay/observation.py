"""Physical request observation — PRD §27 data-plane steps 2-6, §29, §71, §131.

The relay observes WITHOUT changing pass-through behaviour (Phase 3 gate):
- correlation headers (``X-CachePilot-*``) are stripped before forwarding and
  never reach the upstream (PRD §29 primary mechanism);
- the physical request is turned into a :class:`CanonicalRequest` whose
  fingerprints are computed with ``cachepilot_core`` (AGENTS.md invariants 7-8);
- route identity (PRD §71) is extracted from the request/connection and the
  response headers (``x-provider`` / ``x-served-by`` / ... when present);
- bounded responses are parsed read-only for usage (PRD §109/§160) and the
  outcome classified per PRD §68-70; streaming responses are recorded as
  SUCCESS_UNVERIFIED without touching the stream (usage parsing for streams
  is deferred to a later phase — pass-through purity comes first);
- every write is wrapped fail-open: observation errors never break traffic
  (AGENTS.md invariant 9);
- P08 (PRD §55-56): every recorded outcome is fed to the TTL learner, which
  pairs consecutive same-fingerprint observations into idle ages and
  refines route-keyed TTL bounds (only CLEAN observations — stable cache
  identity and route — are applied);
- P09 (PRD §71-72, UC-5): route identity comes from the core
  :class:`~cachepilot_core.route_intel.RouteIdentity` model, and a miss on
  a repeated logical request whose physical route changed is classified
  ROUTE_INSTABILITY (recorded in ``route_events``) instead of short-TTL
  evidence — the learner's §56 clean-check keeps the instability miss out
  of TTL refinement.

Only hashes, timestamps, usage, prices, route identities and outcomes are
persisted (AGENTS.md invariant 10, PRD §30, §83).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from cachepilot_core.churn import (
    ChurnClassification,
    LayeredHashes,
    classify,
    classify_hashes,
    request_content_from_payload,
)
from cachepilot_core.fingerprint import cache_fingerprint, request_fingerprint
from cachepilot_core.identity import ApiMode, CanonicalRequest, hash_content
from cachepilot_core.route_intel import (
    RouteChangeEvent,
    RouteIdentity,
    RouterMissClassifier,
)
from cachepilot_core.storage import TelemetryStore
from cachepilot_core.telemetry import (
    ChurnEvent,
    Outcome,
    TelemetryEvent,
    classify_outcome,
    usage_has_cache_telemetry,
)
from cachepilot_core.ttl import TTLLearner, TTLObservation
from cachepilot_core.usage import TokenUsage, UsageNormalizer

logger = logging.getLogger("cachepilot_relay.observation")

#: Correlation headers injected by the Hermes plugin (PRD §29 primary
#: mechanism). The relay MUST strip these before forwarding upstream — they
#: must never affect provider cache identity. ``x-cachepilot-targets`` is the
#: Phase 5 addition: the plugin's active background-target COUNT for the
#: session (PRD §46, §132), which the lease controller feeds into the lease
#: manager.
CORRELATION_HEADERS: frozenset[str] = frozenset(
    {
        "x-cachepilot-session",
        "x-cachepilot-request",
        "x-cachepilot-turn",
        "x-cachepilot-targets",
    }
)

#: Well-known gateway hosts → provider labels. Derivation is physical (the
#: upstream URL the request is actually sent to); unknown hosts keep their
#: raw hostname as the label.
_KNOWN_GATEWAYS = {
    "api.openai.com": "openai",
    "api.anthropic.com": "anthropic",
    "openrouter.ai": "openrouter",
    "api.deepseek.com": "deepseek",
}

#: Response headers that reveal upstream provider identity when present
#: (OpenRouter-style ``x-served-by`` deployment host, ``x-provider`` label).
_HEADER_UPSTREAM_PROVIDER = ("x-provider", "x-cachepilot-provider")
_HEADER_REGION = ("x-region", "x-cachepilot-region")
_HEADER_DEPLOYMENT = ("x-served-by", "x-cachepilot-deployment")

#: P10 churn-classifier inputs (PRD §24-25): the observer keeps the LAST
#: request body per session IN MEMORY so a fingerprint transition can be
#: classified against the previous request (first divergent byte, estimated
#: prefix loss). Memory-only, dies on relay restart (PRD §30), bounded: bodies
#: larger than the cap are not cached, and only the most recent sessions are
#: kept — beyond that the classifier falls back to hash-only attribution
#: (cause + confidence, no offset/loss).
_BODY_CACHE_MAX_BYTES = 1_048_576  # 1 MiB per body
_BODY_CACHE_MAX_SESSIONS = 32


def strip_correlation_headers(headers: dict[str, str]) -> None:
    """Remove the relay-internal correlation headers (PRD §29), in place.

    Callers pass the already-hop-by-hop-stripped outgoing header dict. Only
    the three correlation names are removed; everything else is untouched so
    pass-through remains byte-identical for all other headers.
    """
    for name in list(headers):
        if name.lower() in CORRELATION_HEADERS:
            del headers[name]


def provider_from_upstream(url: str) -> str:
    """Provider label derived from the upstream URL host (physical, from config)."""
    host = url.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0].lower()
    return _KNOWN_GATEWAYS.get(host, host)


def derive_auth_scope(headers: Mapping[str, str]) -> str:
    """Auth/profile scope label derived WITHOUT persisting credentials.

    A stable hash of the ``Authorization`` header when present (only the hash
    is ever stored — AGENTS.md invariant 10), else ``relay-default``.
    """
    auth = headers.get("authorization")
    if auth:
        digest = hashlib.sha256(auth.encode("utf-8")).hexdigest()
        return f"auth-{digest[:16]}"
    return "relay-default"


def hash_correlation_value(value: str | None) -> str | None:
    """Hash a correlation-header value for storage (never the raw id)."""
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_targets_count(value: str | None) -> int:
    """Parse the ``X-CachePilot-Targets`` header (active background-target count).

    Fail-open: absent, malformed or negative values parse to 0 (no targets
    claimed — the lease simply stays unarmed for that request).
    """
    if not value:
        return 0
    try:
        count = int(value.strip())
    except (TypeError, ValueError):
        return 0
    return max(0, count)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _json_or_none(body: bytes) -> dict[str, Any] | None:
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def infer_api_mode(path: str, payload: dict[str, Any] | None) -> ApiMode:
    """API mode from the physical request path and body (PRD §22)."""
    lowered = path.lower()
    if "responses" in lowered:
        return ApiMode.RESPONSES
    if "chat" in lowered:
        return ApiMode.CHAT
    if lowered.endswith("/completions"):
        return ApiMode.COMPLETION
    if isinstance(payload, dict):
        if "messages" in payload:
            return ApiMode.CHAT
        if "prompt" in payload:
            return ApiMode.COMPLETION
    return ApiMode.CHAT


def _system_content(payload: dict[str, Any]) -> str | None:
    """System prompt text: top-level ``system`` field or system-role messages."""
    system = payload.get("system")
    if isinstance(system, str):
        return system
    if system is not None:
        return _canonical_json(system)
    texts: list[str] = []
    for message in payload.get("messages") or []:
        if not isinstance(message, Mapping) or message.get("role") != "system":
            continue
        content = message.get("content")
        texts.append(content if isinstance(content, str) else _canonical_json(content))
    return "\n".join(texts) if texts else None


def _non_system_messages(payload: dict[str, Any]) -> list[Any]:
    return [
        message
        for message in payload.get("messages") or []
        if not (isinstance(message, Mapping) and message.get("role") == "system")
    ]


def build_canonical_request(
    body: bytes,
    *,
    path: str,
    upstream_url: str,
    route_hash: str | None,
    auth_scope: str,
) -> CanonicalRequest:
    """Build the canonical request from the physical HTTP request (PRD §22-23).

    Only hashes are carried: prompt/system/tools content is hashed at
    construction time and never stored (AGENTS.md invariant 10).
    """
    payload = _json_or_none(body)
    model = (payload or {}).get("model") or "unknown"
    messages = _non_system_messages(payload) if payload else []
    tools = (payload or {}).get("tools") or (payload or {}).get("functions")
    return CanonicalRequest.from_content(
        provider=provider_from_upstream(upstream_url),
        model=model,
        api_mode=infer_api_mode(path, payload),
        endpoint=upstream_url,
        auth_scope=auth_scope,
        prompt_prefix=_canonical_json(messages) if messages else None,
        system=_system_content(payload) if payload else None,
        tools=tools if isinstance(tools, list) else None,
        route=route_hash,
        max_tokens=(payload or {}).get("max_tokens"),
        stream=bool((payload or {}).get("stream", False)),
    )


def extract_history_hash(body: bytes) -> str | None:
    """Hash of the conversation history (messages minus system) for storage."""
    payload = _json_or_none(body)
    if not payload:
        return None
    messages = _non_system_messages(payload)
    if not messages:
        return None
    return hash_content(_canonical_json(messages))


def extract_route_identity(
    upstream_url: str,
    response_headers: Mapping[str, str],
) -> RouteIdentity:
    """Extract observable route identity (PRD §71) from the physical request
    and response headers. Unobservable fields stay None."""
    gateway = provider_from_upstream(upstream_url)
    endpoint = upstream_url
    lower_headers = {key.lower(): value for key, value in response_headers.items()}
    upstream_provider = next(
        (lower_headers[key] for key in _HEADER_UPSTREAM_PROVIDER if key in lower_headers), None
    )
    region = next((lower_headers[key] for key in _HEADER_REGION if key in lower_headers), None)
    deployment = next(
        (lower_headers[key] for key in _HEADER_DEPLOYMENT if key in lower_headers), None
    )
    return RouteIdentity(
        gateway=gateway,
        upstream_provider=upstream_provider,
        endpoint=endpoint,
        region=region,
        deployment=deployment,
    )


def request_route_identity(upstream_url: str) -> RouteIdentity:
    """Route identity observable at REQUEST time (before any response).

    Only the request/connection facts are available then — gateway +
    endpoint. Response-header signals (``x-provider`` etc.) are folded in
    later by :func:`extract_route_identity`; when none are present the two
    hashes are identical, keeping the lease's route key in sync with the
    observed route (P08 TTL route keying, PRD §82).
    """
    return RouteIdentity(
        gateway=provider_from_upstream(upstream_url),
        endpoint=upstream_url,
    )


class RequestObserver:
    """Observes physical requests/responses and writes telemetry.

    Every public method is fail-open at the call site (the proxy wraps them);
    internally they never mutate the request or the response bytes.
    """

    def __init__(
        self,
        *,
        store: TelemetryStore | None = None,
        db_path: str | None = None,
        enabled: bool = True,
        route_intel_enabled: bool = True,
        churn_detection_enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.route_intel_enabled = route_intel_enabled
        #: P10 (PRD §25, §137, §164): churn detection master switch — when
        #: False the observer records ZERO churn events (``CACHEPILOT_CHURN_DETECTION_ENABLED``).
        self.churn_detection_enabled = churn_detection_enabled
        self._normalizer = UsageNormalizer()
        #: P09 (PRD UC-5): classifies a miss on a repeated logical request
        #: after a route change as ROUTE_INSTABILITY (never short-TTL
        #: evidence). Gated by ``CACHEPILOT_ROUTE_INTEL`` (default true).
        self._classifier = RouterMissClassifier()
        self.store = store
        if self.store is None and enabled:
            # Fail open at construction too: an unusable telemetry path must
            # never prevent the relay from starting (AGENTS.md invariant 9).
            try:
                self.store = TelemetryStore(db_path)
            except Exception as exc:  # noqa: BLE001 — fail open: storage must never break the relay
                logger.warning("telemetry store unavailable; observation disabled: %s", exc)
                self.store = None
        #: P08 TTL learner over the same store (PRD §55-56): every recorded
        #: outcome is fed in; pairing + refinement happen inside the learner.
        #: None when the store is unavailable — learning is best-effort and
        #: never blocks traffic (fail open, invariant 9).
        self._learner = TTLLearner(self.store) if self.store is not None else None
        #: P10 (PRD §24-25, §30): memory-only per-session cache of the last
        #: request body, for content-level churn classification. Dies on relay
        #: restart; bounded (see ``_BODY_CACHE_*``).
        self._last_body: dict[str, bytes] = {}

    def close(self) -> None:
        if self.store is not None:
            self.store.close()

    # -- observation entry points -------------------------------------------

    def observe_bounded(
        self,
        request_body: bytes,
        response_body: bytes,
        *,
        path: str,
        upstream_url: str,
        status_code: int,
        response_headers: Mapping[str, str],
        session_header: str | None = None,
        auth_headers: Mapping[str, str] | None = None,
    ) -> tuple[Outcome | None, TokenUsage]:
        """Record one buffered (non-streaming) request/response pair.

        The response body is parsed read-only — the same bytes are returned
        to the client untouched. Streaming responses never reach this path.

        Returns ``(outcome, usage)``: the classified
        :class:`~cachepilot_core.telemetry.Outcome` (None when observation is
        disabled) plus the normalized usage, so the proxy can feed both to
        the lease controller (PRD §148 normal-request-reset; P07 cost
        estimation, PRD §65).
        """
        if not self.enabled or self.store is None:
            return None, TokenUsage()
        route = extract_route_identity(upstream_url, response_headers)
        route_hash = route.route_hash()
        canonical = build_canonical_request(
            request_body,
            path=path,
            upstream_url=upstream_url,
            route_hash=route_hash,
            auth_scope=derive_auth_scope(auth_headers or {}),
        )
        payload = _json_or_none(response_body)
        usage = TokenUsage()
        telemetry_present = False
        if 200 <= status_code < 300:
            usage = self._normalizer.normalize(payload, provider=canonical.provider)
            telemetry_present = usage_has_cache_telemetry((payload or {}).get("usage", payload))
        outcome = classify_outcome(
            status_code=status_code,
            telemetry_present=telemetry_present,
            cache_read_tokens=usage.cache_read_tokens,
        )
        self._record(
            canonical,
            usage=usage,
            outcome=outcome,
            route_hash=route_hash,
            route=route,
            session_header=session_header,
            history_hash=extract_history_hash(request_body),
            body=request_body,
        )
        return outcome, usage

    def observe_streaming(
        self,
        body: bytes,
        *,
        path: str,
        upstream_url: str,
        status_code: int,
        response_headers: Mapping[str, str],
        session_header: str | None = None,
        auth_headers: Mapping[str, str] | None = None,
    ) -> Outcome | None:
        """Record a streaming request as SUCCESS_UNVERIFIED with zero usage.

        The response stream is NEVER consumed or modified — pass-through
        purity first. Streaming usage parsing (e.g. OpenAI ``stream_options``
        final-chunk usage) is deferred to a later phase.

        Returns the classified outcome (None when observation is disabled).
        """
        if not self.enabled or self.store is None:
            return None
        if not 200 <= status_code < 300:
            # A non-2xx "streaming" response is simply a failed request; the
            # (never-consumed) response body is irrelevant to the FAILED
            # classification, so only the request body is passed.
            outcome, _ = self.observe_bounded(
                body,
                b"",
                path=path,
                upstream_url=upstream_url,
                status_code=status_code,
                response_headers=response_headers,
                session_header=session_header,
                auth_headers=auth_headers,
            )
            return outcome
        route = extract_route_identity(upstream_url, response_headers)
        route_hash = route.route_hash()
        canonical = build_canonical_request(
            body,
            path=path,
            upstream_url=upstream_url,
            route_hash=route_hash,
            auth_scope=derive_auth_scope(auth_headers or {}),
        )
        self._record(
            canonical,
            usage=TokenUsage(),
            outcome=Outcome.SUCCESS_UNVERIFIED,
            route_hash=route_hash,
            route=route,
            session_header=session_header,
            history_hash=extract_history_hash(body),
            body=body,
        )
        return Outcome.SUCCESS_UNVERIFIED

    def observe_failure(
        self,
        body: bytes,
        *,
        path: str,
        upstream_url: str,
        session_header: str | None = None,
        auth_headers: Mapping[str, str] | None = None,
    ) -> Outcome | None:
        """Record a request that never produced a provider response (FAILED).

        Returns ``Outcome.FAILED`` (None when observation is disabled) so the
        lease controller can honour §148: a failed call never refreshes the
        cache.
        """
        if not self.enabled or self.store is None:
            return None
        route = extract_route_identity(upstream_url, {})
        route_hash = route.route_hash()
        canonical = build_canonical_request(
            body,
            path=path,
            upstream_url=upstream_url,
            route_hash=route_hash,
            auth_scope=derive_auth_scope(auth_headers or {}),
        )
        self._record(
            canonical,
            usage=TokenUsage(),
            outcome=Outcome.FAILED,
            route_hash=route_hash,
            route=route,
            session_header=session_header,
            history_hash=extract_history_hash(body),
            body=body,
        )
        return Outcome.FAILED

    # -- internals ----------------------------------------------------------

    def _record(
        self,
        canonical: CanonicalRequest,
        *,
        usage: TokenUsage,
        outcome: Outcome,
        route_hash: str | None,
        route: RouteIdentity | None,
        session_header: str | None,
        history_hash: str | None,
        body: bytes | None = None,
    ) -> None:
        if self.store is None:
            return
        session_hash = hash_correlation_value(session_header)
        timestamp = datetime.now(UTC)
        event = TelemetryEvent(
            request_fingerprint=request_fingerprint(canonical),
            cache_fingerprint=cache_fingerprint(canonical),
            provider=canonical.provider,
            model=canonical.model,
            route_hash=route_hash,
            usage=usage,
            outcome=outcome,
            request_kind="normal",
            session_hash=session_hash,
            timestamp=timestamp,
            system_hash=canonical.system_hash,
            tools_hash=canonical.tools_hash,
            history_hash=history_hash,
            # P11 (PRD §138): order-independent tool-set digest for the
            # tool-ordering stability view (derived measurement, never cache
            # identity — fingerprints exclude it).
            tools_set_hash=canonical.tools_set_hash,
        )
        previous = (
            self.store.last_event_for_session(session_hash) if session_hash is not None else None
        )
        # P10 (PRD §30): keep the last request body per session IN MEMORY so a
        # fingerprint transition can be classified against the previous
        # request (first divergent byte / estimated prefix loss). Returns the
        # previous body before replacing it.
        previous_body = self._remember_body(session_hash, body)
        self.store.record_request(event)
        if previous is not None and previous.cache_fingerprint != event.cache_fingerprint:
            self._record_churn(
                previous, event, previous_body=previous_body, current_body=body
            )
        # P09 (PRD §71-72, UC-5): classify a miss after a route change as
        # route instability (never short-TTL evidence) and record the
        # route-change event. The instability miss is still fed to the TTL
        # learner, whose §56 clean-check guarantees it never refines bounds.
        self._run_route_intel(previous, event, route)
        # P08: the learner pairs consecutive same-fingerprint observations
        # into idle ages and refines route TTL bounds (PRD §55-56). Best
        # effort — a learner error never breaks traffic (fail open).
        self._feed_ttl_learner(
            canonical,
            cache_fp=event.cache_fingerprint,
            outcome=outcome,
            route_hash=route_hash,
            timestamp=timestamp,
        )

    def _run_route_intel(
        self,
        previous: Any,
        event: TelemetryEvent,
        route: RouteIdentity | None,
    ) -> None:
        """P09 (PRD §72.1, UC-5): record a route-change event and classify it.

        Called for every recorded request whose route identity differs from
        the session's previous observation. The verdict is ROUTE_INSTABILITY
        when the same logical request (stable system/tools/history/model)
        proved warm on the old route and missed after the switch — so the
        miss is never misread as an extremely short TTL. TTL refinement is
        protected structurally by the learner's §56 clean-check (a route
        change never yields a CLEAN pair), so no extra gate is needed here.

        Gated by ``CACHEPILOT_ROUTE_INTEL``; fail-open (a route-intel error
        never breaks traffic — AGENTS.md invariant 9).
        """
        if not self.route_intel_enabled or self.store is None or previous is None:
            return
        if previous.route_hash == event.route_hash:
            return  # no route change — nothing to record
        try:
            identity_stable = (
                previous.system_hash == event.system_hash
                and previous.tools_hash == event.tools_hash
                and previous.history_hash == event.history_hash
                and previous.model == event.model
                and previous.provider == event.provider
            )
            verdict = self._classifier.classify(
                previous_outcome=previous.outcome,
                previous_route_hash=previous.route_hash,
                current_outcome=event.outcome,
                current_route_hash=event.route_hash,
                identity_stable=identity_stable,
            )
            self.store.record_route_event(
                RouteChangeEvent(
                    timestamp=event.timestamp,
                    session_hash=event.session_hash,
                    cache_fingerprint=event.cache_fingerprint,
                    request_fingerprint=event.request_fingerprint,
                    previous_route_hash=previous.route_hash,
                    new_route_hash=event.route_hash,
                    gateway=route.gateway if route is not None else None,
                    upstream_provider=(
                        route.upstream_provider if route is not None else None
                    ),
                    endpoint=route.endpoint if route is not None else None,
                    region=route.region if route is not None else None,
                    deployment=route.deployment if route is not None else None,
                    verdict=verdict,
                )
            )
            logger.debug(
                "route change observed: %s -> %s verdict=%s (session=%s)",
                previous.route_hash,
                event.route_hash,
                verdict.value,
                event.session_hash,
            )
        except Exception:
            logger.warning("route-intel analysis failed (traffic unaffected)", exc_info=True)

    def _feed_ttl_learner(
        self,
        canonical: CanonicalRequest,
        *,
        cache_fp: str,
        outcome: Outcome,
        route_hash: str | None,
        timestamp: datetime,
    ) -> None:
        """P08: feed one observed outcome into the TTL learner (PRD §55-56).

        The learner pairs consecutive same-fingerprint observations into
        idle ages, applies only CLEAN ones (stable cache identity + stable
        route, no intervening churn — §56), and upserts the route profile.
        Fail-open: any learner/store error is logged and never breaks
        traffic (AGENTS.md invariant 9).
        """
        if self._learner is None:
            return
        try:
            self._learner.learn(
                TTLObservation(
                    outcome=outcome,
                    cache_fingerprint=cache_fp,
                    route_hash=route_hash,
                    provider=canonical.provider,
                    model=canonical.model,
                    api_mode=canonical.api_mode.value,
                    endpoint=canonical.endpoint,
                    timestamp=timestamp,
                )
            )
        except Exception:
            logger.warning("ttl learning failed (traffic unaffected)", exc_info=True)

    def _remember_body(self, session_hash: str | None, body: bytes | None) -> bytes | None:
        """Memory-only per-session cache of the last request body (PRD §30).

        Returns the PREVIOUS body for the session (the classification input),
        then stores ``body`` as the new "last". Bounded by size and session
        count; disabled when churn detection is off (no churn recording ⇒ the
        cache would be dead weight). Dies on relay restart — never persisted.
        """
        if session_hash is None or body is None or not self.churn_detection_enabled:
            return None
        if len(body) > _BODY_CACHE_MAX_BYTES:
            return None  # oversized body: hash-only fallback classification
        previous = self._last_body.get(session_hash)
        if session_hash not in self._last_body and len(self._last_body) >= _BODY_CACHE_MAX_SESSIONS:
            # FIFO eviction of the oldest session (dicts preserve insertion order).
            self._last_body.pop(next(iter(self._last_body)))
        self._last_body[session_hash] = body
        return previous

    def _classify_churn(
        self,
        previous: Any,
        event: TelemetryEvent,
        *,
        previous_body: bytes | None,
        current_body: bytes | None,
    ) -> ChurnClassification:
        """P10 (PRD §24-25): classify one fingerprint transition.

        Content path (both request bodies available — the usual case): full
        classification with first-divergent-byte + estimated prefix loss.
        Hash-only fallback (previous body unavailable, e.g. right after a
        relay restart): booleans + cause + confidence only — the loss and the
        hint stay None, never fabricated. Fail-open: any classifier error
        degrades to an empty classification (booleans/cause still recorded by
        the caller — traffic unaffected, AGENTS.md invariant 9).
        """
        if previous_body is not None and current_body is not None:
            previous_payload = _json_or_none(previous_body)
            current_payload = _json_or_none(current_body)
            if previous_payload is not None and current_payload is not None:
                try:
                    return classify(
                        request_content_from_payload(
                            previous_payload,
                            route_hash=previous.route_hash,
                            model=previous.model,
                        ),
                        request_content_from_payload(
                            current_payload,
                            route_hash=event.route_hash,
                            model=event.model,
                        ),
                    )
                except Exception:
                    logger.warning("churn classification failed (traffic unaffected)", exc_info=True)
        try:
            return classify_hashes(
                LayeredHashes(
                    system_hash=previous.system_hash,
                    tools_hash=previous.tools_hash,
                    history_hash=previous.history_hash,
                    route_hash=previous.route_hash,
                    model=previous.model,
                ),
                LayeredHashes(
                    system_hash=event.system_hash,
                    tools_hash=event.tools_hash,
                    history_hash=event.history_hash,
                    route_hash=event.route_hash,
                    model=event.model,
                ),
            )
        except Exception:
            logger.warning("hash-only churn classification failed (traffic unaffected)", exc_info=True)
            return ChurnClassification()

    def _record_churn(
        self,
        previous: Any,
        event: TelemetryEvent,
        *,
        previous_body: bytes | None,
        current_body: bytes | None,
    ) -> None:
        # P10 (PRD §164): independent master switch — disabled ⇒ ZERO churn
        # events recorded (request telemetry is unaffected).
        if self.store is None or not self.churn_detection_enabled:
            return
        # The boolean flags below are the pre-P10 computation, kept stable:
        # the P08 TTL learner and P09 router-miss analysis rely on these exact
        # hash-equality semantics (PRD §56 clean-check uses history_hash).
        system_changed = previous.system_hash != event.system_hash
        tools_changed = previous.tools_hash != event.tools_hash
        history_changed = previous.history_hash != event.history_hash
        route_changed = previous.route_hash != event.route_hash
        model_changed = previous.model != event.model
        # The cache fingerprint changed but none of the directly-tracked
        # layers did → the remaining identity fields (prompt key / endpoint /
        # auth scope / api mode / provider) must be what moved.
        cache_key_changed = not (
            system_changed or tools_changed or history_changed or route_changed or model_changed
        )
        # P10 (PRD §25, §137): enrich with the classifier diagnosis when cheap
        # (only on fingerprint transitions, never per request).
        classification = self._classify_churn(
            previous, event, previous_body=previous_body, current_body=current_body
        )
        first_divergent = classification.first_divergent_byte
        self.store.record_churn(
            ChurnEvent(
                timestamp=event.timestamp,
                session_hash=event.session_hash,
                previous_cache_fingerprint=previous.cache_fingerprint,
                new_cache_fingerprint=event.cache_fingerprint,
                provider=event.provider,
                model=event.model,
                route_hash=event.route_hash,
                system_changed=system_changed,
                tools_changed=tools_changed,
                history_changed=history_changed,
                route_changed=route_changed,
                cache_key_changed=cache_key_changed,
                model_changed=model_changed,
                likely_cause=classification.likely_cause,
                confidence=classification.confidence,
                estimated_prefix_loss_tokens=classification.estimated_prefix_loss_tokens,
                first_divergent_offset=first_divergent.offset if first_divergent is not None else None,
                first_divergent_layer=first_divergent.layer if first_divergent is not None else None,
            )
        )
