"""Deterministic long-task classifier (PRD §39-44, §108 — classifier.py).

Decides whether a terminal command should run in the foreground or be
auto-backgrounded. Purely deterministic — no LLM call, ever (PRD §41, §45).

Decision order (fail-safe: the default is foreground):

1. explicit ``background=true``  -> LONG_RUNNING (respect the user, §44)
2. explicit ``background=false`` -> FOREGROUND unless a hard policy is
   configured to override it (§44)
3. requested tool timeout >= threshold -> LONG_RUNNING
4. learned duration p90 >= threshold (with enough samples) -> LONG_RUNNING (§43)
5. known-fast command family (pwd, ls, git status, ...) -> FOREGROUND (§42)
6. known-long command family (pytest, docker build, ...) -> LONG_RUNNING (§42)
7. anything else -> FOREGROUND

Inputs (PRD §41): tool name, command, requested timeout, known command
family, historical execution duration, explicit user flags, environment.
The environment is a documented-reserved input: every current signal is
derived from the command/args/history, so the classifier behaves identically
regardless of the caller's environment.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from cachepilot_hermes.config import CachePilotConfig
from cachepilot_hermes.duration_history import CommandDurationStats

FOREGROUND = "foreground"
LONG_RUNNING = "long_running"

Decision = Literal["foreground", "long_running"]

# A single observation is noise; require at least two historical samples
# before learned durations may promote a command (PRD §43).
MIN_LEARNED_SAMPLES = 2


@dataclass(frozen=True)
class Classification:
    """Classifier verdict + human-readable reason (PRD §145 explainability)."""

    decision: Decision
    reason: str


class LongTaskClassifier:
    """Stateless deterministic classifier bound to one plugin configuration."""

    def __init__(self, config: CachePilotConfig | None = None) -> None:
        self.config = config or CachePilotConfig()
        self._settings = self.config.long_tasks
        self._long_families = _tokenize_families(self._settings.known_long_commands)
        self._fast_families = _tokenize_families(self._settings.known_foreground_commands)

    def classify(
        self,
        args: Mapping[str, Any] | None,
        history: CommandDurationStats | None = None,
        env: Mapping[str, str] | None = None,
        *,
        tool_name: str = "terminal",
    ) -> Classification:
        """Classify one terminal tool call.

        Args:
            args: The tool arguments mapping (``command``, ``timeout``,
                ``background``, ...).
            history: Optional learned duration stats for the command
                (:class:`CommandDurationStats`); None when unknown.
            env: Reserved input (PRD §41) — currently unused by every signal;
                accepted so the environment is part of the public surface.
            tool_name: Only ``terminal`` is classified; anything else is
                foreground by definition.
        """
        if tool_name != "terminal":
            return Classification(FOREGROUND, "not a terminal call")
        if not isinstance(args, Mapping):
            return Classification(FOREGROUND, "malformed tool args")
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return Classification(FOREGROUND, "no command")

        # 1/2. Explicit user intent wins (PRD §44).
        explicit_background = args.get("background")
        if explicit_background is True:
            return Classification(LONG_RUNNING, "explicit background=true")
        if explicit_background is False and not self._settings.enforce_foreground_hard_policy:
            return Classification(FOREGROUND, "explicit background=false")

        # 3. Requested timeout hint.
        requested_timeout = _as_float(args.get("timeout"))
        if (
            requested_timeout is not None
            and requested_timeout >= self._settings.timeout_threshold_s
        ):
            return Classification(
                LONG_RUNNING,
                f"requested timeout {requested_timeout:g}s >= threshold",
            )

        # 4. Learned duration promotion (PRD §43).
        if (
            history is not None
            and history.sample_count >= MIN_LEARNED_SAMPLES
            and history.runtime_p90 is not None
            and history.runtime_p90 >= self._settings.timeout_threshold_s
        ):
            return Classification(
                LONG_RUNNING,
                f"learned p90 {history.runtime_p90:g}s >= threshold",
            )

        # 5/6. Static hints are prefix-matched command families (§42).
        tokens = _command_tokens(command)
        if _matches_any_family(tokens, self._fast_families):
            return Classification(FOREGROUND, "known-fast command family")
        if _matches_any_family(tokens, self._long_families):
            return Classification(LONG_RUNNING, "known-long command family")

        return Classification(FOREGROUND, "default")


def _command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _tokenize_families(entries: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(entry.split()) for entry in entries if entry.strip())


def _matches_any_family(tokens: list[str], families: tuple[tuple[str, ...], ...]) -> bool:
    for family in families:
        if len(tokens) >= len(family) and all(
            actual.lower() == expected.lower()
            for actual, expected in zip(tokens[: len(family)], family)
        ):
            return True
    return False


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
