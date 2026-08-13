"""Layered prefix hashing + cache churn classification — PRD §24-25, §75, §137 (Phase 10).

Phase 10 (PRD §137) is the cache churn DETECTOR: it tells *which layer* of the
prompt topology destroyed a reusable prefix and *why*, so accidental cache
destruction (PRD §25: timestamps, UUIDs, dynamic ordering, tool-list mutation,
memory-prefix drift, provider failover, ...) stops being folklore.

PRD §24 layered topology — one hash per layer:

    static system prefix / dynamic system suffix / tool schemas /
    historical conversation / recent conversation tail

Everything here is DETECT-ONLY (PRD §25: "P0 should detect, not
automatically rewrite. Automatic canonicalization belongs later."): the
classifier returns booleans, a numeric divergence hint, a token estimate, a
human-readable cause and a confidence — never a rewritten request, and it
never mutates its inputs.

Invariant-10 posture (AGENTS.md rule 10, PRD §30/§83): layers are stored and
compared as SHA-256 hashes only. The :class:`DivergenceHint` carries a *bounded
in-memory* snippet around the first divergent byte (the PRD §25 output window)
for diagnostics; callers must NOT persist it — only the numeric offset and the
layer name are storage-safe.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cachepilot_core.identity import hash_content, tools_set_hash


#: Stable serialization for hashing (must match identity/observation: sorted
#: keys, compact separators) so layered hashes agree with the relay's stored
#: ``system_hash`` / ``tools_hash`` / ``history_hash``.
def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


# -- PRD §24 layer names (stable strings used in diagnostics) ----------------

LAYER_SYSTEM_PREFIX = "static system prefix"
LAYER_SYSTEM_SUFFIX = "dynamic system suffix"
LAYER_TOOLS = "tool schemas"
LAYER_HISTORY = "historical conversation"
LAYER_HISTORY_TAIL = "recent conversation tail"
LAYER_ROUTE = "route"
LAYER_MODEL = "model"
LAYER_CACHE_KEY = "cache key"

#: Content layers in prefix order — the order in which a divergence breaks the
#: reusable prefix (the earlier the layer, the more expensive the churn).
_CONTENT_LAYER_ORDER: tuple[str, ...] = (
    LAYER_SYSTEM_PREFIX,
    LAYER_SYSTEM_SUFFIX,
    LAYER_TOOLS,
    LAYER_HISTORY,
    LAYER_HISTORY_TAIL,
)

#: Volatile-content markers used by :func:`split_system_layers` (PRD §25 churn
#: vocabulary: timestamps, session timestamps, UUIDs). The *first* match is the
#: presumed static/dynamic boundary of the system prompt.
_VOLATILE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm)?\b"),
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
)

#: P11 (PRD §138 volatile prompt isolation): EXTENDED volatile markers used to
#: judge whether a divergence confined to the STATIC system prefix looks like a
#: leaked dynamic value — unix epoch seconds, hex blobs, labelled short ids.
#: Deliberately separate from ``_VOLATILE_PATTERNS``: those define the
#: static/dynamic boundary (PRD §24) and must not change; these only sharpen
#: the classification of prefix-only churn.
_PREFIX_VOLATILE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:1[6-9]|20)\d{8}\b"),  # unix epoch seconds (2000s-2090s)
    re.compile(r"\b[0-9a-fA-F]{8,}\b"),  # hex blob
    re.compile(
        r"\b(?:session|run|turn|attempt|job|id)[-_:\s]+[A-Za-z0-9_-]{4,}\b",
        re.IGNORECASE,
    ),
)

#: Content-window radius around the prefix divergence used for the volatility
#: judgement (P11).
_PREFIX_VOLATILE_RADIUS = 20

#: Approximate characters-per-token for the prefix-loss estimate (documented
#: heuristic — no tokenizer dependency in core).
_CHARS_PER_TOKEN = 4

#: Radius of the in-memory divergence snippet window (PRD §25 output shape).
_SNIPPET_RADIUS = 40


def split_system_layers(system: str) -> tuple[str, str]:
    """Split a system prompt into ``(static prefix, dynamic suffix)``.

    The boundary is the start of the FIRST volatile region (timestamp / date /
    clock time / UUID — PRD §25 vocabulary). Everything before it is presumed
    static; everything from it onward is presumed dynamic. A system prompt
    with no volatile region is entirely static (empty suffix). Deterministic
    and content-only — never hashes, never state.
    """
    if not system:
        return "", ""
    for pattern in _VOLATILE_PATTERNS:
        match = pattern.search(system)
        if match is not None:
            return system[: match.start()], system[match.start() :]
    return system, ""


class LayeredHashes(BaseModel):
    """PRD §24 per-layer hashes for one request — the comparison unit.

    ``system_hash`` / ``tools_hash`` / ``history_hash`` are the FLAT hashes the
    relay already persists (they align 1:1 with ``TelemetryEvent`` /
    ``ChurnEvent``); the layered hashes (``*_prefix_hash`` / ``*_suffix_hash`` /
    ``*_tail_hash``) refine them. Content-derived snapshots always carry every
    layered hash (an empty layer hashes the canonical empty list — stable);
    ``None`` means the snapshot was built from stored hashes only (e.g. after
    a relay restart), so the layered attribution is not computable — never a
    fabricated digest. Only hashes and identity fields are carried: never
    prompt content (AGENTS.md invariant 10).
    """

    model_config = ConfigDict(extra="forbid")

    system_prefix_hash: str | None = None
    system_suffix_hash: str | None = None
    system_hash: str | None = None
    tools_hash: str | None = None
    #: P11 (PRD §138): order-independent tool-set digest — the tool-ordering
    #: stability measurement distinguishes a pure ordering permutation (set
    #: digest stable) from a tool-list mutation (both digests move). Never
    #: part of cache identity; classification booleans ignore it.
    tools_set_hash: str | None = None
    history_hash: str | None = None
    history_prefix_hash: str | None = None
    history_tail_hash: str | None = None
    route_hash: str | None = None
    model: str | None = None
    cache_key: str | None = None


class RequestContent(BaseModel):
    """PRD §24 content view of one request — never persisted (PRD §30 memory
    snapshots may exist in-process; storage carries only :class:`LayeredHashes`).

    ``system`` / ``messages`` / ``tools`` mirror the relay's canonical
    extraction (top-level system or system-role messages; non-system messages;
    ``tools``/``functions``), so the flat hashes produced here agree with the
    relay's stored ``system_hash`` / ``tools_hash`` / ``history_hash``.
    """

    model_config = ConfigDict(extra="forbid")

    system: str | None = None
    messages: Sequence[Any] = Field(default_factory=list)
    tools: Sequence[Mapping[str, Any]] | None = None
    route_hash: str | None = None
    model: str | None = None
    cache_key: str | None = None

    def to_hashes(self) -> LayeredHashes:
        """Hash this request's layered content (PRD §24) — hashes only, never
        content (AGENTS.md invariant 10). Flat hashes use the same extraction
        and serialization as the relay (``identity.hash_content`` +
        ``_canonical_json``), so they agree with persisted events. Layered
        hashes are ALWAYS present for content-derived snapshots — an empty
        layer hashes the canonical empty list, a stable "absent" value that
        compares equal across requests."""
        prefix, suffix = split_system_layers(self.system or "")
        messages = list(self.messages)
        tools_list = list(self.tools) if self.tools is not None else None
        return LayeredHashes(
            system_prefix_hash=hash_content(prefix),
            system_suffix_hash=hash_content(suffix),
            system_hash=hash_content(self.system),
            tools_hash=(
                hash_content(_canonical_json(self.tools))
                if self.tools is not None
                else hash_content(None)
            ),
            tools_set_hash=tools_set_hash(tools_list),
            history_hash=(
                hash_content(_canonical_json(messages)) if messages else None
            ),
            history_prefix_hash=hash_content(_canonical_json(messages[:-1])),
            history_tail_hash=hash_content(_canonical_json(messages[-1:])),
            route_hash=self.route_hash,
            model=self.model,
            cache_key=self.cache_key,
        )

    def serialize(self) -> str:
        """Canonical serialization of the whole request view — used for the
        prefix-loss estimate and the divergence offset (never persisted)."""
        return _canonical_json(
            {
                "system": self.system,
                "messages": list(self.messages),
                "tools": self.tools,
                "route_hash": self.route_hash,
                "model": self.model,
                "cache_key": self.cache_key,
            }
        )


def request_content_from_payload(
    payload: Mapping[str, Any],
    *,
    route_hash: str | None = None,
    model: str | None = None,
    cache_key: str | None = None,
) -> RequestContent:
    """Extract the PRD §24 layered view from a chat-completions-style payload.

    Extraction mirrors the relay's canonical path: top-level ``system`` or
    system-role messages become ``system``; everything else becomes
    ``messages``; ``tools``/``functions`` become ``tools``. Identity fields
    (route/model/cache key) are passed explicitly — they are not part of the
    request body's prompt content.
    """
    system = payload.get("system")
    if not isinstance(system, str):
        # Mirror the relay's canonical extraction: top-level ``system`` wins,
        # else join system-role message content.
        system_texts: list[str] = []
        for message in payload.get("messages") or []:
            if not isinstance(message, Mapping) or message.get("role") != "system":
                continue
            content = message.get("content")
            system_texts.append(content if isinstance(content, str) else _canonical_json(content))
        system = "\n".join(system_texts) if system_texts else None
    messages: list[Any] = []
    for message in payload.get("messages") or []:
        if isinstance(message, Mapping) and message.get("role") == "system":
            continue
        messages.append(message)
    tools = payload.get("tools") or payload.get("functions")
    return RequestContent(
        system=system,
        messages=messages,
        tools=tools if isinstance(tools, list) else None,
        route_hash=route_hash,
        model=model if model is not None else payload.get("model"),
        cache_key=cache_key,
    )


class DivergenceHint(BaseModel):
    """Approximate location of the first divergent byte (PRD §25 output).

    ``layer`` is the PRD §24 layer the divergence falls in, ``offset`` the
    character offset within that layer's content. ``snippet`` is a BOUNDED
    in-memory window around the divergence (the PRD §25 ``near "..."`` shape) —
    callers must never persist it (AGENTS.md invariant 10); only the numeric
    offset and the layer name are storage-safe.
    """

    model_config = ConfigDict(extra="forbid")

    layer: str
    offset: int = Field(..., ge=0)
    snippet: str | None = None


class ChurnClassification(BaseModel):
    """One previous→current classification — PRD §25 output, aligned with
    :class:`~cachepilot_core.telemetry.ChurnEvent` (flat booleans).

    Layered booleans (``system_prefix_changed`` etc.) are ``None`` when the
    layered hashes were unavailable (hash-only snapshots) — ``None`` means
    "not computable", never "unchanged". The classifier NEVER rewrites or
    canonicalizes anything (PRD §25: detection only).
    """

    model_config = ConfigDict(extra="forbid")

    system_changed: bool = False
    tools_changed: bool = False
    history_changed: bool = False
    route_changed: bool = False
    cache_key_changed: bool = False
    model_changed: bool = False
    system_prefix_changed: bool | None = None
    system_suffix_changed: bool | None = None
    history_prefix_changed: bool | None = None
    history_tail_changed: bool | None = None
    first_divergent_byte: DivergenceHint | None = None
    estimated_prefix_loss_tokens: int | None = Field(default=None, ge=0)
    likely_cause: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)

    @property
    def changed_layers(self) -> tuple[str, ...]:
        """PRD §24 layer names flagged as changed (diagnostics/CLI)."""
        layers: list[str] = []
        if self.system_prefix_changed:
            layers.append(LAYER_SYSTEM_PREFIX)
        if self.system_suffix_changed:
            layers.append(LAYER_SYSTEM_SUFFIX)
        if self.tools_changed:
            layers.append(LAYER_TOOLS)
        if self.history_prefix_changed:
            layers.append(LAYER_HISTORY)
        if self.history_tail_changed:
            layers.append(LAYER_HISTORY_TAIL)
        if self.route_changed:
            layers.append(LAYER_ROUTE)
        if self.model_changed:
            layers.append(LAYER_MODEL)
        if self.cache_key_changed:
            layers.append(LAYER_CACHE_KEY)
        return tuple(layers)


#: Human-readable cause + base confidence per primary layer (PRD §24/§25/§75
#: vocabulary; ``0.92`` for a pure route change mirrors the PRD §75 example).
_CAUSE_AND_BASE: dict[str, tuple[str, float]] = {
    LAYER_ROUTE: ("router affinity loss", 0.92),
    LAYER_MODEL: ("provider failover (model switched)", 0.85),
    LAYER_SYSTEM_PREFIX: ("changing memory prefixes (static system prefix moved)", 0.90),
    LAYER_SYSTEM_SUFFIX: ("volatile value inserted into prompt prefix", 0.85),
    LAYER_TOOLS: ("tool list mutation", 0.80),
    LAYER_HISTORY: ("conversation history rewritten (compression/truncation)", 0.75),
    LAYER_HISTORY_TAIL: ("history-boundary churn (recent conversation tail moved)", 0.70),
    LAYER_CACHE_KEY: ("prompt cache key mutation", 0.70),
}

#: P11 (PRD §138 volatile prompt isolation): distinct causes for ISOLATED
#: volatile churn — the divergence is confined to the dynamic system suffix
#: (the volatile value lives after the static/dynamic split boundary) or to a
#: volatile-looking value INSIDE the static prefix (a dynamic value the
#: boundary heuristic did not recognise). Both are sharper than the generic
#: ``system prompt changed`` flat fallback and than the un-confined causes.
CAUSE_SYSTEM_SUFFIX_CHURN = "system_suffix_churn (volatile value in dynamic system suffix)"
CAUSE_VOLATILE_VALUE_IN_PREFIX = "volatile_value_in_prefix (volatile value inside static system prefix)"

#: Base confidence for the isolated volatile causes (volatile values are
#: deterministic churn — high confidence, same tier as the suffix cause).
_ISOLATED_SUFFIX_BASE = 0.85
_ISOLATED_PREFIX_VOLATILE_BASE = 0.85

#: Fallbacks when only flat hashes are known (no layered attribution).
_FLAT_FALLBACKS: dict[str, tuple[str, float]] = {
    "system": ("system prompt changed", 0.65),
    "history": ("conversation history changed", 0.65),
}

#: Residual fallback — fingerprint moved but no tracked layer did (e.g. the
#: relay's auth-scope / endpoint / api-mode churn, PRD §137 "cache key churn").
_RESIDUAL = ("cache identity changed outside the tracked layers", 0.60)


def _only_system_changed(classification: ChurnClassification) -> bool:
    """True when the system prompt is the ONLY changed flat layer.

    The isolation predicates below need to know that the divergence did not
    also touch tools/history or an identity layer — a confined volatile
    change is a stronger diagnosis than a general system change.
    """
    return classification.system_changed and not (
        classification.tools_changed
        or classification.history_changed
        or classification.route_changed
        or classification.cache_key_changed
        or classification.model_changed
    )


def _primary_cause(classification: ChurnClassification) -> tuple[str | None, float | None]:
    """Pick the dominant cause and its base confidence.

    Priority: identity-level destruction first (route, then model — they
    invalidate the WHOLE physical cache), then the earliest changed content
    layer in prefix order (the earlier the break, the more of the reusable
    prefix is destroyed). P11 (PRD §138) refines the system layers: a
    divergence CONFINED to the dynamic system suffix is the volatile-value
    pattern in its purest form (``system_suffix_churn``), sharper than the
    generic suffix cause used when other layers also moved. Every layer beyond
    the primary reduces confidence.
    """
    if classification.route_changed:
        return _CAUSE_AND_BASE[LAYER_ROUTE]
    if classification.model_changed:
        return _CAUSE_AND_BASE[LAYER_MODEL]
    if classification.system_suffix_changed and classification.system_prefix_changed is False:
        if _only_system_changed(classification):
            return CAUSE_SYSTEM_SUFFIX_CHURN, _ISOLATED_SUFFIX_BASE
        return _CAUSE_AND_BASE[LAYER_SYSTEM_SUFFIX]
    if classification.system_prefix_changed and classification.system_suffix_changed is False:
        # A prefix-only move is either a memory-prefix drift or a volatile
        # value the split-boundary heuristic missed; the content path
        # (:func:`_refine_prefix_volatility`) decides, the hash-only path
        # stays conservative. Both are the memory-prefix cause at this stage.
        return _CAUSE_AND_BASE[LAYER_SYSTEM_PREFIX]
    for layer in _CONTENT_LAYER_ORDER:
        if layer in classification.changed_layers:
            return _CAUSE_AND_BASE[layer]
    if classification.system_changed:
        return _FLAT_FALLBACKS["system"]
    if classification.history_changed:
        return _FLAT_FALLBACKS["history"]
    if classification.cache_key_changed:
        return _CAUSE_AND_BASE[LAYER_CACHE_KEY]
    return None, None


def _confidence(base: float, classification: ChurnClassification) -> float:
    """Base confidence minus 0.10 per extra changed flat layer (floor 0.50)."""
    changed = sum(
        1
        for flag in (
            classification.system_changed,
            classification.tools_changed,
            classification.history_changed,
            classification.route_changed,
            classification.cache_key_changed,
            classification.model_changed,
        )
        if flag
    )
    return round(max(0.50, base - 0.10 * (changed - 1)), 2)


def _classify_snapshots(previous: LayeredHashes, current: LayeredHashes) -> ChurnClassification:
    """Booleans + cause + confidence from two hash snapshots (flat + layered).

    Flat booleans compare the same hashes the relay persists, so they stay
    aligned with ``ChurnEvent``; layered booleans are ``None`` whenever either
    side lacks the layered hashes.
    """
    classification = ChurnClassification(
        system_changed=previous.system_hash != current.system_hash,
        tools_changed=previous.tools_hash != current.tools_hash,
        history_changed=previous.history_hash != current.history_hash,
        route_changed=previous.route_hash != current.route_hash,
        model_changed=previous.model != current.model,
        cache_key_changed=previous.cache_key != current.cache_key,
        system_prefix_changed=_maybe_differs(
            previous.system_prefix_hash, current.system_prefix_hash
        ),
        system_suffix_changed=_maybe_differs(
            previous.system_suffix_hash, current.system_suffix_hash
        ),
        history_prefix_changed=_maybe_differs(
            previous.history_prefix_hash, current.history_prefix_hash
        ),
        history_tail_changed=_maybe_differs(
            previous.history_tail_hash, current.history_tail_hash
        ),
    )
    cause, base = _primary_cause(classification)
    if cause is not None and base is not None:
        classification.likely_cause = cause
        classification.confidence = _confidence(base, classification)
    return classification


def _maybe_differs(previous: str | None, current: str | None) -> bool | None:
    """Compare two optional hashes: None on either side ⇒ not computable."""
    if previous is None or current is None:
        return None
    return previous != current


def _first_differing_index(previous: str, current: str) -> int:
    limit = min(len(previous), len(current))
    for index in range(limit):
        if previous[index] != current[index]:
            return index
    return limit


def _snippet(text: str, offset: int) -> str:
    start = max(0, offset - _SNIPPET_RADIUS)
    end = min(len(text), offset + _SNIPPET_RADIUS)
    head = "\u2026" if start > 0 else ""
    tail = "\u2026" if end < len(text) else ""
    return f"{head}{text[start:end]}{tail}"


def _first_divergence(
    previous: RequestContent, current: RequestContent
) -> DivergenceHint | None:
    """First divergent byte across the PRD §24 content layers, in prefix order.

    Returns the layer, the character offset within that layer's content and a
    bounded in-memory snippet of the CURRENT content around the offset.
    Identity layers (route/model/cache key) have no content position and are
    not located here — they surface through the booleans and the cause.
    """
    previous_hashes = previous.to_hashes()
    current_hashes = current.to_hashes()
    previous_texts: dict[str, str] = {
        LAYER_SYSTEM_PREFIX: split_system_layers(previous.system or "")[0],
        LAYER_SYSTEM_SUFFIX: split_system_layers(previous.system or "")[1],
        LAYER_TOOLS: _canonical_json(previous.tools),
        LAYER_HISTORY: _canonical_json(list(previous.messages)[:-1]),
        LAYER_HISTORY_TAIL: _canonical_json(list(previous.messages)[-1:]),
    }
    current_texts: dict[str, str] = {
        LAYER_SYSTEM_PREFIX: split_system_layers(current.system or "")[0],
        LAYER_SYSTEM_SUFFIX: split_system_layers(current.system or "")[1],
        LAYER_TOOLS: _canonical_json(current.tools),
        LAYER_HISTORY: _canonical_json(list(current.messages)[:-1]),
        LAYER_HISTORY_TAIL: _canonical_json(list(current.messages)[-1:]),
    }
    previous_hashes_by_layer = {
        LAYER_SYSTEM_PREFIX: previous_hashes.system_prefix_hash,
        LAYER_SYSTEM_SUFFIX: previous_hashes.system_suffix_hash,
        LAYER_TOOLS: previous_hashes.tools_hash,
        LAYER_HISTORY: previous_hashes.history_prefix_hash,
        LAYER_HISTORY_TAIL: previous_hashes.history_tail_hash,
    }
    current_hashes_by_layer = {
        LAYER_SYSTEM_PREFIX: current_hashes.system_prefix_hash,
        LAYER_SYSTEM_SUFFIX: current_hashes.system_suffix_hash,
        LAYER_TOOLS: current_hashes.tools_hash,
        LAYER_HISTORY: current_hashes.history_prefix_hash,
        LAYER_HISTORY_TAIL: current_hashes.history_tail_hash,
    }
    for layer in _CONTENT_LAYER_ORDER:
        if previous_hashes_by_layer[layer] == current_hashes_by_layer[layer]:
            continue
        previous_text = previous_texts[layer]
        current_text = current_texts[layer]
        offset = _first_differing_index(previous_text, current_text)
        return DivergenceHint(layer=layer, offset=offset, snippet=_snippet(current_text, offset))
    return None


def _common_prefix_length(previous: str, current: str) -> int:
    limit = min(len(previous), len(current))
    for index in range(limit):
        if previous[index] != current[index]:
            return index
    return limit


def _refine_prefix_volatility(
    classification: ChurnClassification, current: RequestContent
) -> None:
    """P11 (PRD §138 volatile prompt isolation): sharpen a prefix-only cause.

    When the static system prefix is the ONLY changed content layer, decide
    between a genuine memory-prefix move (``changing memory prefixes ...``)
    and a volatile value the split-boundary heuristic missed
    (``volatile_value_in_prefix ...``): the current prefix's divergence window
    is scanned for extended volatile markers (epoch seconds, hex blobs,
    labelled ids). Content-only — the hash-only path keeps the conservative
    memory-prefix cause (no content, no volatility claim). DETECT-only: the
    classification is never used to rewrite or canonicalize anything (PRD §25).
    """
    if classification.likely_cause != _CAUSE_AND_BASE[LAYER_SYSTEM_PREFIX][0]:
        return
    if not (classification.system_prefix_changed and classification.system_suffix_changed is False):
        return
    if not _only_system_changed(classification):
        return
    hint = classification.first_divergent_byte
    if hint is None or hint.layer != LAYER_SYSTEM_PREFIX or current.system is None:
        return
    prefix, _ = split_system_layers(current.system)
    if not prefix:
        return
    start = max(0, hint.offset - _PREFIX_VOLATILE_RADIUS)
    end = min(len(prefix), hint.offset + _PREFIX_VOLATILE_RADIUS)
    window = prefix[start:end]
    if any(pattern.search(window) for pattern in _PREFIX_VOLATILE_PATTERNS):
        classification.likely_cause = CAUSE_VOLATILE_VALUE_IN_PREFIX
        classification.confidence = _confidence(
            _ISOLATED_PREFIX_VOLATILE_BASE, classification
        )


def classify(
    previous: RequestContent, current: RequestContent
) -> ChurnClassification:
    """Classify one previous→current request transition (PRD §25 detector).

    Content path: full classification including the first-divergent-byte hint
    and the estimated reusable prefix lost in tokens (~4 chars/token heuristic;
    longest common prefix of the canonical serializations — 0 for identical
    requests). P11 (PRD §138): a prefix-only divergence is additionally judged
    for leaked volatile values (:func:`_refine_prefix_volatility`). Detection
    only: inputs are never mutated and no rewrite is ever produced (PRD §25).
    """
    classification = _classify_snapshots(previous.to_hashes(), current.to_hashes())
    previous_text = previous.serialize()
    current_text = current.serialize()
    if previous_text == current_text:
        classification.estimated_prefix_loss_tokens = 0
    else:
        classification.estimated_prefix_loss_tokens = (
            _common_prefix_length(previous_text, current_text) // _CHARS_PER_TOKEN
        )
    classification.first_divergent_byte = _first_divergence(previous, current)
    _refine_prefix_volatility(classification, current)
    return classification


def classify_hashes(
    previous: LayeredHashes, current: LayeredHashes
) -> ChurnClassification:
    """Hash-only classification — booleans, cause and confidence without content.

    Used when only the stored hashes are available (e.g. the relay right after
    a restart, before any in-memory request snapshot exists). The divergence
    hint and the token loss estimate are unavailable and stay None — never
    fabricated (PRD §25 honesty).
    """
    return _classify_snapshots(previous, current)


def changed_frequency(changed_flags: Sequence[bool], *, subject: str = "requests") -> str:
    """PRD §25 aggregate phrasing: ``changed 11/12 requests``.

    ``subject`` lets callers describe what the flags count (requests, churn
    events, ...). Empty input is reported honestly as ``no observations``.
    """
    total = len(changed_flags)
    if total == 0:
        return "no observations"
    changed = sum(1 for flag in changed_flags if flag)
    if changed == 0:
        return f"unchanged in the last {total} {subject}"
    return f"changed {changed}/{total} {subject}"
