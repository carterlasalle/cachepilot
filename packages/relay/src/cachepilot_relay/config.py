"""Relay settings (PRD §84 ``relay`` block; PRD §26 bind policy).

``cachepilotd`` listens on ``127.0.0.1:8787`` by default and NEVER binds a
wildcard address (``0.0.0.0`` / ``::``) unless explicitly allowed — the
relay is a local process serving Hermes on the same machine (PRD §26, §85).
Phase 4 adds the telemetry store location (PRD §81) and the observation
master switch. No secrets are read here (AGENTS.md rule 10).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal
from typing import Self
from urllib.parse import urlsplit

from cachepilot_core.leases import LeaseSettings
from cachepilot_core.storage import default_db_path
from pydantic import BaseModel, Field, field_validator, model_validator

#: Default listen address (PRD §26).
DEFAULT_LISTEN = "127.0.0.1:8787"

#: Local control endpoint proving CachePilot relay presence (E2E-002).
#: This one path is answered by the relay itself and NEVER forwarded
#: upstream (a deliberate PRD §27 deviation; see
#: ``cachepilot_relay.server.create_app``). The CLI and dashboard relay
#: probes GET it and require its distinctive JSON body before reporting
#: 'healthy' — a bare TCP connect or any other HTTP server on the port is
#: not the relay.
RELAY_HEALTH_PATH = "/cachepilot/health"

#: Environment variables, following the plugin's ``CACHEPILOT_*`` convention.
ENV_LISTEN = "CACHEPILOT_RELAY_LISTEN"
ENV_UPSTREAM = "CACHEPILOT_UPSTREAM"
ENV_ALLOW_EXTERNAL_BIND = "CACHEPILOT_RELAY_ALLOW_EXTERNAL_BIND"
ENV_TELEMETRY_DB = "CACHEPILOT_TELEMETRY_DB"
ENV_OBSERVATION_ENABLED = "CACHEPILOT_RELAY_OBSERVATION_ENABLED"
#: P09 (PRD §71-72, UC-5): router-miss analysis master switch (default on).
ENV_ROUTE_INTEL = "CACHEPILOT_ROUTE_INTEL"
#: P10 (PRD §25, §137, §164): cache churn detection master switch (default on;
#: independent toggle — disabling it records ZERO churn events).
ENV_CHURN_DETECTION = "CACHEPILOT_CHURN_DETECTION_ENABLED"
#: P09 (PRD §73-74): economic route affinity master switch — OPTIONAL,
#: never on by default.
ENV_ROUTE_AFFINITY = "CACHEPILOT_ROUTE_AFFINITY"
ENV_ROUTE_AFFINITY_EXTRA_COST = "CACHEPILOT_ROUTE_AFFINITY_EXTRA_COST_USD"
ENV_ROUTE_AFFINITY_MARGIN = "CACHEPILOT_ROUTE_AFFINITY_SAFETY_MARGIN_USD"

#: Wildcard bind hosts refused without an explicit override (PRD §26).
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::"})


def parse_listen(value: str) -> tuple[str, int]:
    """Split ``host:port`` into ``(host, port)``; raises ValueError when malformed.

    Port 0 means "OS-assigned ephemeral port" (used by the test harness);
    IPv6 literals in brackets are supported (``[::1]:8787``).
    """
    value = value.strip()
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        if not rest.startswith(":"):
            raise ValueError(f"listen address {value!r} must be host:port")
        port_text = rest[1:]
    else:
        host, _, port_text = value.rpartition(":")
    if not host:
        raise ValueError(f"listen address {value!r} has an empty host")
    if not port_text.isdigit():
        raise ValueError(f"listen address {value!r} has a non-numeric port")
    port = int(port_text)
    if not 0 <= port <= 65535:
        raise ValueError(f"listen address {value!r} has an out-of-range port")
    return host, port


class RelayConfig(BaseModel):
    """Relay settings.

    Attributes:
        listen: ``host:port`` the relay binds (default ``127.0.0.1:8787``).
        upstream: provider base URL every request is forwarded to (required).
        allow_external_bind: explicit override permitting wildcard binds
            (``0.0.0.0`` / ``::``). Off by default (PRD §26).
        telemetry_db_path: SQLite telemetry database path (PRD §81); None
            resolves to ``~/.hermes/cachepilot/cachepilot.db``.
        observation_enabled: master switch for Phase 4 observation. When
            False the relay is pure Phase 3 pass-through (no telemetry store
            is opened, no headers are added or parsed).
    """

    listen: str = DEFAULT_LISTEN
    upstream: str
    allow_external_bind: bool = False
    telemetry_db_path: str | None = None
    observation_enabled: bool = True
    #: P09 (PRD §71-72, UC-5): router-miss analysis. When False the observer
    #: neither classifies misses after route changes nor records route
    #: events (``CACHEPILOT_ROUTE_INTEL``, default true).
    route_intel_enabled: bool = True
    #: P10 (PRD §25, §137, §164): cache churn detection. When False the
    #: observer records ZERO churn events — the PRD §164 independent toggle
    #: (``CACHEPILOT_CHURN_DETECTION_ENABLED``, default true). Boolean flags
    #: and classifier enrichment both disappear; request telemetry is
    #: unaffected.
    churn_detection_enabled: bool = True
    #: P09 (PRD §73-74): economic route affinity. OPTIONAL and never on by
    #: default (``CACHEPILOT_ROUTE_AFFINITY``); even when enabled, affinity
    #: is only applied when the provider adapter reports ``can_pin_route()``
    #: and the PRD §73 economic gate approves.
    route_affinity_enabled: bool = False
    #: Per-request premium of pinning to the previous route (PRD §73 "extra
    #: route cost"), compared against the expected cache recompute savings.
    route_affinity_extra_cost_usd: Decimal = Field(default=Decimal("0.0"), ge=0)
    #: Extra margin on top of the strict savings > cost comparison (PRD §73).
    route_affinity_safety_margin_usd: Decimal = Field(default=Decimal("0.0"), ge=0)
    #: Lease scheduling settings (PRD §53-54, §84 cache.scheduling; Phase 5
    #: dry-run defaults). Read from ``CACHEPILOT_LEASE_*`` by
    #: :meth:`LeaseSettings.from_env`.
    lease_settings: LeaseSettings = Field(default_factory=LeaseSettings)

    @field_validator("listen")
    @classmethod
    def _validate_listen(cls, value: str) -> str:
        parse_listen(value)
        return value

    @field_validator("upstream")
    @classmethod
    def _validate_upstream(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"upstream {value!r} must be an absolute http(s) URL")
        return value

    @model_validator(mode="after")
    def _refuse_wildcard_bind(self) -> Self:
        host, _ = parse_listen(self.listen)
        if host in WILDCARD_HOSTS and not self.allow_external_bind:
            raise ValueError(
                f"refusing to bind {host}: cachepilotd is a local relay and never "
                "binds a wildcard address; pass --allow-external-bind or set "
                f"{ENV_ALLOW_EXTERNAL_BIND}=1 to override"
            )
        return self

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        listen: str | None = None,
        upstream: str | None = None,
        allow_external_bind: bool | None = None,
    ) -> RelayConfig:
        """Build settings from ``CACHEPILOT_*`` variables; explicit arguments
        (CLI flags) win over the environment (PRD §84).
        """
        env = os.environ if env is None else env
        effective_listen = listen or env.get(ENV_LISTEN) or DEFAULT_LISTEN
        effective_upstream = upstream or env.get(ENV_UPSTREAM)
        if not effective_upstream:
            raise ValueError(f"no upstream configured: pass --upstream or set {ENV_UPSTREAM}")
        if allow_external_bind is None:
            allow_external_bind = _env_flag(env.get(ENV_ALLOW_EXTERNAL_BIND))
        return cls(
            listen=effective_listen,
            upstream=effective_upstream,
            allow_external_bind=allow_external_bind,
            telemetry_db_path=env.get(ENV_TELEMETRY_DB) or str(default_db_path()),
            observation_enabled=_env_flag(env.get(ENV_OBSERVATION_ENABLED, "true")),
            route_intel_enabled=_env_flag(env.get(ENV_ROUTE_INTEL, "true")),
            churn_detection_enabled=_env_flag(env.get(ENV_CHURN_DETECTION, "true")),
            route_affinity_enabled=_env_flag(env.get(ENV_ROUTE_AFFINITY)),
            route_affinity_extra_cost_usd=_env_decimal(
                env.get(ENV_ROUTE_AFFINITY_EXTRA_COST), Decimal("0.0")
            ),
            route_affinity_safety_margin_usd=_env_decimal(
                env.get(ENV_ROUTE_AFFINITY_MARGIN), Decimal("0.0")
            ),
            lease_settings=LeaseSettings.from_env(env),
        )


def _env_flag(raw: str | None) -> bool:
    return raw is not None and raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_decimal(raw: str | None, default: Decimal) -> Decimal:
    """Parse a non-negative decimal env var; malformed values fall back to
    the default so a bad variable can never break the relay (fail open)."""
    if raw is None:
        return default
    try:
        return max(Decimal(raw.strip()), Decimal(0))
    except (TypeError, ValueError, ArithmeticError):
        return default
