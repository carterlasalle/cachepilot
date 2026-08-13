"""P10 relay integration — churn detection flag + classifier enrichment (PRD §25, §137, §164).

Runs the production ``RequestObserver`` against a fake provider (offline) to
verify:

1. the default-on churn detection records churn events enriched with the
   classifier's diagnosis (likely cause / confidence / estimated prefix loss /
   first divergent byte) while the boolean flags stay stable;
2. ``churn_detection_enabled=False`` records ZERO churn events (PRD §164
   independent toggle) with request telemetry unaffected;
3. ``CACHEPILOT_CHURN_DETECTION_ENABLED`` drives the config flag;
4. the enriched fields round-trip through the telemetry store.
"""

from __future__ import annotations

import json

from cachepilot_core.fake_provider import (
    FakeProvider,
    FakeProviderConfig,
    provider_result_to_http_response,
)
from cachepilot_core.identity import ApiMode, CanonicalRequest
from cachepilot_core.storage import TelemetryStore
from cachepilot_core.telemetry import Outcome
from cachepilot_relay.config import RelayConfig
from cachepilot_relay.observation import RequestObserver

_PAYLOAD_ONE = {
    "model": "gpt-5.2",
    "messages": [{"role": "user", "content": "question one"}],
    "stream": False,
}
_PAYLOAD_TWO = {
    "model": "gpt-5.2",
    "messages": [{"role": "user", "content": "question two"}],
    "stream": False,
}


def _fake_response() -> bytes:
    """A fake-provider completion response with cache telemetry (bytes)."""
    canonical = CanonicalRequest.from_content(
        provider="fake-provider",
        model="gpt-5.2",
        api_mode=ApiMode.CHAT,
        endpoint="https://fake-provider.invalid/v1",
        auth_scope="test-scope",
        prompt_prefix="You are a helpful assistant.",
        system="system prompt",
    )
    result = FakeProvider(FakeProviderConfig(seed=7, completion_tokens=42)).complete(canonical)
    return provider_result_to_http_response(result).content


def _observe(
    observer: RequestObserver,
    payload: dict,
    *,
    session: str,
) -> Outcome | None:
    """One bounded observation through the real observer (offline)."""
    response = _fake_response()
    outcome, _usage = observer.observe_bounded(
        json.dumps(payload).encode("utf-8"),
        response,
        path="/v1/chat/completions",
        upstream_url="https://fake-provider.invalid/v1",
        status_code=200,
        response_headers={"x-provider": "fake-provider"},
        session_header=session,
    )
    return outcome


