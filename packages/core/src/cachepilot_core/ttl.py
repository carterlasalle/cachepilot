"""Route-aware TTL learning — PRD §55-59, §82, §135 (Phase 8).

Replaces fixed TTL hints with route-aware learned bounds:

- :class:`TTLProfile` holds ONE route's learned bounds, keyed by
  provider/model/api_mode/endpoint_hash/route_hash (PRD §82), and refines
  them from observations (PRD §55): a verified cache HIT at idle age A
  proves TTL > A (raise the lower bound), a MISS at idle age A caps
  TTL ≤ A (set the upper bound), and the estimate favors the lower side
  of the interval (PRD §57). Confidence (PRD §58) rises with repeated
  consistent verified observations and falls on unverified responses and
  inconsistent evidence.
- :class:`TTLLearner` feeds observed outcomes into the store, pairing
  consecutive observations of the SAME cache fingerprint to compute the
  idle age (telemetry timestamps), and applies ONLY clean observations
  (stable cache identity AND stable route, no intervening churn event —
  PRD §56). A miss after a route change never modifies the old route's
  bounds, and the new context's confidence starts fresh.
- :class:`TTLResolver` implements the PRD §59 override hierarchy:
  ``force_seconds`` > high-confidence learned TTL (confidence ≥ 0.7 and
  sample_count ≥ minimum_samples, PRD §84) > adapter ``ttl_hint()`` >
  ``default_ttl_s``. Unknown TTLs are never silently guessed: the chain
  ends at the configured default, which is a known quantity.

Only hashes, timestamps, outcomes and route identities are ever stored —
never prompts, auth material or API keys (AGENTS.md invariant 10). All
learner errors are logged and skipped by the relay: normal traffic never
depends on TTL learning (fail open, AGENTS.md invariant 9).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from cachepilot_core.adapters import TTLHint
from cachepilot_core.telemetry import Outcome

logger = logging.getLogger("cachepilot_core.ttl")

#: PRD §59 tier 2: a learned TTL is trusted only at this confidence.
HIGH_CONFIDENCE_THRESHOLD = 0.7

#: PRD §84 ``ttl_learning.minimum_samples`` default (configured per
#: deployment via ``CACHEPILOT_TTL_MINIMUM_SAMPLES``).
DEFAULT_MINIMUM_SAMPLES = 3

#: PRD §58 confidence dynamics. Confidence starts at 0.5 and is clamped to
#: ``[CONFIDENCE_FLOOR, CONFIDENCE_CEIL]``.
CONFIDENCE_FLOOR = 0.05
CONFIDENCE_CEIL = 0.95
_HIT_DELTA = 0.05
_MISS_DELTA = 0.02
_UNVERIFIED_DELTA = -0.05
_INCONSISTENT_DELTA = -0.05

#: PRD §57 interval interpolation — favor the lower side of the bounds.
ESTIMATE_INTERVAL_FRACTION = 0.35


def endpoint_hash(endpoint: str) -> str:
    """Stable SHA-256 hash of a provider endpoint (PRD §82; invariant 10 —
    the raw URL is never persisted)."""
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def _join_key(
    provider: str,
    model: str,
    api_mode: str,
    endpoint_hash_value: str,
    route_hash: str | None,
) -> str:
    """The route-key join: provider/model/api_mode/endpoint_hash/route_hash (PRD §82).

    A ``None`` route_hash keys the \"no observable route\" context. ``|``
    cannot appear in SHA-256 hex digests or provider/model identifiers used
    by the relay, so the join is unambiguous.
    """
    return "|".join((provider, model, api_mode, endpoint_hash_value, route_hash or ""))


def build_profile_key(
    *,
    provider: str,
    model: str,
    api_mode: str,
    endpoint: str,
    route_hash: str | None,
) -> str:
    """Build the route profile key from the physical request facts.

    The raw endpoint is hashed here (``endpoint_hash``) so callers never
    pass a URL into the store — only the derived key is used for lookups.
    """
    return _join_key(provider, model, api_mode, endpoint_hash(endpoint), route_hash)


class TTLProfile(BaseModel):
    """One route's learned TTL bounds — PRD §55, §82.

    Refinement (PRD §55): a CONFIRMED_HIT at idle age A raises the lower
    bound to ``max(lower, A)``; a MISS_REBUILT at idle age A caps the
    upper bound to ``min(upper, A)``. SUCCESS_UNVERIFIED counts as an
    observation but lowers confidence (PRD §58); FAILED is never TTL
    evidence. Only CLEAN observations reach :meth:`observe` (PRD §56 —
    see :class:`TTLLearner`).
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    api_mode: str = Field(..., min_length=1)
    endpoint_hash: str = Field(..., min_length=1)
    route_hash: str | None = None

    #: ``None`` until the first hit observation (PRD §55: ``None`` until
    #: first miss is the spec for the upper bound; the lower bound is
    #: ``None`` until the first verified hit).
    lower_bound_s: float | None = None
    upper_bound_s: float | None = None
    estimated_ttl_s: float | None = None
    confidence: float = Field(default=0.5, ge=CONFIDENCE_FLOOR, le=CONFIDENCE_CEIL)
    sample_count: int = Field(default=0, ge=0)
    #: PRD §82 latency columns — not learned in P08 (no latency data
    #: source yet); always None.
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    updated_at: datetime | None = None

    @property
    def profile_key(self) -> str:
        """The route key this profile is stored under (PRD §82)."""
        return _join_key(
            self.provider,
            self.model,
            self.api_mode,
            self.endpoint_hash,
            self.route_hash,
        )

    def estimate(self, adapter_hint: float | None = None) -> float | None:
        """PRD §57 estimator — favors the lower side of the interval.

        - upper bound known → ``lower + (upper - lower) * 0.35``;
        - otherwise → ``max(adapter_hint, lower_bound)``;
        - no evidence and no hint → ``None`` (never silently guess, §59).
        """
        if self.upper_bound_s is not None:
            lower = self.lower_bound_s if self.lower_bound_s is not None else 0.0
            return lower + (self.upper_bound_s - lower) * ESTIMATE_INTERVAL_FRACTION
        if adapter_hint is None and self.lower_bound_s is None:
            return None
        return max(
            adapter_hint if adapter_hint is not None else 0.0,
            self.lower_bound_s if self.lower_bound_s is not None else 0.0,
        )

    def observe(self, outcome: Outcome, idle_age_s: float | None) -> None:
        """Apply ONE clean observation (PRD §55, §58).

        - CONFIRMED_HIT at idle age A → ``lower_bound = max(lower, A)``;
          confidence rises, unless the evidence contradicts a known upper
          bound (an inconsistent hit lowers it, §58);
        - MISS_REBUILT at idle age A → ``upper_bound = min(upper, A)``
          (or A when unset); confidence rises, unless it contradicts a
          known lower bound;
        - SUCCESS_UNVERIFIED → lowers confidence but is NOT counted as a
          sample (unverified response, §58): only request-completion is known,
          so it is not TTL evidence and must never help a profile clear the
          resolver's ``sample_count >= minimum_samples`` gate for the learned
          tier (PRD §59);
        - FAILED → no TTL evidence; nothing changes (invariant 3).
        """
        if outcome is Outcome.FAILED:
            return
        if outcome is Outcome.SUCCESS_UNVERIFIED:
            self._adjust_confidence(_UNVERIFIED_DELTA)
        elif idle_age_s is not None and idle_age_s >= 0:
            self.sample_count += 1
            if outcome is Outcome.CONFIRMED_HIT:
                new_lower = max(self.lower_bound_s or 0.0, idle_age_s)
                self.lower_bound_s = new_lower
                consistent = self.upper_bound_s is None or new_lower <= self.upper_bound_s
                self._adjust_confidence(_HIT_DELTA if consistent else _INCONSISTENT_DELTA)
            elif outcome is Outcome.MISS_REBUILT:
                new_upper = (
                    idle_age_s
                    if self.upper_bound_s is None
                    else min(self.upper_bound_s, idle_age_s)
                )
                self.upper_bound_s = new_upper
                consistent = self.lower_bound_s is None or new_upper >= self.lower_bound_s
                self._adjust_confidence(_MISS_DELTA if consistent else _INCONSISTENT_DELTA)
        # A degenerate pair (absent/negative idle age) refines nothing but
        # is still recorded by the learner with clean=0.
        self.estimated_ttl_s = self.estimate()

    def _adjust_confidence(self, delta: float) -> None:
        self.confidence = min(CONFIDENCE_CEIL, max(CONFIDENCE_FLOOR, self.confidence + delta))


