"""PRD §102 — unit tests for the two fingerprints (PRD §23, AGENTS.md invariant 8)."""

import pytest
from cachepilot_core.fingerprint import cache_fingerprint, request_fingerprint
from cachepilot_core.identity import ApiMode, CanonicalRequest, hash_content
from pydantic import ValidationError

BASE = {
    "provider": "openai",
    "model": "gpt-5.2",
    "api_mode": ApiMode.CHAT,
    "endpoint": "https://api.openai.com/v1",
    "auth_scope": "default-profile",
    "route": "route-a",
}


def make_request(**overrides):
    fields = dict(BASE)
    fields.update(
        {
            "prompt_prefix": "You are a helpful assistant.",
            "system": "You are a helpful system prompt.",
            "tools": [{"name": "get_weather", "parameters": {"type": "object"}}],
        }
    )
    fields.update(overrides)
    return CanonicalRequest.from_content(**fields)


def test_identical_requests_identical_fingerprints():
    r1 = make_request()
    r2 = make_request()
    assert cache_fingerprint(r1) == cache_fingerprint(r2)
    assert request_fingerprint(r1) == request_fingerprint(r2)


def test_max_tokens_excluded_from_cache_fingerprint():
    base = make_request(max_tokens=100)
    bounded = make_request(max_tokens=1)  # a warm request differs only here
    assert request_fingerprint(base) != request_fingerprint(bounded)
    assert cache_fingerprint(base) == cache_fingerprint(bounded)


def test_stream_excluded_from_cache_fingerprint():
    base = make_request(stream=False)
    streamed = make_request(stream=True)
    assert request_fingerprint(base) != request_fingerprint(streamed)
    assert cache_fingerprint(base) == cache_fingerprint(streamed)


def test_timeout_excluded_from_cache_fingerprint():
    base = make_request(timeout_s=30.0)
    default = make_request(timeout_s=None)
    assert request_fingerprint(base) != request_fingerprint(default)
    assert cache_fingerprint(base) == cache_fingerprint(default)


def test_warm_request_differs_only_in_safe_output_fields():
    """PRD §23: a warm may differ in output-bounding fields, never in identity."""
    real = make_request(max_tokens=2048, stream=False, timeout_s=120.0)
    warm = make_request(max_tokens=1, stream=True, timeout_s=10.0)
    assert cache_fingerprint(real) == cache_fingerprint(warm)


def test_system_prefix_change_changes_cache_fingerprint():
    assert cache_fingerprint(make_request(system="System A")) != cache_fingerprint(
        make_request(system="System B")
    )
    assert request_fingerprint(make_request(system="System A")) != request_fingerprint(
        make_request(system="System B")
    )


def test_tool_schema_change_changes_cache_fingerprint():
    tools_a = [{"name": "get_weather", "parameters": {"type": "object"}}]
    tools_b = [{"name": "get_weather", "parameters": {"type": "object", "required": ["city"]}}]
    assert cache_fingerprint(make_request(tools=tools_a)) != cache_fingerprint(make_request(tools=tools_b))


def test_prompt_prefix_change_changes_cache_fingerprint():
    assert cache_fingerprint(make_request(prompt_prefix="prefix one")) != cache_fingerprint(
        make_request(prompt_prefix="prefix two")
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "anthropic"),
        ("model", "claude-4.6"),
        ("api_mode", ApiMode.RESPONSES),
        ("endpoint", "https://api.anthropic.com/v1"),
        ("auth_scope", "work-profile"),
        ("route", "route-b"),
    ],
)
def test_each_identity_field_changes_cache_fingerprint(field, value):
    assert cache_fingerprint(make_request()) != cache_fingerprint(make_request(**{field: value}))


def test_session_id_is_never_cache_identity():
    """AGENTS.md invariant 7: physical identity, never session-bound."""
    assert "session_id" not in CanonicalRequest.model_fields
    dump = make_request().model_dump(mode="json")
    with pytest.raises(ValidationError):
        CanonicalRequest.model_validate({**dump, "session_id": "session-123"})


def test_fingerprints_stable_across_serialization():
    r1 = make_request()
    r2 = CanonicalRequest.model_validate_json(r1.model_dump_json())
    assert request_fingerprint(r1) == request_fingerprint(r2)
    assert cache_fingerprint(r1) == cache_fingerprint(r2)
    # stable within the same process across calls
    assert request_fingerprint(r1) == request_fingerprint(r1)
    assert cache_fingerprint(r1) == cache_fingerprint(r1)


def test_fingerprints_are_sha256_hex():
    for fp in (request_fingerprint(make_request()), cache_fingerprint(make_request())):
        assert len(fp) == 64
        int(fp, 16)  # valid hex


def test_canonical_request_persists_only_hashes():
    """AGENTS.md invariant 10: raw prompts never leave the construction call."""
    req = make_request(
        prompt_prefix="TOP-SECRET-PROMPT-CONTENT",
        system="TOP-SECRET-SYSTEM-CONTENT",
        tools=[{"name": "secret_tool"}],
    )
    dump = req.model_dump_json()
    assert "TOP-SECRET-PROMPT-CONTENT" not in dump
    assert "TOP-SECRET-SYSTEM-CONTENT" not in dump
    assert "secret_tool" not in dump
    assert req.prompt_key == hash_content("TOP-SECRET-PROMPT-CONTENT")
    assert req.system_hash == hash_content("TOP-SECRET-SYSTEM-CONTENT")
