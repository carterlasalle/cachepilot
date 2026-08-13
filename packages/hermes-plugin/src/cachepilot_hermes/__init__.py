"""CachePilot Hermes plugin — external Hermes Agent plugin (PRD §125, §128-129).

Phase 1 established the skeleton: a plugin package installed via the
``hermes_agent.plugins`` entry-point group that registers all four middleware
kinds and the PRD §16 lifecycle hooks.

Phase 2 adds the long-task runtime on top of that skeleton, still without
touching Hermes source:

- ``classifier.py`` — deterministic terminal long-task classifier (no LLM);
- ``duration_history.py`` — SQLite command-duration learner (§43, §82);
- ``targets.py`` — background-target registry with refcounts (§46, §48);
- ``tool_middleware.py`` — auto-background promotion with completion
  notifications (§40);
- ``lifecycle.py`` — duration recording + subagent target tracking.

Stock Hermes behavior is byte-identical for every non-terminal tool and for
terminal calls that classify foreground (enforced by
``tests/test_stock_hermes_unchanged.py``).
"""

from cachepilot_hermes.config import PLUGIN_NAME, CachePilotConfig
from cachepilot_hermes.plugin import (
    HOOK_NAMES,
    MIDDLEWARE_KINDS,
    PLUGIN_DESCRIPTION,
    PLUGIN_ENTRYPOINT,
    PLUGIN_MANIFEST,
    PLUGIN_VERSION,
    CachePilotPlugin,
    PluginManifest,
    create_plugin,
    register,
)

__version__ = PLUGIN_VERSION

__all__ = [
    "HOOK_NAMES",
    "MIDDLEWARE_KINDS",
    "PLUGIN_DESCRIPTION",
    "PLUGIN_ENTRYPOINT",
    "PLUGIN_MANIFEST",
    "PLUGIN_NAME",
    "PLUGIN_VERSION",
    "CachePilotConfig",
    "CachePilotPlugin",
    "PluginManifest",
    "create_plugin",
    "register",
]
