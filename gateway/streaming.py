"""Streaming passthrough for `POST /v1/messages` with `"stream": true`.

Bytes from Anthropic are forwarded to the client the moment they arrive; a copy is
parsed on the side (see sse.py) to recover usage. The call is recorded exactly once,
in a `finally` block, whatever ends the stream:

    complete             message_stop received
    <api error type>     Anthropic sent an `event: error` mid-stream (HTTP was already 200)
    upstream_timeout     Anthropic went silent longer than the read timeout
    upstream_disconnected  the connection to Anthropic broke
    incomplete_stream    the stream ended without message_stop and without an error
    client_disconnected  the client went away; we close the upstream connection so
                         Anthropic stops generating, and record the partial usage

Once the 200 status line is sent we can't change it, so upstream failures after that
point are reported to the client as a final SSE `error` event, which the SDKs raise.
"""

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import anyio
import httpx
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from gateway.recorder import CallRecord
from gateway.sse import SSEParser, StreamTracker


class ClosingStreamingResponse(StreamingResponse):
    """A StreamingResponse that always closes its body iterator when the response ends.

    Starlette ends a stream in different ways depending on the server: it may cancel the
    task running the iterator (ASGI spec < 2.4, e.g. uvicorn), or just stop iterating
    when a write fails (spec >= 2.4), leaving the generator suspended. Closing it here
    makes its `finally` block (where the call is recorded) run promptly in both cases.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()  # type: ignore[attr-defined]


def sse_error(error_type: str, message: str) -> bytes:
    body = {"type": "error", "error": {"type": error_type, "message": message}}
    return f"event: error\ndata: {json.dumps(body)}\n\n".encode()


def stream_body(
    upstream: httpx.Response,
    record: CallRecord,
    start: float,
    save: Callable[[CallRecord], Awaitable[object]],
    *,
    rebuild_message: bool,
) -> AsyncIterator[bytes]:
    tracker = StreamTracker(rebuild_message=rebuild_message)
    parser = SSEParser()

    async def body() -> AsyncIterator[bytes]:
        transport_error: str | None = None
        client_gone = False
        try:
            async for chunk in upstream.aiter_bytes():
                for event in parser.feed(chunk):
                    tracker.handle(event)
                    if tracker.first_delta_seen and record.ttft_ms is None:
                        record.ttft_ms = round((time.perf_counter() - start) * 1000)
                yield chunk
        except httpx.TimeoutException:
            transport_error = "upstream_timeout"
            yield sse_error("api_error", "upstream stream timed out")
        except httpx.TransportError:
            transport_error = "upstream_disconnected"
            yield sse_error("api_error", "upstream connection lost mid-stream")
        except (anyio.get_cancelled_exc_class(), GeneratorExit):
            # Cancelled (spec < 2.4) or closed while suspended at `yield`: either way
            # the client is no longer reading.
            client_gone = True
            raise
        finally:
            # This can run inside a cancelled scope, where any await would be cancelled
            # again; shield it so the upstream close and the DB write complete.
            with anyio.CancelScope(shield=True):
                await upstream.aclose()
                record.latency_ms = round((time.perf_counter() - start) * 1000)
                record.response_body = tracker.final_message()
                if client_gone and not tracker.completed:
                    record.error_type = "client_disconnected"
                    record.usage_note = (
                        "client disconnected mid-stream; output tokens are a lower bound"
                    )
                elif transport_error:
                    record.error_type = transport_error
                    record.usage_note = "stream cut off; output tokens are a lower bound"
                elif tracker.error_type:
                    record.error_type = tracker.error_type
                elif not tracker.completed:
                    record.error_type = "incomplete_stream"
                    record.usage_note = "stream ended early; output tokens are a lower bound"
                await save(record)

    return body()
