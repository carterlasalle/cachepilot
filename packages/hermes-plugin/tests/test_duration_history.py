"""PRD §43 / §82 — command duration learner tests.

Covers signature normalization (secret-safe, §83), percentile computation,
SQLite persistence roundtrip via ``tmp_path``, and fail-open behavior on
unusable database paths.
"""


import pytest
from cachepilot_hermes.duration_history import (
    CommandDurationHistory,
    normalize_signature,
    percentile,
)

# ---------------------------------------------------------------------------
# Signature normalization (AGENTS.md rule 10 — never persist arg values)
# ---------------------------------------------------------------------------


def test_normalize_signature_keeps_family_and_flag_names():
    assert normalize_signature("uv run pytest tests/unit -x --tb=short") == (
        "uv run pytest --tb -x"
    )
    assert normalize_signature("uv run pytest") == "uv run pytest"
    assert normalize_signature("docker build -t myimage .") == "docker build -t"
    assert normalize_signature("git clone https://github.com/org/repo.git") == "git clone"


def test_normalize_signature_drops_values_and_secrets():
    # Positional values (potentially secrets) are never part of the signature.
    assert normalize_signature("echo SECRET > /tmp/out.txt") == "echo"
    assert normalize_signature("python deploy.py --api-key sk-abcdefghijklm") == (
        "python --api-key"
    )
    assert normalize_signature("tool.sh hunter2 --token=abc") == "tool.sh --token"
    # Everything after `--` is a value.
    assert normalize_signature("uv run pytest -- -x --tb=short") == "uv run pytest"


def test_normalize_signature_shell_fallbacks():
    assert normalize_signature("cat /etc/passwd") == "cat"
    assert normalize_signature("make -j8 all") == "make -j"
    assert normalize_signature("ls -la /var/log") == "ls -l"
    # Empty/whitespace-only input yields nothing persistable.
    assert normalize_signature("") == ""
    assert normalize_signature("   ") == ""


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------


def test_percentile_linear_interpolation():
    samples = sorted(float(x) for x in range(1, 11))  # 1.0..10.0
    assert percentile(samples, 0.50) == pytest.approx(5.5)
    assert percentile(samples, 0.90) == pytest.approx(9.1)
    assert percentile(samples, 0.95) == pytest.approx(9.55)


def test_percentile_single_sample():
    assert percentile([7.0], 0.90) == 7.0


def test_percentile_rejects_empty():
    with pytest.raises(ValueError):
        percentile([], 0.5)


# ---------------------------------------------------------------------------
# Persistence + stats via tmp_path
# ---------------------------------------------------------------------------


def test_record_and_stats_roundtrip(tmp_path):
    db = tmp_path / "history.db"
    store = CommandDurationHistory(db)
    command = "uv run pytest tests/unit -x --tb=short"
    for seconds in range(1, 11):
        store.record(command, seconds)

    stats = store.stats(command)
    assert stats is not None
    assert stats.signature == "uv run pytest --tb -x"
    assert stats.sample_count == 10
    assert stats.runtime_p50 == pytest.approx(5.5)
    assert stats.runtime_p90 == pytest.approx(9.1)
    assert stats.runtime_p95 == pytest.approx(9.55)


def test_stats_persist_across_instances(tmp_path):
    db = tmp_path / "history.db"
    store = CommandDurationHistory(db)
    for seconds in (2, 4, 8, 16, 32):
        store.record("cargo test --release", seconds)

    reopened = CommandDurationHistory(db)
    stats = reopened.stats("cargo test --release")
    assert stats is not None
    assert stats.sample_count == 5
    assert stats.runtime_p90 == pytest.approx(25.6)


def test_record_increments_sample_count(tmp_path):
    store = CommandDurationHistory(tmp_path / "h.db")
    store.record("sleep 5", 5.0)
    store.record("sleep 5", 7.0)
    stats = store.stats("sleep 5")
    assert stats is not None
    assert stats.sample_count == 2
    assert stats.runtime_p50 == pytest.approx(6.0)


def test_background_success_rate(tmp_path):
    store = CommandDurationHistory(tmp_path / "h.db")
    for _ in range(3):
        store.record("long-job.sh", 100.0, background=True, success=True)
    store.record("long-job.sh", 120.0, background=True, success=False)
    stats = store.stats("long-job.sh")
    assert stats is not None
    assert stats.background_success_rate == pytest.approx(0.75)

    # Foreground runs leave the rate untouched.
    store.record("long-job.sh", 1.0, background=False, success=True)
    stats = store.stats("long-job.sh")
    assert stats is not None
    assert stats.background_success_rate == pytest.approx(0.75)


def test_unknown_command_stats_is_none(tmp_path):
    store = CommandDurationHistory(tmp_path / "h.db")
    assert store.stats("never-run-before") is None


def test_empty_command_record_is_noop(tmp_path):
    store = CommandDurationHistory(tmp_path / "h.db")
    store.record("", 5.0)  # must not raise, must not persist
    assert store.stats("") is None


# ---------------------------------------------------------------------------
# Fail open
# ---------------------------------------------------------------------------


def test_fail_open_on_bad_path(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("I am a file, not a directory")
    store = CommandDurationHistory(blocker / "nested" / "h.db")
    assert store.disabled is True
    # All operations are no-ops — never raise.
    store.record("anything", 5.0)
    assert store.stats("anything") is None


def test_env_db_path_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHEPILOT_LONG_TASKS_DB_PATH", str(tmp_path / "env.db"))
    store = CommandDurationHistory()
    assert store.db_path == tmp_path / "env.db"
    store.record("pytest", 60.0)
    assert store.stats("pytest") is not None


def test_default_db_path_under_cachepilot_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CACHEPILOT_LONG_TASKS_DB_PATH", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    store = CommandDurationHistory()
    assert str(store.db_path).startswith(str(tmp_path / ".cachepilot"))
    assert store.db_path.name == "long_tasks.db"


def test_stats_roundtrip_matches_schema_columns(tmp_path):
    """PRD §82 command_history columns are present and typed."""
    import sqlite3

    db = tmp_path / "schema.db"
    store = CommandDurationHistory(db)
    store.record("make", 10.0)
    with sqlite3.connect(db) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(command_history)")]
    assert columns == [
        "signature",
        "sample_count",
        "runtime_p50",
        "runtime_p90",
        "runtime_p95",
        "background_success_rate",
        "updated_at",
    ]
