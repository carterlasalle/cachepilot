"""Cache lease manager unit tests — PRD §21, §46-54, §132, §146-149 (Phase 5).

Pure, offline tests: an injectable fake clock replaces wall time, jitter is
zeroed or computed deterministically, and no network or storage is touched.
Race scenarios (warm-vs-real, warm-vs-complete) hold the per-cache-identity
lock to interleave a competing event with a warm that is about to fire.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from cachepilot_core.adapters import WarmResult
from cachepilot_core.economics import EconomicConfig, EconomicController, WarmAction, WarmDecision
from cachepilot_core.leases import (
    CacheLease,
    LeaseDecision,
    LeaseManager,
    LeaseSettings,
    LeaseState,
)
from cachepilot_core.pricing import PricingTable
from cachepilot_core.snapshots import RequestSnapshot, SnapshotStore
from cachepilot_core.telemetry import Outcome
from cachepilot_core.usage import TokenUsage

#: PRD §62-shaped pricing (matches the FakeProvider defaults): a 4000-token
#: prefix costs $0.00352 cold / $0.00032 cached, so a ~$0.0003 warm is
#: economically positive and several warms fit the $0.002128 budget.
PRICING = PricingTable(
    input_per_mtok=Decimal("0.80"),
    output_per_mtok=Decimal("2.40"),
    cache_read_per_mtok=Decimal("0.08"),
    cache_write_per_mtok=Decimal("0.88"),
)

#: Normal-request usage for a 4000-token prefix (mirrors the fake provider).
USAGE = TokenUsage(prompt_tokens=4000, cache_read_tokens=4000)


class FakeClock:
    """Injectable clock: starts at ``start`` and advances on demand."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make_lease(
    manager: LeaseManager,
    *,
    session_id: str = "sess-1",
    provider: str = "fake-provider",
    model: str = "gpt-5.2",
    cache_fp: str = "cache-fp-1",
) -> CacheLease:
    return manager.find_or_create_lease(
        session_id=session_id,
        provider=provider,
        model=model,
        api_mode="chat",
        base_url="https://fake-provider.invalid/v1",
        auth_scope_hash="auth-1",
        route_fingerprint=None,
        request_fingerprint="req-fp-1",
        cache_fingerprint=cache_fp,
        system_fingerprint="sys-fp-1",
        tools_fingerprint="tools-fp-1",
        history_prefix_fingerprint="hist-fp-1",
    )


def _armed_touched_lease(manager: LeaseManager, clock: FakeClock) -> CacheLease:
    """A lease with one active target and a fresh cache touch (ARMED)."""
    lease = _make_lease(manager)
    manager.target_started(lease.lease_id, "t1")
    manager.before_normal_request(lease.lease_id)
    manager.after_normal_request(lease.lease_id, Outcome.CONFIRMED_HIT)
    # P07: the normal request refreshes the resume-cost estimates (PRD §65).
    manager.update_cost_estimates(lease.lease_id, USAGE)
    assert lease.state is LeaseState.ARMED
    return lease


def _default_manager(clock: FakeClock, **settings) -> LeaseManager:
    return LeaseManager(
        LeaseSettings(jitter_fraction=0.0, pricing=PRICING, **settings),
        time_fn=clock,
        latency_p95_s=4.0,
    )


# -- state machine (§49-50) --------------------------------------------------


def test_state_machine_transitions_arm_schedule_disarm():
    clock = FakeClock()
    manager = _default_manager(clock)
    lease = _make_lease(manager)

    assert lease.state is LeaseState.INACTIVE

    # background target starts → ARMED (§149, §50)
    manager.target_started(lease.lease_id, "t1")
    assert lease.state is LeaseState.ARMED
    assert lease.active_targets == {"t1"}

    # second target: still ARMED, still one lease
    manager.target_started(lease.lease_id, "t2")
    assert lease.state is LeaseState.ARMED
    assert lease.active_targets == {"t1", "t2"}

    # Without a cache touch there is nothing to keep warm yet.
    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.SKIPPED_UNKNOWN_TTL

    # A real request refreshes the cache, then the deadline approaches →
    # WARM_SCHEDULED.
    manager.before_normal_request(lease.lease_id)
    manager.after_normal_request(lease.lease_id, Outcome.CONFIRMED_HIT)
    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.SCHEDULED
    assert lease.state is LeaseState.WARM_SCHEDULED

    # all targets complete → INACTIVE, pending warm canceled (§47, §149)
    manager.target_finished(lease.lease_id, "t1")
    assert lease.state is LeaseState.WARM_SCHEDULED  # still one target left
    manager.target_finished(lease.lease_id, "t2")
    assert lease.state is LeaseState.INACTIVE
    assert lease.active_targets == set()
    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.STOPPED_NO_TARGETS


def test_complete_disarms_immediately():
    clock = FakeClock()
    manager = _default_manager(clock)
    lease = _armed_touched_lease(manager, clock)
    manager.complete(lease.lease_id)
    assert lease.state is LeaseState.INACTIVE
    assert lease.active_targets == set()
    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.STOPPED_NO_TARGETS


