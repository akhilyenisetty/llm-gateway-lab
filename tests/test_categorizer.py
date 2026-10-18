"""Session categorization: client tags, the classifier over stored content, and the worker."""

import json
from datetime import timedelta
from typing import Any

import anthropic
import httpx
import pytest
import respx
from fastapi import FastAPI

from gateway import categorizer
from gateway.categorizer import SessionRef
from gateway.models import SessionCategory
from tests.conftest import ADMIN_KEY, key_id_of, message_response
from tests.test_classifier import api_message, classifier_with

NOW = timedelta(0)  # treat every session as idle


def fake_classifier(category: str = "coding", sent: list[dict[str, Any]] | None = None):  # type: ignore[no-untyped-def]
    out = json.dumps({"category": category, "reason": "test reason"})
    return classifier_with(lambda _: api_message(out), sent if sent is not None else [])


async def call(
    sdk: anthropic.AsyncAnthropic, text: str, session: str = "s1", **headers: str
) -> None:
    await sdk.messages.create(
        model="claude-sonnet-5-5",
        max_tokens=10,
        messages=[{"role": "user", "content": text}],
        extra_headers={"X-Session-Id": session, **headers},
    )


async def row(app: FastAPI, ref: SessionRef) -> SessionCategory | None:
    async with app.state.session_factory() as session:
        return await session.get(SessionCategory, (ref.api_key_id, ref.session_id))


@pytest.fixture
async def ref(app: FastAPI, gateway_key: str) -> SessionRef:
    return SessionRef(await key_id_of(app, gateway_key), "s1")


async def test_client_tag_wins_without_a_model_call(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter, ref: SessionRef
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response())
    for tag in ("writing", "analysis", "writing"):
        await call(sdk, "hi", **{"X-Category": tag})
    sent: list[dict[str, Any]] = []

    result = await categorizer.categorize_session(
        app.state.session_factory, fake_classifier(sent=sent), ref
    )
    assert (result.status, result.category, result.client_category) == (
        "client_tag",
        "writing",  # most frequent tag
        "writing",
    )
    assert sent == []


async def test_without_stored_content_there_is_nothing_to_classify(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter, ref: SessionRef
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response())
    await call(sdk, "hi")
    sent: list[dict[str, Any]] = []
    result = await categorizer.categorize_session(
        app.state.session_factory, fake_classifier(sent=sent), ref
    )
    assert (result.status, result.category) == ("no_content", None)
    assert sent == []


@pytest.mark.parametrize("store_content", [True])
async def test_classifies_stored_conversation(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter, ref: SessionRef
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response(text="Here is the email."))
    await call(sdk, "Draft an email to my landlord about the broken heater")
    sent: list[dict[str, Any]] = []

    result = await categorizer.categorize_session(
        app.state.session_factory, fake_classifier("writing", sent), ref
    )
    assert (result.status, result.category, result.auto_category) == (
        "classified",
        "writing",
        "writing",
    )
    assert (result.auto_reason, result.classifier_model, result.prompt_version) == (
        "test reason",
        "claude-haiku-5-5",
        "v1",
    )
    assert result.categories_version == 3 and result.classified_calls == 1
    assert result.classifier_cost_usd is not None and result.classifier_cost_usd > 0
    digest = sent[0]["messages"][0]["content"]
    assert "User: Draft an email to my landlord" in digest
    assert "Assistant: Here is the email." in digest  # from the stored response


@pytest.mark.parametrize("store_content", [True])
async def test_failure_is_recorded_and_keeps_the_earlier_category(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter, ref: SessionRef
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response())
    await call(sdk, "Fix my test")
    await categorizer.categorize_session(app.state.session_factory, fake_classifier(), ref)

    await call(sdk, "One more thing")
    broken = classifier_with(lambda _: api_message("", "refusal"), [])
    result = await categorizer.categorize_session(app.state.session_factory, broken, ref)
    assert (result.status, result.category) == ("error", "coding")
    assert "refusal" in (result.error or "")


@pytest.mark.parametrize("store_content", [True])
async def test_worker_picks_idle_new_and_grown_sessions(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter, ref: SessionRef
) -> None:
    factory = app.state.session_factory
    upstream.post("/v1/messages").respond(200, json=message_response())
    await call(sdk, "Write a function")
    await call(sdk, "What is TCP?", session="s2")

    # Not idle long enough yet.
    assert await categorizer.run_once(factory, fake_classifier(), idle=timedelta(hours=1)) == 0
    # Idle: both sessions are new.
    assert await categorizer.run_once(factory, fake_classifier(), idle=NOW) == 2
    # Nothing changed: nothing to do.
    assert await categorizer.run_once(factory, fake_classifier(), idle=NOW) == 0
    # s1 grows: it's categorized again.
    await call(sdk, "Now add tests")
    pending = await categorizer.pending_sessions(
        factory, idle=NOW, retry_errors_after=timedelta(minutes=30), limit=10
    )
    assert pending == [ref]


@pytest.mark.parametrize("store_content", [True])
async def test_errors_are_retried_after_a_delay(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter, ref: SessionRef
) -> None:
    factory = app.state.session_factory
    upstream.post("/v1/messages").respond(200, json=message_response())
    await call(sdk, "Write a function")
    broken = classifier_with(lambda _: api_message("not json"), [])
    await categorizer.run_once(factory, broken, idle=NOW)
    assert (await row(app, ref)).status == "error"  # type: ignore[union-attr]

    later = {"idle": NOW, "limit": 10}
    assert (
        await categorizer.pending_sessions(factory, retry_errors_after=timedelta(hours=1), **later)
        == []
    )
    assert await categorizer.pending_sessions(factory, retry_errors_after=NOW, **later) == [ref]


@pytest.mark.parametrize("store_content", [True])
async def test_admin_categorize_now_and_session_status(
    app: FastAPI,
    http: httpx.AsyncClient,
    sdk: anthropic.AsyncAnthropic,
    upstream: respx.MockRouter,
    ref: SessionRef,
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response())
    await call(sdk, "Summarize this CSV of sales by region")
    app.state.classifier = fake_classifier("analysis")
    url = f"/admin/keys/{ref.api_key_id}/sessions/s1"
    admin = {"authorization": f"Bearer {ADMIN_KEY}"}

    assert (await http.get(url, headers=admin)).json()["category"] is None
    body = (await http.post(f"{url}/categorize", headers=admin)).json()
    assert body["category"]["category"] == "analysis"
    assert body["category"]["status"] == "classified"
    assert (await http.get(url, headers=admin)).json()["category"]["auto_reason"] == "test reason"


@pytest.mark.parametrize("store_content", [True])
async def test_classifier_calls_do_not_go_through_the_gateway(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter, ref: SessionRef
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response())
    await call(sdk, "Write a function")
    before = len(upstream.calls)
    await categorizer.categorize_session(app.state.session_factory, fake_classifier(), ref)
    assert len(upstream.calls) == before  # the gateway saw no new request


async def test_worker_starts_and_stops_with_the_app(app: FastAPI, settings) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    import anyio

    from gateway.main import create_app

    def workers() -> list[asyncio.Task]:  # the categorizer's task, identified by its coroutine
        return [t for t in asyncio.all_tasks() if t.get_coro().__qualname__ == "worker"]

    enabled = create_app(settings.model_copy(update={"categorize_enabled": True}))
    with anyio.fail_after(5):
        async with enabled.router.lifespan_context(enabled):
            await asyncio.sleep(0.05)
            [task] = workers()
        assert task.done()  # cancelled and awaited on shutdown
    assert workers() == []
