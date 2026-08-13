"""Provider adapter tests — PRD §31, §33-35 (Phase 6).

Unit-level checks for the OpenAI-compatible adapter: warm-request bounding
for each output-bound field family, never-invents-fields, no-bound-field →
warm skipped (fail closed), tool_choice policy (PRD §33), capability
declarations (PRD §35), and honest usage parsing / outcome classification
(invariant 3: HTTP 200 ≠ cache hit).
"""

from __future__ import annotations

import httpx
import pytest
from cachepilot_core.adapters import CacheCapabilities, OpenAICompatibleAdapter
from cachepilot_core.fingerprint import cache_fingerprint as fingerprint_of
from cachepilot_core.identity import ApiMode, CanonicalRequest
from cachepilot_core.telemetry import Outcome

_MESSAGES = [{"role": "user", "content": "hello"}]


def _adapter() -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter()


# -- warm-request bounding (PRD §31) -----------------------------------------


def test_build_warm_request_bounds_max_tokens():
    warm = _adapter().build_warm_request(
        {"model": "gpt-5.2", "messages": _MESSAGES, "max_tokens": 512, "temperature": 0.7}
    )
    assert warm is not None
    assert warm["max_tokens"] == 1
    assert warm["temperature"] == 0.7  # untouched
    assert warm["model"] == "gpt-5.2"
    assert warm["messages"] == _MESSAGES


def test_build_warm_request_prefers_max_completion_tokens_when_present():
    # Both present → max_tokens wins (PRD §31 first-field-present order).
    original = {
        "model": "gpt-5.2",
        "messages": _MESSAGES,
        "max_tokens": 50,
        "max_completion_tokens": 100,
    }
    warm = _adapter().build_warm_request(original)
    assert warm is not None
    assert warm["max_tokens"] == 1
    assert warm["max_completion_tokens"] == 100  # never touched


def test_build_warm_request_bounds_max_output_tokens():
    warm = _adapter().build_warm_request(
        {"model": "gpt-5.2", "messages": _MESSAGES, "max_output_tokens": 200}
    )
    assert warm is not None
    assert warm["max_output_tokens"] == 1


def test_build_warm_request_all_three_fields_first_present_wins():
    original = {
        "model": "gpt-5.2",
        "messages": _MESSAGES,
        "max_tokens": 5,
        "max_completion_tokens": 6,
        "max_output_tokens": 7,
    }
    warm = _adapter().build_warm_request(original)
    assert warm is not None
    assert warm["max_tokens"] == 1
    assert warm["max_completion_tokens"] == 6
    assert warm["max_output_tokens"] == 7


def test_build_warm_request_never_invents_fields_and_is_a_copy():
    original = {"model": "gpt-5.2", "messages": _MESSAGES, "max_tokens": 10}
    warm = _adapter().build_warm_request(original)
    assert warm is not None
    assert warm is not original  # deepcopy, never mutates the snapshot
    assert set(warm) == set(original)  # no invented fields
    assert original["max_tokens"] == 10  # original untouched


def test_build_warm_request_no_bound_field_skips():
    # No output-bound field → the stream-cancel fallback is unverified for
    # this adapter → the warm is SKIPPED (fail closed, invariant 9).
    assert _adapter().build_warm_request({"model": "gpt-5.2", "messages": _MESSAGES}) is None
    assert (
        _adapter().build_warm_request(
            {"model": "gpt-5.2", "messages": _MESSAGES, "temperature": 0.2, "stream": True}
        )
        is None
    )


def test_build_warm_request_tool_policy_keeps_tools_and_tool_choice():
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]
    original = {
        "model": "gpt-5.2",
        "messages": _MESSAGES,
        "tools": tools,
        "tool_choice": "auto",
        "max_tokens": 10,
    }
    warm = _adapter().build_warm_request(original)
    assert warm is not None
    # PRD §33: tools are part of the provider cache identity → replayed.
    assert warm["tools"] == tools
    # tool_choice is NOT forced to "none": its cache-identity role is
    # unverified for the generic dialect → do not mutate.
    assert warm["tool_choice"] == "auto"


# -- capabilities (PRD §35) --------------------------------------------------


