"""Real warm executor tests — PRD §30-33, §68-70, §94, §147 (Phase 6).

Two layers:

- manager-level outcome semantics with stub executors: CONFIRMED_HIT
  refreshes, SUCCESS_UNVERIFIED / FAILED never refresh, MISS_REBUILT
  refreshes only with write-telemetry evidence, the §94 circuit breaker,
  non-warmable fresh managers, warm-vs-real / warm-vs-complete /
  model-switch races (nothing is ever sent on a skip);
- a fake-provider integration (never "HTTP 200 = success"): the real
  OpenAI-compatible adapter drives the deterministic FakeProvider cache
  simulator — bounded replay (max_tokens=1) reaches the cache, generated
  content is discarded, usage is parsed into warm_count/warm_cost_usd.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from cachepilot_core.adapters import OpenAICompatibleAdapter, WarmResult
from cachepilot_core.fake_provider import (
    FakeProvider,
    FakeProviderConfig,
    provider_result_to_http_response,
)
from cachepilot_core.identity import ApiMode, CanonicalRequest
from cachepilot_core.leases import (
    CacheLease,
    LeaseDecision,
    LeaseManager,
    LeaseSettings,
    LeaseState,
)
from cachepilot_core.pricing import PricingTable, estimate_cost
from cachepilot_core.snapshots import RequestSnapshot, SnapshotStore
from cachepilot_core.telemetry import Outcome
from cachepilot_core.usage import TokenUsage

#: PRD §62-shaped pricing (matches the FakeProvider defaults): a 4000-token
#: prefix costs $0.00352 cold / $0.00032 cached — warm economics positive.
PRICING = PricingTable(
    input_per_mtok=Decimal("0.80"),
    output_per_mtok=Decimal("2.40"),
    cache_read_per_mtok=Decimal("0.08"),
    cache_write_per_mtok=Decimal("0.88"),
)

#: Normal-request usage for a 4000-token prefix (mirrors the fake provider).
_USAGE = TokenUsage(prompt_tokens=4000, cache_read_tokens=4000)


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
    model: str = "gpt-5.2",
    cache_fp: str = "cache-fp-1",
) -> CacheLease:
    return manager.find_or_create_lease(
        session_id=session_id,
        provider="fake-provider",
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
    manager.update_cost_estimates(lease.lease_id, _USAGE)
    assert lease.state.name == "ARMED"
    return lease


def _manager(
    clock: FakeClock,
    *,
    dry_run: bool = False,
    snapshot_store: SnapshotStore | None = None,
    warm_executor=None,
    **settings,
) -> LeaseManager:
    return LeaseManager(
        LeaseSettings(jitter_fraction=0.0, dry_run=dry_run, pricing=PRICING, **settings),
        time_fn=clock,
        latency_p95_s=4.0,
        snapshot_store=snapshot_store,
        warm_executor=warm_executor,
    )


def _snapshot(lease: CacheLease, body: dict | None = None) -> RequestSnapshot:
    return RequestSnapshot(
        cache_fingerprint=lease.cache_fingerprint,
        body=body
        or {
            "model": lease.model,
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 512,
        },
        upstream_url="https://fake-provider.invalid/v1/chat/completions",
        stored_at=0.0,
    )


class StubExecutor:
    """Fixed-result executor that records every snapshot it was given."""

    def __init__(self, result: WarmResult) -> None:
        self.result = result
        self.calls: list[RequestSnapshot] = []

    async def execute(self, snapshot: RequestSnapshot) -> WarmResult:
        self.calls.append(snapshot)
        return self.result


class FakeProviderWarmExecutor:
    """Real warm path against the deterministic FakeProvider cache simulator.

    Uses the REAL OpenAI-compatible adapter to build the bounded warm from
    the snapshot, completes it against the fake provider through the same
    HTTP-response interface the relay uses (``provider_result_to_http_response``),
    parses usage and classifies honestly. Records every body actually sent
    so tests can prove the replay was bounded.
    """

    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider
        self.adapter = OpenAICompatibleAdapter()
        self.sent_bodies: list[dict[str, Any]] = []
        self.last_result: WarmResult | None = None

    async def execute(self, snapshot: RequestSnapshot) -> WarmResult:
        body = self.adapter.build_warm_request(snapshot.body)
        if body is None:
            return WarmResult(outcome=None, usage=TokenUsage(), cost_usd=Decimal(0))
        self.sent_bodies.append(dict(body))
        result = self.provider.complete(self._canonical_from(body))
        response = provider_result_to_http_response(result)
        usage = self.adapter.parse_usage(response)
        outcome = self.adapter.classify_cache_result(usage, response)
        cost = usage.cost if usage.cost is not None else estimate_cost(usage, self.provider.config.pricing)
        self.last_result = WarmResult(outcome=outcome, usage=usage, cost_usd=cost)
        return self.last_result

    @staticmethod
    def _canonical_from(body: Mapping[str, Any]) -> CanonicalRequest:
        """Same body→identity derivation the relay's fake upstream uses."""
        messages = body.get("messages") or []
        return CanonicalRequest.from_content(
            provider="fake-provider",
            model=body.get("model") or "unknown",
            api_mode=ApiMode.CHAT,
            endpoint="https://fake-provider.invalid/v1",
            auth_scope="test-scope",
            prompt_prefix=json.dumps(messages, sort_keys=True),
            system="system prompt",
            max_tokens=body.get("max_tokens"),
            stream=bool(body.get("stream", False)),
        )


