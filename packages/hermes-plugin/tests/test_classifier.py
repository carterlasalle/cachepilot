"""PRD §108 — deterministic long-task classifier tests.

Covers every §108 scenario: 'pwd' → foreground; 'pytest' with a historic p90
of 8 minutes → background; explicit background=false → foreground unless a
hard policy is enabled; known-long static families → background; and learned
duration promotion for commands that are NOT statically known.
"""

from cachepilot_hermes.classifier import (
    FOREGROUND,
    LONG_RUNNING,
    LongTaskClassifier,
)
from cachepilot_hermes.config import CachePilotConfig, LongTasksSettings
from cachepilot_hermes.duration_history import CommandDurationStats

CONFIG = CachePilotConfig()


def make_stats(sample_count=5, p90=480.0, p50=300.0, p95=540.0):
    return CommandDurationStats(
        signature="some-cmd",
        sample_count=sample_count,
        runtime_p50=p50,
        runtime_p90=p90,
        runtime_p95=p95,
        background_success_rate=1.0,
    )


def test_pwd_is_foreground():
    decision = LongTaskClassifier(CONFIG).classify({"command": "pwd"})
    assert decision.decision == FOREGROUND
    assert "known-fast" in decision.reason


def test_pytest_with_historic_p90_8m_is_background():
    """PRD §108: pytest with historic p90 8m → background."""
    stats = CommandDurationStats(
        signature="uv run pytest",
        sample_count=9,
        runtime_p50=540.0,
        runtime_p90=480.0,  # 8 minutes
        runtime_p95=600.0,
    )
    decision = LongTaskClassifier(CONFIG).classify({"command": "uv run pytest"}, history=stats)
    assert decision.decision == LONG_RUNNING
    assert "p90" in decision.reason


def test_known_long_static_family_is_background():
    for command in (
        "uv run pytest",
        "pytest -x --tb=short",
        "docker build -t myimg .",
        "cargo test --release",
        "make -j8",
        "git clone https://github.com/example/repo.git",
        "yarn build",
    ):
        decision = LongTaskClassifier(CONFIG).classify({"command": command})
        assert decision.decision == LONG_RUNNING, command
        assert "known-long" in decision.reason


def test_known_fast_family_is_foreground():
    for command in (
        "ls -la",
        "git status --short",
        "git diff --stat",
        "cat README.md",
        "which python",
        "echo hello",
        "rg -n 'TODO' src/",
    ):
        decision = LongTaskClassifier(CONFIG).classify({"command": command})
        assert decision.decision == FOREGROUND, command


def test_explicit_background_true_wins():
    """Explicit background=true backgrounds even a known-fast command (§44)."""
    decision = LongTaskClassifier(CONFIG).classify(
        {"command": "pwd", "background": True}
    )
    assert decision.decision == LONG_RUNNING
    assert "explicit background=true" in decision.reason


def test_explicit_background_false_wins_unless_hard_policy():
    """§44: background=false → foreground; a hard policy may override it."""
    decision = LongTaskClassifier(CONFIG).classify(
        {"command": "uv run pytest", "background": False}
    )
    assert decision.decision == FOREGROUND
    assert "explicit background=false" in decision.reason

    hard = CachePilotConfig(long_tasks=LongTasksSettings(enforce_foreground_hard_policy=True))
    decision = LongTaskClassifier(hard).classify(
        {"command": "uv run pytest", "background": False}
    )
    assert decision.decision == LONG_RUNNING

    # Hard policy does NOT override genuinely fast commands.
    decision = LongTaskClassifier(hard).classify({"command": "pwd", "background": False})
    assert decision.decision == FOREGROUND


def test_requested_timeout_hint_backgrounds():
    decision = LongTaskClassifier(CONFIG).classify(
        {"command": "custom-tool.sh", "timeout": 300}
    )
    assert decision.decision == LONG_RUNNING
    assert "requested timeout" in decision.reason

    decision = LongTaskClassifier(CONFIG).classify(
        {"command": "custom-tool.sh", "timeout": 5}
    )
    assert decision.decision == FOREGROUND


def test_learned_duration_promotion():
    """A command with no static hint backgrounds once learned p90 crosses the
    threshold with enough samples (§43)."""
    classifier = LongTaskClassifier(CONFIG)
    command = "data-crunch.sh --full"

    decision = classifier.classify(
        {"command": command}, history=make_stats(p90=480.0)
    )
    assert decision.decision == LONG_RUNNING
    assert "learned p90" in decision.reason

    # Fast learned history stays foreground.
    decision = classifier.classify({"command": command}, history=make_stats(p90=3.0))
    assert decision.decision == FOREGROUND

    # Too few samples: evidence is noise — foreground.
    decision = classifier.classify(
        {"command": command}, history=make_stats(sample_count=1, p90=480.0)
    )
    assert decision.decision == FOREGROUND


def test_unknown_command_defaults_to_foreground():
    decision = LongTaskClassifier(CONFIG).classify({"command": "some-random-tool"})
    assert decision.decision == FOREGROUND
    assert decision.reason == "default"


def test_malformed_and_empty_commands_are_foreground():
    classifier = LongTaskClassifier(CONFIG)
    assert classifier.classify({}).decision == FOREGROUND
    assert classifier.classify({"command": ""}).decision == FOREGROUND
    assert classifier.classify({"command": "   "}).decision == FOREGROUND
    assert classifier.classify({"command": 42}).decision == FOREGROUND
    assert classifier.classify(None).decision == FOREGROUND
    assert classifier.classify({"command": "pwd"}, tool_name="read_file").decision == FOREGROUND


def test_classifier_is_deterministic_and_env_independent():
    """Environment is a reserved input — the verdict never depends on it."""
    classifier = LongTaskClassifier(CONFIG)
    command = {"command": "uv run pytest -x"}
    results = {
        classifier.classify(command, env=env).decision
        for env in (None, {"CI": "true"}, {"PATH": "/nonexistent"}, {"CI": "false"})
    }
    assert results == {LONG_RUNNING}


def test_no_llm_ever():
    """The classifier is pure Python over args/history — it must not import
    or reference any LLM/HTTP machinery."""
    import inspect

    source = inspect.getsource(LongTaskClassifier)
    for forbidden in ("http", "openai", "anthropic", "requests", "asyncio"):
        assert forbidden not in source.lower()
