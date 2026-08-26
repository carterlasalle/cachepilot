"""Warm-request transport — PRD §31 / §90 header replay (``HttpWarmExecutor``).

A warm is a *replay* of the captured request, not a synthesized one. Which
headers it may resend is the adapter's decision (``replay_headers``): a
hardcoded ``content-type`` + ``authorization`` pair silently restricts warming
to bearer-token dialects, so an ``x-api-key`` provider's warm is rejected
upstream, earns no cache telemetry and opens the §94 circuit breaker.

Only the allowlist travels: hop-by-hop headers and the relay's own
``X-CachePilot-*`` correlation headers must never reach the provider, and the
set of secret-bearing values the memory-only snapshot holds stays enumerable
(PRD §30, AGENTS.md rule 10).
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx
from cachepilot_core.adapters import OpenAICompatibleAdapter
from cachepilot_core.snapshots import RequestSnapshot
from cachepilot_core.telemetry import Outcome
from cachepilot_relay.lease_controller import LeaseController
from cachepilot_relay.warm_executor import HttpWarmExecutor

_UPSTREAM = "https://fake-provider.invalid/v1/chat/completions"

_RESPONSE = {
    "id": "cmpl-1",
    "usage": {
        "prompt_tokens": 4000,
        "completion_tokens": 1,
        "prompt_tokens_details": {"cached_tokens": 4000},
    },
}


class _RecordingTransport(httpx.AsyncBaseTransport):
    """Captures the warm request without any network access."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            json=_RESPONSE,
            headers={"content-type": "application/json"},
            request=request,
        )


def _execute(snapshot: RequestSnapshot):
    transport = _RecordingTransport()

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            executor = HttpWarmExecutor(client, OpenAICompatibleAdapter())
            return await executor.execute(snapshot)

    result = asyncio.run(run())
    return result, transport.requests


def _snapshot(**overrides) -> RequestSnapshot:
    fields = {
        "cache_fingerprint": "cache-fp-1",
        "body": {
            "model": "gpt-5.2",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 512,
        },
        "upstream_url": _UPSTREAM,
    }
    fields.update(overrides)
    return RequestSnapshot(**fields)


def test_warm_resends_every_allowlisted_header():
    """An x-api-key dialect must be able to authenticate its own warm."""
    snapshot = _snapshot(
        replay_headers={
            "authorization": "Bearer dev-token",
            "api-key": "azure-secret",
            "openai-beta": "assistants=v2",
            "openai-organization": "org-1",
        }
    )
    result, requests = _execute(snapshot)
    assert len(requests) == 1
    sent = requests[0]
    assert sent.headers["authorization"] == "Bearer dev-token"
    assert sent.headers["api-key"] == "azure-secret"
    assert sent.headers["openai-beta"] == "assistants=v2"
    assert sent.headers["openai-organization"] == "org-1"
    # Still a bounded, non-streaming, JSON warm.
    assert sent.headers["content-type"] == "application/json"
    assert json.loads(sent.content)["max_tokens"] == 1
    assert result.outcome is Outcome.CONFIRMED_HIT
    assert result.cost_usd == Decimal(0)  # no pricing configured → visible zero


def test_warm_content_type_is_never_overridden_by_the_snapshot():
    """The warm re-serializes the body as JSON, so it owns content-type."""
    _, requests = _execute(
        _snapshot(replay_headers={"content-type": "text/plain", "authorization": "Bearer t"})
    )
    assert requests[0].headers["content-type"] == "application/json"
    assert requests[0].headers["authorization"] == "Bearer t"


def test_warm_without_replay_headers_still_sends():
    """Fail open: a snapshot captured before any allowlisted header existed."""
    result, requests = _execute(_snapshot())
    assert len(requests) == 1
    assert "authorization" not in requests[0].headers
    assert result.outcome is Outcome.CONFIRMED_HIT


# -- capture side: only the adapter's allowlist is retained -------------------


def test_controller_retains_only_allowlisted_headers():
    controller = LeaseController(enabled=False)
    retained = controller._replay_headers(
        {
            "Authorization": "Bearer dev-token",
            "X-Api-Key": "anthropic-secret",
            "Anthropic-Version": "2023-06-01",
            "X-CachePilot-Session": "sess-1",
            "Connection": "keep-alive",
            "Cookie": "session=abc",
            "User-Agent": "hermes/0.20.0",
        }
    )
    # The OpenAI dialect declares only these; names are normalized lower-case.
    assert retained == {"authorization": "Bearer dev-token"}
    # Never the relay's own correlation headers, hop-by-hop headers or cookies.
    assert not any(name.startswith("x-cachepilot") for name in retained)


def test_controller_allowlist_is_adapter_supplied():
    """A different dialect keeps its own credential headers, not OpenAI's."""
    controller = LeaseController(
        enabled=False, replay_headers=frozenset({"x-api-key", "anthropic-version"})
    )
    retained = controller._replay_headers(
        {
            "Authorization": "Bearer dev-token",
            "X-Api-Key": "anthropic-secret",
            "Anthropic-Version": "2023-06-01",
        }
    )
    assert retained == {
        "x-api-key": "anthropic-secret",
        "anthropic-version": "2023-06-01",
    }


def test_controller_defaults_to_the_openai_allowlist():
    assert LeaseController(enabled=False).replay_headers == (
        OpenAICompatibleAdapter.replay_headers
    )


def test_controller_handles_absent_headers():
    assert LeaseController(enabled=False)._replay_headers(None) == {}
    assert LeaseController(enabled=False)._replay_headers({}) == {}