# -- fake-provider integration (acceptance D) --------------------------------


def test_warm_bounded_replay_reaches_fake_cache_and_discards_content():
    clock = FakeClock()
    provider = FakeProvider(FakeProviderConfig(seed=7, completion_tokens=42, ttl_s=10_000.0))
    executor = FakeProviderWarmExecutor(provider)
    store = SnapshotStore()
    manager = _manager(clock, snapshot_store=store, warm_executor=executor)
    lease = _armed_touched_lease(manager, clock)  # touch at t0 (generation 1)
    store.store(_snapshot(lease))

    clock.advance(1000)  # far past the deadline → the warm fires
    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.WARMED_MISS_REBUILT

    # Bounded replay: EXACTLY one request, output-bound to max_tokens=1,
    # everything else byte-equivalent to the snapshot.
    assert len(executor.sent_bodies) == 1
    assert executor.sent_bodies[0]["max_tokens"] == 1
    assert executor.sent_bodies[0]["messages"] == _snapshot(lease).body["messages"]
    assert executor.sent_bodies[0]["model"] == "gpt-5.2"

    # The warm's usage is visible (invariant 4): warm_count + warm_cost_usd.
    assert lease.warm_count == 1
    assert lease.warm_cost_usd > 0
    # The cache was genuinely rebuilt by THIS warm (write telemetry) → touch.
    assert lease.last_cache_touch_at == clock.now

    # Generated content is discarded: the result carries only usage/outcome/
    # cost, and no lease field ever received generated text.
    assert executor.last_result is not None
    assert "fake completion" not in str(vars(lease))

    # P07 economics: the warm MISSED and paid the full cache-write price
    # (≈ the cold resume cost) — that alone exhausts the warm budget
    # (0.70 × R × avoidable < cold resume by construction), so the lease
    # stops warming even though the rebuilt entry is still alive. Warming
    # is economic, never a watchdog (AGENTS.md invariant 5).
    clock.advance(1000)
    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.STOPPED_ECONOMIC
    assert lease.state is LeaseState.ECONOMIC_STOP
    assert lease.warm_count == 1
    assert len(executor.sent_bodies) == 1  # nothing more was ever sent


# -- §50 WARMING: the one state only an in-flight warm can hold --------------


def test_lease_is_warming_while_the_warm_is_in_flight():
    """§50/§78: `economics positive → WARMING`, and the CLI must be able to show it.

    ``WARMING`` is a resting state PRD §78's ``cachepilot leases`` example
    renders, but nothing ever assigned it — the lease stayed WARM_SCHEDULED
    through the whole request, so the state machine could not distinguish
    "a warm is scheduled" from "a warm is on the wire right now".
    """
    clock = FakeClock()
    observed: list[LeaseState] = []

    class _StateWatchingExecutor:
        async def execute(self, snapshot):
            observed.append(lease.state)
            return WarmResult(
                outcome=Outcome.CONFIRMED_HIT,
                usage=TokenUsage(prompt_tokens=4000, cache_read_tokens=4000),
                cost_usd=Decimal("0.001"),
            )

    store = SnapshotStore()
    manager = _manager(clock, snapshot_store=store, warm_executor=_StateWatchingExecutor())
    lease = _armed_touched_lease(manager, clock)
    store.store(_snapshot(lease))
    clock.advance(1000)

    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.WARMED_CONFIRMED_HIT
    assert observed == [LeaseState.WARMING]
    # §50 `→ ARMED`: the warm's own outcome travels in the decision, so the
    # lease returns to its resting state rather than parking on the outcome.
    assert lease.state is LeaseState.ARMED


