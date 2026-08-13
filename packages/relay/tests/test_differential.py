"""Golden differential tests — PRD §130 / Phase 3 acceptance.

For each scenario the IDENTICAL request is sent (a) directly to the offline
fake upstream and (b) through the relay to the same upstream. The relayed
response must match the direct one: same status code, same body bytes, same
headers where relevant, same streaming behaviour. Zero network to real
providers; fully deterministic (no sleeps for races; generous
``asyncio.wait_for`` timeouts; ephemeral ports via ``port=0``).

Scenarios (PRD §130): plain JSON completion, streaming SSE (identical
accumulated bytes AND identical parsed event sequence — chunk boundaries are
not semantic, transfer chunking is not), error responses (400/500 with JSON
bodies) pass through unchanged, and a ``tool_calls`` response passes through
byte-identical.
"""

from __future__ import annotations

import asyncio
import json

import httpx
from cachepilot_core.fake_provider import (
    FakeProvider,
    FakeProviderConfig,
    provider_result_to_http_response,
)
from cachepilot_core.identity import ApiMode, CanonicalRequest
from helpers import DifferentialHarness
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

_COMPLETION_REQUEST = {
    "model": "gpt-5.2",
    "messages": [{"role": "user", "content": "hello"}],
    "stream": False,
}

_SSE_EVENTS = [
    {
        "id": "chatcmpl-relay1",
        "object": "chat.completion.chunk",
        "created": 1755129600,
        "model": "gpt-5.2",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
    },
    {
        "id": "chatcmpl-relay1",
        "object": "chat.completion.chunk",
        "created": 1755129600,
        "model": "gpt-5.2",
        "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}],
    },
    {
        "id": "chatcmpl-relay1",
        "object": "chat.completion.chunk",
        "created": 1755129600,
        "model": "gpt-5.2",
        "choices": [{"index": 0, "delta": {"content": " world"}, "finish_reason": None}],
    },
    {
        "id": "chatcmpl-relay1",
        "object": "chat.completion.chunk",
        "created": 1755129600,
        "model": "gpt-5.2",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    },
]

_TOOL_CALL_PAYLOAD = {
    "id": "chatcmpl-tool1",
    "object": "chat.completion",
    "created": 1755129600,
    "model": "gpt-5.2",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_weather_paris",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Paris", "unit": "celsius"}'},
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 37, "completion_tokens": 12, "total_tokens": 49},
}


def _canonical_request(model: str) -> CanonicalRequest:
    return CanonicalRequest.from_content(
        provider="fake-provider",
        model=model,
        api_mode=ApiMode.CHAT,
        endpoint="https://fake-provider.invalid/v1",
        auth_scope="test-scope",
        route="relay-test",
        prompt_prefix="You are a helpful assistant.",
        system="system prompt",
    )


def _completion_response() -> Response:
    """Fixed fake-provider completion so every call is byte-identical.

    A stateful simulator would make the second call a cache hit and change the
    usage payload, which would break the golden comparison.
    """
    provider = FakeProvider(FakeProviderConfig(seed=7, completion_tokens=42))
    baseline = provider_result_to_http_response(provider.complete(_canonical_request("gpt-5.2")))
    return Response(
        content=baseline.content,
        status_code=baseline.status_code,
        headers=dict(baseline.headers),
    )


async def _sse_body():
    for event in _SSE_EVENTS:
        yield f"data: {json.dumps(event)}\n\n".encode()
        await asyncio.sleep(0.005)  # deterministic inter-chunk pause -> multiple writes
    yield b"data: [DONE]\n\n"


