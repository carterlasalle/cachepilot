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
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass

from cachepilot_core.adapters import WarmExecutor
from cachepilot_core.fingerprint import cache_fingerprint, request_fingerprint
from cachepilot_core.leases import CacheLease, LeaseDecision, LeaseManager, LeaseSettings
from cachepilot_core.snapshots import RequestSnapshot, SnapshotStore
from cachepilot_core.storage import TelemetryStore
from cachepilot_core.telemetry import Outcome

from cachepilot_relay.observation import (
    build_canonical_request,
    derive_auth_scope,
    parse_targets_count,
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
    ) -> None:
        self.settings = settings or LeaseSettings()
        #: Memory-only request snapshots (PRD §30). A controller constructed
        #: without one never stores snapshots and its leases stay
        #: non-warmable (fail closed for warming, invariant 9).
        self.snapshot_store = snapshot_store
        self.manager = manager or LeaseManager(
            settings=self.settings,
            latency_p95_s=latency_p95_s,
            snapshot_store=snapshot_store,
            warm_executor=warm_executor,
        )
        self.store = store
        self.enabled = enabled
        #: Synthetic target ids per lease, reconciling the plugin's per-session
        #: COUNT header with the manager's per-target-id API (PRD §149).
        self._target_ids: dict[str, set[str]] = {}
        self._scheduler_task: asyncio.Task[None] | None = None

    # -- request wiring (PRD §148) ------------------------------------------

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
            canonical = build_canonical_request(
                body,
                path=path,
                upstream_url=upstream_url,
                route_hash=None,
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

    def on_request_end(self, ctx: LeaseRequestContext, outcome: Outcome) -> None:
        """After the response: PRD §148 normal-request-reset with the outcome.

        A failed provider call never refreshes the cache (invariant 3) and
        never counts as cache-producing: its snapshot is dropped, so the
        lease stays non-warmable until the next successful request (PRD §30
        fail-closed semantics — no unsafe reconstruction from a failed body).
        """
        if not self.enabled:
            return
        try:
            self.manager.after_normal_request(ctx.lease_id, outcome)
            lease = self.manager.get(ctx.lease_id)
            if lease is not None:
                self._persist(lease)
                if outcome is Outcome.FAILED and self.snapshot_store is not None:
                    self.snapshot_store.drop(lease.cache_fingerprint)
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

    def _store_snapshot(
        self,
        lease: CacheLease,
        body: bytes,
        upstream_url: str,
        request_headers: Mapping[str, str] | None,
    ) -> None:
        """Remember the request body for a due warm — IN MEMORY ONLY (PRD §30).

        The raw body (prompts, history, tool arguments) and the
        Authorization header live only in this memory store: they are never
        persisted, never logged. A body that is not valid JSON is skipped
        (uncertain warm = skip, invariant 9). Fail-open: a snapshot error
        never breaks forwarding.
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
                authorization=(request_headers or {}).get("authorization"),
                stored_at=time.time(),
            )
        )

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