# -- generation counter (§51) ------------------------------------------------


def test_before_normal_request_increments_generation_and_records_timestamp():
    clock = FakeClock()
    manager = _default_manager(clock)
    lease = _make_lease(manager)
    assert lease.generation == 0
    manager.before_normal_request(lease.lease_id)
    assert lease.generation == 1
    assert lease.last_real_request_at == clock.now
    manager.before_normal_request(lease.lease_id)
    assert lease.generation == 2


# -- normal-request-reset (§148) ---------------------------------------------


def test_successful_normal_request_resets_deadline():
    clock = FakeClock()
    manager = _default_manager(clock)
    lease = _armed_touched_lease(manager, clock)  # touch at t0
    deadline_after_first = manager.next_deadline(lease)
    assert deadline_after_first == clock.now + 300 * 0.8  # ttl 300, fraction .8

    clock.advance(100)
    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.SCHEDULED  # deadline 140s out

    # A real request succeeds → cache genuinely refreshed → deadline resets.
    manager.before_normal_request(lease.lease_id)
    manager.after_normal_request(lease.lease_id, Outcome.MISS_REBUILT)
    assert lease.last_cache_touch_at == clock.now
    assert manager.next_deadline(lease) == clock.now + 240
    assert lease.state is LeaseState.ARMED

    clock.advance(10)
    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.SCHEDULED  # 230s out again


def test_failed_normal_request_never_touches_cache():
    clock = FakeClock()
    manager = _default_manager(clock)
    lease = _armed_touched_lease(manager, clock)
    touch_before = lease.last_cache_touch_at
    deadline_before = manager.next_deadline(lease)

    clock.advance(50)
    manager.before_normal_request(lease.lease_id)
    manager.after_normal_request(lease.lease_id, Outcome.FAILED)

    # §148: a failed provider call is NOT a cache refresh (invariant 3).
    assert lease.last_cache_touch_at == touch_before
    assert manager.next_deadline(lease) == deadline_before
    assert lease.state is LeaseState.ARMED  # unchanged


def test_confirmed_hit_records_last_confirmed_hit_at():
    clock = FakeClock()
    manager = _default_manager(clock)
    lease = _make_lease(manager)
    manager.target_started(lease.lease_id, "t1")
    manager.before_normal_request(lease.lease_id)
    manager.after_normal_request(lease.lease_id, Outcome.CONFIRMED_HIT)
    assert lease.last_confirmed_hit_at == clock.now
    assert lease.last_cache_touch_at == clock.now


# -- warm-vs-real races (§51) ------------------------------------------------


def test_real_request_in_flight_during_warm_skips_busy():
    clock = FakeClock()
    manager = _default_manager(clock)
    lease = _armed_touched_lease(manager, clock)
    clock.advance(1000)  # far past the deadline → warm would fire

    async def scenario():
        lock = manager.lock_for(lease.cache_fingerprint)
        await lock.acquire()
        task = asyncio.create_task(manager.evaluate_lease(lease.lease_id))
        await asyncio.sleep(0)  # let the warm reach the lock wait
        manager.before_normal_request(lease.lease_id)  # real request wins
        lock.release()
        return await task

    decision = asyncio.run(scenario())
    assert decision is LeaseDecision.SKIPPED_BUSY
    assert lease.last_cache_touch_at == 1_000_000.0  # dry-run never touched cache


def test_completed_real_request_during_warm_skips_stale():
    clock = FakeClock()
    manager = _default_manager(clock)
    lease = _armed_touched_lease(manager, clock)
    clock.advance(1000)

    async def scenario():
        lock = manager.lock_for(lease.cache_fingerprint)
        await lock.acquire()
        task = asyncio.create_task(manager.evaluate_lease(lease.lease_id))
        await asyncio.sleep(0)
        # A real request runs and COMPLETES while the warm waits: generation
        # bumped, but no request is in flight anymore.
        manager.before_normal_request(lease.lease_id)
        manager.after_normal_request(lease.lease_id, Outcome.CONFIRMED_HIT)
        lock.release()
        return await task

    decision = asyncio.run(scenario())
    assert decision is LeaseDecision.SKIPPED_STALE


def test_warm_vs_complete_race_target_finished_skips_no_targets():
    clock = FakeClock()
    manager = _default_manager(clock)
    lease = _armed_touched_lease(manager, clock)
    clock.advance(1000)

    async def scenario():
        lock = manager.lock_for(lease.cache_fingerprint)
        await lock.acquire()
        task = asyncio.create_task(manager.evaluate_lease(lease.lease_id))
        await asyncio.sleep(0)
        manager.target_finished(lease.lease_id, "t1")  # last target finishes
        lock.release()
        decision = await task
        return decision

    decision = asyncio.run(scenario())
    assert decision is LeaseDecision.SKIPPED_NO_TARGETS
    assert lease.state is LeaseState.INACTIVE


# -- lease ownership (§21) ---------------------------------------------------


