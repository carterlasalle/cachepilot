"""P10 churn intelligence — layered hashing + diff classification (PRD §24-25, §137).

Pure unit coverage of the Phase 10 classifier: PRD §24 layered prefix hashing
(stable/deterministic, hashes only), the PRD §25 diff classification (per-layer
booleans aligned with ChurnEvent, first divergent byte, estimated prefix loss,
likely cause, confidence), the aggregate frequency helper, the hash-only
fallback, and the DETECT-only guarantee (no rewrite API, inputs never mutated,
storage carries only hashes + numeric hints).
"""

from __future__ import annotations

import json

import cachepilot_core.churn as churn_module
from cachepilot_core.churn import (
    LAYER_HISTORY_TAIL,
    LAYER_ROUTE,
    LAYER_SYSTEM_SUFFIX,
    LayeredHashes,
    RequestContent,
    changed_frequency,
    classify,
    classify_hashes,
    request_content_from_payload,
    split_system_layers,
)
from cachepilot_core.identity import hash_content
from cachepilot_core.storage import TelemetryStore
from cachepilot_core.telemetry import ChurnEvent

_PAYLOAD = {
    "model": "gpt-5.2",
    "system": "You are a helpful assistant.",
    "messages": [{"role": "user", "content": "hello"}],
    "tools": [{"type": "function", "function": {"name": "get_weather"}}],
}


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# -- PRD §24 layered prefix hashing ------------------------------------------


def test_layered_hashes_stable_and_deterministic():
    first = request_content_from_payload(_PAYLOAD).to_hashes()
    second = request_content_from_payload(_PAYLOAD).to_hashes()
    assert first == second
    for value in (
        first.system_hash,
        first.tools_hash,
        first.history_hash,
        first.system_prefix_hash,
        first.system_suffix_hash,
        first.history_tail_hash,
    ):
        assert value is not None and len(value) == 64  # sha256 hex digests only


def test_layered_hashes_change_with_content():
    changed = dict(_PAYLOAD, messages=[{"role": "user", "content": "goodbye"}])
    base = request_content_from_payload(_PAYLOAD).to_hashes()
    moved = request_content_from_payload(changed).to_hashes()
    assert base.history_hash != moved.history_hash
    assert base.history_tail_hash != moved.history_tail_hash
    # history prefix (all-but-last message) is untouched
    assert base.history_prefix_hash == moved.history_prefix_hash
    assert base.system_hash == moved.system_hash
    assert base.tools_hash == moved.tools_hash


def test_layered_hashes_are_hashes_only_never_content():
    secret = "supersecret-prompt-content-that-must-never-persist"
    payload = {"model": "m", "messages": [{"role": "user", "content": secret}]}
    hashes = request_content_from_payload(payload).to_hashes()
    for value in hashes.model_dump().values():
        if value is not None:
            assert secret not in str(value)
            # hex digest or an identity field (model), never raw content
            assert len(str(value)) <= 64


