"""Canonical request representation.

Implements PRD §22 (cache identity) and AGENTS.md invariants 7 (cache identity
is physical, never session-bound), 8 (two fingerprints) and 10 (never persist
secrets or raw prompts by default).

The canonical request carries NO ``session_id`` and NO raw prompt/system/tool
content — only deterministic hashes. It is therefore safe to log, persist and
fingerprint. Raw content is hashed at construction time via
:meth:`CanonicalRequest.from_content` and never stored on the model.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Cache identity is physical (AGENTS.md invariant 7). The canonical request
# deliberately has no session_id field and rejects unknown fields.
_MODEL_CONFIG = ConfigDict(extra="forbid")


class ApiMode(str, Enum):
    """Provider API mode — part of the physical request shape and cache layout."""

    CHAT = "chat"
    COMPLETION = "completion"
    RESPONSES = "responses"


def hash_content(content: str | bytes | None) -> str:
    """Deterministic SHA-256 hex digest of raw content.

    ``None`` (absent content) hashes the empty string so absent and empty stay
    stable. Only hashes are ever carried by canonical requests or persisted.
    """
    if isinstance(content, str):
        data = content.encode("utf-8")
    elif isinstance(content, bytes):
        data = content
    else:
        data = b""
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> str:
    """Stable JSON encoding for hashing (sorted keys, compact separators)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class CacheIdentity(BaseModel):
    """Physical cache identity — PRD §22 canonical form.

    Identity is provider/model/api-mode/endpoint/auth-scope/route/prompt/
    system/tools — never ``session_id`` (AGENTS.md invariant 7). ``auth_scope``
    is a profile *label*; credentials never appear here (invariant 10).
    """

    model_config = _MODEL_CONFIG

    provider: str = Field(..., min_length=1, description="Provider name, e.g. 'openai'.")
    model: str = Field(..., min_length=1, description="Model identifier.")
    api_mode: ApiMode = Field(..., description="API mode: chat / completion / responses.")
    endpoint: str = Field(..., min_length=1, description="Normalized base URL.")
    auth_scope: str = Field(
        ...,
        min_length=1,
        description="Auth/profile scope label (never the credential itself).",
    )
    route: str | None = Field(default=None, description="Route identity if available.")
    prompt_key: str = Field(
        ...,
        min_length=1,
        description="Stable prompt prefix / prompt-cache key (hash of the prefix).",
    )
    system_hash: str = Field(..., min_length=1, description="Hash of the system prompt.")
    tools_hash: str = Field(..., min_length=1, description="Hash of the tool schemas.")


class CanonicalRequest(CacheIdentity):
    """Full canonical LLM request — cache identity plus safe output-bounding fields.

    ``max_tokens``, ``stream`` and ``timeout_s`` do NOT participate in cache
    identity (PRD §23, AGENTS.md invariant 8): a warm request may differ only in
    these fields and still hit the same physical cache entry.
    """

    max_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Output bound — NOT part of cache identity.",
    )
    stream: bool = Field(default=False, description="Streaming flag — NOT part of cache identity.")
    timeout_s: float | None = Field(
        default=None,
        gt=0,
        description="Client timeout — NOT part of cache identity.",
    )

    @classmethod
    def from_content(
        cls,
        *,
        provider: str,
        model: str,
        api_mode: ApiMode,
        endpoint: str,
        auth_scope: str,
        prompt_prefix: str | None,
        system: str | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        route: str | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        timeout_s: float | None = None,
    ) -> CanonicalRequest:
        """Build a canonical request from raw content, persisting only hashes.

        Raw prompt/system/tool content is hashed here and never stored on the
        model (AGENTS.md invariant 10).
        """
        tools_hash = hash_content(_canonical_json(tools)) if tools is not None else hash_content(None)
        return cls(
            provider=provider,
            model=model,
            api_mode=api_mode,
            endpoint=endpoint,
            auth_scope=auth_scope,
            route=route,
            prompt_key=hash_content(prompt_prefix),
            system_hash=hash_content(system),
            tools_hash=tools_hash,
            max_tokens=max_tokens,
            stream=stream,
            timeout_s=timeout_s,
        )
