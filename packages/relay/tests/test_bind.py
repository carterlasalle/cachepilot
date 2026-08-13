"""Relay bind policy (PRD §26): 127.0.0.1:8787 by default, never a wildcard.

The default-address logic is tested here, separately from the in-process
server tests (which use ephemeral ports to avoid collisions).
"""

from __future__ import annotations

import pytest
from cachepilot_relay.config import DEFAULT_LISTEN, RelayConfig, parse_listen
from cachepilot_relay.server import main
from pydantic import ValidationError


def test_default_listen_is_loopback_8787():
    config = RelayConfig(upstream="http://127.0.0.1:1")
    assert config.listen == DEFAULT_LISTEN == "127.0.0.1:8787"
    assert parse_listen(config.listen) == ("127.0.0.1", 8787)


def test_wildcard_bind_refused_without_explicit_override():
    with pytest.raises(ValidationError, match="refusing to bind"):
        RelayConfig(listen="0.0.0.0:8787", upstream="http://127.0.0.1:1")


def test_wildcard_bind_allowed_with_explicit_override():
    config = RelayConfig(
        listen="0.0.0.0:8787", upstream="http://127.0.0.1:1", allow_external_bind=True
    )
    assert parse_listen(config.listen) == ("0.0.0.0", 8787)


def test_main_refuses_wildcard_bind_with_clear_error(capsys):
    exit_code = main(["--listen", "0.0.0.0:8787", "--upstream", "http://127.0.0.1:1"])
    assert exit_code == 2
    assert "0.0.0.0" in capsys.readouterr().err


def test_main_requires_upstream(capsys):
    exit_code = main(["--listen", "127.0.0.1:8787"])
    assert exit_code == 2
    assert "upstream" in capsys.readouterr().err


def test_upstream_from_env_when_flag_absent(monkeypatch):
    monkeypatch.setenv("CACHEPILOT_UPSTREAM", "https://api.openai.com/v1")
    config = RelayConfig.from_env()
    assert config.upstream == "https://api.openai.com/v1"
    assert config.listen == "127.0.0.1:8787"


def test_upstream_flag_wins_over_env(monkeypatch):
    monkeypatch.setenv("CACHEPILOT_UPSTREAM", "https://env.invalid/v1")
    config = RelayConfig.from_env(upstream="https://flag.invalid/v1")
    assert config.upstream == "https://flag.invalid/v1"


def test_listen_env_is_honoured(monkeypatch):
    monkeypatch.setenv("CACHEPILOT_RELAY_LISTEN", "127.0.0.1:9999")
    config = RelayConfig.from_env(upstream="http://127.0.0.1:1")
    assert config.listen == "127.0.0.1:9999"


def test_malformed_listen_rejected():
    with pytest.raises(ValidationError):
        RelayConfig(listen="not-an-address", upstream="http://127.0.0.1:1")
    with pytest.raises(ValidationError):
        RelayConfig(listen="127.0.0.1:notaport", upstream="http://127.0.0.1:1")
    with pytest.raises(ValidationError):
        RelayConfig(listen=":8787", upstream="http://127.0.0.1:1")


def test_non_http_upstream_rejected():
    with pytest.raises(ValidationError):
        RelayConfig(upstream="ftp://files.invalid/v1")


def test_parse_listen_supports_ipv6_brackets():
    assert parse_listen("[::1]:8787") == ("::1", 8787)
