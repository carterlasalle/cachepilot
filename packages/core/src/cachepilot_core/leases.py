"""Cache lease manager — PRD §20-21, §46-54, §132, §146-149 (Phase 5 + 6).

A cache lease represents a physical prompt-cache opportunity that is
valuable while one or more background operations may eventually need the
same conversational prefix again (PRD §20). This module is the pure,
offline-testable core: the state machine (§49-50), the generation counter
and real-request-wins rule (§51), per-cache-identity locking (§52), the
safe warm deadline (§53) with deterministic per-fingerprint jitter (§54)
and the background-target arm/disarm algorithm (§149).

Phase 5 was DRY-RUN ONLY (PRD §132): the scheduler computed safe deadlines
and emitted ``WOULD WARM IN Ns`` / ``WOULD WARM NOW`` log lines without
sending any network request. Phase 6 (PRD §133) replaces the due branch
with REAL bounded warm replay through an injectable executor (transport +
adapter): ``last_cache_touch_at`` advances ONLY when the warm produced a
verified cache touch (invariant 3: HTTP 200 ≠ cache hit) — CONFIRMED_HIT
refreshes, MISS_REBUILT refreshes only when write telemetry proves this
request rebuilt the cache, SUCCESS_UNVERIFIED and FAILED never refresh.

Warm safety (PRD §32, invariant 9): an uncertain warm is skipped
(``SKIPPED_UNSUPPORTED``) — the manager never sends a request it cannot
bound, and the warm circuit breaker (§94) stops warming a lease after 2
consecutive misses until a normal request produces new cache evidence.

P07 (PRD §134) turns the watchdog into an optimizer: :meth:`LeaseManager.economic_gate`
consults the :class:`~cachepilot_core.economics.EconomicController` (PRD §60-65,
§146) instead of always passing. Cost data is refreshed on the normal-request
path only (:meth:`LeaseManager.update_cost_estimates`, PRD §64/§65 — never on
scheduler ticks), every due evaluation records an explainable
:class:`~cachepilot_core.economics.WarmDecision` (PRD §145), and an exhausted
warm budget stops warming with ``ECONOMIC_STOP`` (PRD §61-62, §103).

Lease identity is physical, never the session alone (AGENTS.md invariant
7): the lease carries provider, model, api_mode, base_url, auth scope and
all five fingerprints. One session may own several leases concurrently
(PRD §21) — e.g. one per provider — and a model switch invalidates the old
model's lease instead of refreshing it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field

from cachepilot_core.adapters import WarmExecutor, WarmResult
from cachepilot_core.economics import EconomicConfig, EconomicController, WarmDecision
from cachepilot_core.pricing import CostResolver, PricingTable, estimate_cost, estimate_resume_costs
from cachepilot_core.snapshots import SnapshotStore
from cachepilot_core.telemetry import Outcome
from cachepilot_core.ttl import TTLResolver
from cachepilot_core.usage import TokenUsage

logger = logging.getLogger("cachepilot_core.leases")

#: Environment variables for :class:`LeaseSettings` (``CACHEPILOT_LEASE_*``).
ENV_WARM_FRACTION = "CACHEPILOT_LEASE_WARM_FRACTION"
ENV_MINIMUM_MARGIN_S = "CACHEPILOT_LEASE_MINIMUM_MARGIN_S"
ENV_LATENCY_MULTIPLIER = "CACHEPILOT_LEASE_LATENCY_MULTIPLIER"
ENV_JITTER_FRACTION = "CACHEPILOT_LEASE_JITTER_FRACTION"
ENV_DEFAULT_TTL_S = "CACHEPILOT_LEASE_DEFAULT_TTL_S"
ENV_SCHEDULER_INTERVAL_S = "CACHEPILOT_LEASE_SCHEDULER_INTERVAL_S"
ENV_DRY_RUN = "CACHEPILOT_LEASE_DRY_RUN"

#: Environment variables for TTL learning (P08, PRD §59, §84
#: ``cache.ttl_learning``).
ENV_TTL_FORCE_SECONDS = "CACHEPILOT_TTL_FORCE_SECONDS"
ENV_TTL_MINIMUM_SAMPLES = "CACHEPILOT_TTL_MINIMUM_SAMPLES"

#: Environment variables for the economic controller (PRD §60-61, §63, §84
#: ``cache.economics`` block).
ENV_ECONOMICS_ENABLED = "CACHEPILOT_ECONOMICS_ENABLED"
ENV_ECONOMICS_BUDGET_RATIO = "CACHEPILOT_ECONOMICS_BUDGET_RATIO"
ENV_ECONOMICS_MINIMUM_SAVINGS = "CACHEPILOT_ECONOMICS_MINIMUM_EXPECTED_SAVINGS_USD"
ENV_ECONOMICS_RESUME_PROBABILITY = "CACHEPILOT_ECONOMICS_RESUME_PROBABILITY"
ENV_ECONOMICS_DETACHED_RESUME_PROBABILITY = "CACHEPILOT_ECONOMICS_DETACHED_RESUME_PROBABILITY"

#: Environment variables for the configured pricing snapshot (PRD §65
#: priority 2/3, §66 fallback snapshot — never authority). All four must be
#: set for a pricing table to engage (all-or-nothing, fail closed).
ENV_PRICING_INPUT_PER_MTK = "CACHEPILOT_PRICING_INPUT_PER_MTK"
ENV_PRICING_OUTPUT_PER_MTK = "CACHEPILOT_PRICING_OUTPUT_PER_MTK"
ENV_PRICING_CACHE_READ_PER_MTK = "CACHEPILOT_PRICING_CACHE_READ_PER_MTK"
ENV_PRICING_CACHE_WRITE_PER_MTK = "CACHEPILOT_PRICING_CACHE_WRITE_PER_MTK"

#: PRD §63 P0 heuristic defaults: a background target with
#: ``notify_on_complete`` resumes with high probability; an explicitly
#: detached target resumes with low probability.
DEFAULT_RESUME_PROBABILITY = 0.95
DEFAULT_DETACHED_RESUME_PROBABILITY = 0.20


class LeaseState(str, Enum):
    """Cache lease state machine — PRD §49."""

    INACTIVE = "inactive"
    ARMED = "armed"
    WARM_SCHEDULED = "warm_scheduled"
    WARMING = "warming"
    CONFIRMED_HIT = "confirmed_hit"
    SUCCESS_UNVERIFIED = "success_unverified"
    MISS_REBUILT = "miss_rebuilt"
    ECONOMIC_STOP = "economic_stop"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    FAILED = "failed"


@dataclass
class CacheLease:
    """One cache lease — EXACTLY the fields of PRD §20.

    Runtime-only flags (``real_request_active``, ``warm_request_active``,
    pending-warm bookkeeping) deliberately live OUTSIDE this dataclass, in
    the manager's per-lease runtime record: they are process-local and must
    never be persisted (only the durable lease fields are stored, PRD §81-83).

    ``session_id`` is the raw Hermes session identifier in memory; storage
    hashes it before persisting (AGENTS.md invariant 10).
    """

    lease_id: str
    session_id: str

    provider: str
    model: str
    api_mode: str
    base_url: str

    auth_scope_hash: str

    route_fingerprint: str | None

    request_fingerprint: str
    cache_fingerprint: str

    system_fingerprint: str
    tools_fingerprint: str
    history_prefix_fingerprint: str

    last_real_request_at: float
    last_cache_touch_at: float | None
    last_confirmed_hit_at: float | None

    estimated_ttl_s: float
    ttl_confidence: float

    active_targets: set[str] = field(default_factory=set)

    generation: int = 0

    warm_count: int = 0
    warm_cost_usd: float = 0.0

    estimated_cold_resume_cost_usd: float | None = None
    estimated_cached_resume_cost_usd: float | None = None

    #: In-memory economics bookkeeping (P07) — NEVER persisted (the stored
    #: snapshot only carries the two estimated-resume-cost columns; the
    #: prefix size, the next-warm predictor and the last decision are
    #: process-local and re-derived on the next normal request).
    prefix_tokens: int | None = None
    next_warm_cost_usd: float | None = None
    last_warm_decision: WarmDecision | None = None

    state: LeaseState = LeaseState.INACTIVE

    def arm(self) -> None:
        """Transition to ARMED unless invalidated (§50, §21)."""
        if self.state is not LeaseState.INVALIDATED:
            self.state = LeaseState.ARMED

    def disarm(self) -> None:
        """All targets complete → no watchdog relevance → INACTIVE (§47, §50)."""
        self.state = LeaseState.INACTIVE


class LeaseSettings(BaseModel):
    """Scheduling settings — PRD §53-54 defaults, §84 ``cache.scheduling``.

    Read from ``CACHEPILOT_LEASE_*`` environment variables via
    :meth:`from_env` (malformed values fall back to defaults so a bad
    variable can never break the relay — fail open for traffic).
    """

    warm_fraction: float = Field(default=0.80, gt=0.0, le=1.0)
    minimum_margin_s: float = Field(default=10.0, ge=0.0)
    latency_multiplier: float = Field(default=2.0, ge=0.0)
    jitter_fraction: float = Field(default=0.03, ge=0.0, le=1.0)
    default_ttl_s: float = Field(default=300.0, gt=0.0)
    scheduler_interval_s: float = Field(default=1.0, gt=0.0)
    #: PRD §59 tier 1: an explicit configured TTL overrides everything.
    #: ``None`` = not set → the learned/hint/default chain applies.
    ttl_force_seconds: float | None = Field(default=None, gt=0.0)
    #: PRD §84 ``ttl_learning.minimum_samples``: the learned TTL tier (PRD
    #: §59 tier 2) also needs at least this many observations, alongside
    #: confidence ≥ 0.7.
    ttl_minimum_samples: int = Field(default=3, ge=1)
    #: Phase 5: the warm executor logs ``WOULD WARM`` and never sends a
    #: network request (PRD §132).
    dry_run: bool = True
    #: Economic controller tunables (PRD §60-61, §84 ``cache.economics``).
    #: Read from ``CACHEPILOT_ECONOMICS_*``.
    economics: EconomicConfig = Field(default_factory=EconomicConfig)
    #: Configured pricing snapshot (PRD §65 priority 2/3, §66 fallback — never
    #: authority). ``None`` means unknown pricing: the economic gate skips with
    #: SKIP_UNKNOWN_PRICING and no savings are ever claimed (invariant 4).
    pricing: PricingTable | None = None
    #: PRD §63 P0 heuristic: background target with ``notify_on_complete``
    #: resumes with high probability (default 0.95).
    resume_probability: float = Field(default=DEFAULT_RESUME_PROBABILITY, ge=0.0, le=1.0)
    #: PRD §63 P0 heuristic: an explicitly detached target resumes with low
    #: probability (default 0.20).
    detached_resume_probability: float = Field(
        default=DEFAULT_DETACHED_RESUME_PROBABILITY, ge=0.0, le=1.0
    )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LeaseSettings:
        env = os.environ if env is None else env
        return cls(
            warm_fraction=_env_float(env.get(ENV_WARM_FRACTION), 0.80),
            minimum_margin_s=_env_float(env.get(ENV_MINIMUM_MARGIN_S), 10.0),
            latency_multiplier=_env_float(env.get(ENV_LATENCY_MULTIPLIER), 2.0),
            jitter_fraction=_env_float(env.get(ENV_JITTER_FRACTION), 0.03),
            default_ttl_s=_env_float(env.get(ENV_DEFAULT_TTL_S), 300.0),
            scheduler_interval_s=_env_float(env.get(ENV_SCHEDULER_INTERVAL_S), 1.0),
            ttl_force_seconds=_env_float_or_none(env.get(ENV_TTL_FORCE_SECONDS)),
            ttl_minimum_samples=_env_int(env.get(ENV_TTL_MINIMUM_SAMPLES), 3),
            dry_run=_env_flag(env.get(ENV_DRY_RUN, "true"), True),
            economics=EconomicConfig(
                enabled=_env_flag(env.get(ENV_ECONOMICS_ENABLED, "true"), True),
                budget_ratio=_env_bounded(
                    env.get(ENV_ECONOMICS_BUDGET_RATIO),
                    Decimal("0.70"),
                    minimum=Decimal(0),
                    minimum_exclusive=True,
                    maximum=Decimal(1),
                ),
                minimum_expected_savings=_env_bounded(
                    env.get(ENV_ECONOMICS_MINIMUM_SAVINGS),
                    Decimal("0.0"),
                    minimum=Decimal(0),
                ),
            ),
            pricing=_pricing_from_env(env),
            resume_probability=_env_bounded(
                env.get(ENV_ECONOMICS_RESUME_PROBABILITY),
                Decimal(str(DEFAULT_RESUME_PROBABILITY)),
                minimum=Decimal(0),
                maximum=Decimal(1),
            ),
            detached_resume_probability=_env_bounded(
                env.get(ENV_ECONOMICS_DETACHED_RESUME_PROBABILITY),
                Decimal(str(DEFAULT_DETACHED_RESUME_PROBABILITY)),
                minimum=Decimal(0),
                maximum=Decimal(1),
            ),
        )


class LeaseDecision(str, Enum):
    """One scheduler evaluation outcome — §146-147 decision vocabulary.

    ``SKIPPED_*`` decisions mean the warm did not run; ``WARMED_*``
    decisions mean a warm WAS executed and map 1:1 onto the classified
    cache outcome (PRD §67-70). ``SKIPPED_UNSUPPORTED`` is the PRD §67
    non-warmable decision (no snapshot / no adapter support); the warm
    circuit breaker (§94) uses ``SKIPPED_CIRCUIT_OPEN``.
    """

    SCHEDULED = "scheduled"
    SKIPPED_DRY_RUN = "skipped_dry_run"
    SKIPPED_BUSY = "skipped_busy"
    SKIPPED_STALE = "skipped_stale"
    SKIPPED_NO_TARGETS = "skipped_no_targets"
    SKIPPED_ALREADY_WARMING = "skipped_already_warming"
    SKIPPED_UNKNOWN_TTL = "skipped_unknown_ttl"
    SKIPPED_UNSUPPORTED = "skipped_unsupported"
    SKIPPED_CIRCUIT_OPEN = "skipped_circuit_open"
    STOPPED_NO_TARGETS = "stopped_no_targets"
    STOPPED_INVALIDATED = "stopped_invalidated"
    STOPPED_ECONOMIC = "stopped_economic"
    WARMED_CONFIRMED_HIT = "warmed_confirmed_hit"
    WARMED_MISS_REBUILT = "warmed_miss_rebuilt"
    WARMED_UNVERIFIED = "warmed_unverified"
    WARMED_FAILED = "warmed_failed"


@dataclass
class _LeaseRuntime:
    """Process-local lease state — never persisted (PRD §51 flags)."""

    real_request_active: bool = False
    warm_request_active: bool = False
    scheduled_generation: int | None = None
    pending_warm: bool = False
    #: PRD §94 warm circuit breaker: consecutive warm outcomes that did NOT
    #: verify a cache touch. A normal request with new cache evidence resets
    #: both fields (see :meth:`LeaseManager.after_normal_request`).
    consecutive_warm_misses: int = 0
    circuit_open: bool = False


class LeaseManager:
    """Pure lease manager: state machine, scheduler, warm executor driver.

    Offline-testable by construction: no network, no storage, no wall-clock
    dependence — the clock is injectable (``time_fn``, default
    ``time.time``), the p95 latency used by the §53 network margin is
    injectable (``latency_p95_s``), and the Phase 6 warm transport is
    injectable (``warm_executor``, PRD §147) with its memory-only request
    snapshots (``snapshot_store``, PRD §30). A manager constructed without
    them is non-warmable — due leases are skipped with
    ``SKIPPED_UNSUPPORTED`` (fail closed for warming, invariant 9).
    Persistence and the background scheduler task are the relay controller's
    job (``cachepilot_relay.lease_controller``).
    """

    def __init__(
        self,
        settings: LeaseSettings | None = None,
        *,
        time_fn: Callable[[], float] = time.time,
        latency_p95_s: float = 4.0,
        snapshot_store: SnapshotStore | None = None,
        warm_executor: WarmExecutor | None = None,
        pricing: PricingTable | None = None,
        price_override: Decimal | None = None,
        economic_controller: EconomicController | None = None,
        ttl_resolver: TTLResolver | None = None,
    ) -> None:
        self.settings = settings or LeaseSettings()
        self.time_fn = time_fn
        self.latency_p95_s = latency_p95_s
        self.snapshot_store = snapshot_store
        self.warm_executor = warm_executor
        #: Configured pricing snapshot for cost estimation (PRD §65 priority
        #: 2/3): the constructor argument wins, else ``settings.pricing``.
        self.pricing = pricing if pricing is not None else self.settings.pricing
        #: Flat configured price override (PRD §65 priority 3).
        self.price_override = price_override
        #: The economic controller (PRD §60-65). Injectable for deterministic
        #: tests; defaults to a real controller over ``settings.economics``.
        self.economic_controller = economic_controller or EconomicController(
            self.settings.economics
        )
        #: P08 (PRD §59): resolves the TTL override hierarchy (force_seconds
        #: > high-confidence learned TTL > adapter hint > default). Without
        #: one, leases use the configured ``default_ttl_s`` (bootstrap).
        self.ttl_resolver = ttl_resolver
        self._cost_resolver = CostResolver()
        self._leases: dict[str, CacheLease] = {}
        self._by_cache_fingerprint: dict[str, str] = {}
        self._runtime: dict[str, _LeaseRuntime] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # -- registry -----------------------------------------------------------

    @property
    def leases(self) -> tuple[CacheLease, ...]:
        """Every tracked lease (including INVALIDATED / INACTIVE ones)."""
        return tuple(self._leases.values())

    @property
    def lease_ids(self) -> frozenset[str]:
        return frozenset(self._leases)

    @property
    def cache_fingerprints(self) -> frozenset[str]:
        """Every tracked lease's cache fingerprint (for snapshot pruning)."""
        return frozenset(lease.cache_fingerprint for lease in self._leases.values())

    def get(self, lease_id: str) -> CacheLease | None:
        return self._leases.get(lease_id)

    def is_warming(self, lease_id: str) -> bool:
        """True while a warm request for this lease is in flight (§51)."""
        return self._runtime_for(lease_id).warm_request_active

    def find_or_create_lease(
        self,
        *,
        session_id: str,
        provider: str,
        model: str,
        api_mode: str,
        base_url: str,
        auth_scope_hash: str,
        route_fingerprint: str | None,
        request_fingerprint: str,
        cache_fingerprint: str,
        system_fingerprint: str,
        tools_fingerprint: str,
        history_prefix_fingerprint: str,
        estimated_ttl_s: float | None = None,
        ttl_confidence: float = 0.5,
    ) -> CacheLease:
        """Find the lease for a physical cache identity, or create one.

        - An existing lease with the SAME cache fingerprint for this session
          is reused (the natural-request refresh path).
        - A lease for the same session AND provider with a DIFFERENT cache
          fingerprint is a model/identity switch (PRD §21, §50): the old
          lease is INVALIDATED and a fresh, independent lease is created —
          a model switch must NEVER refresh the old model's lease.
        - Leases for different providers coexist (PRD §21 multi-lease).

        P08 (PRD §59): when no explicit ``estimated_ttl_s`` is supplied and
        a :class:`TTLResolver` is configured, the new lease's TTL is
        resolved through the override hierarchy for its route instead of
        the bootstrap default. An unknown TTL is never guessed — the
        resolver's chain ends at the configured default.
        """
        existing_id = self._by_cache_fingerprint.get(cache_fingerprint)
        if existing_id is not None and self._leases[existing_id].session_id == session_id:
            return self._leases[existing_id]
        for lease_id, lease in list(self._leases.items()):
            if (
                lease.session_id == session_id
                and lease.provider == provider
                and lease.cache_fingerprint != cache_fingerprint
                and lease.state is not LeaseState.INVALIDATED
            ):
                logger.info(
                    "lease %s invalidated: cache identity changed (session=%s provider=%s)",
                    lease_id,
                    session_id,
                    provider,
                )
                self.invalidate(lease_id)
        ttl_s = estimated_ttl_s
        ttl_confidence_value = ttl_confidence
        if ttl_s is None and self.ttl_resolver is not None:
            # P08 (PRD §59): resolve through the override hierarchy for the
            # lease's route instead of the bootstrap default.
            resolution = self.ttl_resolver.resolve(
                provider=provider,
                model=model,
                api_mode=api_mode,
                endpoint=base_url,
                route_hash=route_fingerprint,
            )
            ttl_s = resolution.ttl_s
            ttl_confidence_value = resolution.confidence
        lease = CacheLease(
            lease_id=str(uuid.uuid4()),
            session_id=session_id,
            provider=provider,
            model=model,
            api_mode=api_mode,
            base_url=base_url,
            auth_scope_hash=auth_scope_hash,
            route_fingerprint=route_fingerprint,
            request_fingerprint=request_fingerprint,
            cache_fingerprint=cache_fingerprint,
            system_fingerprint=system_fingerprint,
            tools_fingerprint=tools_fingerprint,
            history_prefix_fingerprint=history_prefix_fingerprint,
            last_real_request_at=0.0,
            last_cache_touch_at=None,
            last_confirmed_hit_at=None,
            estimated_ttl_s=(
                ttl_s if ttl_s is not None else self.settings.default_ttl_s
            ),
            ttl_confidence=ttl_confidence_value,
        )
        self._leases[lease.lease_id] = lease
        self._by_cache_fingerprint[cache_fingerprint] = lease.lease_id
        logger.debug(
            "lease %s created (session=%s provider=%s model=%s)",
            lease.lease_id,
            session_id,
            provider,
            model,
        )
        return lease

    # -- lifecycle: arm / invalidate / complete -----------------------------

    def arm(self, lease_id: str) -> CacheLease:
        """Arm the lease (INACTIVE → ARMED). A no-op once INVALIDATED (§21)."""
        lease = self._require(lease_id)
        lease.arm()
        return lease

    def invalidate(self, lease_id: str) -> CacheLease:
        """Invalidate the lease: identity changed (PRD §21, §50).

        Cancels any pending warm and drops in-flight flags. An invalidated
        lease is never re-armed and never refreshed by later requests.
        """
        lease = self._require(lease_id)
        lease.state = LeaseState.INVALIDATED
        self._cancel_pending_warm(lease_id)
        runtime = self._runtime_for(lease_id)
        runtime.real_request_active = False
        runtime.warm_request_active = False
        return lease

    def complete(self, lease_id: str) -> CacheLease:
        """Lease completion — PRD §47: all targets done, no more warming.

        The final normal resumption request is itself the last cache
        consumer; the lease drops back to INACTIVE.
        """
        lease = self._require(lease_id)
        lease.active_targets.clear()
        self._cancel_pending_warm(lease_id)
        lease.disarm()
        return lease

    # -- background target algorithm (PRD §149) ------------------------------

    def target_started(self, lease_id: str, target_id: str) -> CacheLease:
        """First target arms the lease (§149: ``len == 1 → arm``)."""
        lease = self._require(lease_id)
        if not target_id:
            return lease
        lease.active_targets.add(target_id)
        if len(lease.active_targets) == 1:
            lease.arm()
        return lease

    def target_finished(self, lease_id: str, target_id: str) -> CacheLease:
        """Last target disarms the lease (§149: ``not targets → cancel + disarm``)."""
        lease = self._require(lease_id)
        if not target_id:
            return lease
        lease.active_targets.discard(target_id)
        if not lease.active_targets:
            self._cancel_pending_warm(lease_id)
            lease.disarm()
        return lease

    # -- real-request-wins (PRD §148) ----------------------------------------

    def before_normal_request(self, lease_id: str) -> None:
        """PRD §148: real request starts — bump the generation, cancel warm.

        ``real_request_active`` is set so any warm racing this request is
        skipped (§51); every natural request increments ``generation`` so a
        warm scheduled against an older generation is SKIPPED_STALE.
        """
        lease = self._require(lease_id)
        runtime = self._runtime_for(lease_id)
        runtime.real_request_active = True
        lease.generation += 1
        lease.last_real_request_at = self.time_fn()
        self._cancel_pending_warm(lease_id)

    def after_normal_request(self, lease_id: str, outcome: Outcome) -> None:
        """PRD §148: real request finished.

        On success the cache was genuinely touched by a real request, so the
        deadline resets (``last_cache_touch_at = now``, rearm). On FAILED the
        cache is NOT refreshed — a failed provider call is never treated as
        a cache touch (invariant 3, §148). An INVALIDATED lease is never
        refreshed either (§21).
        """
        lease = self._require(lease_id)
        runtime = self._runtime_for(lease_id)
        runtime.real_request_active = False
        if lease.state is LeaseState.INVALIDATED:
            return
        if outcome == Outcome.FAILED:
            return
        # §94: a successful normal request produced fresh cache evidence —
        # reopen the warm circuit (and clear the miss streak) if it was open.
        runtime.circuit_open = False
        runtime.consecutive_warm_misses = 0
        now = self.time_fn()
        lease.last_cache_touch_at = now
        if outcome == Outcome.CONFIRMED_HIT:
            lease.last_confirmed_hit_at = now
        if lease.active_targets:
            lease.arm()
        else:
            # §47: no watchdog relevance — the final normal request is the
            # last consumer of this cache entry.
            lease.disarm()

    def refresh_ttl(self, lease_id: str) -> CacheLease:
        """P08: re-resolve the lease's TTL through the configured resolver.

        Called by the relay after every observed request so a freshly
        learned profile (or a new ``force_seconds``) is picked up. A
        manager without a :class:`TTLResolver` keeps whatever TTL the lease
        already carries (bootstrap default or explicit value).
        """
        lease = self._require(lease_id)
        if self.ttl_resolver is None:
            return lease
        resolution = self.ttl_resolver.resolve(
            provider=lease.provider,
            model=lease.model,
            api_mode=lease.api_mode,
            endpoint=lease.base_url,
            route_hash=lease.route_fingerprint,
        )
        lease.estimated_ttl_s = resolution.ttl_s
        lease.ttl_confidence = resolution.confidence
        return lease

    # -- scheduler -----------------------------------------------------------

    def network_margin(self) -> float:
        """PRD §53: ``max(minimum_margin_s, latency_p95_s * latency_multiplier)``."""
        return max(
            self.settings.minimum_margin_s,
            self.latency_p95_s * self.settings.latency_multiplier,
        )

    def next_deadline(self, lease: CacheLease) -> float | None:
        """PRD §53 safe deadline with §54 deterministic jitter.

        ``None`` when the cache was never touched (nothing to keep warm).
        """
        if lease.last_cache_touch_at is None:
            return None
        ttl = max(lease.estimated_ttl_s, 0.0)
        safe_deadline = min(
            lease.last_cache_touch_at + ttl * self.settings.warm_fraction,
            lease.last_cache_touch_at + ttl - self.network_margin(),
        )
        return safe_deadline * (1.0 + self._jitter_for(lease.cache_fingerprint))

    def lock_for(self, cache_fingerprint: str) -> asyncio.Lock:
        """Per-cache-identity asyncio.Lock — PRD §52, never one global lock.

        Independent cache leases proceed independently; the lock is shared
        only by requests/warms for the SAME physical cache identity.
        """
        lock = self._locks.get(cache_fingerprint)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[cache_fingerprint] = lock
        return lock

    # -- economics (P07: PRD §60-65, §134, §145-146) -------------------------

    def update_cost_estimates(self, lease_id: str, usage: TokenUsage) -> CacheLease:
        """Refresh the lease's resume-cost estimates from request usage (PRD §65).

        Called on the normal-request path ONLY (:meth:`after_normal_request`
        via the relay controller — PRD §64 free heartbeat), never on a
        scheduler tick. When pricing is known, ``prefix_tokens`` are priced
        with :func:`~cachepilot_core.pricing.estimate_resume_costs` and the
        next-warm predictor is the bounded warm shape (cache-read prefix +
        one output token, PRD §31/§147 — the warm replays the still-alive
        cache). When pricing is unknown the resume-cost fields stay/return
        ``None``: the gate then skips with SKIP_UNKNOWN_PRICING and no
        savings are ever claimed (PRD §65, AGENTS.md invariant 4).
        """
        lease = self._require(lease_id)
        prefix = max(usage.prompt_tokens, 0)
        if prefix <= 0:
            # No usable usage (e.g. a failed/empty observation) — keep the
            # last known estimates rather than clobbering them with zeros.
            return lease
        lease.prefix_tokens = prefix
        if self.pricing is not None:
            cold, cached = estimate_resume_costs(prefix, self.pricing)
            lease.estimated_cold_resume_cost_usd = float(cold)
            lease.estimated_cached_resume_cost_usd = float(cached)
            warm_usage = TokenUsage(
                prompt_tokens=prefix,
                cache_read_tokens=prefix,
                completion_tokens=1,
            )
            lease.next_warm_cost_usd = float(estimate_cost(warm_usage, self.pricing))
        else:
            # PRD §65: unknown pricing → never claim savings. The resolved
            # request cost (provider-returned > configured override) is still
            # recorded as the warm-cost predictor when it exists.
            lease.estimated_cold_resume_cost_usd = None
            lease.estimated_cached_resume_cost_usd = None
            resolution = self._cost_resolver.resolve(usage, None, self.price_override)
            if resolution.is_known and resolution.amount is not None:
                lease.next_warm_cost_usd = float(resolution.amount)
            else:
                lease.next_warm_cost_usd = None
        return lease

    def resume_probability(self, lease: CacheLease) -> float:
        """PRD §63 P0 heuristic — no ML, deterministic.

        - no active targets → 0.0 (no continuation possible; never warm);
        - every target explicitly detached (``detached-`` id prefix) → low
          (``settings.detached_resume_probability``, default 0.20);
        - otherwise a background target with ``notify_on_complete`` (the
          relay's ``bg-N`` synthetic ids) → high
          (``settings.resume_probability``, default 0.95).
        """
        if not lease.active_targets:
            return 0.0
        if all(target.startswith("detached-") for target in lease.active_targets):
            return self.settings.detached_resume_probability
        return self.settings.resume_probability

    def evaluate_economics(self, lease: CacheLease) -> WarmDecision:
        """Run the economic controller for a due lease — PRD §146 economics step.

        Derives the controller inputs from the lease (resume-cost estimates,
        cumulative warm cost, next-warm predictor, §63 resume probability),
        records the explainable :class:`WarmDecision` on the lease and in a
        log line (PRD §145), and returns it.
        """
        cold = lease.estimated_cold_resume_cost_usd
        cached = lease.estimated_cached_resume_cost_usd
        pricing_known = cold is not None and cached is not None
        decision = self.economic_controller.evaluate(
            cold_resume_cost=cold if cold is not None else Decimal(0),
            cached_resume_cost=cached if cached is not None else Decimal(0),
            next_warm_cost=(
                lease.next_warm_cost_usd if lease.next_warm_cost_usd is not None else Decimal(0)
            ),
            cumulative_warm_cost=lease.warm_cost_usd,
            resume_probability=self.resume_probability(lease),
            pricing_known=pricing_known,
        )
        lease.last_warm_decision = decision  # PRD §145 (in-memory only)
        logger.info(
            "warm decision %s (%s): expected_avoidable_loss=%s remaining_budget=%s "
            "next_warm_cost=%s cumulative_warm_cost=%s resume_probability=%s "
            "lease=%s ttl=%ss confidence=%s",
            decision.action.value,
            decision.reason,
            decision.expected_avoidable_loss,
            decision.remaining_budget,
            decision.next_warm_cost,
            decision.cumulative_warm_cost,
            decision.resume_probability,
            lease.lease_id,
            lease.estimated_ttl_s,
            lease.ttl_confidence,
        )
        return decision

    def economic_gate(self, lease: CacheLease) -> bool:
        """P07: warm iff the economic controller says WARM (PRD §134, invariant 5).

        Disabled economics (``economics.enabled=False``) restores the P05/P06
        watchdog behaviour — every due lease passes the gate. Otherwise the
        gate consults the controller and returns ``decision.should_warm``:
        SKIP_UNKNOWN_PRICING / SKIP_NO_CONTINUATION / SKIP_NOT_ECONOMIC /
        ECONOMIC_STOP all mean NO warm (fail closed for warming, invariant 9).
        """
        if not self.settings.economics.enabled:
            return True
        return self.evaluate_economics(lease).should_warm

    async def evaluate_lease(self, lease_id: str) -> LeaseDecision:
        """PRD §146 core algorithm for one lease (dry-run warm executor)."""
        lease = self._leases.get(lease_id)
        if lease is None:
            return LeaseDecision.STOPPED_NO_TARGETS
        runtime = self._runtime_for(lease_id)
        if not lease.active_targets:
            return LeaseDecision.STOPPED_NO_TARGETS
        if runtime.real_request_active:
            return LeaseDecision.SKIPPED_BUSY
        if runtime.warm_request_active:
            return LeaseDecision.SKIPPED_ALREADY_WARMING
        if lease.state is LeaseState.INVALIDATED:
            return LeaseDecision.STOPPED_INVALIDATED
        if runtime.circuit_open:
            # §94: 2 consecutive warm misses — stop warming this lease until
            # a normal request produces new cache evidence (invariant 9).
            return LeaseDecision.SKIPPED_CIRCUIT_OPEN
        due_at = self.next_deadline(lease)
        if due_at is None:
            return LeaseDecision.SKIPPED_UNKNOWN_TTL
        now = self.time_fn()
        if now < due_at:
            # §51: capture the generation the scheduled warm is valid for.
            runtime.scheduled_generation = lease.generation
            runtime.pending_warm = True
            if lease.state is not LeaseState.ECONOMIC_STOP:
                lease.state = LeaseState.WARM_SCHEDULED
            seconds = max(0, math.ceil(due_at - now))
            logger.info(
                "WOULD WARM IN %ds (dry run; lease=%s cache_fp=%s)",
                seconds,
                lease.lease_id,
                lease.cache_fingerprint[:12],
            )
            return LeaseDecision.SCHEDULED
        if not self.economic_gate(lease):
            lease.state = LeaseState.ECONOMIC_STOP
            return LeaseDecision.STOPPED_ECONOMIC
        return await self._warm_if_due(lease, runtime)

    async def tick(self) -> list[tuple[str, LeaseDecision]]:
        """Evaluate every tracked lease; one broken lease never stops the loop."""
        results: list[tuple[str, LeaseDecision]] = []
        for lease_id in list(self._leases):
            try:
                results.append((lease_id, await self.evaluate_lease(lease_id)))
            except Exception:
                logger.exception("lease evaluation failed (fail open): lease=%s", lease_id)
        return results

    # -- warm executor (PRD §147, §132; Phase 6 real path) ---------------------

    async def _warm_if_due(
        self,
        lease: CacheLease,
        runtime: _LeaseRuntime,
    ) -> LeaseDecision:
        """The §147 warm algorithm, driven by the injectable warm executor.

        - §51: captures ``scheduled_generation`` BEFORE any await, then
          re-checks it (and ``real_request_active``) under the per-cache
          lock, so a real request racing the warm yields SKIPPED_STALE /
          SKIPPED_BUSY.
        - dry_run (the default, PRD §132) logs ``WOULD WARM NOW`` and never
          sends a network request.
        - Otherwise the warm executes through the injected executor
          (transport + adapter): an uncertain warm (no snapshot, no adapter
          support, adapter declined to bound the request) is SKIPPED /
          SKIPPED_UNSUPPORTED — fail closed for warming (invariant 9).
        - The runtime ``warm_request_active`` flag is set for the duration
          of the warm (§51 warm_request_in_flight) and cleared afterwards.
        """

        scheduled_generation = lease.generation  # §51: capture before awaits
        if runtime.real_request_active:
            return LeaseDecision.SKIPPED_BUSY
        if lease.generation != scheduled_generation:
            return LeaseDecision.SKIPPED_STALE
        async with self.lock_for(lease.cache_fingerprint):  # §52
            if runtime.real_request_active:
                return LeaseDecision.SKIPPED_BUSY
            if not lease.active_targets:
                return LeaseDecision.SKIPPED_NO_TARGETS
            if lease.generation != scheduled_generation:
                return LeaseDecision.SKIPPED_STALE
            if self.settings.dry_run:
                logger.info(
                    "WOULD WARM NOW (dry run; lease=%s cache_fp=%s)",
                    lease.lease_id,
                    lease.cache_fingerprint[:12],
                )
                return LeaseDecision.SKIPPED_DRY_RUN
            if runtime.circuit_open:
                return LeaseDecision.SKIPPED_CIRCUIT_OPEN
            if self.snapshot_store is None or self.warm_executor is None:
                logger.info(
                    "warm skipped: no snapshot store / warm executor configured "
                    "(lease=%s cache_fp=%s)",
                    lease.lease_id,
                    lease.cache_fingerprint[:12],
                )
                return LeaseDecision.SKIPPED_UNSUPPORTED
            snapshot = self.snapshot_store.get(lease.cache_fingerprint)
            if snapshot is None:
                logger.info(
                    "warm skipped: no request snapshot for cache identity "
                    "(lease=%s cache_fp=%s)",
                    lease.lease_id,
                    lease.cache_fingerprint[:12],
                )
                return LeaseDecision.SKIPPED_UNSUPPORTED
            runtime.warm_request_active = True
            try:
                try:
                    result = await self.warm_executor.execute(snapshot)
                except Exception:
                    # Fail open for the scheduler loop: a broken executor
                    # must never kill the tick. The warm is recorded as
                    # FAILED with zero usage/cost (nothing verified).
                    logger.exception(
                        "warm execution failed (fail closed): lease=%s",
                        lease.lease_id,
                    )
                    result = WarmResult(
                        outcome=Outcome.FAILED,
                        usage=TokenUsage(),
                        cost_usd=Decimal(0),
                    )
            finally:
                runtime.warm_request_active = False
            return self._apply_warm_result(lease, runtime, result)

    def _apply_warm_result(
        self,
        lease: CacheLease,
        runtime: _LeaseRuntime,
        result: WarmResult,
    ) -> LeaseDecision:
        """Fold one warm's parsed usage/outcome into the lease (PRD §147).

        Invariant 3 / PRD §147 wording — ``last_cache_touch_at`` advances
        ONLY on a verified cache touch:

        - CONFIRMED_HIT refreshes (provider telemetry proved the read);
        - MISS_REBUILT refreshes only when ``cache_write_tokens > 0`` — the
          write telemetry is what genuinely proves THIS request rebuilt the
          cache entry (PRD §69 evidence; §147);
        - SUCCESS_UNVERIFIED (only request-completion known, §70) and
          FAILED never refresh.

        Warm costs are always visible (invariant 4): ``warm_count`` /
        ``warm_cost_usd`` update for every warm that ran, whatever its
        outcome. The §94 circuit breaker counts every outcome that did NOT
        verify a cache touch; 2 consecutive misses open the circuit until a
        normal request produces new cache evidence.
        """
        if result.outcome is None:
            # Adapter declined to build a bounded warm → nothing was sent,
            # nothing was paid for (uncertain warm = skip, invariant 9).
            logger.info(
                "warm skipped: adapter declined to build a bounded request "
                "(lease=%s cache_fp=%s)",
                lease.lease_id,
                lease.cache_fingerprint[:12],
            )
            return LeaseDecision.SKIPPED_UNSUPPORTED

        # Invariant 4: warm costs are visible, never hidden.
        lease.warm_count += 1
        lease.warm_cost_usd += float(result.cost_usd)
        now = self.time_fn()

        if result.outcome is Outcome.CONFIRMED_HIT:
            runtime.consecutive_warm_misses = 0
            lease.last_cache_touch_at = now
            lease.last_confirmed_hit_at = now
            logger.info(
                "warm CONFIRMED_HIT (lease=%s cache_fp=%s)",
                lease.lease_id,
                lease.cache_fingerprint[:12],
            )
            return LeaseDecision.WARMED_CONFIRMED_HIT

        # Not a verified cache touch — miss streak for the §94 breaker.
        runtime.consecutive_warm_misses += 1
        if runtime.consecutive_warm_misses >= 2:
            runtime.circuit_open = True
            logger.warning(
                "warm circuit open: %d consecutive misses without a verified "
                "cache touch (lease=%s cache_fp=%s)",
                runtime.consecutive_warm_misses,
                lease.lease_id,
                lease.cache_fingerprint[:12],
            )

        if result.outcome is Outcome.MISS_REBUILT:
            if result.usage.cache_write_tokens > 0:
                # Write telemetry proves this request rebuilt the cache
                # entry — the cache genuinely exists again, so the deadline
                # resets (PRD §69 evidence, §147 wording).
                lease.last_cache_touch_at = now
            logger.info(
                "warm MISS_REBUILT (lease=%s cache_fp=%s write_evidence=%s)",
                lease.lease_id,
                lease.cache_fingerprint[:12],
                result.usage.cache_write_tokens > 0,
            )
            return LeaseDecision.WARMED_MISS_REBUILT

        if result.outcome is Outcome.SUCCESS_UNVERIFIED:
            # §70: only request-completion is known — MUST NOT refresh.
            logger.info(
                "warm SUCCESS_UNVERIFIED — no verified cache touch, no "
                "refresh (lease=%s cache_fp=%s)",
                lease.lease_id,
                lease.cache_fingerprint[:12],
            )
            return LeaseDecision.WARMED_UNVERIFIED

        logger.warning(
            "warm FAILED (lease=%s cache_fp=%s)",
            lease.lease_id,
            lease.cache_fingerprint[:12],
        )
        return LeaseDecision.WARMED_FAILED

    # -- internals -----------------------------------------------------------

    def _jitter_for(self, cache_fingerprint: str) -> float:
        """PRD §54: deterministic ±``jitter_fraction`` from the fingerprint."""
        digest = hashlib.sha256(cache_fingerprint.encode("utf-8")).digest()
        unit = int.from_bytes(digest[:8], "big") / float(1 << 64)
        return (unit * 2.0 - 1.0) * self.settings.jitter_fraction

    def _cancel_pending_warm(self, lease_id: str) -> None:
        runtime = self._runtime_for(lease_id)
        runtime.pending_warm = False
        runtime.scheduled_generation = None

    def _runtime_for(self, lease_id: str) -> _LeaseRuntime:
        runtime = self._runtime.get(lease_id)
        if runtime is None:
            runtime = _LeaseRuntime()
            self._runtime[lease_id] = runtime
        return runtime

    def _require(self, lease_id: str) -> CacheLease:
        lease = self._leases.get(lease_id)
        if lease is None:
            raise KeyError(f"unknown lease: {lease_id}")
        return lease