def test_warming_resolves_back_even_when_the_warm_fails():
    clock = FakeClock()
    executor = StubExecutor(
        WarmResult(outcome=Outcome.FAILED, usage=TokenUsage(), cost_usd=Decimal(0))
    )
    store = SnapshotStore()
    manager = _manager(clock, snapshot_store=store, warm_executor=executor)
    lease = _armed_touched_lease(manager, clock)
    store.store(_snapshot(lease))
    clock.advance(1000)

    assert asyncio.run(manager.evaluate_lease(lease.lease_id)) is LeaseDecision.WARMED_FAILED
    # No absorbing FAILED state: repeated failure is the §94 breaker's job.
    assert lease.state is LeaseState.ARMED


def test_lease_state_has_no_unreachable_members():
    """Every declared state must have a producer (or it lies to the CLI)."""
    assert {state.value for state in LeaseState} == {
        "inactive",
        "armed",
        "warm_scheduled",
        "warming",
        "economic_stop",
        "invalidated",
    }


# -- outcome semantics: last_cache_touch_at (invariant 3, PRD §147) ----------


def test_warm_confirm_hit_refreshes_cache_touch():
    clock = FakeClock()
    executor = StubExecutor(
        WarmResult(
            outcome=Outcome.CONFIRMED_HIT,
            usage=TokenUsage(prompt_tokens=4000, cache_read_tokens=4000),
            cost_usd=Decimal("0.001"),
        )
    )
    store = SnapshotStore()
    manager = _manager(clock, snapshot_store=store, warm_executor=executor)
    lease = _armed_touched_lease(manager, clock)
    store.store(_snapshot(lease))
    clock.advance(1000)

    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.WARMED_CONFIRMED_HIT
    assert lease.last_cache_touch_at == clock.now
    assert lease.last_confirmed_hit_at == clock.now
    assert lease.warm_count == 1


def test_warm_success_unverified_never_refreshes():
    clock = FakeClock()
    executor = StubExecutor(
        WarmResult(
            outcome=Outcome.SUCCESS_UNVERIFIED,
            usage=TokenUsage(prompt_tokens=4000),
            cost_usd=Decimal("0.001"),
        )
    )
    store = SnapshotStore()
    manager = _manager(clock, snapshot_store=store, warm_executor=executor)
    lease = _armed_touched_lease(manager, clock)
    store.store(_snapshot(lease))
    touch_before = lease.last_cache_touch_at
    clock.advance(1000)

    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.WARMED_UNVERIFIED
    # §70: only request-completion is known → MUST NOT refresh (invariant 3).
    assert lease.last_cache_touch_at == touch_before
    # …but the warm still cost something and that cost is visible.
    assert lease.warm_count == 1
    assert lease.warm_cost_usd == 0.001


def test_warm_failed_never_refreshes():
    clock = FakeClock()
    executor = StubExecutor(
        WarmResult(outcome=Outcome.FAILED, usage=TokenUsage(), cost_usd=Decimal("0.004"))
    )
    store = SnapshotStore()
    manager = _manager(clock, snapshot_store=store, warm_executor=executor)
    lease = _armed_touched_lease(manager, clock)
    store.store(_snapshot(lease))
    touch_before = lease.last_cache_touch_at
    clock.advance(1000)

    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.WARMED_FAILED
    assert lease.last_cache_touch_at == touch_before
    assert lease.warm_count == 1
    assert lease.warm_cost_usd == 0.004  # never hidden


