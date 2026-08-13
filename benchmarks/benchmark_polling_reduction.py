#!/usr/bin/env python3
"""PRD §116-119 — polling-reduction benchmark: CachePilot vs stock Hermes.

Deterministic simulation of the two agent-loop behaviors for the PRD §117
Workload 2 (8-minute test task):

- **Stock Hermes** launches the command in the foreground and then wakes the
  LLM every *poll_interval_s* seconds to ask "is it finished?" — each of
  those turns is an LLM call that exists solely to poll (the exact
  anti-pattern PRD §45 forbids).
- **CachePilot long-task manager** classifies the command LONG_RUNNING,
  promotes it to ``background=True`` with ``notify_on_complete=True``
  (PRD §40), and lets Hermes' local process registry watch it. The LLM is
  woken exactly once — by the completion notification that resumes the
  session. Zero polling calls.

The simulation is pure arithmetic over the workload parameters: no randomness,
no wall-clock waiting, no network, no LLM. It is therefore deterministic and
safe to assert on from pytest (``packages/hermes-plugin/tests/
test_benchmark_polling_reduction.py``).

Usage::

    uv run python benchmarks/benchmark_polling_reduction.py

Exits 0 and prints the per-mode LLM-call table.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# PRD §117 Workload 2: 8-minute test task (480 s).
TASK_DURATION_S = 480.0
# Stock loop poll cadence ("check again in 30 seconds" turns).
POLL_INTERVAL_S = 30.0


@dataclass(frozen=True)
class SimulationResult:
    """Per-mode LLM call accounting for one workload."""

    task_duration_s: float
    poll_interval_s: float
    stock_polling_calls: int
    stock_total_calls: int
    cachepilot_polling_calls: int
    cachepilot_total_calls: int

    @property
    def polling_reduction_pct(self) -> float:
        """Percent reduction in LLM polling calls vs stock (PRD §13: >= 95%)."""
        if self.stock_polling_calls <= 0:
            return 0.0
        return 100.0 * (
            1.0 - self.cachepilot_polling_calls / self.stock_polling_calls
        )


def simulate(
    task_duration_s: float = TASK_DURATION_S,
    poll_interval_s: float = POLL_INTERVAL_S,
    *,
    short_calls: int = 0,
) -> SimulationResult:
    """Count LLM calls per mode for a workload of *short_calls* quick shell
    calls plus one *task_duration_s* long task.

    Model (documented assumptions):
    - stock: 1 launch turn + ``ceil(duration / poll_interval)`` polling turns
      + 1 completion turn per long task; quick calls never poll.
    - cachepilot: 1 launch turn + 0 polling turns + 1 completion turn per
      long task; the completion notification is the resume request — not a
      poll (PRD §45).
    """
    long_task_count = 1
    stock_polling = long_task_count * max(1, math.ceil(task_duration_s / poll_interval_s))
    stock_total = (
        short_calls + long_task_count * 2 + stock_polling
    )  # quick calls + (launch + completion) + polls
    cachepilot_polling = 0
    cachepilot_total = short_calls + long_task_count * 2  # no polling ever
    return SimulationResult(
        task_duration_s=task_duration_s,
        poll_interval_s=poll_interval_s,
        stock_polling_calls=stock_polling,
        stock_total_calls=stock_total,
        cachepilot_polling_calls=cachepilot_polling,
        cachepilot_total_calls=cachepilot_total,
    )


def main() -> int:
    """Run the workload-2 simulation, print the table, exit 0."""
    result = simulate()
    print("CachePilot polling-reduction benchmark (deterministic simulation)")
    print(f"Workload: 1 long task @ {result.task_duration_s:g}s"
          f" (PRD §117 workload 2); stock poll interval {result.poll_interval_s:g}s")
    print()
    print(f"{'mode':<22}{'llm_polling_calls':>18}{'llm_total_calls':>18}")
    print(f"{'stock hermes':<22}{result.stock_polling_calls:>18}{result.stock_total_calls:>18}")
    print(
        f"{'cachepilot':<22}{result.cachepilot_polling_calls:>18}"
        f"{result.cachepilot_total_calls:>18}"
    )
    print()
    print(f"LLM polling reduction vs stock: {result.polling_reduction_pct:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