def _env_float(raw: str | None, default: float) -> float:
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_float_or_none(raw: str | None) -> float | None:
    """Parse an optional positive float env var (PRD §59 ``force_seconds``).

    Absent/blank/malformed/non-positive values resolve to None (not set) —
    a bad variable can never break the relay (fail open) nor bypass the
    learned/hint/default chain with a nonsense override.
    """
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    return value if value > 0 else None


def _env_int(raw: str | None, default: int) -> int:
    """Parse a positive integer env var, falling back on malformed values."""
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


def _env_flag(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    # Unrecognized → the safe default. For dry_run that means Phase 5
    # dry-run stays ON (fail closed for warming: uncertain config → no warm).
    return default


def _env_bounded(
    raw: str | None,
    default: Decimal,
    *,
    minimum: Decimal,
    maximum: Decimal | None = None,
    minimum_exclusive: bool = False,
    maximum_exclusive: bool = False,
) -> Decimal:
    """Parse a Decimal env var, falling back when malformed or out of range.

    A bad or out-of-range variable can never break the relay (fail open for
    traffic): the value must satisfy the (optionally exclusive) bounds, else
    the default is returned.
    """
    if raw is None:
        return default
    try:
        value = Decimal(raw.strip())
    except (TypeError, ValueError, ArithmeticError):
        return default
    if value < minimum or (not minimum_exclusive and value == minimum):
        return default
    if maximum is not None and (value > maximum or (not maximum_exclusive and value == maximum)):
        return default
    return value


def _pricing_from_env(env: Mapping[str, str]) -> PricingTable | None:
    """Build the configured pricing snapshot from ``CACHEPILOT_PRICING_*``.

    All-or-nothing (fail closed): every rate must parse to a non-negative
    number, otherwise ``None`` (unknown pricing → the economic gate skips
    with SKIP_UNKNOWN_PRICING). Fallback snapshot only, never authority
    (PRD §66).
    """
    raw = {
        "input_per_mtok": env.get(ENV_PRICING_INPUT_PER_MTK),
        "output_per_mtok": env.get(ENV_PRICING_OUTPUT_PER_MTK),
        "cache_read_per_mtok": env.get(ENV_PRICING_CACHE_READ_PER_MTK),
        "cache_write_per_mtok": env.get(ENV_PRICING_CACHE_WRITE_PER_MTK),
    }
    if any(value is None for value in raw.values()):
        return None
    rates: dict[str, Decimal] = {}
    for field_name, value in raw.items():
        try:
            rate = Decimal(str(value).strip())
        except (TypeError, ValueError, ArithmeticError):
            return None
        if rate < 0:
            return None
        rates[field_name] = rate
    return PricingTable(**rates)