def test_model_switch_invalidates_old_lease_and_creates_independent_one():
    clock = FakeClock()
    manager = _default_manager(clock)
    old = _make_lease(manager, model="gpt-5.2", cache_fp="cache-fp-A")
    manager.target_started(old.lease_id, "t1")
    manager.before_normal_request(old.lease_id)
    manager.after_normal_request(old.lease_id, Outcome.CONFIRMED_HIT)
    assert old.state is LeaseState.ARMED
    touch_before_switch = old.last_cache_touch_at

    # Model switch: same session + provider, different cache identity.
    new = _make_lease(manager, model="gpt-5.2-reasoning", cache_fp="cache-fp-B")
    assert new.lease_id != old.lease_id
    assert old.state is LeaseState.INVALIDATED
    assert new.state is LeaseState.INACTIVE
    assert new.generation == 0  # fresh, independent lease

    # The old lease is NEVER refreshed by later traffic (§21).
    clock.advance(50)
    manager.target_started(old.lease_id, "t9")
    manager.before_normal_request(old.lease_id)
    manager.after_normal_request(old.lease_id, Outcome.CONFIRMED_HIT)
    assert old.state is LeaseState.INVALIDATED
    assert old.last_cache_touch_at == touch_before_switch  # not refreshed
    assert old.generation == 2  # counter bumped, but nothing else moved

    # The new lease works independently.
    manager.target_started(new.lease_id, "n1")
    assert new.state is LeaseState.ARMED


def test_multiple_leases_per_session_coexist_by_provider():
    clock = FakeClock()
    manager = _default_manager(clock)
    openrouter = _make_lease(
        manager, provider="openrouter", model="deepseek-chat", cache_fp="fp-or"
    )
    anthropic = _make_lease(
        manager, provider="anthropic", model="claude", cache_fp="fp-an"
    )
    openai = _make_lease(manager, provider="openai", model="gpt", cache_fp="fp-oa")
    assert openrouter.state is LeaseState.INACTIVE
    assert anthropic.state is LeaseState.INACTIVE
    assert openai.state is LeaseState.INACTIVE
    assert len(manager.leases) == 3
    # Same cache identity → same lease reused, no duplicates.
    again = manager.find_or_create_lease(
        session_id="sess-1",
        provider="openrouter",
        model="deepseek-chat",
        api_mode="chat",
        base_url="https://fake-provider.invalid/v1",
        auth_scope_hash="auth-1",
        route_fingerprint=None,
        request_fingerprint="req-fp-1",
        cache_fingerprint="fp-or",
        system_fingerprint="sys-fp-1",
        tools_fingerprint="tools-fp-1",
        history_prefix_fingerprint="hist-fp-1",
    )
    assert again.lease_id == openrouter.lease_id


# -- deadline math (§53) -----------------------------------------------------


def test_deadline_math_exact_values_from_prd():
    clock = FakeClock(start=0.0)
    # PRD §53 example: TTL 300s, 80% = 240s, p95 4s → 2×p95 = 8s,
    # minimum margin 10s → network_margin = 10s → safe = 240s.
    manager = LeaseManager(
        LeaseSettings(
            warm_fraction=0.80,
            minimum_margin_s=10.0,
            latency_multiplier=2.0,
            jitter_fraction=0.0,
        ),
        time_fn=clock,
        latency_p95_s=4.0,
    )
    lease = _make_lease(manager)
    manager.before_normal_request(lease.lease_id)
    manager.after_normal_request(lease.lease_id, Outcome.CONFIRMED_HIT)  # touch at 0

    assert manager.network_margin() == 10.0
    assert manager.next_deadline(lease) == 240.0


def test_deadline_math_network_margin_branch_wins():
    # When the safe window is narrower than the TTL fraction, the margin
    # branch wins: warm_fraction 0.95 → 285s vs ttl - margin (16s) → 284s.
    clock = FakeClock(start=0.0)
    manager = LeaseManager(
        LeaseSettings(
            warm_fraction=0.95,
            minimum_margin_s=10.0,
            latency_multiplier=2.0,
            jitter_fraction=0.0,
        ),
        time_fn=clock,
        latency_p95_s=8.0,  # margin = max(10, 16) = 16
    )
    lease = _make_lease(manager)
    manager.before_normal_request(lease.lease_id)
    manager.after_normal_request(lease.lease_id, Outcome.CONFIRMED_HIT)
    assert manager.network_margin() == 16.0
    assert manager.next_deadline(lease) == 284.0


def test_next_deadline_none_without_cache_touch():
    manager = _default_manager(FakeClock())
    lease = _make_lease(manager)
    assert manager.next_deadline(lease) is None
    manager.target_started(lease.lease_id, "t1")
    # Armed but never touched by a real request → nothing to keep warm.
    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.SKIPPED_UNKNOWN_TTL


# -- jitter (§54) ------------------------------------------------------------


