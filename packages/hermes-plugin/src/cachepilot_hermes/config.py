"""CachePilot plugin settings and structured-debug emitter (PRD §125 — config.py).

Minimal pydantic v2 settings for the Hermes plugin, driven by
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

from pydantic import BaseModel, Field

# Canonical distribution/plugin name — mirrored by pyproject.toml and
# PLUGIN_MANIFEST. Defined here (the leaf module) so emit_debug can tag every
# line without a config -> plugin import cycle.
PLUGIN_NAME = "cachepilot-hermes-plugin"

# One shared logger for every plugin module so a single level change gates all
# CachePilot debug output.
PLUGIN_LOGGER_NAME = "cachepilot_hermes"

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogFormat = Literal["kv", "json"]

# PRD §42 static long-running hints. Prefix matches against the normalized
# command tokens; entries are lower-case families ("uv run pytest" matches
# "uv run pytest tests/unit -x"). Overridable via
# CACHEPILOT_LONG_TASKS_KNOWN_LONG_COMMANDS (comma-separated).
DEFAULT_KNOWN_LONG_COMMANDS: tuple[str, ...] = (
    "pytest",
    "uv run pytest",
    "yarn test",
    "yarn build",
    "yarn lint",
    "docker build",
    "docker compose build",
    "cargo build",
    "cargo test",
    "make",
    "cmake --build",
    "ninja",
    "git clone",
    "git submodule update",
    "pip install",
    "npm install",
    "apt-get install",
    "brew install",
    "mvn test",
    "mvn package",
    "gradle build",
    "go test",
    "go build",
    "benchmark",
    "bench",
)

# PRD §42 likely-fast commands (also prefix-matched).
DEFAULT_KNOWN_FOREGROUND_COMMANDS: tuple[str, ...] = (
    "pwd",
    "ls",
    "git status",
    "git diff",
    "git diff --stat",
    "cat",
    "head",
    "tail",
    "rg",
    "sed",
    "which",
    "echo",
    "printf",
    "true",
    "false",
)


class LongTasksSettings(BaseModel):
    """Long-task manager settings (PRD §84 ``long_tasks``).

    Attributes:
        enabled: Master switch for the long-task runtime (classifier,
            auto-background, duration learning, target tracking).
        auto_background: When True the ``tool_request`` middleware promotes
            LONG_RUNNING terminal calls to ``background=True`` with a
            completion notification (PRD §40).
        timeout_threshold_s: A requested tool timeout at or above this many
            seconds is a long-running hint (PRD §41); learned durations are
            promoted when their p90 crosses this threshold (PRD §43).
        learn_command_durations: When True, terminal command durations are
            recorded into the SQLite ``command_history`` store (PRD §82).
        notify_on_complete: When True, auto-backgrounded calls also get
            ``notify_on_complete=True`` so the LLM is woken exactly once on
            completion instead of polling (PRD §40, §45).
        known_long_commands: Static long-running command families (§42).
        known_foreground_commands: Static likely-fast command families (§42).
        db_path: SQLite database path for ``command_history``
            (``CACHEPILOT_LONG_TASKS_DB_PATH``); default ``~/.cachepilot/``.
        enforce_foreground_hard_policy: PRD §44 — when True, an explicit
            ``background=false`` is overridden for commands that otherwise
            classify LONG_RUNNING (requested timeout / learned / static).
            Default False: explicit user intent always wins.
    """

    enabled: bool = True
    auto_background: bool = True
    timeout_threshold_s: float = 20.0
    learn_command_durations: bool = True
    notify_on_complete: bool = True
    known_long_commands: tuple[str, ...] = DEFAULT_KNOWN_LONG_COMMANDS
    known_foreground_commands: tuple[str, ...] = DEFAULT_KNOWN_FOREGROUND_COMMANDS
    db_path: str | None = None
    enforce_foreground_hard_policy: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LongTasksSettings:
        """Build long-task settings from ``CACHEPILOT_LONG_TASKS_*`` variables.

        Malformed numeric values fall back to the default so a bad variable can
        never break plugin load (fail open for traffic).
        """
        env = os.environ if env is None else env
        return cls(
            enabled=_env_flag(env.get("CACHEPILOT_LONG_TASKS_ENABLED", "true")),
            auto_background=_env_flag(
                env.get("CACHEPILOT_LONG_TASKS_AUTO_BACKGROUND", "true")
            ),
            timeout_threshold_s=_env_float(
                env.get("CACHEPILOT_LONG_TASKS_TIMEOUT_THRESHOLD_S"), 20.0
            ),
            learn_command_durations=_env_flag(
                env.get("CACHEPILOT_LONG_TASKS_LEARN_COMMAND_DURATIONS", "true")
            ),
            notify_on_complete=_env_flag(
                env.get("CACHEPILOT_LONG_TASKS_NOTIFY_ON_COMPLETE", "true")
            ),
            known_long_commands=(
                _env_list(env.get("CACHEPILOT_LONG_TASKS_KNOWN_LONG_COMMANDS"))
                or DEFAULT_KNOWN_LONG_COMMANDS
            ),
            known_foreground_commands=(
                _env_list(env.get("CACHEPILOT_LONG_TASKS_KNOWN_FOREGROUND_COMMANDS"))
                or DEFAULT_KNOWN_FOREGROUND_COMMANDS
            ),
            db_path=env.get("CACHEPILOT_LONG_TASKS_DB_PATH") or None,
            enforce_foreground_hard_policy=_env_flag(
                env.get("CACHEPILOT_LONG_TASKS_ENFORCE_FOREGROUND_HARD_POLICY", "false")
            ),
        )


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
        long_tasks: Long-task runtime settings (PRD §84).
    """

    enabled: bool = True
    log_level: LogLevel = "DEBUG"
    log_format: LogFormat = "kv"
    long_tasks: LongTasksSettings = Field(default_factory=LongTasksSettings)

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
            long_tasks=LongTasksSettings.from_env(env),
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


def _env_float(raw: str | None, default: float) -> float:
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_list(raw: str | None) -> tuple[str, ...] | None:
    if raw is None:
        return None
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    return tuple(parts) if parts else None


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
