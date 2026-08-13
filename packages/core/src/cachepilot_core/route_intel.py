"""Route intelligence — PRD §71-74, UC-5, §136 (Phase 9).

Phase 9 adds four things on top of the Phase 8 (P08) TTL learning:

- :class:`RouteIdentity` — the PRD §71 cache route identity
  (gateway / upstream_provider / endpoint / region / deployment), with only
  the observable fields populated, a stable :meth:`RouteIdentity.route_hash`
  (SHA-256 of the observable fields — the compact string form the relay
  already carries everywhere), and a lossless :meth:`RouteIdentity.to_str` /
  :meth:`RouteIdentity.from_str` round-trip for diagnostics and CLI display.
- :class:`RouterMissClassifier` — the PRD UC-5 router-miss analysis: a
  repeated logical request (stable system/tools/history/model/provider)
  whose physical route changed (route A → route B) followed by a
  MISS_REBUILT where the previous observation proved a warm cache
  (CONFIRMED_HIT on route A) is classified ROUTE_INSTABILITY instead of
  being misread as an extremely short TTL. Same-route misses stay SHORT_TTL
  (genuine TTL evidence); everything else is CLEAN.
- :class:`RouteChangeEvent` — the persisted route A→B transition (PRD §72.1,
  §75) carrying the observable identity of the NEW route plus the verdict,
  so the CLI can list observed routes and instability stats.
- :class:`RouteIntelStats` — the aggregate for ``cachepilot routes`` (PRD
  §76): route switch count, last switch time, instability verdict count.

TTL protection is structural and kept from P08 (PRD §56): the learner only
refines bounds from CLEAN observations (stable cache identity AND stable
route, no intervening churn). A route-change miss therefore never reaches
the TTL bounds — the classifier's verdict is recorded on top of that
protection, it does not replace it.

Only hashes, timestamps, route identities and outcomes are carried here —
never raw prompts, history or auth material (AGENTS.md invariant 10).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from cachepilot_core.telemetry import Outcome


class RouteIdentity(BaseModel):
    """Cache route identity — PRD §71.

    Only fields actually observable from the physical request/connection and
    the response are populated; the rest stay None.
    """

    model_config = ConfigDict(extra="forbid")

    gateway: str | None = None
    upstream_provider: str | None = None
    endpoint: str | None = None
    region: str | None = None
    deployment: str | None = None

    def route_hash(self) -> str | None:
        """Stable SHA-256 of the observable route fields; None when nothing
        is observable (PRD §71 — no fabricated identity, no ``""`` hash).

        This is the compact string form the relay stores in
        ``request_events`` / ``ttl_observations`` and feeds into the cache
        fingerprint (AGENTS.md invariant 7: route is part of cache identity).
        """
        if not any(
            (
                self.gateway,
                self.upstream_provider,
                self.endpoint,
                self.region,
                self.deployment,
            )
        ):
            return None
        payload = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_str(self) -> str:
        """Lossless serialization of the observable route identity.

        Unlike :meth:`route_hash` (one-way), this round-trips the PRD §71
        fields for diagnostics and ``cachepilot routes`` display.
        """
        return json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_str(cls, value: str) -> RouteIdentity:
        """Parse the string form produced by :meth:`to_str`."""
        parsed = json.loads(value)
        return cls(**parsed)


class RouteMissVerdict(str, Enum):
    """Classification of a miss on a repeated logical request (PRD UC-5)."""

    ROUTE_INSTABILITY = "route_instability"
    SHORT_TTL = "short_ttl"
    CLEAN = "clean"


class RouteChangeEvent(BaseModel):
    """One observed route transition (route A → route B) — PRD §72.1, §75.

    Persisted rows carry the observable identity of the NEW route so the CLI
    can list observed routes (PRD §71 fields) — never raw prompts or auth
    material (AGENTS.md invariant 10: route identities are whitelisted).
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    session_hash: str | None = None
    cache_fingerprint: str = Field(..., min_length=1)
    request_fingerprint: str | None = None
    previous_route_hash: str | None = None
    new_route_hash: str | None = None
    gateway: str | None = None
    upstream_provider: str | None = None
    endpoint: str | None = None
    region: str | None = None
    deployment: str | None = None
    verdict: RouteMissVerdict
    #: Row id when read back from storage; None for freshly-built events.
    id: int | None = None


class RouterMissClassifier:
    """PRD UC-5 router-miss analysis.

    Inputs are the previous observation (route A, cache fingerprint F) and
    the current observation (route B ≠ A, the same logical request, outcome
    MISS_REBUILT where a hit was expected):

    - ROUTE_INSTABILITY — the physical route changed between two
      observations of the same logical request, the previous observation
      proved the cache was warm (CONFIRMED_HIT on route A), and the current
      one missed. The miss is a cold cache on route B, NOT short-TTL
      evidence.
    - SHORT_TTL — same route, same logical request, a verified hit followed
      by a miss: genuine TTL evidence (the learner refines bounds from it).
    - CLEAN — everything else (no prior hit expectation, identity changed,
      or observability gaps): no instability attribution, no TTL conclusion.

    Route-specific confidence invalidation for the changed context is
    delegated to :class:`~cachepilot_core.ttl.TTLLearner` (PRD §56 — the new
    route context's profile confidence starts fresh), and the instability
    miss is guaranteed NOT to reach TTL refinement by the same learner's
    clean-check: a route change never produces a CLEAN pair.
    """

    def classify(
        self,
        *,
        previous_outcome: Outcome | None,
        previous_route_hash: str | None,
        current_outcome: Outcome,
        current_route_hash: str | None,
        identity_stable: bool = True,
    ) -> RouteMissVerdict:
        """Classify one miss on a repeated logical request.

        Args:
            previous_outcome: outcome of the previous observation of the
                same logical request (None when there was none).
            previous_route_hash: route identity of the previous observation.
            current_outcome: outcome of the current observation.
            current_route_hash: route identity of the current observation.
            identity_stable: whether the logical request content stayed
                stable (same system/tools/history/model/provider — the
                proxy for UC-5's "same logical request fingerprint").
        """
        if previous_route_hash != current_route_hash:
            # A genuine A→B switch needs BOTH identities observable.
            if (
                previous_route_hash is not None
                and current_route_hash is not None
                and identity_stable
                and previous_outcome is Outcome.CONFIRMED_HIT
                and current_outcome is Outcome.MISS_REBUILT
            ):
                return RouteMissVerdict.ROUTE_INSTABILITY
            return RouteMissVerdict.CLEAN
        if (
            identity_stable
            and previous_outcome is Outcome.CONFIRMED_HIT
            and current_outcome is Outcome.MISS_REBUILT
        ):
            return RouteMissVerdict.SHORT_TTL
        return RouteMissVerdict.CLEAN


class RouteIntelStats(BaseModel):
    """Route-intelligence aggregates for the CLI (PRD §76 ``cachepilot routes``)."""

    model_config = ConfigDict(extra="forbid")

    route_switches: int = 0
    instability_verdicts: int = 0
    short_ttl_verdicts: int = 0
    last_switch_at: datetime | None = None
