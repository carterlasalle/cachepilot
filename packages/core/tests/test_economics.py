"""PRD §103 — unit tests for the economic controller (PRD §60-65, AGENTS.md invariant 5)."""

from decimal import Decimal

from cachepilot_core.economics import EconomicConfig, EconomicController, WarmAction
from cachepilot_core.fake_provider import FakeProvider
from cachepilot_core.pricing import PricingTable

# PRD §62 example numbers: cold prefix $0.80, cached prefix $0.08.
COLD = Decimal("0.80")
CACHED = Decimal("0.08")
WARM_COST = Decimal("0.11")

controller = EconomicController()


def test_one_cheap_warm():
    """PRD §103 'One cheap warm': warm cost < avoidable miss -> WARM."""
    decision = controller.evaluate(
        cold_resume_cost=COLD,
        cached_resume_cost=CACHED,
        next_warm_cost=WARM_COST,
        cumulative_warm_cost=Decimal(0),
        resume_probability=1.0,
    )
    assert decision.action is WarmAction.WARM
    assert decision.should_warm


def test_too_many_warms():
    """PRD §103 'Too many warms': cumulative warm cost > budget -> ECONOMIC_STOP.

    PRD §62 example verbatim: $0.44 spent on 4 warms, predicted warm #5 $0.11,
    budget $0.504 -> LET CACHE EXPIRE.
    """
    decision = controller.evaluate(
        cold_resume_cost=COLD,
        cached_resume_cost=CACHED,
        next_warm_cost=WARM_COST,
        cumulative_warm_cost=Decimal("0.44"),
        resume_probability=1.0,
    )
    assert decision.action is WarmAction.ECONOMIC_STOP
    assert not decision.should_warm
    assert decision.max_warm_budget == Decimal("0.504")
    assert decision.remaining_budget == Decimal("0.064")


def test_prd62_example_warms_exactly_four_times():
    """Walking the PRD §62 narrative: warms #1-#4 pass, warm #5 is refused."""
    cumulative = Decimal(0)
    warm_count = 0
    for _ in range(10):
        decision = controller.evaluate(
            cold_resume_cost=COLD,
            cached_resume_cost=CACHED,
            next_warm_cost=WARM_COST,
            cumulative_warm_cost=cumulative,
            resume_probability=1.0,
        )
        if decision.action is not WarmAction.WARM:
            assert decision.action is WarmAction.ECONOMIC_STOP
            break
        warm_count += 1
        cumulative += WARM_COST
    assert warm_count == 4
    assert cumulative == Decimal("0.44")


def test_unknown_pricing():
    """PRD §103 'Unknown pricing': economically unbounded warming disabled by default."""
    decision = controller.evaluate(
        cold_resume_cost=COLD,
        cached_resume_cost=CACHED,
        next_warm_cost=WARM_COST,
        cumulative_warm_cost=Decimal(0),
        resume_probability=1.0,
        pricing_known=False,
    )
    assert decision.action is WarmAction.SKIP_UNKNOWN_PRICING
    assert not decision.should_warm


def test_zero_probability_of_continuation():
    """PRD §103 'Zero probability of continuation': never warm."""
    decision = controller.evaluate(
        cold_resume_cost=COLD,
        cached_resume_cost=CACHED,
        next_warm_cost=WARM_COST,
        cumulative_warm_cost=Decimal(0),
        resume_probability=0.0,
    )
    assert decision.action is WarmAction.SKIP_NO_CONTINUATION
    # negative probability is nonsensical but must also never warm
    negative = controller.evaluate(
        cold_resume_cost=COLD,
        cached_resume_cost=CACHED,
        next_warm_cost=WARM_COST,
        cumulative_warm_cost=Decimal(0),
        resume_probability=-0.1,
    )
    assert negative.action is WarmAction.SKIP_NO_CONTINUATION


def test_never_warm_forever_with_live_fake_pricing():
    """Warming terminates: the budget guarantees a stop, never a watchdog."""
    provider = FakeProvider()
    cold, cached = provider.resume_costs()
    next_warm = Decimal("0.0004")
    cumulative = Decimal(0)
    decisions = []
    for _ in range(20):
        decision = controller.evaluate(
            cold_resume_cost=cold,
            cached_resume_cost=cached,
            next_warm_cost=next_warm,
            cumulative_warm_cost=cumulative,
            resume_probability=1.0,
        )
        decisions.append(decision)
        if decision.action is WarmAction.WARM:
            cumulative += next_warm
        else:
            break
    assert [d.action for d in decisions[:-1]] == [WarmAction.WARM] * 5
    assert decisions[-1].action is WarmAction.ECONOMIC_STOP
    assert cumulative == Decimal("0.0020")
    # and it stays stopped no matter how much more has been spent
    stopped = controller.evaluate(
        cold_resume_cost=cold,
        cached_resume_cost=cached,
        next_warm_cost=next_warm,
        cumulative_warm_cost=Decimal("0.10"),
        resume_probability=1.0,
    )
    assert stopped.action is WarmAction.ECONOMIC_STOP