def build_upstream_app() -> Starlette:
    """Offline deterministic fake-provider upstream (PRD §109) + custom routes."""
    completion_response = _completion_response()
    app = Starlette()

    async def chat_completions(request: Request) -> Response:
        body = await request.json()
        if body.get("stream"):
            return StreamingResponse(_sse_body(), media_type="text/event-stream")
        return completion_response

    async def tool_calls(request: Request) -> Response:
        return JSONResponse(_TOOL_CALL_PAYLOAD)

    def make_error_handler(status_code: int):
        async def error_response(request: Request) -> Response:
            return JSONResponse(
                {
                    "error": {
                        "message": f"simulated {status_code}",
                        "type": "simulated_error",
                        "code": status_code,
                    }
                },
                status_code=status_code,
            )

        return error_response

    async def echo(request: Request) -> Response:
        observed = {
            key: value
            for key, value in sorted(request.headers.items())
            if key.lower() in {"host", "content-type", "x-test-header"}
        }
        return JSONResponse(
            {
                "method": request.method,
                "path": request.url.path,
                "query": request.url.query,
                "headers": observed,
                "body": (await request.body()).decode("utf-8", "replace"),
            }
        )

    app.add_route("/v1/chat/completions", chat_completions, methods=["POST"])
    app.add_route("/v1/tool-calls", tool_calls, methods=["POST"])
    app.add_route("/v1/errors/400", make_error_handler(400), methods=["POST"])
    app.add_route("/v1/errors/500", make_error_handler(500), methods=["POST"])
    app.add_route("/v1/echo", echo, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    return app


def parse_sse_events(body: bytes) -> list[object]:
    """Parse ``data:`` lines into JSON payloads (``[DONE]`` stays a raw string)."""
    events: list[object] = []
    for block in body.decode("utf-8").split("\n\n"):
        data = "".join(
            line[len("data:") :].lstrip()
            for line in block.splitlines()
            if line.startswith("data:")
        )
        if not data:
            continue
        events.append(data if data == "[DONE]" else json.loads(data))
    return events


# -- scenarios ---------------------------------------------------------------


def test_plain_json_completion_passes_through_byte_identical():
    asyncio.run(_scenario_plain_json())


async def _scenario_plain_json() -> None:
    async with DifferentialHarness(build_upstream_app()) as harness:
        direct, relayed = await harness.compare("POST", "/v1/chat/completions", json=_COMPLETION_REQUEST)
        assert relayed.status_code == 200
        # the fake provider's own x-cachepilot-* headers pass through unchanged
        assert relayed.headers["x-cachepilot-cache-hit"] == direct.headers["x-cachepilot-cache-hit"]
        assert relayed.json()["choices"][0]["message"]["content"] == "fake completion"


def test_streaming_sse_preserves_event_stream():
    asyncio.run(_scenario_sse())


async def _scenario_sse() -> None:
    payload = {"model": "gpt-5.2", "messages": [{"role": "user", "content": "stream"}], "stream": True}
    async with DifferentialHarness(build_upstream_app()) as harness:
        assert harness.client is not None
        assert harness.upstream is not None and harness.relay is not None

        async def collect(base_url: str) -> tuple[httpx.Response, list[bytes]]:
            chunks: list[bytes] = []
            async with harness.client.stream("POST", base_url + "/v1/chat/completions", json=payload) as response:
                assert response.status_code == 200
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
            return response, chunks

        direct_response, direct_chunks = await collect(harness.upstream.base_url)
        relayed_response, relayed_chunks = await collect(harness.relay.base_url)

        direct_body = b"".join(direct_chunks)
        relayed_body = b"".join(relayed_chunks)
        # identical accumulated bytes AND identical parsed event sequence
        assert relayed_body == direct_body
        expected_events = [*_SSE_EVENTS, "[DONE]"]
        assert parse_sse_events(relayed_body) == expected_events
        assert parse_sse_events(relayed_body) == parse_sse_events(direct_body)
        # the relay streamed it (chunked, no content-length), SSE media type kept
        assert relayed_response.headers["content-type"] == direct_response.headers["content-type"]
        assert relayed_response.headers["content-type"].startswith("text/event-stream")
        assert "content-length" not in relayed_response.headers


def test_error_400_json_passes_through_unchanged():
    asyncio.run(_scenario_error(400))


def test_error_500_json_passes_through_unchanged():
    asyncio.run(_scenario_error(500))


async def _scenario_error(status_code: int) -> None:
    async with DifferentialHarness(build_upstream_app()) as harness:
        direct, relayed = await harness.compare(
            "POST", f"/v1/errors/{status_code}", json={"model": "gpt-5.2"}
        )
        assert relayed.status_code == status_code == direct.status_code
        assert relayed.json() == {
            "error": {
                "message": f"simulated {status_code}",
                "type": "simulated_error",
                "code": status_code,
            }
        }


def test_tool_calls_response_passes_through_byte_identical():
    asyncio.run(_scenario_tool_calls())


async def _scenario_tool_calls() -> None:
    async with DifferentialHarness(build_upstream_app()) as harness:
        direct, relayed = await harness.compare("POST", "/v1/tool-calls", json={"model": "gpt-5.2"})
        assert relayed.json() == _TOOL_CALL_PAYLOAD == direct.json()
        tool_call = relayed.json()["choices"][0]["message"]["tool_calls"][0]
        assert tool_call["function"]["name"] == "get_weather"
        assert tool_call["function"]["arguments"] == '{"city": "Paris", "unit": "celsius"}'


def test_method_path_query_and_headers_forwarded_verbatim():
    asyncio.run(_scenario_echo())


async def _scenario_echo() -> None:
    async with DifferentialHarness(build_upstream_app()) as harness:
        assert harness.upstream is not None
        direct, relayed = await harness.compare(
            "PUT",
            "/v1/echo?model=gpt-5.2&stream=false",
            headers={"x-test-header": "golden-value", "content-type": "application/json"},
            content=b'{"echo": true}',
        )
        echoed = relayed.json()
        assert echoed["method"] == "PUT"
        assert echoed["path"] == "/v1/echo"
        assert echoed["query"] == "model=gpt-5.2&stream=false"
        assert echoed["headers"]["x-test-header"] == "golden-value"
        # the relay rewrites Host to the upstream, so both paths see the same host
        assert echoed["headers"]["host"] == harness.upstream.base_url.removeprefix("http://")
        assert echoed["body"] == '{"echo": true}'
        assert relayed.status_code == direct.status_code == 200


def test_relay_adds_no_headers_and_never_rewrites():
    asyncio.run(_scenario_no_modification())


async def _scenario_no_modification() -> None:
    async with DifferentialHarness(build_upstream_app()) as harness:
        direct, relayed = await harness.compare("POST", "/v1/chat/completions", json=_COMPLETION_REQUEST)
        # 0 cache modification: the relayed header set is EXACTLY the upstream's
        # (bounded responses keep Content-Length; nothing is added or rewritten)
        assert set(relayed.headers) == set(direct.headers)
        assert relayed.content == direct.content
        # no cachepilot instrumentation headers invented by the relay
        assert "x-cachepilot-request-id" not in relayed.headers