@dataclass(frozen=True)
class TTLObservation:
    """One observed outcome fed to the learner (the relay observation feed).

    Only hashes/outcomes/timestamps travel here — never prompts or auth
    material (AGENTS.md invariant 10).
    """

    outcome: Outcome
    cache_fingerprint: str
    route_hash: str | None
    provider: str
    model: str
    api_mode: str
    endpoint: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class StoredTTLObservation:
    """A ``ttl_observations`` row read back from the store.

    P11 (PRD §99/§138): the route-identity columns (``provider`` / ``model`` /
    ``api_mode`` / ``endpoint_hash``) are ``None`` on rows recorded before the
    P11 schema migration — those rows cannot be attributed to a route profile
    and are excluded from per-profile survival curves.
    """

    id: int
    timestamp: datetime
    cache_fingerprint: str
    route_hash: str | None
    idle_age_s: float | None
    outcome: Outcome
    clean: bool
    provider: str | None = None
    model: str | None = None
    api_mode: str | None = None
    endpoint_hash: str | None = None


class TTLStore(Protocol):
    """The minimal store surface the learner needs.

    Satisfied by :class:`~cachepilot_core.storage.TelemetryStore`; the
    protocol keeps the learner decoupled so it stays offline-testable.
    """

    def profile_for(self, key: str) -> TTLProfile | None: ...

    def upsert_profile(self, profile: TTLProfile) -> None: ...

    def last_ttl_observation(self, cache_fingerprint: str) -> StoredTTLObservation | None: ...

    def record_ttl_observation(
        self,
        *,
        timestamp: datetime,
        cache_fingerprint: str,
        route_hash: str | None,
        idle_age_s: float | None,
        outcome: Outcome,
        clean: bool,
        provider: str | None = None,
        model: str | None = None,
        api_mode: str | None = None,
        endpoint_hash: str | None = None,
    ) -> int: ...

    def churn_between(self, cache_fingerprint: str, start: datetime, end: datetime) -> bool: ...