def test_jitter_is_deterministic_per_cache_fingerprint():
    clock = FakeClock(start=0.0)
    manager = LeaseManager(
        LeaseSettings(jitter_fraction=0.03),
        time_fn=clock,
        latency_p95_s=4.0,
    )
    lease_a = _make_lease(manager, session_id="sess-a", cache_fp="fp-aaaa", model="m-a")
    lease_b = _make_lease(manager, session_id="sess-b", cache_fp="fp-bbbb", model="m-b")
    for lease in (lease_a, lease_b):
        manager.before_normal_request(lease.lease_id)
        manager.after_normal_request(lease.lease_id, Outcome.CONFIRMED_HIT)

    deadline_a1 = manager.next_deadline(lease_a)
    deadline_a2 = manager.next_deadline(lease_a)
    deadline_b = manager.next_deadline(lease_b)

    assert deadline_a1 == deadline_a2  # same fingerprint → same deadline
    assert deadline_a1 != deadline_b  # different fingerprint → different jitter
    # ±3% of the unjittered 240s deadline
    assert 240.0 * 0.97 <= deadline_a1 <= 240.0 * 1.03
    assert 240.0 * 0.97 <= deadline_b <= 240.0 * 1.03


def test_zero_jitter_fraction_gives_exact_deadline():
    clock = FakeClock(start=0.0)
    manager = _default_manager(clock)
    lease = _make_lease(manager)
    manager.before_normal_request(lease.lease_id)
    manager.after_normal_request(lease.lease_id, Outcome.CONFIRMED_HIT)
    assert manager.next_deadline(lease) == 240.0


# -- dry-run output (§132) ---------------------------------------------------


def test_dry_run_scheduled_output_contains_would_warm_in(caplog):
    clock = FakeClock(start=1_000_000.0)
    manager = _default_manager(clock)
    lease = _armed_touched_lease(manager, clock)  # touch at t0, deadline t0+240
    clock.advance(193)  # 47s before the deadline
    with caplog.at_level("INFO", logger="cachepilot_core.leases"):
        decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.SCHEDULED
    assert lease.state is LeaseState.WARM_SCHEDULED
    assert any("WOULD WARM IN 47s" in record.message for record in caplog.records)
    # Dry-run scheduling never advances the cache touch.
    assert lease.last_cache_touch_at == 1_000_000.0


def test_dry_run_due_output_contains_would_warm_now_and_never_touches(caplog):
    clock = FakeClock(start=1_000_000.0)
    manager = _default_manager(clock)
    lease = _armed_touched_lease(manager, clock)
    clock.advance(1000)  # past the deadline → warm would fire
    with caplog.at_level("INFO", logger="cachepilot_core.leases"):
        decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.SKIPPED_DRY_RUN
    assert any("WOULD WARM NOW" in record.message for record in caplog.records)
    # Invariant 3: last_cache_touch advances ONLY via a real request/warm.
    assert lease.last_cache_touch_at == 1_000_000.0


def _stub_decision(action: WarmAction, **overrides) -> WarmDecision:
    """A fully-populated WarmDecision (PRD §145 shape) for stub controllers."""
    fields: dict[str, object] = {
        "action": action,
        "reason": "stub",
        "resume_probability": 0.5,
        "expected_avoidable_loss": Decimal("0.1"),
        "expected_value": Decimal("0.05"),
        "next_warm_cost": Decimal("0.01"),
        "cumulative_warm_cost": Decimal(0),
        "max_warm_budget": Decimal("0.035"),
        "remaining_budget": Decimal("0.035"),
        "safety_margin": Decimal(0),
    }
    fields.update(overrides)
    return WarmDecision(**fields)


class _StubController(EconomicController):
    """Deterministic controller stub: records every evaluation, returns a
    fixed decision (PRD §103 'inject a stub controller for determinism')."""

    def __init__(self, decision: WarmDecision) -> None:
        super().__init__()
        self.decision = decision
        self.calls: list[dict[str, object]] = []

    def evaluate(self, **kwargs: object) -> WarmDecision:
        self.calls.append(kwargs)
        return self.decision


# -- economics: P07 (PRD §60-65, §103, §134, §145) ---------------------------


def test_economic_gate_consults_the_controller_and_records_decision():
    """P07: the gate is no longer a pass-through placeholder — it returns
    exactly the controller's should_warm (PRD §134) and records the decision."""
    clock = FakeClock()
    manager = _default_manager(clock)
    lease = _make_lease(manager)
    manager.target_started(lease.lease_id, "bg-1")  # §63: R = 0.95
    manager.update_cost_estimates(lease.lease_id, USAGE)

    assert manager.economic_gate(lease) is True  # cheap warm → WARM
    assert lease.last_warm_decision is not None
    assert lease.last_warm_decision.action is WarmAction.WARM
    assert lease.last_warm_decision.reason == "due_and_economically_positive"


def test_economic_gate_refuses_with_unknown_pricing():
    """PRD §65: unknown pricing → the gate refuses and no savings are claimed."""
    clock = FakeClock()
    manager = LeaseManager(LeaseSettings(jitter_fraction=0.0), time_fn=clock, latency_p95_s=4.0)
    lease = _make_lease(manager)
    manager.update_cost_estimates(lease.lease_id, USAGE)

    assert lease.estimated_cold_resume_cost_usd is None
    assert lease.estimated_cached_resume_cost_usd is None
    assert manager.economic_gate(lease) is False
    assert lease.last_warm_decision is not None
    assert lease.last_warm_decision.action is WarmAction.SKIP_UNKNOWN_PRICING


