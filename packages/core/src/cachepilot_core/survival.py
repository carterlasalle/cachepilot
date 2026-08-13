"""Empirical survival estimator — PRD §99, §138 (Phase 11).

Moves from ONE deterministic estimated TTL (PRD §57) toward
``P(cache survives | age)`` (PRD §99): provider caches can be evicted before
their nominal TTL, so learned data should model survival probability, not
merely a single TTL point. This module implements a non-parametric
Kaplan-Meier-style estimator over CLEAN TTL observations (PRD §56 clean
flag), keyed by route profile key (PRD §82):

- Outcome.CONFIRMED_HIT at idle age A is a right-censored observation —
  "survived to age A" (we only know it was still alive at A);
- Outcome.MISS_REBUILT at idle age A is a death event — "died at age A".

The estimator keeps the same honesty discipline as the rest of the phase:
only CLEAN observations enter (stable cache identity and route — PRD §56);
unverified/failed outcomes are never survival evidence (AGENTS.md invariant
3); ages beyond the observed horizon return None, never a fabricated
probability.

DETECT/measurement-only (PRD §138: "Only after measurement"): the curve is a
diagnostic layer ALONGSIDE the PRD §59 TTL override hierarchy — it never
changes warm decisions, TTL resolution or request forwarding. If it ever
feeds warm decisions, that wiring must be gated behind an explicit config
flag (default off) and proven semantically safe first.

Only hashes, timestamps, outcomes and route identities are ever read from
storage — never prompts or auth material (AGENTS.md invariant 10).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from cachepilot_core.telemetry import Outcome
from cachepilot_core.ttl import StoredTTLObservation, TTLProfile

#: Outcomes that carry survival evidence. CONFIRMED_HIT = "survived to age A"
#: (right-censored); MISS_REBUILT = "died at age A" (event). Everything else
#: (SUCCESS_UNVERIFIED / FAILED) is never survival evidence (invariant 3).
SURVIVAL_OUTCOMES: frozenset[Outcome] = frozenset(
    {Outcome.CONFIRMED_HIT, Outcome.MISS_REBUILT}
)


@dataclass(frozen=True)
class SurvivalObservation:
    """One survival input: an idle age and its classified outcome.

    Constructed from clean ``ttl_observations`` rows; the estimator filters
    for :data:`SURVIVAL_OUTCOMES` and non-negative ages defensively.
    """

    idle_age_s: float
    outcome: Outcome


@dataclass(frozen=True)
class SurvivalStep:
    """One Kaplan-Meier step: the survival probability AFTER the events at
    ``age_s``, with the number at risk just before them."""

    age_s: float
    survival: float
    at_risk: int
    events: int


class SurvivalCurve(BaseModel):
    """The empirical survival curve for one route profile key (PRD §99/§138).

    ``steps`` holds the Kaplan-Meier steps (one per distinct death age),
    ``sample_count`` the number of clean observations that entered the
    estimate, and ``horizon_s`` the largest observed age — beyond it the
    estimate is undefined and :meth:`survival_at` returns None (honest
    unknown, never a fabricated probability).
    """

    model_config = ConfigDict(extra="forbid")

    profile_key: str | None = None
    steps: list[SurvivalStep] = Field(default_factory=list)
    sample_count: int = Field(default=0, ge=0)
    horizon_s: float | None = None

    @property
    def empty(self) -> bool:
        return self.sample_count == 0

    def survival_at(self, age_s: float) -> float | None:
        """P(cache survives at age t) — the KM estimate at ``age_s``.

        - below the first death age: 1.0 (no death observed yet);
        - between steps: the last step at or below ``age_s``;
        - beyond the observed horizon: None — the estimate is undefined past
          the largest observation (never fabricated).
        """
        if self.empty or self.horizon_s is None:
            return None
        if age_s > self.horizon_s:
            return None
        if not self.steps or age_s < self.steps[0].age_s:
            # no death observed yet (or none at all — only censored hits):
            # the product-limit estimate is 1.0 up to the horizon
            return 1.0
        estimate = self.steps[0].survival
        for step in self.steps:
            if step.age_s > age_s:
                break
            estimate = step.survival
        return estimate

    def median_survival_s(self) -> float | None:
        """The smallest observed death age at which survival drops to <= 0.5,
        else None (the median was not reached within the observed horizon)."""
        for step in self.steps:
            if step.survival <= 0.5:
                return step.age_s
        return None


def estimate_survival(
    observations: Sequence[SurvivalObservation],
    *,
    profile_key: str | None = None,
) -> SurvivalCurve:
    """Kaplan-Meier-style survival estimate over clean observations.

    Observations are sorted by idle age; at each distinct age, deaths
    (MISS_REBUILT) lower survival by the standard product-limit factor
    ``(1 - d/n)`` with ``n`` the number at risk just before that age, then
    the age group (deaths + censored hits) leaves the risk set. Hits are
    right-censored: they contribute to the risk set until their observed age
    but never lower survival themselves (PRD §99 semantics).

    Deterministic and offline-testable: no randomness, no storage dependency.
    """
    pairs = sorted(
        (obs.idle_age_s, obs.outcome)
        for obs in observations
        if obs.idle_age_s is not None
        and obs.idle_age_s >= 0
        and obs.outcome in SURVIVAL_OUTCOMES
    )
    steps: list[SurvivalStep] = []
    if not pairs:
        return SurvivalCurve(profile_key=profile_key)
    at_risk = len(pairs)
    survival = 1.0
    index = 0
    while index < len(pairs):
        age = pairs[index][0]
        deaths = 0
        censored = 0
        while index < len(pairs) and pairs[index][0] == age:
            if pairs[index][1] is Outcome.MISS_REBUILT:
                deaths += 1
            else:
                censored += 1
            index += 1
        if deaths:
            survival *= 1.0 - deaths / at_risk
            steps.append(
                SurvivalStep(
                    age_s=age,
                    survival=round(survival, 6),
                    at_risk=at_risk,
                    events=deaths,
                )
            )
        at_risk -= deaths + censored
    return SurvivalCurve(
        profile_key=profile_key,
        steps=steps,
        sample_count=len(pairs),
        horizon_s=pairs[-1][0],
    )


def curve_from_observations(
    profile_key: str | None,
    observations: Sequence[StoredTTLObservation],
) -> SurvivalCurve:
    """Survival curve from stored TTL observations for one profile key.

    Only CLEAN rows with an idle age are survival evidence (PRD §56); rows
    without route identity or recorded before the P11 migration carry a NULL
    identity and are filtered by the caller's query — they are never
    mis-attributed to a profile here.
    """
    return estimate_survival(
        [
            SurvivalObservation(idle_age_s=obs.idle_age_s, outcome=obs.outcome)
            for obs in observations
            if obs.clean and obs.idle_age_s is not None
        ],
        profile_key=profile_key,
    )


class ProfileObservationStore(Protocol):
    """The minimal store surface the CLI needs for per-profile curves."""

    def clean_observations_for_profile(
        self,
        *,
        provider: str,
        model: str,
        api_mode: str,
        endpoint_hash: str,
        route_hash: str | None,
    ) -> list[StoredTTLObservation]: ...


def curve_from_profile(
    store: ProfileObservationStore, profile: TTLProfile
) -> SurvivalCurve:
    """Survival curve for one route profile (PRD §82 key) from the store.

    The per-profile keying uses the same identity columns as the TTL profile
    (provider / model / api_mode / endpoint_hash / route_hash), so the curve
    is directly comparable to the profile's estimated TTL (the CLI shows
    ``P(survive)`` at the current lease TTL, PRD §138).
    """
    observations = store.clean_observations_for_profile(
        provider=profile.provider,
        model=profile.model,
        api_mode=profile.api_mode,
        endpoint_hash=profile.endpoint_hash,
        route_hash=profile.route_hash,
    )
    return curve_from_observations(profile.profile_key, observations)
