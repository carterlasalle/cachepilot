"""Two fingerprints — PRD §23, AGENTS.md invariant 8.

``request_fingerprint`` hashes the entire canonical request; used for
debugging, duplicate detection and response-cache analysis.

``cache_fingerprint`` hashes only the fields relevant to provider prefix-cache
identity — ``max_tokens``, ``stream`` and ``timeout_s`` are intentionally
excluded so a warm request can differ in safe output-bounding fields while
retaining the same cache identity.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from cachepilot_core.identity import CanonicalRequest

# Fields that participate in provider prefix-cache identity (PRD §22).
# max_tokens / stream / timeouts are deliberately absent (PRD §23).
CACHE_IDENTITY_FIELDS: tuple[str, ...] = (
    "provider",
    "model",
    "api_mode",
    "endpoint",
    "auth_scope",
    "route",
    "prompt_key",
    "system_hash",
    "tools_hash",
)


def _canonical_bytes(data: Mapping[str, Any]) -> bytes:
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_fingerprint(request: CanonicalRequest) -> str:
    """Hash of the entire canonical request (PRD §23 'Full request fingerprint').

    ``tools_set_hash`` (P11) is excluded: it is derived measurement carrying
    no identity information beyond ``tools_hash``, and excluding it keeps
    fingerprint values stable across the P11 schema addition.
    """
    dump = request.model_dump(mode="json")
    dump.pop("tools_set_hash", None)
    return _sha256_hex(_canonical_bytes(dump))


def cache_fingerprint(request: CanonicalRequest) -> str:
    """Hash of only the prefix-cache-relevant fields (PRD §23 'Cache fingerprint')."""
    dump = request.model_dump(mode="json")
    subset = {name: dump[name] for name in CACHE_IDENTITY_FIELDS}
    return _sha256_hex(_canonical_bytes(subset))