class TTLLearner:
    """Feed observed outcomes into the store (PRD §55-56).

    Idle age = the time delta between consecutive observations of the SAME
    cache fingerprint (from telemetry timestamps). An observation is CLEAN
    when the cache identity stayed stable across the pair — same
    cache_fingerprint (the pairing key) AND same route_hash, with no churn
    event touching the fingerprint in between (PRD §56). Only clean
    observations refine bounds. A route change leaves the OLD route's
    profile untouched and starts the NEW context's confidence fresh.
    """

    def __init__(self, store: TTLStore | None = None) -> None:
        self._store = store

    def learn(self, obs: TTLObservation) -> TTLProfile | None:
        """Record one observation and refine the route profile.

        Returns the updated profile when the observation was applied (or
        the fresh-started profile on a route change), else None.
        """
        if self._store is None:
            return None
        key = build_profile_key(
            provider=obs.provider,
            model=obs.model,
            api_mode=obs.api_mode,
            endpoint=obs.endpoint,
            route_hash=obs.route_hash,
        )
        previous = self._store.last_ttl_observation(obs.cache_fingerprint)
        idle_age_s: float | None = None
        clean = False
        if previous is not None:
            delta = (obs.timestamp - previous.timestamp).total_seconds()
            if delta > 0:
                idle_age_s = delta
                clean = (
                    previous.route_hash == obs.route_hash
                    and not self._store.churn_between(
                        obs.cache_fingerprint,
                        previous.timestamp,
                        obs.timestamp,
                    )
                )
        self._store.record_ttl_observation(
            timestamp=obs.timestamp,
            cache_fingerprint=obs.cache_fingerprint,
            route_hash=obs.route_hash,
            idle_age_s=idle_age_s,
            outcome=obs.outcome,
            clean=clean,
            # P11 (PRD §99/§138): persist the route-identity columns so CLEAN
            # observations can be attributed to a profile key for the
            # per-profile survival curve (pre-P11 rows stay NULL and are
            # excluded from per-profile curves, never mis-attributed).
            provider=obs.provider,
            model=obs.model,
            api_mode=obs.api_mode,
            endpoint_hash=endpoint_hash(obs.endpoint),
        )
        profile = self._store.profile_for(key)
        if clean and idle_age_s is not None:
            if profile is None:
                profile = TTLProfile(
                    provider=obs.provider,
                    model=obs.model,
                    api_mode=obs.api_mode,
                    endpoint_hash=endpoint_hash(obs.endpoint),
                    route_hash=obs.route_hash,
                )
            profile.observe(obs.outcome, idle_age_s)
            profile.updated_at = obs.timestamp
            self._store.upsert_profile(profile)
            return profile
        if previous is not None and previous.route_hash != obs.route_hash and profile is not None:
            # PRD §56: the observation belongs to a NEW route context —
            # its confidence starts fresh. Bounds are untouched: a miss
            # under a changed identity is not TTL evidence.
            profile.confidence = 0.5
            profile.updated_at = obs.timestamp
            self._store.upsert_profile(profile)
            return profile
        return profile


