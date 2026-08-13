"""CachePilot plugin settings + structured-debug emitter tests (PRD §128-129)."""

import json
import logging

import pytest
from cachepilot_hermes.config import (
    DEFAULT_KNOWN_FOREGROUND_COMMANDS,
    DEFAULT_KNOWN_LONG_COMMANDS,
    CachePilotConfig,
    LongTasksSettings,
    emit_debug,
)

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_LONG_TASKS_ENV_VARS = (
    "CACHEPILOT_LONG_TASKS_ENABLED",
    "CACHEPILOT_LONG_TASKS_AUTO_BACKGROUND",
    "CACHEPILOT_LONG_TASKS_TIMEOUT_THRESHOLD_S",
    "CACHEPILOT_LONG_TASKS_LEARN_COMMAND_DURATIONS",
    "CACHEPILOT_LONG_TASKS_NOTIFY_ON_COMPLETE",
    "CACHEPILOT_LONG_TASKS_KNOWN_LONG_COMMANDS",
    "CACHEPILOT_LONG_TASKS_KNOWN_FOREGROUND_COMMANDS",
    "CACHEPILOT_LONG_TASKS_DB_PATH",
    "CACHEPILOT_LONG_TASKS_ENFORCE_FOREGROUND_HARD_POLICY",
)


def test_defaults():
    cfg = CachePilotConfig()
    assert cfg.enabled is True
    assert cfg.log_level == "DEBUG"
    assert cfg.log_format == "kv"
    long_tasks = cfg.long_tasks
    assert long_tasks.enabled is True
    assert long_tasks.auto_background is True
    assert long_tasks.timeout_threshold_s == 20.0
    assert long_tasks.learn_command_durations is True
    assert long_tasks.notify_on_complete is True
    assert long_tasks.enforce_foreground_hard_policy is False
    assert long_tasks.db_path is None


def test_long_tasks_defaults_are_prd_lists():
    assert "pytest" in DEFAULT_KNOWN_LONG_COMMANDS
    assert "docker build" in DEFAULT_KNOWN_LONG_COMMANDS
    assert "uv run pytest" in DEFAULT_KNOWN_LONG_COMMANDS
    assert "pwd" in DEFAULT_KNOWN_FOREGROUND_COMMANDS
    assert "git status" in DEFAULT_KNOWN_FOREGROUND_COMMANDS
    assert LongTasksSettings().known_long_commands == DEFAULT_KNOWN_LONG_COMMANDS
    assert LongTasksSettings().known_foreground_commands == DEFAULT_KNOWN_FOREGROUND_COMMANDS


def test_from_env_defaults(monkeypatch):
    for var in ("CACHEPILOT_ENABLED", "CACHEPILOT_LOG_LEVEL", "CACHEPILOT_LOG_FORMAT"):
        monkeypatch.delenv(var, raising=False)
    for var in _LONG_TASKS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    assert CachePilotConfig.from_env() == CachePilotConfig()


def test_from_env_overrides(monkeypatch):
    monkeypatch.setenv("CACHEPILOT_ENABLED", "false")
    monkeypatch.setenv("CACHEPILOT_LOG_LEVEL", "info")
    monkeypatch.setenv("CACHEPILOT_LOG_FORMAT", "JSON")
    cfg = CachePilotConfig.from_env()
    assert cfg.enabled is False
    assert cfg.log_level == "INFO"
    assert cfg.log_format == "json"


def test_long_tasks_from_env_overrides(monkeypatch):
    monkeypatch.setenv("CACHEPILOT_LONG_TASKS_ENABLED", "false")
    monkeypatch.setenv("CACHEPILOT_LONG_TASKS_AUTO_BACKGROUND", "off")
    monkeypatch.setenv("CACHEPILOT_LONG_TASKS_TIMEOUT_THRESHOLD_S", "45")
    monkeypatch.setenv("CACHEPILOT_LONG_TASKS_LEARN_COMMAND_DURATIONS", "0")
    monkeypatch.setenv("CACHEPILOT_LONG_TASKS_NOTIFY_ON_COMPLETE", "no")
    monkeypatch.setenv("CACHEPILOT_LONG_TASKS_KNOWN_LONG_COMMANDS", "rsync,terraform apply")
    monkeypatch.setenv("CACHEPILOT_LONG_TASKS_KNOWN_FOREGROUND_COMMANDS", "env,id")
    monkeypatch.setenv("CACHEPILOT_LONG_TASKS_DB_PATH", "/tmp/cp/history.db")
    monkeypatch.setenv("CACHEPILOT_LONG_TASKS_ENFORCE_FOREGROUND_HARD_POLICY", "yes")
    cfg = CachePilotConfig.from_env()
    long_tasks = cfg.long_tasks
    assert long_tasks.enabled is False
    assert long_tasks.auto_background is False
    assert long_tasks.timeout_threshold_s == 45.0
    assert long_tasks.learn_command_durations is False
    assert long_tasks.notify_on_complete is False
    assert long_tasks.known_long_commands == ("rsync", "terraform apply")
    assert long_tasks.known_foreground_commands == ("env", "id")
    assert long_tasks.db_path == "/tmp/cp/history.db"
    assert long_tasks.enforce_foreground_hard_policy is True


