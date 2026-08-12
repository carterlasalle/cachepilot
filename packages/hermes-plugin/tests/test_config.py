"""CachePilot plugin settings + structured-debug emitter tests (PRD §128)."""

import json
import logging

import pytest
from cachepilot_hermes.config import CachePilotConfig, emit_debug

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_defaults():
    cfg = CachePilotConfig()
    assert cfg.enabled is True
    assert cfg.log_level == "DEBUG"
    assert cfg.log_format == "kv"


def test_from_env_defaults(monkeypatch):
    for var in ("CACHEPILOT_ENABLED", "CACHEPILOT_LOG_LEVEL", "CACHEPILOT_LOG_FORMAT"):
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
