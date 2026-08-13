"""Command duration learner (PRD §43 / §82 — duration_history.py).

Learns per-command runtime percentiles so the long-task classifier can promote
commands that are *empirically* slow even when they are not statically known
(PRD §43). Data lives in a SQLite (WAL) database whose ``command_history``
table matches the PRD §82 columns:

    signature, sample_count, runtime_p50, runtime_p90, runtime_p95,
    background_success_rate, updated_at

Privacy (AGENTS.md rule 10): the *signature* is a normalized command identity
— executable + recognized verb subcommands + flag NAMES only. Flag values,
positional arguments, secrets, prompts and tool output are never persisted.
``echo $TOKEN`` and ``python deploy.py --api-key sk-...`` collapse to safe
signatures (``echo``, ``python deploy.py --api-key``).

Fail open: any database error (bad path, unwritable dir, locked file) disables
the store with one warning; ``record``/``stats`` become no-ops and never
raise, so the plugin's traffic path is unaffected.
"""

from __future__ import annotations

import logging
import math
import os
import shlex
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from cachepilot_hermes.config import PLUGIN_LOGGER_NAME

logger = logging.getLogger(PLUGIN_LOGGER_NAME)

ENV_DB_PATH = "CACHEPILOT_LONG_TASKS_DB_PATH"
DEFAULT_DB_PATH = "~/.cachepilot/long_tasks.db"

# Rolling window of most recent durations kept per signature for percentile
# computation. Aggregates (p50/p90/p95) persist to SQLite; the window itself
# is process-local memory only (raw durations are transient metrics, not
# secrets — but keeping the window bounded keeps the learner honest).
WINDOW_SIZE = 512

# Shell metacharacters that terminate the "command" portion of a line — tokens
# after these are values of another command/redirection and never kept.
_SHELL_OPERATORS = frozenset({"&&", "||", ";", "|", ">", ">>", "<", "2>", "2>>", "&", "(", ")"})

