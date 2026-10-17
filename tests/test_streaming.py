"""Streaming: SDK -> gateway -> mocked Anthropic SSE stream, then check the DB row."""

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import anthropic
import anyio
import httpx
import pytest
import respx
from fastapi import FastAPI
from starlette.requests import ClientDisconnect

from tests.conftest import UPSTREAM, fetch_contents, fetch_rows
from tests.streams import message_start, sse, text_block_start, text_delta, text_stream

SSE_HEADERS = {"content-type": "text/event-stream", "request-id": "req_stream"}
PARAMS: dict[str, Any] = {
    "model": "claude-sonnet-5-5",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Hi"}],
}


def chunked(raw: bytes, size: int) -> AsyncIterator[bytes]:
    async def gen() -> AsyncIterator[bytes]:
        for i in range(0, len(raw), size):
            yield raw[i : i + size]

    return gen()


async def test_sdk_stream_works_and_logs_usage(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(200, headers=SSE_HEADERS, content=text_stream())

    async with sdk.messages.stream(**PARAMS, extra_headers={"X-Session-Id": "s1"}) as stream:
        text = "".join([t async for t in stream.text_stream])
        final = await stream.get_final_message()

    assert text == "Hello!"
    assert final.usage.output_tokens == 15

    [row] = await fetch_rows(app)
    assert row.is_stream is True
    assert row.status_code == 200
    assert row.error_type is None
    assert row.stop_reason == "end_turn"
    assert row.session_id == "s1"
    assert row.upstream_request_id == "req_stream"
    assert (row.input_tokens, row.cache_read_tokens, row.output_tokens) == (25, 100, 15)
    # Sonnet 5.5: 25*2 + 100*0.10 + 15*10 = 210 per MTok
    assert row.cost_usd == Decimal("0.00021")
    assert row.cost_note is None
    assert row.ttft_ms is not None and row.ttft_ms <= row.latency_ms


async def test_stream_bytes_are_forwarded_unchanged(
    http: httpx.AsyncClient, gateway_key: str, upstream: respx.MockRouter
) -> None:
    raw = text_stream()
    upstream.post("/v1/messages").respond(200, headers=SSE_HEADERS, content=chunked(raw, 5))
    resp = await http.post(
        "/v1/messages", headers={"x-api-key": gateway_key}, json={**PARAMS, "stream": True}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.content == raw  # pings and unknown events included


async def test_usage_is_captured_from_tiny_chunks(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    raw = text_stream(output_tokens=42)
    upstream.post("/v1/messages").respond(200, headers=SSE_HEADERS, content=chunked(raw, 3))
    async with sdk.messages.stream(**PARAMS) as stream:
        await stream.get_final_message()
    [row] = await fetch_rows(app)
    assert (row.input_tokens, row.output_tokens) == (25, 42)


async def test_mid_stream_error_event_is_raised_and_logged(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    raw = (
        message_start()
        + text_block_start()
        + text_delta("Hel")
        + sse("error", {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
    )
    upstream.post("/v1/messages").respond(200, headers=SSE_HEADERS, content=raw)

    with pytest.raises(anthropic.APIStatusError) as exc_info:
        async with sdk.messages.stream(**PARAMS) as stream:
            await stream.get_final_message()
    assert "overloaded_error" in str(exc_info.value)

    [row] = await fetch_rows(app)
    assert row.status_code == 200  # the HTTP status the client got
    assert row.error_type == "overloaded_error"
    assert row.input_tokens == 25  # usage seen before the error is still recorded


async def test_error_before_stream_starts_is_plain_json(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(
        429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}
    )
    with pytest.raises(anthropic.RateLimitError):
        async with sdk.messages.stream(**PARAMS) as stream:
            await stream.get_final_message()
    [row] = await fetch_rows(app)
    assert (row.status_code, row.error_type, row.is_stream) == (429, "rate_limit_error", True)


async def test_upstream_connection_lost_mid_stream(
    app: FastAPI, http: httpx.AsyncClient, gateway_key: str, upstream: respx.MockRouter
) -> None:
    async def broken() -> AsyncIterator[bytes]:
        yield message_start() + text_block_start()
        yield text_delta("Hel")
        raise httpx.ReadError("connection reset")

    upstream.post("/v1/messages").respond(200, headers=SSE_HEADERS, content=broken())
    resp = await http.post(
        "/v1/messages", headers={"x-api-key": gateway_key}, json={**PARAMS, "stream": True}
    )
    # The client gets what arrived, then an SSE error event instead of a silent cut-off.
    assert resp.content.startswith(message_start())
    assert resp.content.endswith(b"\n\n")
    last = resp.content.strip().split(b"\n\n")[-1]
    assert last.startswith(b"event: error")
    assert json.loads(last.split(b"data: ", 1)[1])["error"]["type"] == "api_error"

    [row] = await fetch_rows(app)
    assert row.error_type == "upstream_disconnected"
    assert row.input_tokens == 25
    assert "lower bound" in (row.cost_note or "")


async def test_stream_ending_without_message_stop_is_incomplete(
    app: FastAPI, http: httpx.AsyncClient, gateway_key: str, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(
        200, headers=SSE_HEADERS, content=message_start() + text_block_start() + text_delta("Hel")
    )
    await http.post(
        "/v1/messages", headers={"x-api-key": gateway_key}, json={**PARAMS, "stream": True}
    )
    [row] = await fetch_rows(app)
    assert row.error_type == "incomplete_stream"


@pytest.mark.parametrize("store_content", [True])
async def test_store_content_rebuilds_streamed_message(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(200, headers=SSE_HEADERS, content=text_stream())
    async with sdk.messages.stream(**PARAMS) as stream:
        await stream.get_final_message()
    [row] = await fetch_rows(app)
    [content] = await fetch_contents(app)
    assert content.request_id == row.id
    assert content.request_json["stream"] is True
    assert content.response_json["content"] == [{"type": "text", "text": "Hello!"}]


# --- Client disconnects -------------------------------------------------------------
# These call the ASGI app directly so the test controls exactly when the client leaves.


def asgi_scope(gateway_key: str, spec_version: str) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/messages",
        "raw_path": b"/v1/messages",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"gateway.test"),
            (b"content-type", b"application/json"),
            (b"x-api-key", gateway_key.encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("gateway.test", 80),
    }


def slow_upstream() -> AsyncIterator[bytes]:
    """Sends the start of a stream, then stalls as if the model were still generating."""

    async def gen() -> AsyncIterator[bytes]:
        yield message_start() + text_block_start()
        yield text_delta("Hel")
        await anyio.sleep(30)
        yield text_delta("lo")

    return gen()


@pytest.fixture
def closed_upstreams(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """URLs of upstream responses the gateway closed. On a real connection, closing
    drops the socket, which is what makes Anthropic stop generating."""
    closed: list[str] = []
    original = httpx.Response.aclose

    async def spy(self: httpx.Response) -> None:
        if self.request.url.host == httpx.URL(UPSTREAM).host:
            closed.append(str(self.request.url))
        await original(self)

    monkeypatch.setattr(httpx.Response, "aclose", spy)
    return closed


async def test_client_disconnect_is_logged_and_upstream_closed(
    app: FastAPI, gateway_key: str, upstream: respx.MockRouter, closed_upstreams: list[str]
) -> None:
    """ASGI spec 2.3 (uvicorn): Starlette notices the disconnect and cancels the stream."""
    upstream.post("/v1/messages").respond(200, headers=SSE_HEADERS, content=slow_upstream())

    got_body = anyio.Event()
    request_sent = False
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            body = json.dumps({**PARAMS, "stream": True}).encode()
            return {"type": "http.request", "body": body, "more_body": False}
        await got_body.wait()  # leave once some of the stream has arrived
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            got_body.set()

    with anyio.fail_after(5):  # the gateway must not wait out the stalled upstream
        await app(asgi_scope(gateway_key, "2.3"), receive, send)

    assert sent[0]["status"] == 200
    assert closed_upstreams == [f"{UPSTREAM}/v1/messages"]
    [row] = await fetch_rows(app)
    assert row.error_type == "client_disconnected"
    assert row.input_tokens == 25
    assert row.cost_usd is not None
    assert "lower bound" in (row.cost_note or "")


async def test_client_disconnect_detected_by_failed_write(
    app: FastAPI, gateway_key: str, upstream: respx.MockRouter, closed_upstreams: list[str]
) -> None:
    """ASGI spec 2.4: no disconnect message; the next write to the client fails."""
    upstream.post("/v1/messages").respond(200, headers=SSE_HEADERS, content=slow_upstream())

    request_sent = False
    body_writes = 0

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            body = json.dumps({**PARAMS, "stream": True}).encode()
            return {"type": "http.request", "body": body, "more_body": False}
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    async def send(message: dict[str, Any]) -> None:
        nonlocal body_writes
        if message["type"] == "http.response.body":
            body_writes += 1
            if body_writes == 2:
                raise OSError("client went away")

    with anyio.fail_after(5):
        with pytest.raises(ClientDisconnect):
            await app(asgi_scope(gateway_key, "2.4"), receive, send)

    assert closed_upstreams == [f"{UPSTREAM}/v1/messages"]
    [row] = await fetch_rows(app)
    assert row.error_type == "client_disconnected"
    assert row.input_tokens == 25