def test_layered_hashes_align_with_relay_flat_hashes():
    """The classifier's flat hashes must equal the relay's stored ones, so
    classification booleans agree with persisted ChurnEvent flags."""
    system = "You are a helpful assistant."
    messages = [{"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "get_weather"}}]
    payload = {"model": "gpt-5.2", "system": system, "messages": messages, "tools": tools}
    hashes = request_content_from_payload(payload).to_hashes()
    assert hashes.system_hash == hash_content(system)
    assert hashes.tools_hash == hash_content(_canonical(tools))
    assert hashes.history_hash == hash_content(_canonical(messages))
    assert hashes.route_hash is None


def test_request_content_from_payload_extracts_system_role_messages():
    payload = {
        "model": "m",
        "messages": [
            {"role": "system", "content": "sys text"},
            {"role": "user", "content": "hi"},
        ],
    }
    content = request_content_from_payload(payload)
    assert content.system == "sys text"
    assert content.messages == [{"role": "user", "content": "hi"}]
    # absent system → None, never a fabricated empty string
    assert request_content_from_payload({"model": "m"}).system is None


def test_split_system_layers_volatile_boundary():
    static, dynamic = split_system_layers("You are helpful.\nCurrent time: 3:14 PM\nBe concise.")
    assert static == "You are helpful.\nCurrent time: "
    assert dynamic == "3:14 PM\nBe concise."
    # dates
    static, dynamic = split_system_layers("Today is 2026-08-13. Be nice.")
    assert static == "Today is "
    assert dynamic == "2026-08-13. Be nice."
    # UUIDs
    static, dynamic = split_system_layers("job 123e4567-e89b-12d3-a456-426614174000 done")
    assert static == "job "
    assert dynamic.startswith("123e4567-e89b-12d3-a456-426614174000")
    # no volatile region → entirely static
    assert split_system_layers("plain static instructions") == (
        "plain static instructions",
        "",
    )
    assert split_system_layers("") == ("", "")


# -- PRD §25 diff classification ---------------------------------------------


def _content(**overrides) -> RequestContent:
    base = RequestContent(
        system="You are a helpful assistant.",
        messages=[{"role": "user", "content": "question one"}],
        tools=[{"type": "function", "function": {"name": "get_weather"}}],
        route_hash="route-a",
        model="gpt-5.2",
    )
    return base.model_copy(update=overrides)


def test_classify_unchanged_requests_is_honest_no_cause():
    classification = classify(_content(), _content())
    assert classification.system_changed is False
    assert classification.tools_changed is False
    assert classification.history_changed is False
    assert classification.route_changed is False
    assert classification.model_changed is False
    assert classification.likely_cause is None
    assert classification.confidence is None
    assert classification.first_divergent_byte is None
    assert classification.estimated_prefix_loss_tokens == 0


def test_classify_route_churn_matches_prd_75_example():
    """PRD §24 example: route changed → 'router affinity loss', and the PRD
    §75 example confidence (0.92) for a pure route change."""
    classification = classify(_content(), _content(route_hash="route-b"))
    assert classification.route_changed is True
    assert classification.system_changed is False
    assert classification.tools_changed is False
    assert classification.history_changed is False
    assert classification.model_changed is False
    assert classification.likely_cause == "router affinity loss"
    assert classification.confidence == 0.92


def test_classify_volatile_system_suffix_prd_25_example():
    """PRD §25: a timestamp inside the system prompt churns every request →
    the dynamic suffix is the culprit, the static prefix stays stable."""
    previous = _content(system="You are helpful.\nCurrent time: 3:14 PM\nBe concise.")
    current = _content(system="You are helpful.\nCurrent time: 3:15 PM\nBe concise.")
    classification = classify(previous, current)
    assert classification.system_changed is True
    assert classification.system_prefix_changed is False
    assert classification.system_suffix_changed is True
    assert classification.likely_cause == "volatile value inserted into prompt prefix"
    assert classification.confidence == 0.85
    hint = classification.first_divergent_byte
    assert hint is not None
    assert hint.layer == LAYER_SYSTEM_SUFFIX
    assert hint.offset == 3  # "3:14 PM" vs "3:15 PM" diverge at the minute digit
    assert hint.snippet is not None and "3:1" in hint.snippet


def test_classify_changing_memory_prefix():
    """PRD §25 churn vocabulary: the STATIC system prefix itself moved."""
    classification = classify(
        _content(system="memory block A.\nCurrent time: 3:14 PM"),
        _content(system="memory block B.\nCurrent time: 3:14 PM"),
    )
    assert classification.system_prefix_changed is True
    assert classification.system_suffix_changed is False
    assert classification.likely_cause == "changing memory prefixes (static system prefix moved)"
    assert classification.confidence == 0.90


def test_classify_tool_list_mutation():
    classification = classify(
        _content(tools=[{"type": "function", "function": {"name": "get_weather"}}]),
        _content(tools=[{"type": "function", "function": {"name": "get_time"}}]),
    )
    assert classification.tools_changed is True
    assert classification.likely_cause == "tool list mutation"
    assert classification.confidence == 0.80
    hint = classification.first_divergent_byte
    assert hint is not None and hint.layer == "tool schemas"
    assert hint.offset > 0


def test_classify_history_boundary_churn():
    classification = classify(
        _content(messages=[{"role": "user", "content": "question one"}]),
        _content(messages=[{"role": "user", "content": "question two"}]),
    )
    assert classification.history_changed is True
    assert classification.history_prefix_changed is False
    assert classification.history_tail_changed is True
    assert classification.likely_cause == "history-boundary churn (recent conversation tail moved)"
    assert classification.confidence == 0.70
    hint = classification.first_divergent_byte
    assert hint is not None
    assert hint.layer == LAYER_HISTORY_TAIL
    assert hint.offset > 0
    # bounded in-memory snippet near the volatile content (never persisted)
    assert hint.snippet is not None
    assert "question" in hint.snippet
    assert len(hint.snippet) <= 2 * 40 + 2  # _SNIPPET_RADIUS both sides + ellipses


def test_classify_history_prefix_rewrite_compression():
    classification = classify(
        _content(messages=[{"role": "user", "content": "old"}, {"role": "user", "content": "new"}]),
        _content(messages=[{"role": "user", "content": "compressed"}]),
    )
    assert classification.history_changed is True
    assert classification.history_prefix_changed is True
    assert (
        classification.likely_cause == "conversation history rewritten (compression/truncation)"
    )


def test_classify_model_switch_provider_failover():
    classification = classify(_content(), _content(model="gpt-5.3"))
    assert classification.model_changed is True
    assert classification.likely_cause == "provider failover (model switched)"
    assert classification.confidence == 0.85


def test_classify_cache_key_churn():
    classification = classify(
        _content(cache_key="prompt-key-1"),
        _content(cache_key="prompt-key-2"),
    )
    assert classification.cache_key_changed is True
    assert classification.likely_cause == "prompt cache key mutation"
    assert classification.confidence == 0.70


def test_classify_route_dominates_content_churn():
    """A route change destroys the whole physical cache — it stays the primary
    cause even when content also moved; extra layers lower the confidence."""
    classification = classify(
        _content(messages=[{"role": "user", "content": "question one"}]),
        _content(
            messages=[{"role": "user", "content": "question two"}], route_hash="route-b"
        ),
    )
    assert classification.route_changed is True
    assert classification.history_changed is True
    assert classification.likely_cause == "router affinity loss"
    assert classification.confidence == 0.82  # 0.92 - 0.10 * (2 - 1)
    assert classification.changed_layers == (LAYER_HISTORY_TAIL, LAYER_ROUTE)


def test_classify_earliest_content_layer_wins_cause():
    classification = classify(
        _content(
            system="You are helpful.\nCurrent time: 3:14 PM",
            tools=[{"type": "function", "function": {"name": "get_weather"}}],
        ),
        _content(
            system="You are helpful.\nCurrent time: 3:15 PM",
            tools=[{"type": "function", "function": {"name": "get_time"}}],
        ),
    )
    assert classification.system_changed is True
    assert classification.tools_changed is True
    assert classification.likely_cause == "volatile value inserted into prompt prefix"
    assert classification.confidence == 0.75  # 0.85 - 0.10 * (2 - 1)


# -- first divergent byte + estimated loss ------------------------------------


def test_estimated_prefix_loss_tokens_heuristic():
    long_static = "a" * 400
    previous = _content(messages=[{"role": "user", "content": long_static + " one"}])
    current = _content(messages=[{"role": "user", "content": long_static + " two"}])
    classification = classify(previous, current)
    assert classification.estimated_prefix_loss_tokens is not None
    assert classification.estimated_prefix_loss_tokens > 0  # ~100+ tokens shared
    # longer shared prefix ⇒ larger estimate (monotonic)
    shorter = classify(
        _content(messages=[{"role": "user", "content": "one"}]),
        _content(messages=[{"role": "user", "content": "two"}]),
    )
    assert shorter.estimated_prefix_loss_tokens is not None
    assert classification.estimated_prefix_loss_tokens > shorter.estimated_prefix_loss_tokens


def test_estimated_prefix_loss_identical_requests_zero():
    classification = classify(_content(), _content())
    assert classification.estimated_prefix_loss_tokens == 0


# -- hash-only fallback (relay restart path) ----------------------------------


def _flat(system_hash: str = "sys-a", **overrides) -> LayeredHashes:
    base = {
        "system_hash": system_hash,
        "tools_hash": "tools-a",
        "history_hash": "hist-a",
        "route_hash": "route-a",
        "model": "gpt-5.2",
    }
    base.update(overrides)
    return LayeredHashes(**base)


def test_classify_hashes_flat_system_change_no_attribution():
    classification = classify_hashes(_flat(), _flat(system_hash="sys-b"))
    assert classification.system_changed is True
    assert classification.system_prefix_changed is None  # not computable, not False
    assert classification.system_suffix_changed is None
    assert classification.likely_cause == "system prompt changed"
    assert classification.confidence == 0.65
    # no content ⇒ no divergence hint, no loss estimate — never fabricated
    assert classification.first_divergent_byte is None
    assert classification.estimated_prefix_loss_tokens is None


def test_classify_hashes_flat_route_and_history():
    route = classify_hashes(_flat(), _flat(route_hash="route-b"))
    assert route.route_changed is True
    assert route.likely_cause == "router affinity loss"
    assert route.confidence == 0.92
    history = classify_hashes(_flat(), _flat(history_hash="hist-b"))
    assert history.history_changed is True
    assert history.likely_cause == "conversation history changed"
    assert history.confidence == 0.65


# -- PRD §25 aggregate frequency helper ---------------------------------------


def test_changed_frequency_prd_25_style():
    assert changed_frequency([True] * 11 + [False]) == "changed 11/12 requests"
    assert changed_frequency([False, False, False]) == "unchanged in the last 3 requests"
    assert changed_frequency([]) == "no observations"
    assert changed_frequency([True, False], subject="churn events") == "changed 1/2 churn events"


# -- DETECT-only (PRD §25: P0 detects, never rewrites) ------------------------


def test_no_rewrite_or_canonicalization_api():
    for name in ("canonicalize", "rewrite", "normalize_prompt"):
        assert not hasattr(churn_module, name), f"rewrite API must not exist: {name}"


def test_classify_never_mutates_inputs():
    previous = _content()
    current = _content(messages=[{"role": "user", "content": "question two"}])
    previous_before = previous.model_dump()
    current_before = current.model_dump()
    classify(previous, current)
    assert previous.model_dump() == previous_before
    assert current.model_dump() == current_before
    # the classification carries no content field of its own — diagnostics only
    classification = classify(previous, current)
    for field in type(classification).model_fields:
        assert "content" not in field and "prompt" not in field


# -- P10 churn_events storage migration (PRD §82) -----------------------------


def test_churn_events_table_migrates_p10_columns(tmp_path):
    """A pre-P10 database (old churn_events shape) gains the classifier columns
    on connect, and the enrichment round-trips through record_churn/churn_list."""
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE churn_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            session_hash TEXT,
            previous_cache_fingerprint TEXT NOT NULL,
            new_cache_fingerprint TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            route_hash TEXT,
            system_changed INTEGER NOT NULL DEFAULT 0,
            tools_changed INTEGER NOT NULL DEFAULT 0,
            history_changed INTEGER NOT NULL DEFAULT 0,
            route_changed INTEGER NOT NULL DEFAULT 0,
            cache_key_changed INTEGER NOT NULL DEFAULT 0,
            model_changed INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()

    store = TelemetryStore(db)
    try:
        store.record_churn(
            ChurnEvent(
                session_hash="s1",
                previous_cache_fingerprint="fp-a",
                new_cache_fingerprint="fp-b",
                history_changed=True,
                likely_cause="history-boundary churn (recent conversation tail moved)",
                confidence=0.70,
                estimated_prefix_loss_tokens=1200,
                first_divergent_offset=9,
                first_divergent_layer="recent conversation tail",
            )
        )
        read_back = store.churn_list(limit=5)
        assert len(read_back) == 1
        event = read_back[0]
        assert event.likely_cause == "history-boundary churn (recent conversation tail moved)"
        assert event.confidence == 0.70
        assert event.estimated_prefix_loss_tokens == 1200
        assert event.first_divergent_offset == 9
        assert event.first_divergent_layer == "recent conversation tail"
        assert event.history_changed is True
        # re-connecting is idempotent (the ALTERs are skipped, never re-run)
        store.close()
        store = TelemetryStore(db)
        assert store.churn_list(limit=5)[0].confidence == 0.70
    finally:
        store.close()
    # the migrated table actually carries the new columns
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(churn_events)")}
    finally:
        conn.close()
    assert {"likely_cause", "confidence", "estimated_prefix_loss_tokens",
            "first_divergent_offset", "first_divergent_layer"} <= columns