def test_p07_one_cheap_warm_executes():
    """PRD §103 'One cheap warm': warm cost < avoidable miss → WARMED_*."""
    clock = FakeClock()
    executor = _RecordingExecutor()
    manager = _real_warm_manager(clock, executor)
    lease = _armed_touched_lease(manager, clock)
    _store_snapshot(manager, lease)
    clock.advance(1000)  # far past the deadline → due → gate → warm

    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.WARMED_CONFIRMED_HIT
    assert len(executor.calls) == 1  # the warm executed
    assert lease.warm_count == 1
    assert lease.warm_cost_usd == 0.001  # visible (invariant 4)
    assert lease.last_warm_decision is not None
    assert lease.last_warm_decision.action is WarmAction.WARM


def test_p07_exhausted_budget_stops_economic_and_lets_cache_expire():
    """PRD §103 'Too many warms' / §62: cumulative warm cost + next warm >
    budget → ECONOMIC_STOP, cache allowed to expire (no warm ever runs)."""
    clock = FakeClock()
    manager = _default_manager(clock)
    lease = _armed_touched_lease(manager, clock)
    # Budget = 0.70 * (0.95 * (0.00352 - 0.00032)) = 0.002128; spending
    # 0.0042 leaves nothing — the ~$0.0003 next warm no longer fits.
    lease.warm_cost_usd = 0.0042
    lease.warm_count = 4
    touch_before = lease.last_cache_touch_at
    clock.advance(1000)

    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.STOPPED_ECONOMIC
    assert lease.state is LeaseState.ECONOMIC_STOP
    assert lease.last_warm_decision is not None
    assert lease.last_warm_decision.action is WarmAction.ECONOMIC_STOP
    # The cache is never touched by a warm and the decision is stable.
    assert lease.last_cache_touch_at == touch_before
    assert asyncio.run(manager.evaluate_lease(lease.lease_id)) is LeaseDecision.STOPPED_ECONOMIC


def test_p07_unknown_pricing_never_warms():
    """PRD §103 'Unknown pricing': economically unbounded warming disabled —
    even a fully warmable manager (executor + snapshot) never sends a warm."""
    clock = FakeClock()
    executor = _RecordingExecutor()
    manager = LeaseManager(
        LeaseSettings(jitter_fraction=0.0, dry_run=False),
        time_fn=clock,
        latency_p95_s=4.0,
        snapshot_store=SnapshotStore(),
        warm_executor=executor,
    )
    lease = _make_lease(manager)
    manager.target_started(lease.lease_id, "t1")
    manager.before_normal_request(lease.lease_id)
    manager.after_normal_request(lease.lease_id, Outcome.CONFIRMED_HIT)
    manager.update_cost_estimates(lease.lease_id, USAGE)
    assert lease.estimated_cold_resume_cost_usd is None  # never claimed
    _store_snapshot(manager, lease)
    clock.advance(1000)

    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.STOPPED_ECONOMIC
    assert lease.state is LeaseState.ECONOMIC_STOP
    assert executor.calls == []  # nothing was sent
    assert lease.warm_count == 0  # no warm, no savings claim
    assert lease.last_warm_decision is not None
    assert lease.last_warm_decision.action is WarmAction.SKIP_UNKNOWN_PRICING


def test_p07_zero_resume_probability_never_warms():
    """PRD §103 'Zero probability of continuation': never warm."""
    clock = FakeClock()
    manager = _default_manager(clock, detached_resume_probability=0.0)
    lease = _armed_touched_lease(manager, clock)
    # Re-arm with an explicitly detached target → PRD §63 R = 0.
    manager.target_finished(lease.lease_id, "t1")
    manager.target_started(lease.lease_id, "detached-t1")
    assert manager.resume_probability(lease) == 0.0
    clock.advance(1000)

    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.STOPPED_ECONOMIC
    assert lease.last_warm_decision is not None
    assert lease.last_warm_decision.action is WarmAction.SKIP_NO_CONTINUATION


def test_resume_probability_heuristic_prd63():
    """PRD §63 P0: no targets → 0; detached → low (0.20); background
    target with notify_on_complete → high (0.95)."""
    clock = FakeClock()
    manager = _default_manager(clock)
    lease = _make_lease(manager)
    assert manager.resume_probability(lease) == 0.0  # no continuation possible

    manager.target_started(lease.lease_id, "bg-1")
    assert manager.resume_probability(lease) == 0.95  # notify_on_complete

    manager.target_started(lease.lease_id, "detached-d1")
    # A mix of detached and notify targets → not all detached → high.
    assert manager.resume_probability(lease) == 0.95

    manager.target_finished(lease.lease_id, "bg-1")
    assert manager.resume_probability(lease) == 0.20  # all detached → low


