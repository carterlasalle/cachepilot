"""P09 route affinity — PRD §73 economic gate + §74 lease-scoped state.

- the policy refuses affinity when the expected cache recompute savings do
  not strictly exceed the extra route cost (+ safety margin), and allows it
  when they do — never blind stickiness;
- pricing-derived savings reuse :func:`estimate_resume_costs` (PRD §65
  helpers, economics.py cost helpers);
- the registry state is lease-scoped, temporary (expires), and reversible
  (generation reset / clear / prune).
"""

from __future__ import annotations

from decimal import Decimal

from cachepilot_core.pricing import PricingTable
from cachepilot_core.route_affinity import (
    AffinityConfig,
    AffinityDecision,
    RouteAffinityPolicy,
    RouteAffinityRegistry,
)

# Mirrors the PRD §62-shaped fake pricing (write ≈ input, read ~10%).
_PRICING = PricingTable(
    input_per_mtok=Decimal("0.80"),
    output_per_mtok=Decimal("2.40"),
    cache_read_per_mtok=Decimal("0.08"),
    cache_write_per_mtok=Decimal("0.88"),
)


# -- economic gate (PRD §73) --------------------------------------------------


def test_affinity_refused_when_uneconomic():
    decision = RouteAffinityPolicy().evaluate(
        cache_recompute_savings=Decimal("0.01"),
        extra_route_cost=Decimal("0.02"),
    )
    assert decision.apply is False
    assert isinstance(decision, AffinityDecision)
    assert decision.expected_savings == Decimal("0.01")
    assert decision.extra_route_cost == Decimal("0.02")


def test_affinity_allowed_when_savings_exceed_cost():
    decision = RouteAffinityPolicy().evaluate(
        cache_recompute_savings=Decimal("0.05"),
        extra_route_cost=Decimal("0.01"),
    )
    assert decision.apply is True
    assert decision.net_benefit == Decimal("0.04")


def test_affinity_is_strict_and_respects_safety_margin():
    policy = RouteAffinityPolicy()
    # equal is NOT enough — strict savings > cost
    assert (
        policy.evaluate(
            cache_recompute_savings=Decimal("0.01"),
            extra_route_cost=Decimal("0.01"),
        ).apply
        is False
    )
    # margin pushes an otherwise-positive decision negative
    assert (
        policy.evaluate(
            cache_recompute_savings=Decimal("0.02"),
            extra_route_cost=Decimal("0.01"),
            safety_margin=Decimal("0.01"),
        ).apply
        is False
    )
    # config-level margin applies by default
    strict = RouteAffinityPolicy(AffinityConfig(safety_margin=Decimal("0.02")))
    assert (
        strict.evaluate(
            cache_recompute_savings=Decimal("0.03"),
            extra_route_cost=Decimal("0.01"),
        ).apply
        is False
    )


def test_affinity_scales_savings_by_resume_probability():
    policy = RouteAffinityPolicy()
    # savings 0.02, cost 0.01 → allowed at probability 1.0
    assert (
        policy.evaluate(
            cache_recompute_savings=Decimal("0.02"),
            extra_route_cost=Decimal("0.01"),
            resume_probability=1.0,
        ).apply
        is True
    )
    # probability 0.5 → expected savings 0.01, NOT > cost 0.01 → refused
    assert (
        policy.evaluate(
            cache_recompute_savings=Decimal("0.02"),
            extra_route_cost=Decimal("0.01"),
            resume_probability=0.5,
        ).apply
        is False
    )
    # zero probability never pins
    assert (
        policy.evaluate(
            cache_recompute_savings=Decimal("1.0"),
            extra_route_cost=Decimal("0.0"),
            resume_probability=0.0,
        ).apply
        is False
    )


def test_affinity_evaluate_resume_derives_savings_from_pricing():
    policy = RouteAffinityPolicy()
    # 4000-token prefix: cold write (0.88/M) vs cached read (0.08/M)
    # → savings = 4000 * 0.80 / 1e6 = 0.0032
    cheap = policy.evaluate_resume(
        prefix_tokens=4000,
        pricing=_PRICING,
        extra_route_cost=Decimal("0.001"),
    )
    assert cheap.apply is True
    assert cheap.expected_savings == Decimal("0.0032")
    expensive = policy.evaluate_resume(
        prefix_tokens=4000,
        pricing=_PRICING,
        extra_route_cost=Decimal("0.01"),
    )
    assert expensive.apply is False


def test_affinity_config_defaults_off():
    assert AffinityConfig().enabled is False  # optional, never on by default


# -- registry state (PRD §74: lease-scoped, temporary, reversible) ------------


def test_registry_is_lease_scoped_and_generation_aware():
    now = [1000.0]
    registry = RouteAffinityRegistry(now_fn=lambda: now[0])
    registry.set(lease_id="lease-1", route="route-a", expires_at=2000.0, generation=5)
    # the owning lease sees the pin (monotonic generation advance is fine)
    assert registry.active_route_for("lease-1", generation=5) == "route-a"
    assert registry.active_route_for("lease-1", generation=6) == "route-a"
    # other leases never see it
    assert registry.active_route_for("lease-2", generation=5) is None


def test_registry_expires_at_deadline():
    now = [1000.0]
    registry = RouteAffinityRegistry(now_fn=lambda: now[0])
    registry.set(lease_id="lease-1", route="route-a", expires_at=1500.0, generation=3)
    assert registry.active_route_for("lease-1", generation=3) == "route-a"
    now[0] = 1500.0  # lease deadline reached → temporary state expires
    assert registry.active_route_for("lease-1", generation=3) is None
    assert registry.generation_for("lease-1") is None  # and is cleared


def test_registry_generation_reset_is_reversible():
    registry = RouteAffinityRegistry(now_fn=lambda: 1000.0)
    registry.set(lease_id="lease-1", route="route-a", expires_at=2000.0, generation=5)
    # a generation RESET (lease epoch restarted) invalidates the entry
    assert registry.active_route_for("lease-1", generation=0) is None
    assert registry.generation_for("lease-1") is None


def test_registry_clear_and_prune_on_lease_end():
    registry = RouteAffinityRegistry(now_fn=lambda: 1000.0)
    registry.set(lease_id="lease-1", route="route-a", expires_at=2000.0, generation=1)
    registry.set(lease_id="lease-2", route="route-b", expires_at=2000.0, generation=1)
    registry.clear("lease-1")
    assert registry.active_route_for("lease-1", generation=1) is None
    # prune drops entries whose lease no longer exists (lease end)
    registry.prune(["lease-2"])
    assert registry.active_route_for("lease-2", generation=1) == "route-b"
    registry.prune([])
    assert registry.active_route_for("lease-2", generation=1) is None
