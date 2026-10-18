"""Classifier with a fake Anthropic API (httpx2 mock transport, since SDK 1.x uses httpx2)."""

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import anthropic
import httpx2
import pytest

from gateway.classifier import PROMPT_VERSION, Categories, ClassificationError, Classifier
from gateway.pricing import PriceTable
from gateway.settings import PROJECT_ROOT

CATEGORIES = Categories.load(PROJECT_ROOT / "config" / "categories.yaml")
PRICES = PriceTable.load(PROJECT_ROOT / "config" / "prices.yaml")


def api_message(text: str, stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_c",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 1_000, "output_tokens": 40},
    }


def classifier_with(
    respond: Callable[[dict[str, Any]], dict[str, Any]], sent: list[dict[str, Any]]
) -> Classifier:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        sent.append(body)
        return httpx2.Response(200, json=respond(body))

    client = anthropic.AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return Classifier(client, CATEGORIES, PRICES, model="claude-haiku-5-5", effort="low")


def test_categories_config() -> None:
    assert CATEGORIES.names == ["coding", "writing", "analysis", "question_answering", "other"]
    assert CATEGORIES.version == 3
    assert "most of the session's work" in CATEGORIES.tie_breaker


async def test_classify_sends_schema_and_returns_category() -> None:
    sent: list[dict[str, Any]] = []
    out = json.dumps({"category": "coding", "reason": "Adds an endpoint and tests"})
    classifier = classifier_with(lambda _: api_message(out), sent)

    result = await classifier.classify("User: Add a /health endpoint")

    assert (result.category, result.reason) == ("coding", "Adds an endpoint and tests")
    assert (result.model, result.prompt_version, result.effort) == (
        "claude-haiku-5-5",
        PROMPT_VERSION,
        "low",
    )
    # Haiku 5.5 low tier: 1000 * $0.10 + 40 * $0.50 = 120 per MTok
    assert result.cost_usd == Decimal("0.00012")

    [body] = sent
    assert body["model"] == "claude-haiku-5-5"
    assert "temperature" not in body  # rejected by current models
    assert body["output_config"]["effort"] == "low"
    schema = body["output_config"]["format"]["schema"]
    assert schema["properties"]["category"]["enum"] == CATEGORIES.names
    assert schema["additionalProperties"] is False
    assert "<session>\nUser: Add a /health endpoint\n</session>" in body["messages"][0]["content"]
    for name in CATEGORIES.names:
        assert f"- {name}:" in body["system"]


async def test_empty_digest_is_other_without_an_api_call() -> None:
    sent: list[dict[str, Any]] = []
    classifier = classifier_with(lambda _: api_message("{}"), sent)
    result = await classifier.classify("   ")
    assert (result.category, result.note, result.cost_usd) == ("other", "empty digest", 0)
    assert sent == []


@pytest.mark.parametrize(
    ("text", "stop_reason", "error"),
    [
        ('{"category": "poetry", "reason": "x"}', "end_turn", "unknown category"),
        ("not json", "end_turn", "not JSON"),
        ("", "refusal", "refusal"),
        ('{"category": "cod', "max_tokens", "max_tokens"),
    ],
)
async def test_unusable_outputs_raise(text: str, stop_reason: str, error: str) -> None:
    classifier = classifier_with(lambda _: api_message(text, stop_reason), [])
    with pytest.raises(ClassificationError, match=error):
        await classifier.classify("User: hello")