def test_p07_economic_gate_consults_injected_controller():
    """PRD §103: inject a stub controller — the gate returns its decision and
    passes it the lease-derived economic inputs (PRD §146 economics step)."""
    clock = FakeClock()
    stop = _stub_decision(WarmAction.ECONOMIC_STOP)
    stub = _StubController(stop)
    manager = LeaseManager(
        LeaseSettings(jitter_fraction=0.0, pricing=PRICING),
        time_fn=clock,
        latency_p95_s=4.0,
        economic_controller=stub,
    )
    lease = _make_lease(manager)
    manager.target_started(lease.lease_id, "bg-1")  # §63: R = 0.95
    manager.update_cost_estimates(lease.lease_id, USAGE)

    assert manager.economic_gate(lease) is False  # stub says stop → no warm
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["cold_resume_cost"] == lease.estimated_cold_resume_cost_usd
    assert call["cached_resume_cost"] == lease.estimated_cached_resume_cost_usd
    assert call["next_warm_cost"] == lease.next_warm_cost_usd
    assert call["cumulative_warm_cost"] == lease.warm_cost_usd
    assert call["resume_probability"] == 0.95
    assert call["pricing_known"] is True

    # A WARM decision from the stub passes the gate; the decision is recorded.
    stub.decision = _stub_decision(WarmAction.WARM, reason="due_and_economically_positive")
    assert manager.economic_gate(lease) is True
    assert lease.last_warm_decision is stub.decision  # PRD §145


def test_p07_normal_request_is_a_free_heartbeat():
    """PRD §64: a natural request refreshes the cache and the cost estimates
    WITHOUT consuming any warm budget (free heartbeat)."""
    clock = FakeClock()
    manager = _default_manager(clock)
    lease = _armed_touched_lease(manager, clock)
    lease.warm_count = 3
    lease.warm_cost_usd = 0.003  # budget already partly spent by warms
    clock.advance(60)

    manager.before_normal_request(lease.lease_id)
    manager.after_normal_request(lease.lease_id, Outcome.CONFIRMED_HIT)
    manager.update_cost_estimates(lease.lease_id, USAGE)

    assert lease.last_cache_touch_at == clock.now  # cache refreshed for free
    assert lease.warm_count == 3  # no warm ran, no budget consumed
    assert lease.warm_cost_usd == 0.003
    assert lease.estimated_cold_resume_cost_usd == 0.00352  # re-derived
    assert lease.state is LeaseState.ARMED


def test_p07_warm_decision_recorded_for_due_evaluation(caplog):
    """PRD §145: every due evaluation records the explainable WarmDecision
    (on the lease AND in a log line) with the full economic breakdown."""
    clock = FakeClock()
    manager = _default_manager(clock)
    lease = _armed_touched_lease(manager, clock)
    clock.advance(1000)

    with caplog.at_level("INFO", logger="cachepilot_core.leases"):
        decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.SKIPPED_DRY_RUN  # gate passed, dry run

    recorded = lease.last_warm_decision
    assert recorded is not None
    assert recorded.action is WarmAction.WARM
    assert recorded.reason == "due_and_economically_positive"
    assert recorded.expected_avoidable_loss == Decimal("0.0032")  # 0.00352 - 0.00032
    assert recorded.expected_value == Decimal("0.00304")  # 0.95 * 0.0032
    assert recorded.max_warm_budget == Decimal("0.002128")  # 0.70 * EV
    assert recorded.remaining_budget == Decimal("0.002128")
    assert recorded.next_warm_cost == Decimal("0.0003224")
    assert recorded.cumulative_warm_cost == Decimal(0)
    assert any("warm decision warm" in record.message for record in caplog.records)


def test_p07_disabled_economics_restores_watchdog_gate():
    """PRD §84 cache.economics.enabled=false: the operator explicitly opts
    back into watchdog behaviour — the gate always passes and records nothing."""
    clock = FakeClock()
    manager = _default_manager(clock, economics=EconomicConfig(enabled=False))
    lease = _make_lease(manager)  # no cost data at all
    assert manager.economic_gate(lease) is True
    assert lease.last_warm_decision is None  # no decision was made


# -- locking (§52) -----------------------------------------------------------


def test_per_cache_identity_locks_are_never_global():
    manager = _default_manager(FakeClock())
    lock_a1 = manager.lock_for("fp-a")
    lock_a2 = manager.lock_for("fp-a")
    lock_b = manager.lock_for("fp-b")
    assert lock_a1 is lock_a2  # same cache identity → same lock
    assert lock_a1 is not lock_b  # different identity → independent lock


# -- invalidation guards -----------------------------------------------------


def test_arm_is_noop_after_invalidate():
    clock = FakeClock()
    manager = _default_manager(clock)
    lease = _armed_touched_lease(manager, clock)
    manager.invalidate(lease.lease_id)
    assert lease.state is LeaseState.INVALIDATED
    manager.arm(lease.lease_id)
    manager.target_started(lease.lease_id, "t2")
    assert lease.state is LeaseState.INVALIDATED
    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.STOPPED_INVALIDATED


