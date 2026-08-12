"""CachePilot plugin settings and structured-debug emitter (PRD §125 — config.py).

Minimal pydantic v2 settings for the Hermes plugin skeleton, driven by
``CACHEPILOT_*`` environment variables with sensible defaults. No secrets
are ever read here — AGENTS.md rule 10 (no raw prompt/secret persistence)
is a hard boundary for the whole plugin.

Only pydantic is required (matching ``packages/core``); ``pydantic-settings``
can replace the tiny :meth:`CachePilotConfig.from_env` loader later if the
config surface grows.

The structured-debug emitter also lives here (keeping to the PRD §125 module
list): it is the single, level-gated, secret-safe path through which every
hook and middleware callback writes its ``key=value`` / JSON line.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel

# Canonical distribution/plugin name — mirrored by pyproject.toml and
# PLUGIN_MANIFEST. Defined here (the leaf module) so emit_debug can tag every
# line without a config -> plugin import cycle.
PLUGIN_NAME = "cachepilot-hermes-plugin"

# One shared logger for every plugin module so a single level change gates all
# CachePilot debug output.
PLUGIN_LOGGER_NAME = "cachepilot_hermes"

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogFormat = Literal["kv", "json"]


class CachePilotConfig(BaseModel):
    """Plugin settings.

    Attributes:
        enabled: Master switch. When False the plugin still registers every
            middleware kind and hook (deterministic registration, zero traffic
            impact) but emits no logs and performs no observation. Fail open:
            middleware pass-through never depends on this flag.
        log_level: Minimum level at which the plugin's structured debug lines
            are emitted. The skeleton only produces DEBUG lines, so
            ``log_level="INFO"`` silences them.
        log_format: ``"kv"`` for one ``key=value`` line per event, ``"json"``
            for one JSON object per line.
    """

    enabled: bool = True
    log_level: LogLevel = "DEBUG"
    log_format: LogFormat = "kv"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CachePilotConfig:
        """Build settings from ``CACHEPILOT_*`` environment variables.

        Unknown/absent variables fall back to the model defaults. Values are
        normalized (case-insensitive) before pydantic validation.
        """
        env = os.environ if env is None else env
        return cls(
            enabled=_env_flag(env.get("CACHEPILOT_ENABLED", "true")),
            log_level=env.get("CACHEPILOT_LOG_LEVEL", "DEBUG").strip().upper(),
            log_format=env.get("CACHEPILOT_LOG_FORMAT", "kv").strip().lower(),
        )


def emit_debug(
    config: CachePilotConfig,
    logger: logging.Logger,
    event: str,
    **fields: Any,
) -> None:
    """Emit one structured DEBUG line, level-gated and secret-safe.

    Three gates: the plugin master switch, the configured ``log_level``
    threshold, and the logger's effective level. Callbacks must never pass
    payload values (prompts, args, results, headers, error messages) as
    fields — this emitter additionally reduces containers to
    ``Type(len=N)`` summaries and unknown objects to their type name, so a
    stray dict can never leak its contents into a log line.
    """
    if not config.enabled:
        return
    if logging.DEBUG < _level_number(config.log_level):
        return
    if not logger.isEnabledFor(logging.DEBUG):
        return
    safe = {key: _safe_value(value) for key, value in fields.items() if value is not None}
    if config.log_format == "json":
        line = json.dumps(
            {"event": event, "plugin": PLUGIN_NAME, **safe},
            sort_keys=True,
            default=str,
        )
    else:
        parts = [f"event={event}", f"plugin={PLUGIN_NAME}"]
        parts.extend(f"{key}={_kv_value(value)}" for key, value in safe.items())
        line = " ".join(parts)
    logger.debug("%s", line)


def _env_flag(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _level_number(name: LogLevel) -> int:
    return getattr(logging, name, logging.DEBUG)


def _safe_value(value: Any) -> Any:
    """Scalars pass through; containers/objects become summary tokens."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return f"{type(value).__name__}(len={len(value)})"
    return type(value).__name__


def _kv_value(value: Any) -> str:
    text = str(value)
    if any(ch.isspace() for ch in text) or "=" in text:
        return f'"{text}"'
    return text
