"""PRD §128 task 6 — CI test asserting stock Hermes is never modified.

Snapshots the Hermes install tree's file hashes, imports and registers the
CachePilot plugin (against a duck-typed PluginContext), re-hashes, and
asserts zero differences — proving the plugin performs no monkey-patching
and no source modification (AGENTS.md rule 1 / Definition of Done).

The install root comes from ``$HERMES_AGENT_DIR`` when set, else the
default ``/home/hermes/.hermes/hermes-agent``. When the tree is absent
(e.g. CI without Hermes) the test skips gracefully with ``pytest.skip`` so
it can never false-fail.

``__pycache__``/``*.pyc`` artifacts are excluded from both snapshots: they
are volatile import caches, not source. Everything else (sources, tests,
docs, config) is hashed byte-for-byte.
"""

import hashlib
import importlib
import os
from pathlib import Path

import pytest

DEFAULT_HERMES_INSTALL = Path("/home/hermes/.hermes/hermes-agent")
_IGNORED_DIRS = {"__pycache__", ".git", ".venv", "node_modules", ".pytest_cache", "dist", "build"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


def _hermes_install_dir() -> Path | None:
    raw = os.environ.get("HERMES_AGENT_DIR", "").strip()
    candidate = Path(raw) if raw else DEFAULT_HERMES_INSTALL
    return candidate if candidate.is_dir() else None


def _snapshot(root: Path) -> dict:
    """Map relative path -> sha256 for every non-cache file under *root*."""
    hashes = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _IGNORED_DIRS for part in path.parts):
            continue
        if path.suffix in _IGNORED_SUFFIXES:
            continue
        rel = str(path.relative_to(root))
        hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


class TestStockHermesUnchanged:
    def test_import_and_register_leave_install_tree_untouched(self):
        install = _hermes_install_dir()
        if install is None:
            pytest.skip(
                "Hermes install tree not found (set HERMES_AGENT_DIR to enable "
                "this test)"
            )

        before = _snapshot(install)
        assert before, f"empty snapshot of {install}"

        # Import AFTER the snapshot so the measured window covers the import.
        plugin_mod = importlib.import_module("cachepilot_hermes.plugin")

        class FakePluginContext:
            def __init__(self):
                self.middleware = {}
                self.hooks = {}

            def register_middleware(self, kind, callback):
                self.middleware.setdefault(kind, []).append(callback)

            def register_hook(self, hook_name, callback):
                self.hooks.setdefault(hook_name, []).append(callback)

        plugin_mod.register(FakePluginContext())

        after = _snapshot(install)
        changed = {p for p in before if before.get(p) != after.get(p)}
        added = set(after) - set(before)
        removed = set(before) - set(after)
        diffs = sorted(changed | added | removed)
        assert not diffs, (
            "importing/registering cachepilot_hermes modified the Hermes "
            f"install tree ({install}): {diffs}"
        )
