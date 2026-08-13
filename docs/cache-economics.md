# Cache Economics — the Economic Controller (PRD §60-65, §134, §145-147)

This runbook describes the economic controller — the component that turns a
KV-cache watchdog into a KV-cache optimizer (PRD §134). It cross-references
the authoritative spec (`docs/PRD.md` sections cited inline) and the code it
describes: `packages/core/src/cachepilot_core/economics.py`,
`packages/core/src/cachepilot_core/pricing.py` and the economics wiring in
`packages/core/src/cachepilot_core/leases.py`.

The one-sentence contract (AGENTS.md invariant 5):

> WARM iff `expected_avoidable_loss > expected_next_warm_cost + safety_margin`,
> and warming can never continue forever.

A "warm while the process is alive" watchdog is the anti-pattern this phase
exists to forbid. A 3-hour compile must NOT trigger 36 refreshes just because
the job exists.

---

## The decision math (PRD §60-62)

For each lease, the controller computes:

```text
avoidable_loss        = cold_resume_cost − cached_resume_cost        (PRD §60)
expected_value        = resume_probability × avoidable_loss          (PRD §60)
max_warm_budget       = expected_value × budget_ratio                (PRD §60, default 0.70)
remaining_budget      = max_warm_budget − cumulative_warm_cost       (PRD §61)
should_warm           = next_warm_expected_cost < remaining_budget
                        and expected_net_savings ≥ minimum_expected_savings  (PRD §61)
```

The 0.70 `budget_ratio` keeps 30% safety headroom. The PRD §62 worked example
ends with `LET CACHE EXPIRE` once `$0.44 + $0.11 > $0.504` — that exact
bound is `ECONOMIC_STOP` in this codebase: cumulative spend plus the next
predicted warm no longer fits the budget, so the cache is allowed to expire
instead of being warmed into a loss.

### Decisions are explainable (PRD §145)

Every evaluation returns a `WarmDecision` carrying the full breakdown plus a
machine-readable `action` and a human-readable `reason`:

```text
WarmDecision(
    action="warm",                    # or economic_stop / skip_unknown_pricing /
    reason="due_and_economically_positive",   # skip_no_continuation / skip_not_economic
    resume_probability=0.95,
    expected_avoidable_loss=0.144,
    expected_value=0.1368,
    next_warm_cost=0.012,
    cumulative_warm_cost=0.044,
    max_warm_budget=0.09576,
    remaining_budget=0.05176,
    safety_margin=0.0,
)
```

`evaluate_economics` (`leases.py`) stores the decision on the lease
(in-memory only — never persisted, PRD §83) and logs the same breakdown as a
structured INFO line. The scheduler's skip vocabulary — `SKIPPED_ECONOMIC` /
`SKIPPED_NO_TARGETS` / `SKIPPED_DRY_RUN` / `SKIPPED_UNKNOWN_TTL` /
`SKIPPED_UNSUPPORTED` / `SKIPPED_CIRCUIT_OPEN` — mirrors the PRD §145 skip
names.

## Where the numbers come from

### Resume costs from the pricing resolver (PRD §65, §66)

`pricing.py` implements the PRD §65 resolution priority:

1. provider-returned monetary usage (`usage.cost`)
2. provider usage × live pricing metadata (`PricingTable`)
3. configured price override
4. unknown

`estimate_resume_costs(prefix_tokens, pricing, completion_tokens)` prices the
two resume shapes: a **cold** resume must write the prefix into the provider
cache (`cache_write` rate), a **cached** resume is served from cache
(`cache_read` rate). Their difference is the avoidable loss. Pricing tables
are deliberately *fallback snapshots* (they carry an `as_of` timestamp) —
never permanent authority (PRD §66).

`CACHEPILOT_PRICING_INPUT_PER_MTK` / `CACHEPILOT_PRICING_OUTPUT_PER_MTK` /
`CACHEPILOT_PRICING_CACHE_READ_PER_MTK` / `CACHEPILOT_PRICING_CACHE_WRITE_PER_MTK`
configure the snapshot — **all four must be set** for the table to engage
(all-or-nothing, fail closed). Unknown pricing ⇒ `SKIP_UNKNOWN_PRICING` and
**no savings are ever claimed** (invariant 4).

### When estimates are refreshed — and when they are not

`update_cost_estimates` (`leases.py`) refreshes the lease's resume-cost
estimates **on the normal-request path only** — never on a scheduler tick.
This is the PRD §64 "natural requests are free heartbeats" rule: the natural
request already touches/reuses the cache, so it resets the lease age *and*
re-prices the prefix. The next-warm predictor is the bounded warm shape: the
same prefix billed at `cache_read` plus one output token (PRD §31/§147 — a
warm replays the still-alive cache). A scheduler tick that finds the lease
due does NOT re-derive costs from stale data; it evaluates against the last
normal-request snapshot.

### Resume probability per target type (PRD §63 — P0 heuristics, no ML)

