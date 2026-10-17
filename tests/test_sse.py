import json

from gateway.sse import SSEEvent, SSEParser, StreamTracker
from tests.streams import sse, text_stream


def parse_all(raw: bytes, chunk_size: int) -> list[SSEEvent]:
    parser = SSEParser()
    events = []
    for i in range(0, len(raw), chunk_size):
        events += parser.feed(raw[i : i + chunk_size])
    return events


def test_events_split_across_chunks_are_reassembled() -> None:
    raw = text_stream()
    whole = parse_all(raw, len(raw))
    for size in (1, 3, 7, 64):
        assert parse_all(raw, size) == whole
    assert [e.event for e in whole][:2] == ["message_start", "ping"]


def test_crlf_line_endings_and_comments() -> None:
    raw = b': keep-alive comment\r\n\r\nevent: ping\r\ndata: {"type": "ping"}\r\n\r\n'
    assert SSEParser().feed(raw) == [SSEEvent("ping", '{"type": "ping"}')]


def test_incomplete_event_waits_for_more_data() -> None:
    parser = SSEParser()
    assert parser.feed(b'event: ping\ndata: {"type"') == []
    assert parser.feed(b': "ping"}\n\n') == [SSEEvent("ping", '{"type": "ping"}')]


def track(raw: bytes, rebuild: bool = False) -> StreamTracker:
    tracker = StreamTracker(rebuild_message=rebuild)
    for event in SSEParser().feed(raw):
        tracker.handle(event)
    return tracker


def test_usage_merges_message_start_and_cumulative_deltas() -> None:
    tracker = track(text_stream())
    assert tracker.completed
    assert tracker.stop_reason == "end_turn"
    # input/cache fields from message_start; output from the last message_delta
    assert tracker.usage["input_tokens"] == 25
    assert tracker.usage["cache_read_input_tokens"] == 100
    assert tracker.usage["output_tokens"] == 15


def test_later_delta_overrides_earlier_values_including_input() -> None:
    raw = (
        sse("message_start", {"type": "message_start", "message": {
            "type": "message", "model": "m", "content": [],
            "usage": {"input_tokens": 2679, "output_tokens": 3}}})
        + sse("message_delta", {"type": "message_delta", "delta": {},
                                "usage": {"output_tokens": 200}})
        + sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                "usage": {"input_tokens": 10682, "output_tokens": 510}})
        + sse("message_stop", {"type": "message_stop"})
    )  # fmt: skip
    tracker = track(raw)
    assert tracker.usage["input_tokens"] == 10682
    assert tracker.usage["output_tokens"] == 510


def test_error_event_is_captured() -> None:
    raw = sse("error", {"type": "error", "error": {"type": "overloaded_error", "message": "x"}})
    tracker = track(raw)
    assert tracker.error_type == "overloaded_error"
    assert not tracker.completed


def test_unknown_events_and_bad_json_are_ignored() -> None:
    raw = sse("brand_new_event", {"type": "brand_new_event"}) + b"event: x\ndata: {not json\n\n"
    tracker = track(raw)
    assert tracker.message is None and tracker.error_type is None


def test_rebuilds_text_and_tool_use_blocks() -> None:
    tool_input = {"city": "Paris", "days": 3}
    encoded = json.dumps(tool_input)
    raw = (
        sse("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "m",
            "content": [], "stop_reason": None, "usage": {"input_tokens": 5, "output_tokens": 1}}})
        + sse("content_block_start", {"type": "content_block_start", "index": 0,
                                      "content_block": {"type": "text", "text": ""}})
        + sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                      "delta": {"type": "text_delta", "text": "Checking "}})
        + sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                      "delta": {"type": "text_delta", "text": "weather."}})
        + sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        + sse("content_block_start", {"type": "content_block_start", "index": 1,
                                      "content_block": {"type": "tool_use", "id": "tu_1",
                                                        "name": "weather", "input": {}}})
        + sse("content_block_delta", {"type": "content_block_delta", "index": 1,
                                      "delta": {"type": "input_json_delta",
                                                "partial_json": encoded[:9]}})
        + sse("content_block_delta", {"type": "content_block_delta", "index": 1,
                                      "delta": {"type": "input_json_delta",
                                                "partial_json": encoded[9:]}})
        + sse("content_block_stop", {"type": "content_block_stop", "index": 1})
        + sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                                "usage": {"output_tokens": 30}})
        + sse("message_stop", {"type": "message_stop"})
    )  # fmt: skip
    message = track(raw, rebuild=True).final_message()
    assert message is not None
    assert message["content"] == [
        {"type": "text", "text": "Checking weather."},
        {"type": "tool_use", "id": "tu_1", "name": "weather", "input": tool_input},
    ]
    assert message["stop_reason"] == "tool_use"
    assert message["usage"]["output_tokens"] == 30


def test_no_rebuild_keeps_content_empty() -> None:
    message = track(text_stream()).final_message()
    assert message is not None
    assert message["content"] == []
    assert message["usage"]["output_tokens"] == 15
