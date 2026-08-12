"""Usage normalization — canonical token usage from provider payloads.

Provides a single canonical structure (PRD §109, §160):

- ``prompt_tokens``: total input tokens (including any cached portion)
- ``completion_tokens``: output tokens
- ``cache_read_tokens``: input served from the prefix cache (cache hit)
- ``cache_write_tokens``: input written to the prefix cache (miss/rebuild —
  tracked separately because some providers charge extra for writes, PRD §160)
- ``cost``: provider-returned monetary usage when the provider supplies it
  (e.g. OpenRouter ``usage.cost``) — resolution priority 1 in PRD §65

The normalizer is tolerant by design: provider payloads vary, and unknown or
missing fields degrade to zero rather than raising.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TokenUsage(BaseModel):
    """Canonical, provider-agnostic token usage."""

    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: Decimal | None = Field(
        default=None,
        description="Provider-returned monetary cost (e.g. OpenRouter usage.cost).",
    )

    @field_validator("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("token counts must be >= 0")
        return value


def _to_int(value: Any) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _to_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (TypeError, ValueError, ArithmeticError):
        return None


class UsageNormalizer:
    """Maps provider-specific usage payloads onto :class:`TokenUsage`.

    Supported dialects:

    - ``anthropic``: ``input_tokens`` / ``output_tokens`` /
      ``cache_read_input_tokens`` / ``cache_creation_input_tokens``
      (Anthropic's ``input_tokens`` excludes cached portions, so the canonical
      ``prompt_tokens`` is the sum of input + read + write).
    - OpenAI / OpenRouter / generic OpenAI-compatible: ``prompt_tokens`` /
      ``completion_tokens`` / ``prompt_tokens_details.cached_tokens``
      (OpenAI's ``prompt_tokens`` already includes the cached portion), plus
      optional monetary ``cost``.
    """

    def normalize(self, payload: Any, provider: str | None = None) -> TokenUsage:
        if not isinstance(payload, Mapping):
            return TokenUsage()
        usage = payload.get("usage", payload)
        if not isinstance(usage, Mapping):
            return TokenUsage()
        name = (provider or "").lower()

        if name == "anthropic":
            read = _to_int(usage.get("cache_read_input_tokens"))
            write = _to_int(usage.get("cache_creation_input_tokens"))
            return TokenUsage(
                prompt_tokens=_to_int(usage.get("input_tokens")) + read + write,
                completion_tokens=_to_int(usage.get("output_tokens")),
                cache_read_tokens=read,
                cache_write_tokens=write,
                cost=_to_decimal(usage.get("cost")),
            )

        details = usage.get("prompt_tokens_details")
        cached = _to_int(details.get("cached_tokens")) if isinstance(details, Mapping) else 0
        if not cached:
            cached = _to_int(usage.get("cache_read_input_tokens"))
        write = _to_int(usage.get("cache_creation_input_tokens"))
        return TokenUsage(
            prompt_tokens=_to_int(usage.get("prompt_tokens")),
            completion_tokens=_to_int(usage.get("completion_tokens")),
            cache_read_tokens=cached,
            cache_write_tokens=write,
            cost=_to_decimal(usage.get("cost")),
        )
