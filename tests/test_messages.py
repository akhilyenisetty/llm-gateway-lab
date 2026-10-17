"""End-to-end: Anthropic SDK -> gateway -> mocked Anthropic API, then check the DB row."""

import json
from decimal import Decimal

import anthropic
import httpx
import pytest
import respx
from fastapi import FastAPI

from gateway import recorder
from tests.conftest import REAL_KEY, fetch_contents, fetch_rows, message_response

USER_MSG = [{"role": "user", "content": "Hi"}]


async def test_sdk_call_returns_normal_message_and_logs_exact_usage(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, gateway_key: str, upstream: respx.MockRouter
) -> None:
    usage = {
        "input_tokens": 1_000,
        "output_tokens": 500,
        "cache_creation_input_tokens": 5_000,
        "cache_read_input_tokens": 2_000,
        "cache_creation": {"ephemeral_5m_input_tokens": 4_000, "ephemeral_1h_input_tokens": 1_000},
        "service_tier": "standard",
    }
    route = upstream.post("/v1/messages").respond(
        200, json=message_response(usage=usage), headers={"request-id": "req_abc"}
    )

    message = await sdk.messages.create(
        model="claude-sonnet-5-5",
        max_tokens=100,
        messages=USER_MSG,
        extra_headers={"X-Session-Id": "sess-1", "X-Category": "coding", "X-App": "pytest"},
    )

    # The client sees an ordinary SDK Message.
    assert isinstance(message, anthropic.types.Message)
    assert message.content[0].text == "Hello!"
    assert message.usage.input_tokens == 1_000

    # Upstream got the real key; the gateway key and gateway-only headers were stripped.
    sent = route.calls.last.request
    assert sent.headers["x-api-key"] == REAL_KEY
    assert gateway_key not in str(sent.headers)
    assert "x-session-id" not in sent.headers
    assert "x-category" not in sent.headers
    assert sent.headers["x-app"] == "pytest"  # forwarded: Claude Code sends x-app itself
    assert "anthropic-version" in sent.headers
    assert json.loads(sent.content)["model"] == "claude-sonnet-5-5"

    [row] = await fetch_rows(app)
    assert (row.session_id, row.session_id_source) == ("sess-1", "header")
    assert (row.category, row.app) == ("coding", "pytest")
    assert row.model == "claude-sonnet-5-5"
    assert row.status_code == 200
    assert row.stop_reason == "end_turn"
    assert row.upstream_request_id == "req_abc"
    assert row.is_stream is False
    assert row.error_type is None
    assert (row.input_tokens, row.output_tokens) == (1_000, 500)
    assert row.cache_read_tokens == 2_000
    assert (row.cache_write_5m_tokens, row.cache_write_1h_tokens) == (4_000, 1_000)
    # Sonnet 5.5: 1000*2 + 500*10 + 2000*0.10 + 4000*2.50 + 1000*4 = 21,200 / 1M
    assert row.cost_usd == Decimal("0.0212")
    assert row.latency_ms >= 0


async def test_beta_client_query_string_is_forwarded(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    route = upstream.post("/v1/messages").respond(200, json=message_response())
    await sdk.beta.messages.create(model="claude-sonnet-5-5", max_tokens=10, messages=USER_MSG)
    assert route.calls.last.request.url.params["beta"] == "true"
    assert len(await fetch_rows(app)) == 1


async def test_derived_session_is_stable_across_turns(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response())
    turn1 = USER_MSG
    turn2 = [*turn1, {"role": "assistant", "content": "Hello!"}, {"role": "user", "content": "?"}]
    other = [{"role": "user", "content": "A different conversation"}]
    for messages in (turn1, turn2, other):
        await sdk.messages.create(model="claude-sonnet-5-5", max_tokens=10, messages=messages)

    rows = await fetch_rows(app)
    assert all(r.session_id_source == "derived" for r in rows)
    assert rows[0].session_id == rows[1].session_id
    assert rows[2].session_id != rows[0].session_id


async def test_upstream_error_is_passed_through_and_logged(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(
        429,
        json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
        headers={"retry-after": "7"},
    )
    with pytest.raises(anthropic.RateLimitError) as exc_info:
        await sdk.messages.create(model="claude-sonnet-5-5", max_tokens=10, messages=USER_MSG)
    assert exc_info.value.response.headers["retry-after"] == "7"

    [row] = await fetch_rows(app)
    assert row.status_code == 429
    assert row.error_type == "rate_limit_error"
    assert row.model == "claude-sonnet-5-5"
    assert row.input_tokens is None
    assert row.cost_usd is None


async def test_unreachable_upstream_returns_502_and_is_logged(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").mock(side_effect=httpx.ConnectError("connection refused"))
    with pytest.raises(anthropic.InternalServerError) as exc_info:
        await sdk.messages.create(model="claude-sonnet-5-5", max_tokens=10, messages=USER_MSG)
    assert exc_info.value.status_code == 502

    [row] = await fetch_rows(app)
    assert (row.status_code, row.error_type) == (502, "upstream_unreachable")


async def test_upstream_timeout_returns_504(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").mock(side_effect=httpx.ReadTimeout("too slow"))
    with pytest.raises(anthropic.APIStatusError) as exc_info:
        await sdk.messages.create(model="claude-sonnet-5-5", max_tokens=10, messages=USER_MSG)
    assert exc_info.value.status_code == 504
    [row] = await fetch_rows(app)
    assert row.error_type == "upstream_timeout"


async def test_unknown_model_is_logged_without_cost(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response(model="claude-new-9"))
    await sdk.messages.create(model="claude-new-9", max_tokens=10, messages=USER_MSG)
    [row] = await fetch_rows(app)
    assert row.input_tokens == 12
    assert row.cost_usd is None
    assert row.cost_note == "no price entry for model 'claude-new-9'"


async def test_invalid_json_is_rejected(http: httpx.AsyncClient, gateway_key: str) -> None:
    resp = await http.post("/v1/messages", headers={"x-api-key": gateway_key}, content=b"not json")
    assert resp.status_code == 400


async def test_content_not_stored_by_default(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response())
    await sdk.messages.create(model="claude-sonnet-5-5", max_tokens=10, messages=USER_MSG)
    assert await fetch_contents(app) == []


@pytest.mark.parametrize("store_content", [True])
async def test_content_stored_when_enabled(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response())
    await sdk.messages.create(model="claude-sonnet-5-5", max_tokens=10, messages=USER_MSG)

    [row] = await fetch_rows(app)
    [content] = await fetch_contents(app)
    assert content.request_id == row.id
    assert content.request_json["messages"] == USER_MSG
    assert content.response_json["content"][0]["text"] == "Hello!"


async def test_recording_failure_does_not_break_the_call(
    app: FastAPI,
    sdk: anthropic.AsyncAnthropic,
    upstream: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("database is down")

    monkeypatch.setattr(recorder, "build_row", broken)
    upstream.post("/v1/messages").respond(200, json=message_response())
    message = await sdk.messages.create(model="claude-sonnet-5-5", max_tokens=10, messages=USER_MSG)
    assert message.content[0].text == "Hello!"
