"""Relay-hosted lease controller — PRD §132 Phase 5, §133 Phase 6.

Wires the relay observer + the plugin's ``X-CachePilot-Targets`` header into
a pure :class:`~cachepilot_core.leases.LeaseManager`:

- before every forwarded request, :meth:`LeaseController.on_request_start`
  finds-or-creates the lease for the physical cache identity, reconciles the
  active background-target COUNT from the plugin header (PRD §46 — the relay
  sees counts, the plugin owns the target registry), runs PRD §148
  ``before_normal_request`` (generation bump + warm cancel), and stores the
  memory-only request snapshot for the lease (PRD §30) so a due warm can
  replay it;
- after the response, :meth:`LeaseController.on_request_end` runs PRD §148
  ``after_normal_request`` with the observer-classified outcome — a FAILED
  call never refreshes the cache (invariant 3) and drops the snapshot (a
  failed request is never cache-producing);
- a background asyncio task ticks the manager every ``scheduler_interval_s``
  (PRD §146). With ``dry_run`` (the default) the scheduler emits ``WOULD
  WARM NOW`` lines; with warming enabled the warm executes through the
  injected :class:`~cachepilot_core.adapters.WarmExecutor` (transport +
  adapter, PRD §147) — warm requests never re-enter the observation or
  forwarding path (no recursive lease tracking, no re-observation).

Everything is fail-open (AGENTS.md invariant 9): a lease-tracking error never
breaks forwarding, persistence failures only log a warning, and snapshots
are memory-only (they die with the relay — leases become non-warmable, PRD
§30).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from cachepilot_core.adapters import OpenAICompatibleAdapter, WarmExecutor
from cachepilot_core.fingerprint import cache_fingerprint, request_fingerprint
from cachepilot_core.leases import CacheLease, LeaseDecision, LeaseManager, LeaseSettings
from cachepilot_core.pricing import estimate_resume_costs
from cachepilot_core.route_affinity import (
    AffinityConfig,
    RouteAffinityPolicy,
    RouteAffinityRegistry,
)
from cachepilot_core.route_intel import RouteMissVerdict
from cachepilot_core.snapshots import RequestSnapshot, SnapshotStore
from cachepilot_core.storage import TelemetryStore
from cachepilot_core.telemetry import Outcome
from cachepilot_core.ttl import TTLResolver
from cachepilot_core.usage import TokenUsage

from cachepilot_relay.observation import (
    build_canonical_request,
    derive_auth_scope,
    parse_targets_count,
    request_route_identity,
)

logger = logging.getLogger("cachepilot_relay.lease_controller")

#: Decisions that change durable lease fields and therefore need the stored
#: snapshot refreshed (PRD §78 ``cachepilot leases``).
_PERSIST_ON = frozenset(
    {
        LeaseDecision.SCHEDULED,
        LeaseDecision.WARMED_CONFIRMED_HIT,
        LeaseDecision.WARMED_MISS_REBUILT,
        LeaseDecision.WARMED_UNVERIFIED,
        LeaseDecision.WARMED_FAILED,
    }
)


@dataclass(frozen=True)
class LeaseRequestContext:
    """Per-request lease tracking state (never stored on the proxy itself,
    so concurrent requests never share mutable state)."""

    lease_id: str
    targets_count: int


class LeaseController:
    """Lease manager + observer wiring + background scheduler task."""

    def __init__(
        self,
        manager: LeaseManager | None = None,
        *,
        settings: LeaseSettings | None = None,
        store: TelemetryStore | None = None,
        latency_p95_s: float = 4.0,
        enabled: bool = True,
        snapshot_store: SnapshotStore | None = None,
        warm_executor: WarmExecutor | None = None,
        affinity_config: AffinityConfig | None = None,
        affinity_extra_cost_usd: Decimal | float = Decimal("0.0"),
        affinity_registry: RouteAffinityRegistry | None = None,
        replay_headers: frozenset[str] | None = None,
    ) -> None:
        self.settings = settings or LeaseSettings()
        #: Memory-only request snapshots (PRD §30). A controller constructed
        #: without one never stores snapshots and its leases stay
        #: non-warmable (fail closed for warming, invariant 9).
        self.snapshot_store = snapshot_store
        #: PRD §31/§90: the adapter's allowlist of request headers a warm must
        #: resend. Mirrors ``RelayProxy``'s adapter default so a controller
        #: wired without an explicit adapter still authenticates its warms;
        #: the proxy passes ``adapter.replay_headers`` for any other dialect.
        self.replay_headers = (
            replay_headers
            if replay_headers is not None
            else OpenAICompatibleAdapter.replay_headers
        )
        self.store = store
        #: P08 (PRD §59): the TTL override chain — force_seconds >
        #: high-confidence learned TTL > adapter hint > default. The profile
        #: lookup is the telemetry store; without a store the resolver falls
        #: back to the hint/default tiers (never guesses unknown TTLs).
        self.ttl_resolver = TTLResolver(
            profile_lookup=self.store.profile_for if self.store is not None else None,
            force_seconds=self.settings.ttl_force_seconds,
            default_ttl_s=self.settings.default_ttl_s,
            minimum_samples=self.settings.ttl_minimum_samples,
        )
        self.manager = manager or LeaseManager(
            settings=self.settings,
            latency_p95_s=latency_p95_s,
            snapshot_store=snapshot_store,
            warm_executor=warm_executor,
            # P07: the configured pricing snapshot drives cost estimation
            # (PRD §65 priority 2/3). None → unknown pricing → the economic
            # gate skips with SKIP_UNKNOWN_PRICING (no savings claimed).
            pricing=self.settings.pricing,
            # P08: new leases resolve their TTL through the §59 hierarchy
            # instead of the bootstrap default.
            ttl_resolver=self.ttl_resolver,
        )
        self.enabled = enabled
        #: P09 (PRD §73-74): economic route affinity. The registry is
        #: memory-only and the policy gate is fail-open — an affinity error
        #: never blocks the normal request path (AGENTS.md invariant 9).
        self.affinity_config = affinity_config or AffinityConfig()
        self.affinity_extra_cost_usd = (
            affinity_extra_cost_usd
            if isinstance(affinity_extra_cost_usd, Decimal)
            else Decimal(str(affinity_extra_cost_usd))
        )
        self.affinity_registry = affinity_registry or RouteAffinityRegistry()
        self.affinity_policy = RouteAffinityPolicy(self.affinity_config)
        #: Synthetic target ids per lease, reconciling the plugin's per-session
        #: COUNT header with the manager's per-target-id API (PRD §149).
        self._target_ids: dict[str, set[str]] = {}
        self._scheduler_task: asyncio.Task[None] | None = None

    # -- request wiring (PRD §148) ------------------------------------------

    def active_affinity_route(self, lease_id: str) -> str | None:
        """PRD §73-74: the economically-pinned route for this lease's request.

        None unless affinity is enabled, the lease still exists and the
        registry holds an unexpired, generation-valid entry. Fail-open: a
        registry error logs and returns None — traffic is never blocked.
        """
        if not self.affinity_config.enabled:
            return None
        lease = self.manager.get(lease_id)
        if lease is None:
            return None
        try:
            return self.affinity_registry.active_route_for(
                lease_id, generation=lease.generation
            )
        except Exception:
            logger.warning("route affinity lookup failed (traffic unaffected)", exc_info=True)
            return None

    def on_request_start(
        self,
        *,
        body: bytes,
        path: str,
        upstream_url: str,
        request_headers: Mapping[str, str] | None,
        session_header: str | None,
        targets_header: str | None,
    ) -> LeaseRequestContext | None:
        """Before forwarding: find/create the lease and start a real request.

        Returns a per-request context (or None when the request cannot be
        correlated — no session header, or tracking failed → fail open).
        """
        if not self.enabled or not session_header:
            return None
        try:
            # P08 (PRD §82): the route key must be stable across requests to
            # the same upstream, so a request-time route identity (gateway +
            # endpoint — the response is not back yet) feeds the canonical
            # request instead of None. The observer folds any response-header
            # signals in later; the controller reconciles the lease's route
            # with the observed one in :meth:`on_request_end`.
            route_hash = request_route_identity(upstream_url).route_hash()
            canonical = build_canonical_request(
                body,
                path=path,
                upstream_url=upstream_url,
                route_hash=route_hash,
                auth_scope=derive_auth_scope(request_headers or {}),
            )
            lease = self.manager.find_or_create_lease(
                session_id=session_header,
                provider=canonical.provider,
                model=canonical.model,
                api_mode=canonical.api_mode.value,
                base_url=canonical.endpoint,
                auth_scope_hash=canonical.auth_scope,
                route_fingerprint=canonical.route,
                request_fingerprint=request_fingerprint(canonical),
                cache_fingerprint=cache_fingerprint(canonical),
                system_fingerprint=canonical.system_hash,
                tools_fingerprint=canonical.tools_hash,
                history_prefix_fingerprint=canonical.prompt_key,
            )
            self._prune_target_ids()
            self._prune_snapshots()
            targets_count = parse_targets_count(targets_header)
            self._sync_targets(lease, targets_count)
            self.manager.before_normal_request(lease.lease_id)
            # PRD §30: remember the last cache-producing request body IN
            # MEMORY ONLY (never persisted) so a due warm can replay it.
            self._store_snapshot(lease, body, upstream_url, request_headers)
            self._persist(lease)
            return LeaseRequestContext(lease_id=lease.lease_id, targets_count=targets_count)
        except Exception:
            logger.warning("lease request tracking failed (traffic unaffected)", exc_info=True)
            return None

    def on_request_end(
        self,
        ctx: LeaseRequestContext,
        outcome: Outcome,
        usage: TokenUsage | None = None,
        route_hash: str | None = None,
    ) -> None:
        """After the response: PRD §148 normal-request-reset with the outcome.

        A failed provider call never refreshes the cache (invariant 3) and
        never counts as cache-producing: its snapshot is dropped, so the
        lease stays non-warmable until the next successful request (PRD §30
        fail-closed semantics — no unsafe reconstruction from a failed body).

        When the observer normalized real usage for this request (P07), the
        lease's resume-cost estimates are refreshed from it (PRD §65) — this
        is the ONLY place cost data is written, never on scheduler ticks.

        P08: the observed route (response headers) is authoritative for the
        lease's route key (PRD §82), and the lease TTL is re-resolved
        through the §59 hierarchy so a freshly learned profile takes effect.
        """
        if not self.enabled:
            return
        try:
            self.manager.after_normal_request(ctx.lease_id, outcome)
            if usage is not None and usage.prompt_tokens > 0:
                self.manager.update_cost_estimates(ctx.lease_id, usage)
            lease = self.manager.get(ctx.lease_id)
            if lease is not None:
                if route_hash is not None and lease.route_fingerprint != route_hash:
                    # P08: reconcile the lease's route key with the observed
                    # route so the resolver finds the learned profile.
                    lease.route_fingerprint = route_hash
                self.manager.refresh_ttl(ctx.lease_id)
                self._persist(lease)
                if outcome is Outcome.FAILED and self.snapshot_store is not None:
                    self.snapshot_store.drop(lease.cache_fingerprint)
                # P09 (PRD §73-74): keep affinity reversible (cleared on
                # FAILED / generation advance / lease end), then set a fresh
                # pin when the just-recorded router-miss event says
                # instability AND the economic gate approves.
                self._reconcile_affinity(ctx.lease_id, lease, outcome)
        except Exception:
            logger.warning("lease request completion failed (traffic unaffected)", exc_info=True)

    # -- background scheduler task (PRD §132) --------------------------------

    async def start(self) -> None:
        """Start the background scheduler task (idempotent)."""
        if self._scheduler_task is not None or not self.enabled:
            return
        self._scheduler_task = asyncio.create_task(
            self._scheduler_loop(),
            name="cachepilot-lease-scheduler",
        )

    async def stop(self) -> None:
        """Cancel the scheduler task (idempotent; safe at shutdown)."""
        task = self._scheduler_task
        self._scheduler_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def tick(self) -> list[tuple[str, LeaseDecision]]:
        """One scheduler tick: evaluate every lease, persist state changes.

        Returns ``[(lease_id, decision), ...]`` so tests can observe the
        decisions directly.
        """
        results = await self.manager.tick()
        for lease_id, decision in results:
            if decision in _PERSIST_ON:
                # WARM_SCHEDULED / warm outcomes changed durable lease
                # fields (deadline, warm_count, warm_cost_usd) — keep the
                # stored snapshot fresh for `cachepilot leases` (PRD §78).
                lease = self.manager.get(lease_id)
                if lease is not None:
                    self._persist(lease)
        return results

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("lease scheduler tick failed (fail open)")
            await asyncio.sleep(self.settings.scheduler_interval_s)

    # -- internals -----------------------------------------------------------

    def _sync_targets(self, lease: CacheLease, count: int) -> None:
        """Reconcile the lease's target set with the header COUNT (§149).

        The plugin reports a per-session count, so synthetic ids
        (``bg-1..bg-N``) stand in for the real target identities; the manager
        still arms on the first target and disarms on the last exactly as the
        per-id algorithm specifies.
        """
        desired = {f"bg-{i}" for i in range(1, count + 1)}
        current = self._target_ids.get(lease.lease_id, set())
        for target_id in current - desired:
            self.manager.target_finished(lease.lease_id, target_id)
        for target_id in desired - current:
            self.manager.target_started(lease.lease_id, target_id)
        self._target_ids[lease.lease_id] = desired

    def _prune_target_ids(self) -> None:
        """Drop synthetic target sets for leases the manager no longer tracks
        (e.g. invalidated on a model switch)."""
        self._target_ids = {
            lease_id: targets
            for lease_id, targets in self._target_ids.items()
            if lease_id in self.manager.lease_ids
        }
        # P09 (PRD §74): affinity is lease-scoped — entries for leases that
        # no longer exist are dropped (lease end ⇒ reversible).
        self.affinity_registry.prune(self.manager.lease_ids)

    # -- route affinity (P09: PRD §72.4, §73-74) -----------------------------

    def _reconcile_affinity(
        self, lease_id: str, lease: CacheLease, outcome: Outcome
    ) -> None:
        """Keep affinity reversible, then set a fresh pin when warranted.

        Clearing (PRD §74 reversible): a FAILED call never refreshes the
        cache (pinning is pointless), and an entry created by an EARLIER
        request whose generation has since advanced was consumed by the
        current request — both clear it. Setting only happens when the
        just-recorded route event for this request verdicts
        ROUTE_INSTABILITY and the PRD §73 economic gate approves.
        Fail-open: an affinity error never breaks traffic.
        """
        if not self.affinity_config.enabled or self.store is None:
            return
        try:
            if outcome is Outcome.FAILED:
                self.affinity_registry.clear(lease_id)
                return
            entry_generation = self.affinity_registry.generation_for(lease_id)
            if entry_generation is not None and entry_generation < lease.generation:
                self.affinity_registry.clear(lease_id)
            self._maybe_set_affinity(lease)
        except Exception:
            logger.warning("route affinity reconcile failed (traffic unaffected)", exc_info=True)

    def _maybe_set_affinity(self, lease: CacheLease) -> None:
        """Set a lease-scoped, temporary affinity to the PREVIOUS route.

        Triggers only on a ROUTE_INSTABILITY event recorded DURING the
        current request (fresh evidence — ``since`` guards against stale
        events from earlier requests). The pin is temporary (expires at the
        lease's TTL window) and consumed by the next request's generation
        advance. Unknown pricing never claims savings (invariant 4).
        """
        if self.store is None:
            return
        session_hash = hashlib.sha256(lease.session_id.encode("utf-8")).hexdigest()
        event = self.store.last_route_event_for_session(
            session_hash, since=lease.last_real_request_at
        )
        if event is None or event.verdict is not RouteMissVerdict.ROUTE_INSTABILITY:
            return
        if not event.previous_route_hash or not event.new_route_hash:
            return
        savings = self._affinity_savings(lease)
        if savings is None:
            logger.debug(
                "route affinity skipped for lease %s: pricing unknown (never claim savings)",
                lease.lease_id,
            )
            return
        decision = self.affinity_policy.evaluate(
            cache_recompute_savings=savings,
            extra_route_cost=self.affinity_extra_cost_usd,
            resume_probability=self.settings.resume_probability,
        )
        if not decision.apply:
            logger.debug(
                "route affinity refused for lease %s: %s (savings=%s cost=%s)",
                lease.lease_id,
                decision.reason,
                decision.expected_savings,
                decision.extra_route_cost,
            )
            return
        expires_at = time.time() + max(lease.estimated_ttl_s, 0.0)
        self.affinity_registry.set(
            lease_id=lease.lease_id,
            route=event.previous_route_hash,
            expires_at=expires_at,
            generation=lease.generation,
        )
        logger.info(
            "route affinity set for lease %s: pin to %s (savings %s > cost %s)",
            lease.lease_id,
            event.previous_route_hash[:12],
            decision.expected_savings,
            decision.extra_route_cost,
        )

    def _affinity_savings(self, lease: CacheLease) -> Decimal | None:
        """Expected cache recompute savings (PRD §73): the avoidable loss of
        recomputing the prefix, from the lease's P07 cost estimates or the
        configured pricing table. None when pricing is unknown.
        """
        if (
            lease.estimated_cold_resume_cost_usd is not None
            and lease.estimated_cached_resume_cost_usd is not None
        ):
            return Decimal(str(lease.estimated_cold_resume_cost_usd)) - Decimal(
                str(lease.estimated_cached_resume_cost_usd)
            )
        if self.settings.pricing is not None and lease.prefix_tokens:
            cold, cached = estimate_resume_costs(
                lease.prefix_tokens, self.settings.pricing
            )
            return cold - cached
        return None

    def _store_snapshot(
        self,
        lease: CacheLease,
        body: bytes,
        upstream_url: str,
        request_headers: Mapping[str, str] | None,
    ) -> None:
        """Remember the request body for a due warm — IN MEMORY ONLY (PRD §30).

        The raw body (prompts, history, tool arguments) and the credential
        headers live only in this memory store: they are never persisted,
        never logged. A body that is not valid JSON is skipped (uncertain warm
        = skip, invariant 9). Fail-open: a snapshot error never breaks
        forwarding.

        Only the adapter's ``replay_headers`` allowlist is retained (PRD §31:
        the warm replays the actual request). Keeping just ``authorization``
        would silently restrict warming to dialects whose credential is a
        bearer token — an ``x-api-key`` provider's warm is rejected upstream,
        earns no cache telemetry and opens the §94 circuit breaker.
        """
        if self.snapshot_store is None:
            return
        try:
            parsed = json.loads(body)
        except (TypeError, ValueError):
            logger.debug(
                "request body not JSON — no warm snapshot (lease=%s)", lease.lease_id
            )
            return
        if not isinstance(parsed, dict):
            return
        self.snapshot_store.store(
            RequestSnapshot(
                cache_fingerprint=lease.cache_fingerprint,
                body=parsed,
                upstream_url=upstream_url,
                replay_headers=self._replay_headers(request_headers),
                stored_at=time.time(),
            )
        )

    def _replay_headers(self, request_headers: Mapping[str, str] | None) -> dict[str, str]:
        """The allowlisted subset of *request_headers* a warm may resend."""
        if not request_headers:
            return {}
        return {
            name.lower(): value
            for name, value in request_headers.items()
            if name.lower() in self.replay_headers
        }

    def _prune_snapshots(self) -> None:
        """Forget snapshots for cache identities the manager no longer tracks
        (e.g. invalidated on a model switch) — memory hygiene, never leaks
        content."""
        if self.snapshot_store is None:
            return
        tracked = self.manager.cache_fingerprints
        for fingerprint in list(self.snapshot_store.fingerprints):
            if fingerprint not in tracked:
                self.snapshot_store.drop(fingerprint)

    def _persist(self, lease: CacheLease) -> None:
        """Snapshot the lease to the telemetry store; never breaks traffic."""
        if self.store is None:
            return
        try:
            self.store.update_lease(lease)
        except Exception:
            logger.warning("lease persistence failed (traffic unaffected)", exc_info=True)
