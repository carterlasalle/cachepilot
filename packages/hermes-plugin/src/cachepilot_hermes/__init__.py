"""CachePilot Hermes plugin — external Hermes Agent plugin skeleton (PRD §125, §128).

Phase 1 deliverable: a plugin package installed via the ``hermes_agent.plugins``
entry-point group that registers all four middleware kinds and the PRD §16
lifecycle hooks. Every callback is a pure pass-through observer emitting
structured, level-gated debug logs — stock Hermes behavior is identical with
the plugin installed (enforced by ``tests/test_stock_hermes_unchanged.py``).

``targets.py`` and ``relay_client.py`` (PRD §125) belong to later phases and
are intentionally not created here.
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
