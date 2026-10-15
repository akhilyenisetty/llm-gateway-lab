"""Other Anthropic endpoints are forwarded with the key swap but not recorded."""

import anthropic
import httpx
import respx
from fastapi import FastAPI

from tests.conftest import REAL_KEY, fetch_rows


async def test_count_tokens_passes_through(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    route = upstream.post("/v1/messages/count_tokens").respond(200, json={"input_tokens": 42})
    result = await sdk.messages.count_tokens(
        model="claude-sonnet-5-5", messages=[{"role": "user", "content": "Hi"}]
    )
    assert result.input_tokens == 42
    assert route.calls.last.request.headers["x-api-key"] == REAL_KEY
    assert await fetch_rows(app) == []


async def test_models_list_passes_through_with_query_params(
    sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    page = {
        "data": [
            {
                "type": "model",
                "id": "claude-sonnet-5-5",
                "display_name": "Claude Sonnet 5.5",
                "created_at": "2026-09-28T00:00:00Z",
            }
        ],
        "has_more": False,
        "first_id": "claude-sonnet-5-5",
        "last_id": "claude-sonnet-5-5",
    }
    route = upstream.get("/v1/models").respond(200, json=page)
    models = await sdk.models.list(limit=5)
    assert [m.id for m in models.data] == ["claude-sonnet-5-5"]
    assert route.calls.last.request.url.params["limit"] == "5"


async def test_passthrough_requires_gateway_key(
    http: httpx.AsyncClient, upstream: respx.MockRouter
) -> None:
    route = upstream.get("/v1/models")
    resp = await http.get("/v1/models")
    assert resp.status_code == 401
    assert not route.called


async def test_passthrough_preserves_upstream_errors(
    http: httpx.AsyncClient, gateway_key: str, upstream: respx.MockRouter
) -> None:
    error = {"type": "error", "error": {"type": "not_found_error", "message": "nope"}}
    upstream.get("/v1/models/claude-nope").respond(404, json=error)
    resp = await http.get("/v1/models/claude-nope", headers={"x-api-key": gateway_key})
    assert resp.status_code == 404
    assert resp.json() == error


async def test_health_needs_no_auth(http: httpx.AsyncClient) -> None:
    resp = await http.get("/health")
    assert resp.json() == {"status": "ok"}
