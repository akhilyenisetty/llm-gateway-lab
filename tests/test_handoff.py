"""Context handoff: transcript building, rendering, generation, and the admin/client APIs."""

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import anthropic
import httpx
import httpx2
import pytest
import respx
from fastapi import FastAPI

from gateway.auth import create_key
from gateway.handoff import SCHEMA, HandoffError, generate, to_markdown
from gateway.pricing import PriceTable
from gateway.settings import PROJECT_ROOT
from gateway.transcript import build_transcript, pick_main
from tests.conftest import ADMIN_KEY, key_id_of, message_response

PRICES = PriceTable.load(PROJECT_ROOT / "config" / "prices.yaml")
ADMIN = {"authorization": f"Bearer {ADMIN_KEY}"}

CONTENT: dict[str, Any] = {
    "title": "Add health endpoint",
    "goal": "Add a /health endpoint to the FastAPI app.",
    "current_state": "Endpoint added in app/main.py; tests not written yet.",
    "decisions": [{"decision": "No auth on /health", "reason": "Load balancer must reach it"}],
    "open_tasks": [{"task": "Write a test", "details": "tests/test_health.py"}],
    "constraints": ["Python 3.11"],
    "key_files": [{"path": "app/main.py", "role": "routes"}],
    "key_code": [{"label": "The route", "language": "python", "code": "@app.get('/health')"}],
    "tried_and_failed": [{"attempt": "Using a Starlette route", "why_it_failed": "no docs"}],
    "next_step": "Write tests/test_health.py.",
    "open_questions": [],
}


# --- Transcript ----------------------------------------------------------------------


def test_pick_main_takes_the_longest_conversation_latest_on_ties() -> None:
    a = {"messages": [1]}
    b = {"messages": [1, 2, 3]}
    c = {"messages": [1, 2, 3]}
    assert pick_main([a, b, {"messages": [9]}]) == 1
    assert pick_main([a, b, c]) == 2
    assert pick_main([]) is None


def test_transcript_keeps_tools_and_drops_thinking_and_reminders() -> None:
    request = {
        "system": "You are a coding agent. " * 1000,
        "messages": [
            {"role": "user", "content": "<system-reminder>env</system-reminder>Fix the flaky test"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private reasoning"},
                    {"type": "text", "text": "Running it."},
                    {"type": "tool_use", "id": "t1", "name": "Bash",
                     "input": {"command": "pytest -x"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                     "content": [{"type": "text", "text": "E   AssertionError: 3 != 4"}]},
                ],
            },
        ],
    }  # fmt: skip
    response = {"content": [{"type": "text", "text": "The fixture shares state."}]}
    t = build_transcript(request, response, max_tokens=100_000)
    assert "**User:** Fix the flaky test" in t.text
    assert '→ tool call `Bash`: {"command": "pytest -x"}' in t.text
    assert "← tool result (error): E   AssertionError: 3 != 4" in t.text
    assert t.text.rstrip().endswith("**Assistant:** The fixture shares state.")
    assert "private reasoning" not in t.text and "env" not in t.text
    assert "characters clipped" in t.text  # the long system prompt
    assert (t.messages, t.omitted_blocks) == (4, 0)


def test_long_transcript_keeps_start_and_end() -> None:
    messages = [{"role": "user", "content": f"step {i} " + "x" * 2_000} for i in range(200)]
    t = build_transcript({"messages": messages}, None, max_tokens=10_000)  # ~35K chars
    assert len(t.text) <= 35_000
    assert t.text.startswith("**User:** step 0 ")
    assert "**User:** step 199 " in t.text  # the current state survives
    assert t.omitted_blocks > 100 and "omitted to fit the budget" in t.text


# --- Rendering and generation --------------------------------------------------------


def test_markdown_has_every_section_in_a_fixed_order() -> None:
    empty = {**CONTENT, "constraints": [], "key_code": [], "tried_and_failed": []}
    meta = {"session_id": "s1", "generated_at": "2026-10-18", "model": "m", "prompt_version": "v1",
            "calls": 12, "omitted_blocks": 3}  # fmt: skip
    md = to_markdown(empty, meta)
    order = ["# Handoff: Add health endpoint", "## Goal", "## Current state", "## Next step",
             "## Decisions and why", "## Open tasks", "## Constraints", "## Key files",
             "## Tried and failed", "## Open questions"]  # fmt: skip
    positions = [md.index(h) for h in order]
    assert positions == sorted(positions)
    assert "## Key code" not in md  # omitted when empty
    assert "- **No auth on /health** — Load balancer must reach it" in md
    assert "- [ ] **Write a test** — tests/test_health.py" in md
    assert md.count("_None recorded._") == 3  # constraints, tried and failed, open questions
    assert "3 earlier messages and tool calls were left out" in md
    assert "```python\n@app.get('/health')\n```" in to_markdown(CONTENT, meta)


