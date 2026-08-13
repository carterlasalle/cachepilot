"""PRD §116-119 — polling-reduction benchmark test.

Loads the standalone benchmark simulation (``benchmarks/
benchmark_polling_reduction.py``) and asserts the Phase 2 goal: for the PRD
§117 Workload 2 (8-minute task), the CachePilot long-task manager performs
zero LLM polling calls vs stock Hermes' ``ceil(480/30) = 16`` polling turns
(PRD §13: LLM polling calls ↓ >= 95%).
"""

import importlib.util
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BENCHMARK_PATH = REPO_ROOT / "benchmarks" / "benchmark_polling_reduction.py"

TASK_DURATION_S = 480.0
POLL_INTERVAL_S = 30.0


def _load_benchmark():
    spec = importlib.util.spec_from_file_location(
        "benchmark_polling_reduction", BENCHMARK_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load benchmark module from {BENCHMARK_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["benchmark_polling_reduction"] = module  # dataclasses need it
    spec.loader.exec_module(module)
    return module


def test_benchmark_script_exists_and_is_loadable():
    assert BENCHMARK_PATH.is_file(), f"missing {BENCHMARK_PATH}"


def test_cachepilot_polling_less_than_stock_for_8min_workload():
    bench = _load_benchmark()
    result = bench.simulate(TASK_DURATION_S, POLL_INTERVAL_S)

    # Stock: one polling turn per 30s of the 8-minute task.
    assert result.stock_polling_calls == math.ceil(TASK_DURATION_S / POLL_INTERVAL_S) == 16
    # CachePilot: notify_on_complete means the LLM is never asked to poll.
    assert result.cachepilot_polling_calls == 0
    assert result.cachepilot_polling_calls < result.stock_polling_calls
    # Total LLM calls also shrink.
    assert result.cachepilot_total_calls < result.stock_total_calls


def test_polling_reduction_meets_prd_metric():
    bench = _load_benchmark()
    result = bench.simulate(TASK_DURATION_S, POLL_INTERVAL_S)
    assert result.polling_reduction_pct >= 95.0  # PRD §13: ↓ >= 95%


def test_simulation_is_deterministic():
    bench = _load_benchmark()
    first = bench.simulate(TASK_DURATION_S, POLL_INTERVAL_S)
    second = bench.simulate(TASK_DURATION_S, POLL_INTERVAL_S)
    assert first == second


def test_benchmark_main_prints_table_and_exits_zero(capsys):
    bench = _load_benchmark()
    assert bench.main() == 0
    output = capsys.readouterr().out
    assert "stock hermes" in output
    assert "cachepilot" in output
    assert "llm_polling_calls" in output
    assert "100.0%" in output