def test_churn_detection_enriches_events_with_classifier(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    observer = RequestObserver(store=store)
    try:
        assert _observe(observer, _PAYLOAD_ONE, session="sess-churn") is Outcome.MISS_REBUILT
        assert _observe(observer, _PAYLOAD_TWO, session="sess-churn") is Outcome.MISS_REBUILT
    finally:
        observer.close()
        store.close()

    store = TelemetryStore(tmp_path / "telemetry.db")
    try:
        events = store.recent_events(limit=5)
        assert len(events) == 2
        churn = store.churn_list(limit=5)
        assert len(churn) == 1
        event = churn[0]
        # boolean flags stay the pre-P10 computation (P08/P09 semantics)
        assert event.history_changed is True
        assert event.system_changed is False
        assert event.tools_changed is False
        assert event.route_changed is False
        assert event.model_changed is False
        # classifier enrichment persisted with the row
        assert event.likely_cause == "history-boundary churn (recent conversation tail moved)"
        assert event.confidence == 0.70
        assert event.estimated_prefix_loss_tokens is not None
        assert event.estimated_prefix_loss_tokens > 0
        assert event.first_divergent_layer == "recent conversation tail"
        assert event.first_divergent_offset is not None
        assert event.first_divergent_offset > 0
    finally:
        store.close()


def test_churn_detection_disabled_records_zero_churn_events(tmp_path):
    store = TelemetryStore(tmp_path / "telemetry.db")
    observer = RequestObserver(store=store, churn_detection_enabled=False)
    try:
        assert _observe(observer, _PAYLOAD_ONE, session="sess-off") is Outcome.MISS_REBUILT
        assert _observe(observer, _PAYLOAD_TWO, session="sess-off") is Outcome.MISS_REBUILT
    finally:
        observer.close()
        store.close()

    store = TelemetryStore(tmp_path / "telemetry.db")
    try:
        # request telemetry is unaffected…
        assert len(store.recent_events(limit=5)) == 2
        # …but the fingerprint transition records ZERO churn events (PRD §164)
        assert store.churn_list(limit=5) == []
        assert store.aggregates().churn_events == 0
    finally:
        store.close()


def test_churn_detection_env_flag_defaults_on():
    config = RelayConfig.from_env({}, upstream="https://fake-provider.invalid/v1")
    assert config.churn_detection_enabled is True
    env = {"CACHEPILOT_CHURN_DETECTION_ENABLED": "false"}
    config = RelayConfig.from_env(env, upstream="https://fake-provider.invalid/v1")
    assert config.churn_detection_enabled is False
    env = {"CACHEPILOT_CHURN_DETECTION_ENABLED": "true"}
    config = RelayConfig.from_env(env, upstream="https://fake-provider.invalid/v1")
    assert config.churn_detection_enabled is True


# -- P11 (PRD §138): tools_set_hash + volatile-isolation causes -----------------


def test_observer_records_tools_set_hash_for_ordering_measurement(tmp_path):
    """The order-independent tool-set digest is persisted on request_events:
    the same tool set in a different order keeps the set digest while the
    order-sensitive tools_hash moves — the tool-ordering stability input."""
    store = TelemetryStore(tmp_path / "telemetry.db")
    observer = RequestObserver(store=store)
    tools = [
        {"type": "function", "function": {"name": "get_weather"}},
        {"type": "function", "function": {"name": "get_time"}},
    ]
    payload = {
        "model": "gpt-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": tools,
        "stream": False,
    }
    permuted = {
        "model": "gpt-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": list(reversed(tools)),
        "stream": False,
    }
    try:
        _observe(observer, payload, session="sess-tools")
        _observe(observer, permuted, session="sess-tools")
    finally:
        observer.close()
        store.close()

    store = TelemetryStore(tmp_path / "telemetry.db")
    try:
        events = store.recent_events(limit=5)
        assert len(events) == 2
        first, second = events[1], events[0]
        assert first.tools_set_hash is not None
        # set digest is order-independent: identical across the permutation…
        assert first.tools_set_hash == second.tools_set_hash
        # …while the order-sensitive tools_hash moved
        assert first.tools_hash != second.tools_hash
    finally:
        store.close()


def test_observer_classifies_system_suffix_churn_p11(tmp_path):
    """P11 volatile prompt isolation through the real relay path: a churn
    confined to the dynamic system suffix is classified as system_suffix_churn
    (never the generic 'system prompt changed')."""
    store = TelemetryStore(tmp_path / "telemetry.db")
    observer = RequestObserver(store=store)
    system = "You are helpful.\nCurrent time: 3:14 PM\nBe concise."
    payload = {
        "model": "gpt-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "system": system,
        "stream": False,
    }
    try:
        _observe(observer, payload, session="sess-suffix")
        _observe(observer, {**payload, "system": system.replace("3:14", "3:15")}, session="sess-suffix")
    finally:
        observer.close()
        store.close()

    store = TelemetryStore(tmp_path / "telemetry.db")
    try:
        churn = store.churn_list(limit=5)
        assert len(churn) == 1
        event = churn[0]
        assert event.system_changed is True
        assert event.likely_cause == "system_suffix_churn (volatile value in dynamic system suffix)"
        assert event.confidence == 0.85
        assert event.first_divergent_layer == "dynamic system suffix"
        assert event.first_divergent_offset is not None
    finally:
        store.close()