`resume_probability(lease)` is deterministic over the lease's active
background targets:

| Target situation | R | Default |
|---|---|---|
| No active targets | 0.0 — no continuation possible, never warm | — |
| All targets explicitly detached (`detached-` prefix) | low | 0.20 |
| Any target with `notify_on_complete` (relay `bg-N` synthetic ids) | high | 0.95 |

Tunables: `CACHEPILOT_ECONOMICS_RESUME_PROBABILITY` (0.95),
`CACHEPILOT_ECONOMICS_DETACHED_RESUME_PROBABILITY` (0.20). The plugin's
auto-backgrounded terminal calls carry `notify_on_complete=True` (PRD §40),
so the high-probability branch is the normal case; an explicitly detached
job is a low-probability continuation.

## The gate and the stop (PRD §134, §146)

The lease scheduler runs PRD §146's core algorithm per lease:
`no targets → stop` · `real request active → skip(busy)` · `already warming →
skip` · `identity invalid → stop` · `unknown TTL → skip` · `not yet due →
schedule` · then **the economic gate** · then warm (with re-checks for
`real_request_active` and generation staleness before sending — invariant 6).

```python
def economic_gate(lease):
    if not settings.economics.enabled:
        return True          # P07 off → P05/P06 watchdog behaviour
    return evaluate_economics(lease).should_warm
```

- `CACHEPILOT_ECONOMICS_ENABLED=false` restores the plain watchdog: every
  due lease warms (subject to TTL/breaker/snapshot gates). This is the
  PRD §84-flag and the documented escape hatch.
- When the budget is exhausted the decision is `ECONOMIC_STOP` — the lease
  transitions to the `ECONOMIC_STOP` state and warming stops for that lease
  (PRD §61-62, §103's "zero probability of continuation" test, §65's
  "disable economically unbounded repeated warming").
- `CACHEPILOT_ECONOMICS_MINIMUM_EXPECTED_SAVINGS_USD` opts into the PRD §84
  sample's `minimum_expected_savings_usd: 0.01`; the default stays 0.0 so
  the pure math is the only gate out of the box.

## Warm costs are always visible (invariant 4)

Every executed warm increments `warm_count` and `warm_cost_usd` on the lease
(PRD §147); the cost is estimated from the configured pricing snapshot via
the resolver, so the economic gate sees the real spend. Session economics
are never presented as "money saved" unless cost data are complete — the CLI
(`cachepilot costs`) is labeled recorded-cost-only by construction.

## Verification (PRD §103, §104, §113)

The controller is pure and offline-testable (`packages/core/tests/test_economics.py`):

- **one cheap warm** → WARM
- **too many warms** → ECONOMIC_STOP after the budget is exhausted (the §62
  example is a unit test)
- **unknown pricing** → SKIP_UNKNOWN_PRICING, no savings claimed
- **zero probability of continuation** → SKIP_NO_CONTINUATION
- **economics disabled** → gate passes (watchdog restored)
- integration: the fake-provider suite drives an entire
  warm → economic-stop lifecycle end to end (`test_lease_integration.py`,
  PRD §113).

## Config reference

| Variable | Default | Meaning |
|---|---|---|
| `CACHEPILOT_ECONOMICS_ENABLED` | `true` | `false` = watchdog warming (no economic gate) |
| `CACHEPILOT_ECONOMICS_BUDGET_RATIO` | `0.70` | fraction of expected value spendable on warms |
| `CACHEPILOT_ECONOMICS_MINIMUM_EXPECTED_SAVINGS_USD` | `0.0` | PRD §84 `minimum_expected_savings_usd` opt-in |
| `CACHEPILOT_ECONOMICS_RESUME_PROBABILITY` | `0.95` | R for `notify_on_complete` targets |
| `CACHEPILOT_ECONOMICS_DETACHED_RESUME_PROBABILITY` | `0.20` | R for detached targets |
| `CACHEPILOT_PRICING_INPUT_PER_MTK` | — | pricing snapshot (all-or-nothing) |
| `CACHEPILOT_PRICING_OUTPUT_PER_MTK` | — | pricing snapshot (all-or-nothing) |
| `CACHEPILOT_PRICING_CACHE_READ_PER_MTK` | — | pricing snapshot (all-or-nothing) |
| `CACHEPILOT_PRICING_CACHE_WRITE_PER_MTK` | — | pricing snapshot (all-or-nothing) |

## Relationship to other phases

- **P06 warming** builds the bounded cache-equivalent request the economics
  gate spends money on — see `docs/provider-adapters.md` (PRD §31-36).
- **P08 TTL learning** decides *when* a lease is due; economics decides
  *whether* a due warm is worth paying for (PRD §146's `ttl → due_at → economics`).
- **P09 route intelligence** prices the *route* a warm would take; economic
  route affinity applies its own savings-vs-cost gate on top (PRD §73).
- **P10 churn intelligence** explains why a prefix became un-warmable in the
  first place (PRD §75) — a diagnosis layer, not a cost layer.