def test_warm_miss_rebuilt_refreshes_only_with_write_evidence():
    clock = FakeClock()
    # No write telemetry: the outcome says the prefix was NOT read, but does
    # not prove THIS request rebuilt it → no refresh (PRD §147 wording).
    executor = StubExecutor(
        WarmResult(
            outcome=Outcome.MISS_REBUILT,
            usage=TokenUsage(prompt_tokens=4000),
            cost_usd=Decimal("0.0004"),
        )
    )
    store = SnapshotStore()
    manager = _manager(clock, snapshot_store=store, warm_executor=executor)
    lease = _armed_touched_lease(manager, clock)
    store.store(_snapshot(lease))
    touch_before = lease.last_cache_touch_at
    clock.advance(1000)

    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.WARMED_MISS_REBUILT
    assert lease.last_cache_touch_at == touch_before  # NOT refreshed

    # With write telemetry the rebuild is proven → the deadline resets.
    executor.result = WarmResult(
        outcome=Outcome.MISS_REBUILT,
        usage=TokenUsage(prompt_tokens=4000, cache_write_tokens=4000),
        cost_usd=Decimal("0.0004"),
    )
    clock.advance(1000)
    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.WARMED_MISS_REBUILT
    assert lease.last_cache_touch_at == clock.now  # refreshed


# -- warm circuit breaker (PRD §94) ------------------------------------------


def test_warm_circuit_breaker_opens_after_two_misses_and_resets_on_normal_request():
    clock = FakeClock()
    executor = StubExecutor(WarmResult(outcome=Outcome.FAILED, usage=TokenUsage(), cost_usd=Decimal(0)))
    store = SnapshotStore()
    manager = _manager(clock, snapshot_store=store, warm_executor=executor)
    lease = _armed_touched_lease(manager, clock)
    store.store(_snapshot(lease))
    clock.advance(1000)

    assert asyncio.run(manager.evaluate_lease(lease.lease_id)) is LeaseDecision.WARMED_FAILED
    clock.advance(1)
    assert asyncio.run(manager.evaluate_lease(lease.lease_id)) is LeaseDecision.WARMED_FAILED

    # Third evaluation: the circuit is open — no more warm attempts, and the
    # executor is never called again.
    clock.advance(1)
    assert asyncio.run(manager.evaluate_lease(lease.lease_id)) is LeaseDecision.SKIPPED_CIRCUIT_OPEN
    assert len(executor.calls) == 2

    # A normal request produces new cache evidence → circuit reopens (§94).
    manager.before_normal_request(lease.lease_id)
    manager.after_normal_request(lease.lease_id, Outcome.CONFIRMED_HIT)
    clock.advance(1000)
    assert asyncio.run(manager.evaluate_lease(lease.lease_id)) is LeaseDecision.WARMED_FAILED
    assert len(executor.calls) == 3


def test_warm_hit_resets_miss_streak():
    clock = FakeClock()
    executor = StubExecutor(WarmResult(outcome=Outcome.FAILED, usage=TokenUsage(), cost_usd=Decimal(0)))
    store = SnapshotStore()
    manager = _manager(clock, snapshot_store=store, warm_executor=executor)
    lease = _armed_touched_lease(manager, clock)
    store.store(_snapshot(lease))
    clock.advance(1000)

    assert asyncio.run(manager.evaluate_lease(lease.lease_id)) is LeaseDecision.WARMED_FAILED
    # A verified hit resets the streak → the breaker never trips.
    executor.result = WarmResult(
        outcome=Outcome.CONFIRMED_HIT,
        usage=TokenUsage(prompt_tokens=4000, cache_read_tokens=4000),
        cost_usd=Decimal("0.001"),
    )
    assert asyncio.run(manager.evaluate_lease(lease.lease_id)) is LeaseDecision.WARMED_CONFIRMED_HIT
    # The hit refreshed the deadline — jump past it again.
    executor.result = WarmResult(outcome=Outcome.FAILED, usage=TokenUsage(), cost_usd=Decimal(0))
    clock.advance(1000)
    assert asyncio.run(manager.evaluate_lease(lease.lease_id)) is LeaseDecision.WARMED_FAILED
    clock.advance(1)
    assert asyncio.run(manager.evaluate_lease(lease.lease_id)) is LeaseDecision.WARMED_FAILED
    clock.advance(1)
    # Only now, after 2 new misses, does the circuit open.
    assert asyncio.run(manager.evaluate_lease(lease.lease_id)) is LeaseDecision.SKIPPED_CIRCUIT_OPEN


# -- snapshots: non-warmable managers (PRD §30) ------------------------------


