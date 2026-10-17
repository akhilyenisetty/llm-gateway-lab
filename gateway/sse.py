"""Reading Messages API server-sent events (SSE) as they pass through the gateway.

An SSE stream is text: each event is a few `field: value` lines ending with a blank
line, e.g.

    event: message_delta
    data: {"type": "message_delta", "delta": {...}, "usage": {"output_tokens": 15}}

Network chunks don't line up with events (one chunk can hold half an event, or
several), so `SSEParser` buffers bytes until it sees a complete event.

`StreamTracker` follows the Messages API event flow to recover what the gateway
records: model, usage, stop reason, errors, whether the stream completed, and
(optionally) the final message rebuilt from its deltas.
"""

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SSEEvent:
    event: str | None
    data: str


class SSEParser:
    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        # Normalize CRLF so one separator rule covers both line-ending styles.
        self._buffer += chunk.replace(b"\r\n", b"\n")
        events = []
        while b"\n\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n\n", 1)
            if event := self._parse(raw.decode("utf-8", errors="replace")):
                events.append(event)
        return events

    @staticmethod
    def _parse(raw: str) -> SSEEvent | None:
        name, data_lines = None, []
        for line in raw.split("\n"):
            if line.startswith(":"):  # comment line
                continue
            key, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if key == "event":
                name = value
            elif key == "data":
                data_lines.append(value)
        if not data_lines:
            return None
        return SSEEvent(name, "\n".join(data_lines))


@dataclass
class StreamTracker:
    """Accumulates what the gateway records from a stream of Messages API events."""

    rebuild_message: bool = False
    message: dict[str, Any] | None = None  # from message_start, updated as events arrive
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None
    error_type: str | None = None
    error_body: dict[str, Any] | None = None
    completed: bool = False  # saw message_stop
    first_delta_seen: bool = False
    _partial_json: dict[int, str] = field(default_factory=dict)

    def handle(self, event: SSEEvent) -> None:
        try:
            data = json.loads(event.data)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type") or event.event

        if kind == "message_start":
            self.message = data.get("message") or {}
            self.usage = dict(self.message.get("usage") or {})
            if self.rebuild_message:
                self.message["content"] = []
        elif kind == "message_delta":
            # Usage here is cumulative and may update any field (input too, e.g. after
            # server tool use), so overwrite field by field rather than adding.
            for key, value in (data.get("usage") or {}).items():
                if value is not None:
                    self.usage[key] = value
            delta = data.get("delta") or {}
            if delta.get("stop_reason"):
                self.stop_reason = delta["stop_reason"]
            if self.message is not None:
                self.message.update({k: v for k, v in delta.items() if v is not None})
        elif kind == "message_stop":
            self.completed = True
        elif kind == "error":
            self.error_body = data
            self.error_type = (data.get("error") or {}).get("type") or "stream_error"
        elif kind == "content_block_delta":
            self.first_delta_seen = True
            if self.rebuild_message:
                self._apply_delta(data)
        elif kind == "content_block_start" and self.rebuild_message:
            self._content().append(dict(data.get("content_block") or {}))
        elif kind == "content_block_stop" and self.rebuild_message:
            self._finish_block(data.get("index"))
        # ping and unknown event types: nothing to record.

    def final_message(self) -> dict[str, Any] | None:
        """The message as a non-streaming call would have returned it (best effort)."""
        if self.message is None:
            return None
        return {**self.message, "usage": self.usage, "stop_reason": self.stop_reason}

    def _content(self) -> list[dict[str, Any]]:
        if self.message is None:
            self.message = {"content": []}
        return self.message.setdefault("content", [])

    def _block(self, index: Any) -> dict[str, Any] | None:
        content = self._content()
        return content[index] if isinstance(index, int) and 0 <= index < len(content) else None

    def _apply_delta(self, data: dict[str, Any]) -> None:
        index, delta = data.get("index"), data.get("delta") or {}
        block = self._block(index)
        if block is None:
            return
        match delta.get("type"):
            case "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            case "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            case "signature_delta":
                block["signature"] = delta.get("signature")
            case "input_json_delta":
                assert isinstance(index, int)
                self._partial_json[index] = self._partial_json.get(index, "") + delta.get(
                    "partial_json", ""
                )
            case "citations_delta":
                block.setdefault("citations", []).append(delta.get("citation"))

    def _finish_block(self, index: Any) -> None:
        block = self._block(index)
        if block is None or index not in self._partial_json:
            return
        raw = self._partial_json.pop(index)
        try:
            block["input"] = json.loads(raw) if raw else {}
        except ValueError:
            block["input_raw"] = raw  # incomplete or invalid JSON: keep what arrived
