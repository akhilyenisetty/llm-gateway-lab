"""Builders for fake Messages API SSE streams, shaped like the documented examples."""

import json
from typing import Any


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def message_start(model: str = "claude-sonnet-5-5", **usage: int) -> bytes:
    usage = {
        "input_tokens": 25,
        "output_tokens": 1,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 100,
        **usage,
    }
    return sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_stream1",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": usage,
            },
        },
    )


def text_delta(text: str, index: int = 0) -> bytes:
    return sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
    )


def text_block_start(index: int = 0) -> bytes:
    return sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text", "text": ""},
        },
    )


def text_stream(
    model: str = "claude-sonnet-5-5",
    output_tokens: int = 15,
    texts: tuple[str, ...] = ("Hel", "lo!"),
) -> bytes:
    """A complete stream: start, ping, one text block, an unknown event, delta, stop."""
    return (
        message_start(model)
        + sse("ping", {"type": "ping"})
        + text_block_start()
        + b"".join(text_delta(t) for t in texts)
        + sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        + sse("some_future_event", {"type": "some_future_event", "detail": 1})
        + sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            },
        )
        + sse("message_stop", {"type": "message_stop"})
    )