def test_fresh_manager_without_snapshot_store_is_non_warmable():
    clock = FakeClock()
    # No snapshot store, no executor — a freshly constructed manager must
    # skip due leases, never crash, never invent a warm. Pricing IS
    # configured so the economic gate passes and the warm path is reached;
    # the missing snapshot store is what makes the lease non-warmable.
    manager = LeaseManager(
        LeaseSettings(jitter_fraction=0.0, dry_run=False, pricing=PRICING),
        time_fn=clock,
        latency_p95_s=4.0,
    )
    lease = _armed_touched_lease(manager, clock)
    clock.advance(1000)
    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.SKIPPED_UNSUPPORTED
    assert lease.warm_count == 0
    assert lease.last_cache_touch_at == 1_000_000.0


def test_missing_snapshot_for_identity_is_non_warmable():
    clock = FakeClock()
    executor = StubExecutor(WarmResult(outcome=Outcome.CONFIRMED_HIT, usage=TokenUsage(), cost_usd=Decimal(0)))
    manager = _manager(clock, snapshot_store=SnapshotStore(), warm_executor=executor)
    lease = _armed_touched_lease(manager, clock)
    # No snapshot stored for this cache identity → non-warmable.
    clock.advance(1000)
    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.SKIPPED_UNSUPPORTED
    assert executor.calls == []


def test_adapter_declined_warm_is_skipped_and_costs_nothing():
    clock = FakeClock()
    executor = StubExecutor(WarmResult(outcome=None))
    store = SnapshotStore()
    manager = _manager(clock, snapshot_store=store, warm_executor=executor)
    lease = _armed_touched_lease(manager, clock)
    store.store(_snapshot(lease))
    touch_before = lease.last_cache_touch_at
    clock.advance(1000)

    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.SKIPPED_UNSUPPORTED
    assert lease.warm_count == 0  # nothing sent, nothing paid for
    assert lease.last_cache_touch_at == touch_before


# -- warm-request-in-flight flag (§51) ---------------------------------------


def test_warm_request_active_flag_set_during_execution_and_cleared_after():
    clock = FakeClock()
    observed: list[bool] = []

    class FlagExecutor:
        def __init__(self, manager: LeaseManager, lease_id: str) -> None:
            self.manager = manager
            self.lease_id = lease_id

        async def execute(self, snapshot: RequestSnapshot) -> WarmResult:
            observed.append(self.manager.is_warming(self.lease_id))
            return WarmResult(
                outcome=Outcome.CONFIRMED_HIT,
                usage=TokenUsage(prompt_tokens=4000, cache_read_tokens=4000),
                cost_usd=Decimal("0.001"),
            )

    store = SnapshotStore()
    manager = _manager(clock, snapshot_store=store)
    lease = _armed_touched_lease(manager, clock)
    manager.warm_executor = FlagExecutor(manager, lease.lease_id)
    store.store(_snapshot(lease))
    clock.advance(1000)

    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.WARMED_CONFIRMED_HIT
    assert observed == [True]  # flag was set for the duration of the warm
    assert manager.is_warming(lease.lease_id) is False  # …and cleared after


def test_second_evaluation_while_warming_skips_already_warming():
    async def scenario() -> None:
        clock = FakeClock()
        entered = asyncio.Event()
        released = asyncio.Event()

        class BlockingExecutor:
            async def execute(self, snapshot: RequestSnapshot) -> WarmResult:
                entered.set()
                await released.wait()
                return WarmResult(
                    outcome=Outcome.CONFIRMED_HIT,
                    usage=TokenUsage(prompt_tokens=4000, cache_read_tokens=4000),
                    cost_usd=Decimal("0.001"),
                )

        store = SnapshotStore()
        manager = _manager(clock, snapshot_store=store, warm_executor=BlockingExecutor())
        lease = _armed_touched_lease(manager, clock)
        store.store(_snapshot(lease))
        clock.advance(1000)

        first = asyncio.create_task(manager.evaluate_lease(lease.lease_id))
        await entered.wait()  # the warm is now in flight
        second = await manager.evaluate_lease(lease.lease_id)
        assert second is LeaseDecision.SKIPPED_ALREADY_WARMING
        released.set()
        assert await first is LeaseDecision.WARMED_CONFIRMED_HIT

    asyncio.run(scenario())


# -- dry-run default ---------------------------------------------------------