# -- settings ----------------------------------------------------------------


def test_lease_settings_from_env(monkeypatch):
    monkeypatch.setenv("CACHEPILOT_LEASE_WARM_FRACTION", "0.9")
    monkeypatch.setenv("CACHEPILOT_LEASE_MINIMUM_MARGIN_S", "15")
    monkeypatch.setenv("CACHEPILOT_LEASE_LATENCY_MULTIPLIER", "3.0")
    monkeypatch.setenv("CACHEPILOT_LEASE_JITTER_FRACTION", "0.01")
    monkeypatch.setenv("CACHEPILOT_LEASE_DEFAULT_TTL_S", "600")
    monkeypatch.setenv("CACHEPILOT_LEASE_SCHEDULER_INTERVAL_S", "2")
    monkeypatch.setenv("CACHEPILOT_LEASE_DRY_RUN", "false")
    settings = LeaseSettings.from_env()
    assert settings.warm_fraction == 0.9
    assert settings.minimum_margin_s == 15.0
    assert settings.latency_multiplier == 3.0
    assert settings.jitter_fraction == 0.01
    assert settings.default_ttl_s == 600.0
    assert settings.scheduler_interval_s == 2.0
    assert settings.dry_run is False


def test_lease_settings_from_env_falls_back_on_malformed(monkeypatch):
    monkeypatch.setenv("CACHEPILOT_LEASE_WARM_FRACTION", "not-a-number")
    monkeypatch.setenv("CACHEPILOT_LEASE_DRY_RUN", "nope")
    settings = LeaseSettings.from_env()
    assert settings.warm_fraction == 0.80
    assert settings.dry_run is True


def test_lease_settings_defaults():
    settings = LeaseSettings()
    assert settings.warm_fraction == 0.80
    assert settings.minimum_margin_s == 10.0
    assert settings.latency_multiplier == 2.0
    assert settings.jitter_fraction == 0.03
    assert settings.default_ttl_s == 300.0
    assert settings.scheduler_interval_s == 1.0
    assert settings.dry_run is True

    # Default settings → PRD §53/§54: safe deadline 240s ±3% jitter.
    clock = FakeClock(start=0.0)
    manager = LeaseManager(time_fn=clock, latency_p95_s=4.0)
    lease = _make_lease(manager)
    manager.before_normal_request(lease.lease_id)
    manager.after_normal_request(lease.lease_id, Outcome.CONFIRMED_HIT)
    deadline = manager.next_deadline(lease)
    assert deadline is not None
    assert 240.0 * 0.97 <= deadline <= 240.0 * 1.03


def test_lease_settings_economics_from_env(monkeypatch):
    """PRD §84 cache.economics tunables via CACHEPILOT_ECONOMICS_* + the
    configured pricing snapshot via CACHEPILOT_PRICING_* (PRD §65/§66)."""
    monkeypatch.setenv("CACHEPILOT_ECONOMICS_ENABLED", "false")
    monkeypatch.setenv("CACHEPILOT_ECONOMICS_BUDGET_RATIO", "0.5")
    monkeypatch.setenv("CACHEPILOT_ECONOMICS_MINIMUM_EXPECTED_SAVINGS_USD", "0.01")
    monkeypatch.setenv("CACHEPILOT_ECONOMICS_RESUME_PROBABILITY", "0.8")
    monkeypatch.setenv("CACHEPILOT_ECONOMICS_DETACHED_RESUME_PROBABILITY", "0.1")
    monkeypatch.setenv("CACHEPILOT_PRICING_INPUT_PER_MTK", "1.0")
    monkeypatch.setenv("CACHEPILOT_PRICING_OUTPUT_PER_MTK", "2.0")
    monkeypatch.setenv("CACHEPILOT_PRICING_CACHE_READ_PER_MTK", "0.1")
    monkeypatch.setenv("CACHEPILOT_PRICING_CACHE_WRITE_PER_MTK", "1.1")
    settings = LeaseSettings.from_env()
    assert settings.economics.enabled is False
    assert settings.economics.budget_ratio == Decimal("0.5")
    assert settings.economics.minimum_expected_savings == Decimal("0.01")
    assert settings.resume_probability == 0.8
    assert settings.detached_resume_probability == 0.1
    assert settings.pricing is not None
    assert settings.pricing.input_per_mtok == Decimal("1.0")
    assert settings.pricing.cache_read_per_mtok == Decimal("0.1")
    # Defaults stay intact when nothing is set (dry-run + economics on).
    defaults = LeaseSettings.from_env({})
    assert defaults.dry_run is True
    assert defaults.economics.enabled is True
    assert defaults.economics.budget_ratio == Decimal("0.70")
    assert defaults.resume_probability == 0.95
    assert defaults.detached_resume_probability == 0.20
    assert defaults.pricing is None