# Words that are safe to persist in the second/third position of a signature:
# recognized subcommand verbs and well-known tool names. Anything else in a
# positional slot is treated as an argument VALUE and dropped — this is what
# keeps secrets and free-form arguments out of the database.
SAFE_SUBCOMMAND_WORDS = frozenset(
    {
        # verbs (subcommand position)
        "run", "test", "build", "install", "uninstall", "exec", "compose",
        "clone", "pull", "push", "add", "update", "upgrade", "lint", "fmt",
        "format", "check", "bench", "benchmark", "init", "new", "generate",
        "deploy", "serve", "watch", "dev", "start", "stop", "clean", "audit",
        "sync", "list", "show", "get", "set", "configure", "buildx", "search",
        "download", "fetch", "compile", "package", "publish", "login", "logout",
        "create", "delete", "remove", "reset", "verify", "validate", "export",
        "import", "migrate", "seed", "scaffold", "doctor", "env", "shell",
        "repl", "lock", "unlock", "stats", "status",
        # well-known tool/project words that can appear as subcommands or
        # after ``-m`` (e.g. ``python -m pytest``)
        "pytest", "unittest", "ruff", "mypy", "pip", "npm", "yarn", "pnpm",
        "uv", "poetry", "cargo", "docker", "make", "cmake", "ninja", "git",
        "go", "node", "python", "python3", "pipenv", "nox", "tox",
        "pre-commit", "black", "isort", "flake8", "sphinx", "mkdocs",
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS command_history (
    signature TEXT PRIMARY KEY,
    sample_count INTEGER NOT NULL,
    runtime_p50 REAL,
    runtime_p90 REAL,
    runtime_p95 REAL,
    background_success_rate REAL,
    updated_at REAL NOT NULL
)
"""


@dataclass(frozen=True)
class CommandDurationStats:
    """Aggregate duration stats for one normalized command signature (§82)."""

    signature: str
    sample_count: int
    runtime_p50: float | None = None
    runtime_p90: float | None = None
    runtime_p95: float | None = None
    background_success_rate: float | None = None


def normalize_signature(command: str) -> str:
    """Reduce a shell command to a persistable identity.

    Rules:
    - executable (first token) always kept;
    - up to two following positional words kept only when they are recognized
      safe subcommand words (verbs / tool names);
    - flag NAMES kept (``--flag=value`` -> ``--flag``), values never;
    - positional values, redirections, and anything after ``--`` dropped;
    - shell operators stop signature consumption.

    Returns ``""`` when nothing persistable remains (callers skip recording).
    """
    tokens = _tokenize(command)
    family: list[str] = []
    flags: set[str] = set()
    for token in tokens:
        if token in _SHELL_OPERATORS or token == "--":
            break
        if token.startswith("-"):
            if "=" in token:
                name = token.split("=", 1)[0]
            elif token.startswith("--"):
                name = token
            elif token == "-":
                continue  # stdin marker — a value position, not a flag
            else:
                # Short flag: keep the flag letter only so attached values are
                # never persisted (-j8 -> -j, -pSECRET -> -p).
                name = token[:2]
            if name:
                flags.add(name)
            continue
        if not family or len(family) < 3 and token.lower() in SAFE_SUBCOMMAND_WORDS:
            family.append(token)
        else:
            continue  # positional value — dropped from the signature
    if not family:
        return ""
    signature = " ".join(family)
    if flags:
        signature += " " + " ".join(sorted(flags))
    return signature


def percentile(sorted_samples: Sequence[float], p: float) -> float:
    """Linear-interpolation percentile (0 <= p <= 1) over sorted samples."""
    if not sorted_samples:
        raise ValueError("percentile requires at least one sample")
    if len(sorted_samples) == 1:
        return sorted_samples[0]
    rank = (len(sorted_samples) - 1) * p
    lower = math.floor(rank)
    upper = min(lower + 1, len(sorted_samples) - 1)
    fraction = rank - lower
    return sorted_samples[lower] + fraction * (sorted_samples[upper] - sorted_samples[lower])


class CommandDurationHistory:
    """SQLite-backed duration learner, fail-open by design.

    Args:
        db_path: Explicit database path. When None,
            ``CACHEPILOT_LONG_TASKS_DB_PATH`` is honored, falling back to
            ``~/.cachepilot/long_tasks.db``. Parent directories are created
            automatically.
    """

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self._path = _resolve_db_path(db_path)
        self._lock = threading.Lock()
        self._windows: dict[str, deque[float]] = {}
        self._success_windows: dict[str, deque[bool]] = {}
        self._disabled = False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as conn, conn:
                conn.execute(_SCHEMA)
        except Exception as exc:  # noqa: BLE001 — fail open: telemetry must never block traffic
            self._disabled = True
            logger.warning("cachepilot duration history disabled (%s): %s", self._path, exc)

    @property
    def disabled(self) -> bool:
        """True when the store failed to open and is a no-op (fail open)."""
        return self._disabled

    @property
    def db_path(self) -> Path:
        return self._path

    def record(
        self,
        command: str,
        duration_s: float,
        *,
        background: bool = False,
        success: bool = True,
    ) -> None:
        """Record one observed duration for *command*.

        Normalizes the signature, updates the in-memory rolling window,
        recomputes p50/p90/p95 and upserts the aggregate row. Never raises.
        """
        if self._disabled:
            return
        signature = normalize_signature(command)
        if not signature:
            return
        try:
            with self._lock:
                window = self._windows.setdefault(signature, deque(maxlen=WINDOW_SIZE))
                window.append(float(duration_s))
                samples = sorted(window)
                p50 = percentile(samples, 0.50)
                p90 = percentile(samples, 0.90)
                p95 = percentile(samples, 0.95)
                success_rate = self._updated_success_rate(signature, background, success)
                with closing(self._connect()) as conn, conn:
                    existing = conn.execute(
                        "SELECT background_success_rate FROM command_history "
                        "WHERE signature = ?",
                        (signature,),
                    ).fetchone()
                    previous_rate = existing[0] if existing is not None else None
                    rate = success_rate if background else previous_rate
                    conn.execute(
                        """
                        INSERT INTO command_history
                            (signature, sample_count, runtime_p50, runtime_p90,
                             runtime_p95, background_success_rate, updated_at)
                        VALUES (?, 1, ?, ?, ?, ?, ?)
                        ON CONFLICT(signature) DO UPDATE SET
                            sample_count = command_history.sample_count + 1,
                            runtime_p50 = excluded.runtime_p50,
                            runtime_p90 = excluded.runtime_p90,
                            runtime_p95 = excluded.runtime_p95,
                            background_success_rate = excluded.background_success_rate,
                            updated_at = excluded.updated_at
                        """,
                        (signature, p50, p90, p95, rate, time.time()),
                    )
        except Exception as exc:  # noqa: BLE001 — fail open: learning must never break traffic
            self._disable(exc)

    def stats(self, command: str) -> CommandDurationStats | None:
        """Return aggregate stats for *command* (normalized internally), else None."""
        if self._disabled:
            return None
        signature = normalize_signature(command)
        if not signature:
            return None
        try:
            with self._lock, closing(self._connect()) as conn:
                row = conn.execute(
                    """
                        SELECT signature, sample_count, runtime_p50, runtime_p90,
                               runtime_p95, background_success_rate
                        FROM command_history WHERE signature = ?
                        """,
                    (signature,),
                ).fetchone()
        except Exception as exc:  # noqa: BLE001 — fail open: telemetry must never break traffic
            self._disable(exc)
            return None
        if row is None:
            return None
        return CommandDurationStats(
            signature=row[0],
            sample_count=row[1],
            runtime_p50=row[2],
            runtime_p90=row[3],
            runtime_p95=row[4],
            background_success_rate=row[5],
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _disable(self, exc: Exception) -> None:
        self._disabled = True
        logger.warning("cachepilot duration history disabled (%s): %s", self._path, exc)

    def _updated_success_rate(self, signature: str, background: bool, success: bool) -> float | None:
        """Update the in-memory background-success window; return its mean.

        Only *background* runs count toward the rate (foreground runs leave
        it untouched). The window is bounded like the duration window; the
        mean is what gets persisted. Returns None when there is no background
        history yet.
        """
        if not background:
            return None
        window = self._success_windows.setdefault(signature, deque(maxlen=WINDOW_SIZE))
        window.append(bool(success))
        return sum(1 for flag in window if flag) / len(window)


def _resolve_db_path(db_path: str | os.PathLike[str] | None) -> Path:
    if db_path is None:
        db_path = os.environ.get(ENV_DB_PATH) or DEFAULT_DB_PATH
    return Path(os.path.expanduser(str(db_path)))


def _tokenize(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()