def test_dry_run_default_never_invokes_the_executor():
    clock = FakeClock()
    executor = StubExecutor(WarmResult(outcome=Outcome.CONFIRMED_HIT, usage=TokenUsage(), cost_usd=Decimal(0)))
    store = SnapshotStore()
    manager = _manager(clock, dry_run=True, snapshot_store=store, warm_executor=executor)
    lease = _armed_touched_lease(manager, clock)
    store.store(_snapshot(lease))
    clock.advance(1000)

    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.SKIPPED_DRY_RUN
    assert executor.calls == []  # nothing was sent
    assert lease.warm_count == 0
    assert lease.last_cache_touch_at == 1_000_000.0


# -- races: real-request-wins (PRD §51), targets (PRD §47) -------------------


def test_warm_vs_real_race_skips_busy_and_nothing_is_sent():
    clock = FakeClock()
    provider = FakeProvider(FakeProviderConfig(seed=7, completion_tokens=42, ttl_s=10_000.0))
    executor = FakeProviderWarmExecutor(provider)
    store = SnapshotStore()
    manager = _manager(clock, snapshot_store=store, warm_executor=executor)
    lease = _armed_touched_lease(manager, clock)
    store.store(_snapshot(lease))
    clock.advance(1000)

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
    assert executor.sent_bodies == []  # the warm never executed


def test_warm_vs_complete_race_skips_no_targets_and_nothing_is_sent():
    clock = FakeClock()
    executor = StubExecutor(WarmResult(outcome=Outcome.CONFIRMED_HIT, usage=TokenUsage(), cost_usd=Decimal(0)))
    store = SnapshotStore()
    manager = _manager(clock, snapshot_store=store, warm_executor=executor)
    lease = _armed_touched_lease(manager, clock)
    store.store(_snapshot(lease))
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
    assert lease.state.name == "INACTIVE"
    assert executor.calls == []  # nothing was sent


def test_model_switch_old_identity_warm_never_sent():
    clock = FakeClock()
    provider = FakeProvider(FakeProviderConfig(seed=7, completion_tokens=42, ttl_s=10_000.0))
    executor = FakeProviderWarmExecutor(provider)
    store = SnapshotStore()
    manager = _manager(clock, snapshot_store=store, warm_executor=executor)

    old = _make_lease(manager, model="gpt-5.2", cache_fp="fp-A")
    manager.target_started(old.lease_id, "t1")
    manager.before_normal_request(old.lease_id)
    manager.after_normal_request(old.lease_id, Outcome.CONFIRMED_HIT)
    store.store(_snapshot(old))

    # Model switch → old lease INVALIDATED, fresh independent lease.
    new = _make_lease(manager, model="gpt-5.2-reasoning", cache_fp="fp-B")
    manager.target_started(new.lease_id, "n1")
    manager.before_normal_request(new.lease_id)
    manager.after_normal_request(new.lease_id, Outcome.CONFIRMED_HIT)
    manager.update_cost_estimates(new.lease_id, _USAGE)  # P07: price the prefix
    store.store(_snapshot(new))
    assert old.state.name == "INVALIDATED"

    clock.advance(1000)
    old_decision = asyncio.run(manager.evaluate_lease(old.lease_id))
    assert old_decision is LeaseDecision.STOPPED_INVALIDATED
    assert executor.sent_bodies == []  # the OLD identity's warm is never sent

    new_decision = asyncio.run(manager.evaluate_lease(new.lease_id))
    assert new_decision is LeaseDecision.WARMED_MISS_REBUILT
    assert len(executor.sent_bodies) == 1  # only the new identity warmed
    assert executor.sent_bodies[0]["model"] == "gpt-5.2-reasoning"


# -- fail open for the scheduler loop ----------------------------------------


def test_executor_raise_is_recorded_as_failed_and_never_kills_the_tick():
    clock = FakeClock()

    class RaisingExecutor:
        async def execute(self, snapshot: RequestSnapshot) -> WarmResult:
            raise RuntimeError("transport exploded")

    store = SnapshotStore()
    manager = _manager(clock, snapshot_store=store, warm_executor=RaisingExecutor())
    lease = _armed_touched_lease(manager, clock)
    store.store(_snapshot(lease))
    clock.advance(1000)

    decision = asyncio.run(manager.evaluate_lease(lease.lease_id))
    assert decision is LeaseDecision.WARMED_FAILED
    assert lease.warm_count == 1
    assert lease.warm_cost_usd == 0.0
    # The scheduler loop survives: a full tick over the same lease works.
    results = asyncio.run(manager.tick())
    assert (lease.lease_id, LeaseDecision.WARMED_FAILED) in results
