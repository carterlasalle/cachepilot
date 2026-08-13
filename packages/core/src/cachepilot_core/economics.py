"""Economic controller — the warm decision (PRD §60-65, AGENTS.md invariant 5).

WARM is decided only when ALL of the following hold:

- pricing is known (PRD §65: never claim savings with incomplete cost data;
  economically unbounded repeated warming is disabled by default);
- continuation probability is positive (PRD §103: zero probability → never warm);
- there is actual avoidable loss (cold resume cost > cached resume cost);
- ``expected_avoidable_loss > expected_next_warm_cost + safety_margin``
  (AGENTS.md invariant 5);
- the next warm fits the remaining budget — cumulative warm costs exhaust
  ``expected_value * budget_ratio`` (PRD §61), so warming can never continue
  forever (PRD §62 example, §65 "disable economically unbounded repeated
  warming").

Every decision is explainable (PRD §145): the returned :class:`WarmDecision`
carries the full economic breakdown plus a machine-readable reason.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field

from cachepilot_core.pricing import PricingTable, estimate_resume_costs


class WarmAction(str, Enum):
    WARM = "warm"
    ECONOMIC_STOP = "economic_stop"
    SKIP_UNKNOWN_PRICING = "skip_unknown_pricing"
    SKIP_NO_CONTINUATION = "skip_no_continuation"
    SKIP_NOT_ECONOMIC = "skip_not_economic"


class EconomicConfig(BaseModel):
    """Tunables for the economic controller (PRD §60-61, §84 ``cache.economics``).

    ``enabled`` is the operator's explicit switch (PRD §84 sample): when
    False the lease manager falls back to the P05/P06 watchdog behaviour
    (every due lease warms). The PRD §84 sample also suggests
    ``minimum_expected_savings_usd: 0.01`` — operators opt in via
    ``CACHEPILOT_ECONOMICS_MINIMUM_EXPECTED_SAVINGS_USD``; the default stays
    0.0 so the controller's pure math is the only gate out of the box.
    """

    enabled: bool = True
    budget_ratio: Decimal = Field(default=Decimal("0.70"), gt=0, le=1)
    safety_margin: Decimal = Field(default=Decimal("0.0"), ge=0)
    minimum_expected_savings: Decimal = Field(default=Decimal("0.0"), ge=0)


class WarmDecision(BaseModel):
    """Explainable outcome of one warm evaluation (PRD §145)."""

    action: WarmAction
    reason: str
    resume_probability: float
    expected_avoidable_loss: Decimal
    expected_value: Decimal
    next_warm_cost: Decimal
    cumulative_warm_cost: Decimal
    max_warm_budget: Decimal
    remaining_budget: Decimal
    safety_margin: Decimal

    @property
    def should_warm(self) -> bool:
        return self.action is WarmAction.WARM


def _as_decimal(value: Decimal | float) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class EconomicController:
    """Decides whether a cache warm is economically justified."""

    def __init__(self, config: EconomicConfig | None = None) -> None:
        self.config = config or EconomicConfig()

    def evaluate(
        self,
        *,
        cold_resume_cost: Decimal | float,
        cached_resume_cost: Decimal | float,
        next_warm_cost: Decimal | float,
        cumulative_warm_cost: Decimal | float,
        resume_probability: float,
        pricing_known: bool = True,
    ) -> WarmDecision:
        """Evaluate one warm decision for a lease.

        Args:
            cold_resume_cost: cost of resuming with a cold (expired) cache.
            cached_resume_cost: cost of resuming with a warm cache.
            next_warm_cost: expected cost of the next warm request.
            cumulative_warm_cost: total spent on warms so far.
            resume_probability: probability the background target resumes (0..1).
            pricing_known: whether cost data is complete (PRD §65).
        """
        next_cost = _as_decimal(next_warm_cost)
        cumulative = _as_decimal(cumulative_warm_cost)

        if not pricing_known:
            return self._decision(
                WarmAction.SKIP_UNKNOWN_PRICING,
                "pricing unknown — never claim savings with incomplete cost data",
                resume_probability=resume_probability,
                avoidable=Decimal(0),
                expected=Decimal(0),
                next=next_cost,
                cumulative=cumulative,
                margin=self.config.safety_margin,
            )
        if resume_probability <= 0:
            return self._decision(
                WarmAction.SKIP_NO_CONTINUATION,
                "zero continuation probability — never warm",
                resume_probability=resume_probability,
                avoidable=Decimal(0),
                expected=Decimal(0),
                next=next_cost,
                cumulative=cumulative,
                margin=self.config.safety_margin,
            )

        avoidable = _as_decimal(cold_resume_cost) - _as_decimal(cached_resume_cost)
        if avoidable <= 0:
            return self._decision(
                WarmAction.SKIP_NOT_ECONOMIC,
                "no avoidable loss (cold resume not more expensive than cached resume)",
                resume_probability=resume_probability,
                avoidable=avoidable,
                expected=Decimal(0),
                next=next_cost,
                cumulative=cumulative,
                margin=self.config.safety_margin,
            )

        expected = Decimal(str(resume_probability)) * avoidable
        max_budget = expected * self.config.budget_ratio
        remaining = max_budget - cumulative

        # AGENTS.md invariant 5: WARM iff expected_avoidable_loss >
        # expected_next_warm_cost + safety_margin.
        if expected <= next_cost + self.config.safety_margin:
            return self._decision(
                WarmAction.SKIP_NOT_ECONOMIC,
                "expected avoidable loss does not cover next warm cost + safety margin",
                resume_probability=resume_probability,
                avoidable=avoidable,
                expected=expected,
                next=next_cost,
                cumulative=cumulative,
                margin=self.config.safety_margin,
            )
        # PRD §61: the next warm must fit the remaining budget — this is what
        # guarantees warming can never continue forever.
        if next_cost >= remaining:
            return self._decision(
                WarmAction.ECONOMIC_STOP,
                "cumulative warm cost exhausted the warm budget — let the cache expire",
                resume_probability=resume_probability,
                avoidable=avoidable,
                expected=expected,
                next=next_cost,
                cumulative=cumulative,
                margin=self.config.safety_margin,
            )
        net_savings = expected - (cumulative + next_cost)
        if net_savings < self.config.minimum_expected_savings:
            return self._decision(
                WarmAction.SKIP_NOT_ECONOMIC,
                "expected net savings below minimum",
                resume_probability=resume_probability,
                avoidable=avoidable,
                expected=expected,
                next=next_cost,
                cumulative=cumulative,
                margin=self.config.safety_margin,
            )
        return self._decision(
            WarmAction.WARM,
            "due_and_economically_positive",
            resume_probability=resume_probability,
            avoidable=avoidable,
            expected=expected,
            next=next_cost,
            cumulative=cumulative,
            margin=self.config.safety_margin,
        )

    def evaluate_resume(
        self,
        *,
        prefix_tokens: int,
        pricing: PricingTable,
        resume_probability: float,
        next_warm_cost: Decimal | float,
        cumulative_warm_cost: Decimal | float,
        completion_tokens: int = 0,
        pricing_known: bool = True,
    ) -> WarmDecision:
        """Evaluate warming a prefix using live pricing (PRD §65 priority 2).

        Convenience for harnesses: derives cold/cached resume costs from the
        pricing table, then delegates to :meth:`evaluate`.
        """
        cold, cached = estimate_resume_costs(prefix_tokens, pricing, completion_tokens)
        return self.evaluate(
            cold_resume_cost=cold,
            cached_resume_cost=cached,
            next_warm_cost=next_warm_cost,
            cumulative_warm_cost=cumulative_warm_cost,
            resume_probability=resume_probability,
            pricing_known=pricing_known,
        )

    def _decision(
        self,
        action: WarmAction,
        reason: str,
        *,
        resume_probability: float,
        avoidable: Decimal,
        expected: Decimal,
        next: Decimal,
        cumulative: Decimal,
        margin: Decimal,
    ) -> WarmDecision:
        max_budget = expected * self.config.budget_ratio
        return WarmDecision(
            action=action,
            reason=reason,
            resume_probability=resume_probability,
            expected_avoidable_loss=avoidable,
            expected_value=expected,
            next_warm_cost=next,
            cumulative_warm_cost=cumulative,
            max_warm_budget=max_budget,
            remaining_budget=max_budget - cumulative,
            safety_margin=margin,
        )