def test_lease_settings_economics_env_falls_back_on_malformed(monkeypatch):
    """A bad or out-of-range variable never breaks the relay (fail open)."""
    monkeypatch.setenv("CACHEPILOT_ECONOMICS_BUDGET_RATIO", "2.0")  # > 1 → default
    monkeypatch.setenv("CACHEPILOT_ECONOMICS_MINIMUM_EXPECTED_SAVINGS_USD", "-5")
    monkeypatch.setenv("CACHEPILOT_ECONOMICS_RESUME_PROBABILITY", "nope")
    monkeypatch.setenv("CACHEPILOT_ECONOMICS_DETACHED_RESUME_PROBABILITY", "2.5")
    monkeypatch.setenv("CACHEPILOT_PRICING_INPUT_PER_MTK", "1.0")  # partial → None
    settings = LeaseSettings.from_env()
    assert settings.economics.budget_ratio == Decimal("0.70")
    assert settings.economics.minimum_expected_savings == Decimal("0.0")
    assert settings.resume_probability == 0.95
    assert settings.detached_resume_probability == 0.20
    assert settings.pricing is None


# -- real-warm path races (Phase 6, PRD §51) ---------------------------------


class _RecordingExecutor:
    """Stub warm executor that records snapshots (never really sends)."""

    def __init__(self) -> None:
        self.calls: list[RequestSnapshot] = []

    async def execute(self, snapshot: RequestSnapshot) -> WarmResult:
        self.calls.append(snapshot)
        return WarmResult(
            outcome=Outcome.CONFIRMED_HIT,
            usage=TokenUsage(prompt_tokens=4000, cache_read_tokens=4000),
            cost_usd=Decimal("0.001"),
        )


def _real_warm_manager(clock: FakeClock, executor: _RecordingExecutor) -> LeaseManager:
    return LeaseManager(
        LeaseSettings(jitter_fraction=0.0, dry_run=False, pricing=PRICING),
        time_fn=clock,
        latency_p95_s=4.0,
        snapshot_store=SnapshotStore(),
        warm_executor=executor,
    )


def _store_snapshot(manager: LeaseManager, lease: CacheLease) -> None:
    assert manager.snapshot_store is not None
    manager.snapshot_store.store(
        RequestSnapshot(
            cache_fingerprint=lease.cache_fingerprint,
            body={
                "model": lease.model,
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 512,
            },
            upstream_url="https://fake-provider.invalid/v1/chat/completions",
            stored_at=0.0,
        )
    )


def test_real_warm_path_race_real_request_skips_busy():
    clock = FakeClock()
    executor = _RecordingExecutor()
    manager = _real_warm_manager(clock, executor)
    lease = _armed_touched_lease(manager, clock)
    _store_snapshot(manager, lease)
    clock.advance(1000)  # far past the deadline → the warm would fire

    async def scenario():
        lock = manager.lock_for(lease.cache_fingerprint)
        await lock.acquire()
        task = asyncio.create_task(manager.evaluate_lease(lease.lease_id))
        await asyncio.sleep(0)  # let the warm reach the lock wait
        manager.before_normal_request(lease.lease_id)  # real request wins
        lock.release()
        return await task

    decision = asyncio.run(scenario())
    assert decision is LeaseDecision.SKIPPED_BUSY
    assert executor.calls == []  # the real warm never fired
    assert lease.last_cache_touch_at == 1_000_000.0


def test_real_warm_path_race_completed_request_skips_stale():
    clock = FakeClock()
    executor = _RecordingExecutor()
    manager = _real_warm_manager(clock, executor)
    lease = _armed_touched_lease(manager, clock)
    _store_snapshot(manager, lease)
    clock.advance(1000)

    async def scenario():
        lock = manager.lock_for(lease.cache_fingerprint)
        await lock.acquire()
        task = asyncio.create_task(manager.evaluate_lease(lease.lease_id))
        await asyncio.sleep(0)
        # A real request runs and COMPLETES while the warm waits: generation
        # bumped, no request in flight anymore → SKIPPED_STALE (§51).
        manager.before_normal_request(lease.lease_id)
        manager.after_normal_request(lease.lease_id, Outcome.CONFIRMED_HIT)
        lock.release()
        return await task

    decision = asyncio.run(scenario())
    assert decision is LeaseDecision.SKIPPED_STALE
    assert executor.calls == []


def test_real_warm_path_race_targets_finished_skips_no_targets():
    clock = FakeClock()
    executor = _RecordingExecutor()
    manager = _real_warm_manager(clock, executor)
    lease = _armed_touched_lease(manager, clock)
    _store_snapshot(manager, lease)
    clock.advance(1000)

    async def scenario():
        lock = manager.lock_for(lease.cache_fingerprint)
        await lock.acquire()
        task = asyncio.create_task(manager.evaluate_lease(lease.lease_id))
        await asyncio.sleep(0)
        manager.target_finished(lease.lease_id, "t1")  # last target finishes
        lock.release()
        return await task

    decision = asyncio.run(scenario())
    assert decision is LeaseDecision.SKIPPED_NO_TARGETS
    assert lease.state is LeaseState.INACTIVE
    assert executor.calls == []