def test_openai_compatible_capabilities_are_conservative():
    caps: CacheCapabilities = _adapter().capabilities
    assert caps.supports_cache_telemetry is True
    assert caps.supports_cache_write_telemetry is False
    assert caps.supports_prompt_cache_key is True
    assert caps.supports_explicit_cache_control is False
    assert caps.supports_output_bound is True
    assert caps.supports_stream_cancel is False  # unbounded warm → skip
    assert caps.read_refreshes_ttl == "unknown"
    assert caps.route_identity_available is False
    assert caps.route_affinity_available is False


def test_route_affinity_methods_are_noops_for_generic_dialect():
    adapter = _adapter()
    request = {"model": "gpt-5.2", "messages": _MESSAGES, "max_tokens": 1}
    assert adapter.can_pin_route() is False
    assert adapter.apply_route_affinity(request, "some-route") is request
    assert adapter.extract_route_identity(httpx.Response(200, json={})) is None
    assert adapter.ttl_hint(request) is None


# -- usage parsing + outcome classification (invariant 3) --------------------


def test_parse_usage_openai_dialect():
    response = httpx.Response(
        200,
        json={
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 8},
            }
        },
    )
    usage = _adapter().parse_usage(response)
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 5
    assert usage.cache_read_tokens == 8
    assert usage.cache_write_tokens == 0


def test_classify_cache_result_confirmed_hit():
    response = httpx.Response(
        200,
        json={"usage": {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 8}}},
    )
    adapter = _adapter()
    usage = adapter.parse_usage(response)
    assert adapter.classify_cache_result(usage, response) is Outcome.CONFIRMED_HIT


def test_classify_cache_result_miss_rebuilt():
    response = httpx.Response(
        200,
        json={"usage": {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 0}}},
    )
    adapter = _adapter()
    usage = adapter.parse_usage(response)
    assert adapter.classify_cache_result(usage, response) is Outcome.MISS_REBUILT


def test_classify_cache_result_unverified_when_no_telemetry():
    # Provider returned usage but hid cache telemetry → SUCCESS_UNVERIFIED,
    # never CONFIRMED_HIT (HTTP 200 ≠ cache hit, invariant 3).
    response = httpx.Response(200, json={"usage": {"prompt_tokens": 10, "completion_tokens": 3}})
    adapter = _adapter()
    usage = adapter.parse_usage(response)
    assert adapter.classify_cache_result(usage, response) is Outcome.SUCCESS_UNVERIFIED


def test_classify_cache_result_failed_on_non_2xx():
    response = httpx.Response(500, json={"error": {"message": "boom"}})
    adapter = _adapter()
    usage = adapter.parse_usage(response)
    assert adapter.classify_cache_result(usage, response) is Outcome.FAILED


def test_parse_usage_tolerates_malformed_response():
    assert _adapter().parse_usage(httpx.Response(200, content=b"not json")).prompt_tokens == 0
    assert _adapter().parse_usage(httpx.Response(200)).prompt_tokens == 0


# -- identity methods (PRD §34) ----------------------------------------------


def test_identity_methods_work_on_canonical_request():
    adapter = _adapter()
    canonical = CanonicalRequest.from_content(
        provider="openai",
        model="gpt-5.2",
        api_mode=ApiMode.CHAT,
        endpoint="https://api.openai.com/v1",
        auth_scope="auth-1",
        prompt_prefix="hello",
        system="system prompt",
    )
    identity = adapter.canonical_cache_identity(canonical, None)
    assert identity.provider == "openai"
    assert identity.model == "gpt-5.2"
    assert adapter.cache_fingerprint(canonical) == fingerprint_of(canonical)


def test_identity_methods_reject_raw_body():
    # The raw body lacks the transport facts (provider/endpoint/auth scope/
    # route); a wrong identity is worse than an error.
    adapter = _adapter()
    with pytest.raises(TypeError):
        adapter.canonical_cache_identity({"model": "gpt-5.2", "messages": _MESSAGES}, None)
    with pytest.raises(TypeError):
        adapter.cache_fingerprint({"model": "gpt-5.2", "messages": _MESSAGES})