def fake_client(
    respond: Callable[[dict[str, Any]], dict[str, Any]], sent: list[dict[str, Any]]
) -> anthropic.AsyncAnthropic:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        sent.append(body)
        return httpx2.Response(200, json=respond(body))

    return anthropic.AsyncAnthropic(
        api_key="test", max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )  # fmt: skip


def api_message(text: str, stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_h", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": text}], "stop_reason": stop_reason,
        "stop_sequence": None, "usage": {"input_tokens": 20_000, "output_tokens": 1_500},
    }  # fmt: skip


async def test_generate_sends_schema_and_parses_the_result() -> None:
    sent: list[dict[str, Any]] = []
    client = fake_client(lambda _: api_message(json.dumps(CONTENT)), sent)
    result = await generate(
        client, PRICES, "**User:** hi", model="claude-sonnet-5-5", effort="high"
    )
    assert result.content == CONTENT
    assert (result.input_tokens, result.output_tokens) == (20_000, 1_500)
    # Sonnet 5.5: 20,000 * $2 + 1,500 * $10 per MTok = $0.055
    assert result.cost_usd == Decimal("0.055")
    [body] = sent
    assert body["output_config"] == {
        "effort": "high",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    assert "<transcript>\n**User:** hi\n</transcript>" in body["messages"][0]["content"]
    assert "temperature" not in body


@pytest.mark.parametrize(
    ("text", "stop", "error"),
    [("", "refusal", "refusal"), ('{"title": "x"}', "end_turn", "missing sections"),
     ("nope", "end_turn", "not JSON"), ('{"title"', "max_tokens", "max_tokens")],
)  # fmt: skip
async def test_unusable_output_raises(text: str, stop: str, error: str) -> None:
    client = fake_client(lambda _: api_message(text, stop), [])
    with pytest.raises(HandoffError, match=error):
        await generate(client, PRICES, "t", model="claude-sonnet-5-5", effort="high")


# --- Admin and client APIs -----------------------------------------------------------


async def record_session(
    sdk: anthropic.AsyncAnthropic, session: str = "s1", turns: int = 2
) -> None:
    history: list[dict[str, Any]] = []
    for i in range(turns):
        history.append({"role": "user", "content": f"turn {i}: add a health endpoint"})
        await sdk.messages.create(
            model="claude-sonnet-5-5", max_tokens=10, messages=history,
            extra_headers={"X-Session-Id": session},
        )  # fmt: skip
        history.append({"role": "assistant", "content": "Hello!"})


@pytest.fixture
def handoff_calls(app: FastAPI) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    app.state.anthropic = fake_client(lambda _: api_message(json.dumps(CONTENT)), sent)
    return sent


@pytest.mark.parametrize("store_content", [True])
async def test_admin_generates_lists_and_downloads_a_handoff(
    app: FastAPI,
    http: httpx.AsyncClient,
    sdk: anthropic.AsyncAnthropic,
    gateway_key: str,
    upstream: respx.MockRouter,
    handoff_calls: list[dict[str, Any]],
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response())
    await record_session(sdk, turns=3)
    base = f"/admin/keys/{await key_id_of(app, gateway_key)}/sessions/s1"

    resp = await http.post(f"{base}/handoffs", headers=ADMIN)
    assert resp.status_code == 201
    handoff = resp.json()
    assert (handoff["status"], handoff["requested_by"], handoff["session_calls"]) == (
        "ready",
        "admin",
        3,
    )
    assert handoff["transcript_messages"] == 6  # the longest request's 5 messages + response
    assert handoff["content"] == CONTENT and handoff["markdown"].startswith("# Handoff: Add health")
    assert Decimal(handoff["cost_usd"]) == Decimal("0.055")
    # The model read the longest request (all 3 turns), not the first one.
    transcript = handoff_calls[0]["messages"][0]["content"]
    assert "turn 0" in transcript and "turn 2" in transcript

    [listed] = (await http.get(f"{base}/handoffs", headers=ADMIN)).json()
    assert listed["id"] == handoff["id"] and "markdown" not in listed
    md = await http.get(f"/admin/handoffs/{handoff['id']}/markdown", headers=ADMIN)
    assert md.headers["content-type"].startswith("text/markdown")
    assert md.headers["content-disposition"].startswith('attachment; filename="handoff-s1-')
    assert md.text == handoff["markdown"]


@pytest.mark.parametrize("store_content", [True])
async def test_cooldown_between_generations(
    app: FastAPI, http: httpx.AsyncClient, sdk: anthropic.AsyncAnthropic, gateway_key: str,
    upstream: respx.MockRouter, handoff_calls: list[dict[str, Any]],
) -> None:  # fmt: skip
    upstream.post("/v1/messages").respond(200, json=message_response())
    await record_session(sdk)
    url = f"/admin/keys/{await key_id_of(app, gateway_key)}/sessions/s1/handoffs"
    assert (await http.post(url, headers=ADMIN)).status_code == 201
    again = await http.post(url, headers=ADMIN)
    assert again.status_code == 429 and 0 < int(again.headers["retry-after"]) <= 60
    assert len(handoff_calls) == 1  # no second model call


async def test_no_stored_content_is_409_without_a_model_call(
    app: FastAPI, http: httpx.AsyncClient, sdk: anthropic.AsyncAnthropic, gateway_key: str,
    upstream: respx.MockRouter, handoff_calls: list[dict[str, Any]],
) -> None:  # fmt: skip
    upstream.post("/v1/messages").respond(200, json=message_response())
    await record_session(sdk)  # STORE_CONTENT is off by default
    url = f"/admin/keys/{await key_id_of(app, gateway_key)}/sessions/s1/handoffs"
    resp = await http.post(url, headers=ADMIN)
    assert resp.status_code == 409 and "STORE_CONTENT" in resp.json()["detail"]
    assert handoff_calls == []


@pytest.mark.parametrize("store_content", [True])
async def test_failed_generation_is_stored_and_reported(
    app: FastAPI, http: httpx.AsyncClient, sdk: anthropic.AsyncAnthropic, gateway_key: str,
    upstream: respx.MockRouter,
) -> None:  # fmt: skip
    upstream.post("/v1/messages").respond(200, json=message_response())
    await record_session(sdk)
    app.state.anthropic = fake_client(lambda _: api_message("", "refusal"), [])
    base = f"/admin/keys/{await key_id_of(app, gateway_key)}/sessions/s1"
    resp = await http.post(f"{base}/handoffs", headers=ADMIN)
    assert resp.status_code == 502 and "refusal" in resp.json()["detail"]
    [stored] = (await http.get(f"{base}/handoffs", headers=ADMIN)).json()
    assert stored["status"] == "error" and "refusal" in stored["error"]
    assert (
        await http.get(f"/admin/handoffs/{stored['id']}/markdown", headers=ADMIN)
    ).status_code == 404


async def test_no_anthropic_key_is_503(
    app: FastAPI, http: httpx.AsyncClient, gateway_key: str
) -> None:
    app.state.anthropic = None
    url = f"/admin/keys/{await key_id_of(app, gateway_key)}/sessions/s1/handoffs"
    assert (await http.post(url, headers=ADMIN)).status_code == 503


@pytest.mark.parametrize("store_content", [True])
async def test_client_api_is_scoped_to_its_own_sessions(
    app: FastAPI, http: httpx.AsyncClient, sdk: anthropic.AsyncAnthropic, gateway_key: str,
    upstream: respx.MockRouter, handoff_calls: list[dict[str, Any]],
) -> None:  # fmt: skip
    upstream.post("/v1/messages").respond(200, json=message_response())
    await record_session(sdk, "mine")
    me = {"x-api-key": gateway_key}
    async with app.state.session_factory() as session:
        _, other_key = await create_key(session, "someone-else")
    other = {"x-api-key": other_key}

    [listed] = (await http.get("/gateway/sessions", headers=me)).json()
    assert (listed["session_id"], listed["calls"], listed["has_handoff"]) == ("mine", 2, False)
    assert (await http.get("/gateway/sessions", headers=other)).json() == []

    assert (await http.post("/gateway/sessions/mine/handoffs", headers=other)).status_code == 404
    assert (
        await http.get("/gateway/sessions/mine/handoffs/latest.md", headers=me)
    ).status_code == 404
    created = await http.post("/gateway/sessions/mine/handoffs", headers=me)
    assert created.status_code == 201 and created.json()["requested_by"] == "client"
    md = await http.get("/gateway/sessions/mine/handoffs/latest.md", headers=me)
    assert md.status_code == 200 and md.text.startswith("# Handoff:")
    assert (
        await http.get("/gateway/sessions/mine/handoffs/latest.md", headers=other)
    ).status_code == 404
    assert (await http.get("/gateway/sessions", headers=me)).json()[0]["has_handoff"] is True


async def test_client_api_needs_a_gateway_key(http: httpx.AsyncClient) -> None:
    assert (await http.get("/gateway/sessions")).status_code == 401
    assert (
        await http.get("/gateway/sessions", headers={"x-api-key": "gw_nope"})
    ).status_code == 401
    # The admin key is not a gateway key.
    assert (await http.get("/gateway/sessions", headers=ADMIN)).status_code == 401