def test_long_tasks_bad_timeout_env_falls_back(monkeypatch):
    """A malformed numeric env value must not break plugin load (fail open)."""
    monkeypatch.setenv("CACHEPILOT_LONG_TASKS_TIMEOUT_THRESHOLD_S", "not-a-number")
    assert CachePilotConfig.from_env().long_tasks.timeout_threshold_s == 20.0


def test_invalid_log_level_rejected():
    with pytest.raises(ValueError):
        CachePilotConfig(log_level="LOUD")


def test_invalid_log_format_rejected():
    with pytest.raises(ValueError):
        CachePilotConfig(log_format="xml")


# ---------------------------------------------------------------------------
# emit_debug — format, gating, secret safety
# ---------------------------------------------------------------------------


def test_emit_debug_kv(caplog):
    cfg = CachePilotConfig()
    logger = logging.getLogger("cachepilot_hermes.test")
    with caplog.at_level(logging.DEBUG, logger="cachepilot_hermes.test"):
        emit_debug(cfg, logger, "cachepilot.test", tool_name="read_file", n=3)
    assert len(caplog.records) == 1
    line = caplog.records[0].getMessage()
    assert "event=cachepilot.test" in line
    assert "plugin=cachepilot-hermes-plugin" in line
    assert "tool_name=read_file" in line
    assert "n=3" in line


def test_emit_debug_json(caplog):
    cfg = CachePilotConfig(log_format="json")
    logger = logging.getLogger("cachepilot_hermes.test.json")
    with caplog.at_level(logging.DEBUG, logger="cachepilot_hermes.test.json"):
        emit_debug(cfg, logger, "cachepilot.test", tool_name="read_file")
    assert len(caplog.records) == 1
    payload = json.loads(caplog.records[0].getMessage())
    assert payload["event"] == "cachepilot.test"
    assert payload["plugin"] == "cachepilot-hermes-plugin"
    assert payload["tool_name"] == "read_file"


def test_emit_debug_disabled_is_silent(caplog):
    cfg = CachePilotConfig(enabled=False)
    logger = logging.getLogger("cachepilot_hermes.test.disabled")
    with caplog.at_level(logging.DEBUG, logger="cachepilot_hermes.test.disabled"):
        emit_debug(cfg, logger, "cachepilot.test", tool_name="x")
    assert not caplog.records


def test_emit_debug_level_gated(caplog):
    cfg = CachePilotConfig(log_level="INFO")
    logger = logging.getLogger("cachepilot_hermes.test.gated")
    with caplog.at_level(logging.DEBUG, logger="cachepilot_hermes.test.gated"):
        emit_debug(cfg, logger, "cachepilot.test", tool_name="x")
    assert not caplog.records


def test_emit_debug_redacts_containers(caplog):
    cfg = CachePilotConfig()
    logger = logging.getLogger("cachepilot_hermes.test.redact")
    with caplog.at_level(logging.DEBUG, logger="cachepilot_hermes.test.redact"):
        emit_debug(cfg, logger, "cachepilot.test", payload={"secret": "hunter2"})
    assert len(caplog.records) == 1
    line = caplog.records[0].getMessage()
    assert "hunter2" not in line
    assert "dict(len=1)" in line  # container reduced to a summary token


def test_emit_debug_drops_none_fields(caplog):
    cfg = CachePilotConfig()
    logger = logging.getLogger("cachepilot_hermes.test.none")
    with caplog.at_level(logging.DEBUG, logger="cachepilot_hermes.test.none"):
        emit_debug(cfg, logger, "cachepilot.test", duration_ms=None, session_id="s1")
    line = caplog.records[0].getMessage()
    assert "session_id=s1" in line
    assert "duration_ms" not in line