def test_invariant5_safety_margin_gate():
    """AGENTS.md invariant 5: WARM iff expected_avoidable_loss >
    expected_next_warm_cost + safety_margin."""
    margin_config = EconomicConfig(safety_margin=Decimal("0.20"))
    strict = EconomicController(margin_config)
    relaxed = EconomicController()

    # EV = 0.5 * (1.00 - 0.10) = 0.45; next warm 0.30.
    kwargs = {
        "cold_resume_cost": Decimal("1.00"),
        "cached_resume_cost": Decimal("0.10"),
        "next_warm_cost": Decimal("0.30"),
        "cumulative_warm_cost": Decimal(0),
        "resume_probability": 0.5,
    }
    # 0.45 <= 0.30 + 0.20 -> refused under the margin
    assert strict.evaluate(**kwargs).action is WarmAction.SKIP_NOT_ECONOMIC
    # 0.45 > 0.30 + 0.00 and 0.30 < 0.315 budget -> allowed without it
    assert relaxed.evaluate(**kwargs).action is WarmAction.WARM


def test_no_avoidable_loss_never_warms():
    decision = controller.evaluate(
        cold_resume_cost=Decimal("0.50"),
        cached_resume_cost=Decimal("0.50"),
        next_warm_cost=Decimal("0.01"),
        cumulative_warm_cost=Decimal(0),
        resume_probability=1.0,
    )
    assert decision.action is WarmAction.SKIP_NOT_ECONOMIC


def test_minimum_expected_savings_gate():
    """PRD §61: expected_net_savings >= minimum_expected_savings."""
    strict = EconomicController(EconomicConfig(minimum_expected_savings=Decimal("0.8")))
    kwargs = {
        "cold_resume_cost": Decimal("2.00"),
        "cached_resume_cost": Decimal("0.00"),
        "next_warm_cost": Decimal("0.50"),
        "resume_probability": 1.0,
    }
    # cumulative 0.70: net = 2.0 - 1.20 = 0.80 >= 0.80 -> warm
    assert strict.evaluate(**kwargs, cumulative_warm_cost=Decimal("0.70")).action is WarmAction.WARM
    # cumulative 0.75: net = 2.0 - 1.25 = 0.75 < 0.80 -> refuse
    assert (
        strict.evaluate(**kwargs, cumulative_warm_cost=Decimal("0.75")).action
        is WarmAction.SKIP_NOT_ECONOMIC
    )


def test_evaluate_resume_with_live_pricing():
    """PRD §65 priority 2: usage x live pricing via evaluate_resume()."""
    pricing = PricingTable(
        input_per_mtok=Decimal("0.80"),
        output_per_mtok=Decimal("2.40"),
        cache_read_per_mtok=Decimal("0.08"),
        cache_write_per_mtok=Decimal("0.88"),
    )
    decision = controller.evaluate_resume(
        prefix_tokens=4000,
        pricing=pricing,
        resume_probability=1.0,
        next_warm_cost=Decimal("0.0004"),
        cumulative_warm_cost=Decimal(0),
    )
    assert decision.action is WarmAction.WARM
    assert decision.expected_avoidable_loss == Decimal("0.00320")
    # unknown pricing path is disabled too
    unknown = controller.evaluate_resume(
        prefix_tokens=4000,
        pricing=pricing,
        resume_probability=1.0,
        next_warm_cost=Decimal("0.0004"),
        cumulative_warm_cost=Decimal(0),
        pricing_known=False,
    )
    assert unknown.action is WarmAction.SKIP_UNKNOWN_PRICING


def test_decision_is_explainable():
    """PRD §145: every warm decision carries the full economic breakdown."""
    decision = controller.evaluate(
        cold_resume_cost=COLD,
        cached_resume_cost=CACHED,
        next_warm_cost=WARM_COST,
        cumulative_warm_cost=Decimal(0),
        resume_probability=0.95,
    )
    assert decision.reason == "due_and_economically_positive"
    assert decision.expected_avoidable_loss == Decimal("0.72")
    assert decision.expected_value == Decimal("0.684")  # 0.95 * 0.72
    assert decision.max_warm_budget == Decimal("0.4788")  # 0.70 * 0.684
    assert decision.remaining_budget == decision.max_warm_budget
    assert decision.safety_margin == Decimal(0)