@dataclass(frozen=True)
class TTLResolution:
    """One TTL resolution with its provenance (PRD §59 hierarchy)."""

    ttl_s: float
    confidence: float
    source: str  # "force" | "learned" | "adapter_hint" | "default"


class TTLResolver:
    """PRD §59 override hierarchy for one lease's route.

    ``force_seconds`` > high-confidence learned TTL (confidence ≥ 0.7 AND
    sample_count ≥ minimum_samples) > adapter ``ttl_hint()`` >
    ``default_ttl_s``. The learned tier never guesses: a profile with no
    estimate (no evidence) falls through to the hint/default tiers, both
    of which are known quantities.
    """

    def __init__(
        self,
        profile_lookup: Callable[[str], TTLProfile | None] | None = None,
        *,
        force_seconds: float | None = None,
        default_ttl_s: float = 300.0,
        minimum_samples: int = DEFAULT_MINIMUM_SAMPLES,
    ) -> None:
        self._lookup = profile_lookup
        self.force_seconds = force_seconds
        self.default_ttl_s = default_ttl_s
        self.minimum_samples = minimum_samples

    def resolve(
        self,
        *,
        provider: str,
        model: str,
        api_mode: str,
        endpoint: str,
        route_hash: str | None,
        adapter_hint: TTLHint | None = None,
    ) -> TTLResolution:
        """Resolve the TTL for one route through the §59 hierarchy."""
        if self.force_seconds is not None:
            return TTLResolution(ttl_s=self.force_seconds, confidence=1.0, source="force")
        profile = None
        if self._lookup is not None:
            profile = self._lookup(
                build_profile_key(
                    provider=provider,
                    model=model,
                    api_mode=api_mode,
                    endpoint=endpoint,
                    route_hash=route_hash,
                )
            )
        if profile is not None:
            estimate = profile.estimate(
                adapter_hint.ttl_s if adapter_hint is not None else None
            )
            if (
                estimate is not None
                and profile.confidence >= HIGH_CONFIDENCE_THRESHOLD
                and profile.sample_count >= self.minimum_samples
            ):
                return TTLResolution(
                    ttl_s=estimate,
                    confidence=profile.confidence,
                    source="learned",
                )
        if adapter_hint is not None and adapter_hint.ttl_s > 0:
            return TTLResolution(
                ttl_s=adapter_hint.ttl_s,
                confidence=adapter_hint.confidence,
                source="adapter_hint",
            )
        return TTLResolution(ttl_s=self.default_ttl_s, confidence=0.5, source="default")
