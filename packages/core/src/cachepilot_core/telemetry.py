"""Outcome classification and telemetry event models — PRD §68-70, §80, §131.

Phase 4 (physical request observation) turns the relay's view of a physical
HTTP request/response pair into a structured telemetry event. The outcome
classification implements AGENTS.md invariant 3: **HTTP 200 ≠ cache hit**.
An outcome is only CONFIRMED_HIT with provider-specific evidence
(``cache_read_tokens > 0``); everything else degrades honestly to
MISS_REBUILT, SUCCESS_UNVERIFIED or FAILED (PRD §68-70).

Only hashes, timestamps, usage, route identities and outcomes are ever
carried here — never raw prompts, history or auth material (AGENTS.md
invariant 10, PRD §30).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cachepilot_core.usage import TokenUsage


class Outcome(str, Enum):
    """Cache outcome classification — PRD §68-70 (AGENTS.md invariant 3)."""

    CONFIRMED_HIT = "confirmed_hit"
    MISS_REBUILT = "miss_rebuilt"
    SUCCESS_UNVERIFIED = "success_unverified"
    FAILED = "failed"


#: Provider usage-payload keys that constitute trustworthy cache telemetry.
#: Mirrors the dialects handled by :class:`~cachepilot_core.usage.UsageNormalizer`
#: (OpenAI ``prompt_tokens_details.cached_tokens``, Anthropic
#: ``cache_read_input_tokens`` / ``cache_creation_input_tokens``, generic
#: ``cache_read_tokens``). The *presence* of these keys is what distinguishes
#: "provider returned usage with cache telemetry" from "provider returned
#: usage but hid cache telemetry" (PRD §70).
_TELEMETRY_KEYS = frozenset(
    {"cache_read_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"}
)
_TELEMETRY_DETAILS_KEYS = frozenset({"cached_tokens"})


def usage_has_cache_telemetry(usage: Any) -> bool:
    """True when a provider usage payload carries cache-telemetry fields.

    A payload that lacks these fields entirely provides *no trustworthy
    cache telemetry* (PRD §70) even when it reports plain token counts.
    """
    if not isinstance(usage, Mapping):
        return False
    if any(key in usage for key in _TELEMETRY_KEYS):
        return True
    details = usage.get("prompt_tokens_details")
    return isinstance(details, Mapping) and any(key in details for key in _TELEMETRY_DETAILS_KEYS)


def classify_outcome(
    *,
    status_code: int,
    telemetry_present: bool,
    cache_read_tokens: int,
) -> Outcome:
    """Classify one observed request/response pair (PRD §68-70).

    Args:
        status_code: HTTP status of the provider response.
        telemetry_present: whether the response carried trustworthy cache
            telemetry (see :func:`usage_has_cache_telemetry`).
        cache_read_tokens: normalized cache-read token count.

    Returns:
        FAILED for any non-2xx response (regardless of telemetry);
        CONFIRMED_HIT when telemetry shows ``cache_read_tokens > 0``;
        MISS_REBUILT when telemetry shows ``cache_read_tokens == 0``;
        SUCCESS_UNVERIFIED when the provider returned no trustworthy
        cache telemetry.
    """
    if not 200 <= status_code < 300:
        return Outcome.FAILED
    if telemetry_present:
        return Outcome.CONFIRMED_HIT if cache_read_tokens > 0 else Outcome.MISS_REBUILT
    return Outcome.SUCCESS_UNVERIFIED


class TelemetryEvent(BaseModel):
    """One observed provider request (PRD §80 event shape, §82 request_events).

    Hashes only: the request/cache fingerprints, the system/tools/history
    hashes and the (hashed) session identity. No raw content, no auth
    material (AGENTS.md invariant 10).
    """

    model_config = ConfigDict(extra="forbid")

    request_fingerprint: str = Field(..., min_length=1)
    cache_fingerprint: str = Field(..., min_length=1)
    provider: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    route_hash: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    outcome: Outcome
    request_kind: str = Field(default="normal", pattern="^(normal|warm)$")
    session_hash: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    system_hash: str | None = None
    tools_hash: str | None = None
    history_hash: str | None = None


class ChurnEvent(BaseModel):
    """A cache-identity transition for one session (PRD §25, §75, §82 churn_events)."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    session_hash: str | None = None
    previous_cache_fingerprint: str = Field(..., min_length=1)
    new_cache_fingerprint: str = Field(..., min_length=1)
    provider: str | None = None
    model: str | None = None
    route_hash: str | None = None
    system_changed: bool = False
    tools_changed: bool = False
    history_changed: bool = False
    route_changed: bool = False
    cache_key_changed: bool = False
    model_changed: bool = False
    #: Row id when read back from storage; None for freshly-built events.
    id: int | None = None


class CacheHealthStats(BaseModel):
    """Aggregated cache health for the CLI (PRD §77, §131)."""

    model_config = ConfigDict(extra="forbid")

    total: int = 0
    confirmed_hits: int = 0
    misses: int = 0
    unverified: int = 0
    failed: int = 0
    churn_events: int = 0
    route_changes: int = 0

    @property
    def telemetry_observed(self) -> int:
        """Requests whose outcome was decided by trustworthy cache telemetry."""
        return self.confirmed_hits + self.misses

    @property
    def hit_rate(self) -> float | None:
        """CONFIRMED_HIT / requests with telemetry; None when none observed."""
        observed = self.telemetry_observed
        if observed <= 0:
            return None
        return self.confirmed_hits / observed

    def record(self, outcome: Outcome) -> None:
        """Accumulate one classified outcome."""
        self.total += 1
        if outcome is Outcome.CONFIRMED_HIT:
            self.confirmed_hits += 1
        elif outcome is Outcome.MISS_REBUILT:
            self.misses += 1
        elif outcome is Outcome.SUCCESS_UNVERIFIED:
            self.unverified += 1
        else:
            self.failed += 1

    @classmethod
    def from_outcomes(cls, outcomes: Sequence[Outcome]) -> CacheHealthStats:
        stats = cls()
        for outcome in outcomes:
            stats.record(outcome)
        return stats
