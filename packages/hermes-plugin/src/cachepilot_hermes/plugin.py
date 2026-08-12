"""CachePilot Hermes plugin entry point (PRD §125 / §128 — plugin.py).

Phase 1 skeleton: a manifest, a ``create_plugin()``/``register(ctx)`` API,
and registration of every middleware kind and lifecycle hook as pure
pass-through observers that only emit structured, level-gated debug logs.

Hermes integration (v0.20.0, installed source at
``hermes_cli/plugins.py``/``hermes_cli/middleware.py``):

- The package is discovered as a pip plugin via the ``hermes_agent.plugins``
  entry-point group; ``PluginManager._load_plugin`` imports the module named
  by the entry point and calls its ``register(ctx)`` with a
  :class:`~hermes_cli.plugins.PluginContext`.
- Middleware kinds: PRD §16 lists ``tool_request`` / ``llm_request`` /
  ``llm_execution``; the installed source additionally exposes
  ``tool_execution`` (``VALID_MIDDLEWARE`` in hermes_cli/middleware.py), so
  all four are registered.
- Hooks: every name in PRD §16 exists verbatim in the installed
  ``VALID_HOOKS`` — no name mapping is required.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from cachepilot_hermes.config import PLUGIN_NAME, CachePilotConfig
from cachepilot_hermes.lifecycle import HOOK_NAMES, make_hook_handlers
from cachepilot_hermes.llm_middleware import (
    make_llm_execution_middleware,
    make_llm_request_middleware,
)
from cachepilot_hermes.tool_middleware import (
    make_tool_execution_middleware,
    make_tool_request_middleware,
)

PLUGIN_VERSION = "0.1.0"
PLUGIN_DESCRIPTION = (
    "Cost-aware KV-cache lease optimization for stock Hermes Agent — Phase 1 "
    "skeleton: pass-through middleware + lifecycle hooks with structured debug "
    "logs. No behavior change to stock Hermes."
)
PLUGIN_ENTRYPOINT = "cachepilot_hermes.plugin:register"

# Hermes v0.20.0 middleware kinds (hermes_cli/middleware.py::VALID_MIDDLEWARE).
MIDDLEWARE_KINDS: tuple[str, ...] = (
    "tool_request",
    "tool_execution",
    "llm_request",
    "llm_execution",
)


class PluginManifest(BaseModel):
    """Validated plugin identity (mirrors Hermes' PluginManifest fields)."""

    name: str
    version: str
    description: str
    entrypoint: str
    hooks: tuple[str, ...]
    middleware: tuple[str, ...]


PLUGIN_MANIFEST = PluginManifest(
    name=PLUGIN_NAME,
    version=PLUGIN_VERSION,
    description=PLUGIN_DESCRIPTION,
    entrypoint=PLUGIN_ENTRYPOINT,
    hooks=HOOK_NAMES,
    middleware=MIDDLEWARE_KINDS,
)


class CachePilotPlugin:
    """One configured plugin instance: bound middleware + hook callbacks.

    Registering is deterministic and idempotent-safe by construction: each
    instance builds its own callback set, and ``register()`` only appends to
    the context registries. Whether the plugin observes anything at runtime
    is governed by :attr:`config` (``enabled`` / ``log_level``) — never by
    whether it is registered.
    """

    def __init__(self, config: CachePilotConfig | None = None) -> None:
        self.config = config or CachePilotConfig.from_env()
        self.middleware: dict[str, Callable[..., Any]] = {
            "tool_request": make_tool_request_middleware(self.config),
            "tool_execution": make_tool_execution_middleware(self.config),
            "llm_request": make_llm_request_middleware(self.config),
            "llm_execution": make_llm_execution_middleware(self.config),
        }
        self.hooks: dict[str, Callable[..., Any]] = dict(make_hook_handlers(self.config))

    def register(self, ctx: Any) -> None:
        """Register every middleware kind and lifecycle hook on *ctx*.

        *ctx* is a Hermes :class:`~hermes_cli.plugins.PluginContext` (or any
        duck-typed stand-in exposing ``register_middleware(kind, cb)`` and
        ``register_hook(name, cb)``). No other context surface is touched —
        no tools, commands, skills, or config mutation.
        """
        for kind, callback in self.middleware.items():
            ctx.register_middleware(kind, callback)
        for hook_name, callback in self.hooks.items():
            ctx.register_hook(hook_name, callback)


def create_plugin(config: CachePilotConfig | None = None) -> CachePilotPlugin:
    """Build a configured plugin instance.

    Args:
        config: explicit settings; when None, ``CachePilotConfig.from_env()``
            is used (``CACHEPILOT_*`` environment variables).
    """
    return CachePilotPlugin(config)


def register(ctx: Any) -> None:
    """Hermes plugin entry point — invoked by ``PluginManager._load_plugin``.

    Declared in pyproject.toml under ``[project.entry-points.
    "hermes_agent.plugins"]``; Hermes imports ``cachepilot_hermes.plugin``
    and calls this function with its ``PluginContext``.
    """
    create_plugin().register(ctx)
