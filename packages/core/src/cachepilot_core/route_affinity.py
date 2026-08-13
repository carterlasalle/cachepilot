"""Economic route affinity — PRD §72.4, §73-74, §136 (Phase 9).

PRD §73: *never pin to an expensive provider indefinitely merely to preserve
cache.* Route affinity is route affinity ECONOMICS, not blind stickiness:

- :class:`RouteAffinityPolicy` gates pinning on
  ``expected cache recompute savings > extra route cost + safety margin``,
  reusing the pricing helpers (``estimate_resume_costs``) and the economic
  controller's shape (AGENTS.md invariant 5, guard hierarchy item 6: improve
  route affinity only when economically useful). Unknown pricing never
  claims savings (invariant 4).
- :class:`RouteAffinityRegistry` holds the affinity STATE, which per PRD §74
  is lease-scoped (keyed by ``lease_id``), temporary (expires at the lease
  deadline window) and reversible (cleared by the generation counter /
  lease end / real-request reset — the relay controller consumes the pin
  after the next request's generation advance, and prunes entries for leases
  that no longer exist).

The registry is memory-only and fail-open (AGENTS.md invariant 9): it dies
with the relay, and no error in the affinity path can ever block the normal
request path.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from cachepilot_core.pricing import PricingTable, estimate_resume_costs


class AffinityConfig(BaseModel):
    """Route-affinity tunables (PRD §73-74).

    ``enabled`` is the operator's explicit opt-in — route affinity is
    OPTIONAL and never on by default (``CACHEPILOT_ROUTE_AFFINITY``, default
    false). The generic OpenAI-compatible adapter has no affinity mechanism
    anyway (``can_pin_route()`` is False), so nothing pins without both the
    config switch AND a capable adapter.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    safety_margin: Decimal = Field(default=Decimal("0.0"), ge=0)


class AffinityDecision(BaseModel):
    """Explainable outcome of one affinity evaluation (PRD §73, §145-style)."""

    model_config = ConfigDict(extra="forbid")

    apply: bool
    reason: str
    expected_savings: Decimal
    extra_route_cost: Decimal
    net_benefit: Decimal
    safety_margin: Decimal


def _as_decimal(value: Decimal | float) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class RouteAffinityPolicy:
    """PRD §73 economic gate: pin only when the expected cache recompute
    savings (scaled by continuation probability) strictly exceed the extra
    route cost plus a safety margin.
    """

    def __init__(self, config: AffinityConfig | None = None) -> None:
        self.config = config or AffinityConfig()

    def evaluate(
        self,
        *,
        cache_recompute_savings: Decimal | float,
        extra_route_cost: Decimal | float,
        resume_probability: float = 1.0,
        safety_margin: Decimal | float | None = None,
    ) -> AffinityDecision:
        """Evaluate one affinity decision.

        Args:
            cache_recompute_savings: cost avoided by NOT recomputing the
                prefix on the current route (cold resume − cached resume).
            extra_route_cost: per-request premium of pinning to the previous
                route (PRD §73 "extra route cost").
            resume_probability: probability the cached prefix will actually
                be needed again (0..1) — scales the expected savings.
            safety_margin: explicit margin; defaults to the config margin.
        """
        expected = _as_decimal(cache_recompute_savings) * Decimal(
            str(max(0.0, min(1.0, resume_probability)))
        )
        extra = _as_decimal(extra_route_cost)
        margin = (
            self.config.safety_margin
            if safety_margin is None
            else _as_decimal(safety_margin)
        )
        apply = expected > extra + margin
        return AffinityDecision(
            apply=apply,
            reason=(
                "expected_savings_exceed_extra_route_cost"
                if apply
                else "expected_savings_do_not_cover_extra_route_cost"
            ),
            expected_savings=expected,
            extra_route_cost=extra,
            net_benefit=expected - extra,
            safety_margin=margin,
        )

    def evaluate_resume(
        self,
        *,
        prefix_tokens: int,
        pricing: PricingTable,
        extra_route_cost: Decimal | float,
        resume_probability: float = 1.0,
        completion_tokens: int = 0,
        safety_margin: Decimal | float | None = None,
    ) -> AffinityDecision:
        """Evaluate affinity using the pricing table (PRD §65 helpers).

        The cache recompute savings are derived from
        :func:`~cachepilot_core.pricing.estimate_resume_costs`: a cold resume
        must write the prefix, a cached resume reads it.
        """
        cold, cached = estimate_resume_costs(
            prefix_tokens, pricing, completion_tokens
        )
        return self.evaluate(
            cache_recompute_savings=cold - cached,
            extra_route_cost=extra_route_cost,
            resume_probability=resume_probability,
            safety_margin=safety_margin,
        )


@dataclass(frozen=True)
class AffinityEntry:
    """One lease-scoped, temporary affinity (PRD §74)."""

    lease_id: str
    route: str
    expires_at: float
    generation: int


class RouteAffinityRegistry:
    """In-memory affinity state — lease-scoped, temporary, reversible.

    - lease-scoped: keyed by ``lease_id``;
    - temporary: expires at ``expires_at`` (the lease deadline window);
    - reversible: cleared on lease end (:meth:`prune` / :meth:`clear`), on a
      generation RESET (a lease epoch restart invalidates the entry), and
      consumed by the next request (the relay controller clears it once the
      lease's generation advances past the entry's).

    Memory-only and fail-open: the registry dies with the relay and no error
    here can ever block traffic (AGENTS.md invariant 9).
    """

    def __init__(self, *, now_fn: Callable[[], float] | None = None) -> None:
        self._now = now_fn or time.time
        self._entries: dict[str, AffinityEntry] = {}

    def set(
        self,
        *,
        lease_id: str,
        route: str,
        expires_at: float,
        generation: int,
    ) -> None:
        """Record (or replace) the affinity for one lease."""
        self._entries[lease_id] = AffinityEntry(
            lease_id=lease_id,
            route=route,
            expires_at=expires_at,
            generation=generation,
        )

    def active_route_for(self, lease_id: str, generation: int) -> str | None:
        """The route to pin for this lease's request, or None.

        A generation RESET (``generation < entry.generation`` — the lease's
        epoch restarted) invalidates the entry; a monotonic advance keeps it
        valid so the lease's next request can consume the pin.
        """
        entry = self._entries.get(lease_id)
        if entry is None:
            return None
        if self._now() >= entry.expires_at or generation < entry.generation:
            self._entries.pop(lease_id, None)
            return None
        return entry.route

    def generation_for(self, lease_id: str) -> int | None:
        """The generation an entry was created under, or None."""
        entry = self._entries.get(lease_id)
        return entry.generation if entry is not None else None

    def clear(self, lease_id: str) -> None:
        """Remove the affinity for one lease (lease end / consumed)."""
        self._entries.pop(lease_id, None)

    def prune(self, active_lease_ids: Iterable[str]) -> None:
        """Drop entries for leases that no longer exist (lease end)."""
        known = set(active_lease_ids)
        for lease_id in list(self._entries):
            if lease_id not in known:
                self._entries.pop(lease_id, None)
