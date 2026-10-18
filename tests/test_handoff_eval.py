"""The handoff eval's own code: case building, evidence checks, scoring, and a full run
against a fake Anthropic API (so a bug can't waste a paid run)."""

import json
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from evals import handoff_cases, run_handoff_eval
from evals.dataset import write_jsonl
from evals.handoff_cases import HandoffCase, Question, evidence_found, turns_to_messages
from tests.test_handoff import CONTENT

# The real client class, kept before any test replaces anthropic.AsyncAnthropic.
REAL_ASYNC_ANTHROPIC = anthropic.AsyncAnthropic

TURNS = [
    {"role": "user", "text": "The nightly job is slow, look at report.sql", "tool_calls": []},
    {
        "role": "assistant",
        "text": "Reading it.",
        "tool_calls": [
            {"name": "Read", "input_json": '{"path": "report.sql"}',
             "result": "SELECT * FROM orders -- full scan on 40M rows", "is_error": False},
            {"name": "Bash", "input_json": '{"cmd": "psql -f report.sql"}',
             "result": "ERROR: canceling statement due to statement timeout", "is_error": True},
        ],
    },
    {"role": "user", "text": "Add an index on orders.created_at then", "tool_calls": []},
    {"role": "assistant", "text": "Index added; the query now takes 2.1 s.", "tool_calls": []},
]  # fmt: skip


def test_turns_become_valid_messages_with_tool_results() -> None:
    request, response = turns_to_messages(TURNS)
    roles = [m["role"] for m in request["messages"]]
    assert roles == ["user", "assistant", "user", "user"]  # tool results, then the next prompt
    tool_uses = [b for b in request["messages"][1]["content"] if b["type"] == "tool_use"]
    results = request["messages"][2]["content"]
    assert [b["tool_use_id"] for b in results] == [b["id"] for b in tool_uses]
    assert results[1]["is_error"] is True
    assert tool_uses[0]["input"] == {"path": "report.sql"}
    assert response["content"] == [
        {"type": "text", "text": "Index added; the query now takes 2.1 s."}
    ]


def test_evidence_must_appear_in_the_rendered_transcript() -> None:
    request, response = turns_to_messages(TURNS)
    case = HandoffCase(id="c", source="synthetic", request=request, response=response, questions=[])
    rendered = case.transcript()
    assert evidence_found(
        "canceling statement due to   statement TIMEOUT", rendered
    )  # whitespace/case
    assert evidence_found("the query now takes 2.1 s", rendered)  # in the final response
    assert not evidence_found("the query now takes 0.5 s", rendered)
    assert not evidence_found("index", rendered)  # too short to count as a quote


def test_score_rows_aggregates_per_run_and_type() -> None:
    def row(run: int, section: str, score: float, tokens: int) -> dict[str, Any]:
        return {"run": run, "config": "handoff", "case": "a", "source": "synthetic",
                "section": section, "score": score, "reader_tokens": tokens}  # fmt: skip

    rows = [
        row(1, "decision", 1.0, 1000),
        row(1, "goal", 0.5, 1000),
        row(2, "decision", 0.0, 3000),
        row(2, "goal", 1.0, 3000),
    ]
    r = run_handoff_eval.score_rows(rows)["handoff"]
    assert r["accuracy_per_run"] == [0.75, 0.5]
    assert (r["accuracy_mean"], r["accuracy_min"], r["accuracy_max"]) == (0.625, 0.5, 0.75)
    assert r["by_section"] == {"decision": 0.5, "goal": 0.75}
    assert r["reader_tokens_mean"] == 2000  # per briefing, not per question
    assert r["questions"] == 2


def fake_anthropic(calls: list[dict[str, Any]]) -> anthropic.AsyncAnthropic:
    """Answers like each role would: the summary writer writes prose, the handoff writer
    returns the structured handoff, the reader answers correctly only when its briefing
    contains the fact, and the judge grades by comparing to the reference."""

    def respond(body: dict[str, Any]) -> dict[str, Any]:
        system, prompt = body["system"], body["messages"][0]["content"]
        calls.append({"model": body["model"], "system": system[:40]})
        if system.startswith("Write a summary"):
            text = "Summary: made the report faster with an index on orders.created_at."
        elif system.startswith("You write handoff"):
            text = json.dumps({**CONTENT, "current_state": "Index on orders.created_at added."})
        elif system.startswith("You are taking over"):
            knows = "orders.created_at" in prompt
            answer = "an index on orders.created_at" if knows else "not in the briefing"
            text = json.dumps({"answers": [{"id": "q1", "answer": answer}]})
        else:  # judge
            right = "Answer to grade: an index" in prompt
            text = json.dumps({"verdicts": [
                {"id": "q1", "verdict": "correct" if right else "wrong", "reason": "r"},
            ]})  # fmt: skip
        return {
            "id": "m", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": len(prompt) // 4, "output_tokens": 50},
        }  # fmt: skip

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=respond(json.loads(request.content)))

    return REAL_ASYNC_ANTHROPIC(
        api_key="test",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


async def test_full_run_scores_each_briefing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, response = turns_to_messages(TURNS)
    case = HandoffCase(
        id="hc-syn-01", source="synthetic", request=request, response=response,
        questions=[Question("q1", "decision", "What fixed the slow query?",
                            "An index on orders.created_at", "Add an index on orders.created_at")],
    )  # fmt: skip
    write_jsonl(tmp_path / "cases.jsonl", [case.to_json()])
    monkeypatch.setattr(handoff_cases, "SYNTHETIC_CASES_PATH", tmp_path / "cases.jsonl")
    monkeypatch.setattr(handoff_cases, "REAL_CASES_PATH", tmp_path / "none.jsonl")
    monkeypatch.setattr(run_handoff_eval, "RESULTS_DIR", tmp_path / "results")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        run_handoff_eval.anthropic, "AsyncAnthropic", lambda **_: fake_anthropic(calls)
    )
    monkeypatch.setattr(run_handoff_eval, "get_settings", lambda: _settings())

    assert await run_handoff_eval.main(["--runs", "2"]) == 0

    report = json.loads(next((tmp_path / "results").glob("handoff_eval_*Z.json")).read_text())
    res = report["results"]
    assert {c: res[c]["accuracy_mean"] for c in res} == {
        "raw": 1.0,
        "summary": 1.0,
        "handoff": 1.0,
        "none": 0.0,
    }
    # Every briefing costs reader tokens beyond the bare questions. (With this toy
    # transcript the handoff is longer than the raw text; real sessions are the reverse.)
    tokens = {c: res[c]["reader_tokens_mean"] for c in res}
    assert min(tokens["raw"], tokens["summary"], tokens["handoff"]) > tokens["none"]
    cfg = report["config"]
    assert (cfg["reader_model"], cfg["judge_model"], cfg["runs"], cfg["questions"]) == (
        "claude-haiku-5-5", "claude-opus-5-5", 2, 1,
    )  # fmt: skip
    # The judge never sees which briefing produced the answer it grades.
    judge_calls = [c for c in calls if c["model"] == "claude-opus-5-5"]
    assert len(judge_calls) == 2 * 4
    details = (tmp_path / "results").glob("*_details.jsonl")
    assert len(next(details).read_text().splitlines()) == 2 * 4  # runs x briefings
    md = next((tmp_path / "results").glob("handoff_eval_*Z.md")).read_text()
    assert "| none | 0.0% |" in md and "| handoff | 100.0% |" in md


def _settings():  # type: ignore[no-untyped-def]
    from gateway.settings import Settings

    return Settings(
        anthropic_api_key="test", handoff_model="claude-sonnet-5-5", handoff_effort="high"
    )


async def test_credit_failure_keeps_finished_work_and_resume_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a mid-run credit failure once lost a whole finished run."""
    request, response = turns_to_messages(TURNS)
    case = HandoffCase(
        id="hc-syn-01", source="synthetic", request=request, response=response,
        questions=[Question("q1", "decision", "What fixed the slow query?",
                            "An index on orders.created_at", "Add an index on orders.created_at")],
    )  # fmt: skip
    write_jsonl(tmp_path / "cases.jsonl", [case.to_json()])
    monkeypatch.setattr(handoff_cases, "SYNTHETIC_CASES_PATH", tmp_path / "cases.jsonl")
    monkeypatch.setattr(handoff_cases, "REAL_CASES_PATH", tmp_path / "none.jsonl")
    monkeypatch.setattr(run_handoff_eval, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(run_handoff_eval, "get_settings", lambda: _settings())

    calls: list[dict[str, Any]] = []
    healthy = fake_anthropic(calls)
    budget = {"left": 8}  # the account runs dry after 8 model calls

    def handler(request: httpx2.Request) -> httpx2.Response:
        if budget["left"] <= 0:
            message = "Your credit balance is too low to access the Anthropic API."
            error = {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": message},
            }
            return httpx2.Response(400, json=error)
        budget["left"] -= 1
        body = json.loads(request.content)
        return httpx2.Response(200, json=_respond_like(healthy, calls, body))

    broke = REAL_ASYNC_ANTHROPIC(
        api_key="test", max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )  # fmt: skip
    monkeypatch.setattr(run_handoff_eval.anthropic, "AsyncAnthropic", lambda **_: broke)
    assert await run_handoff_eval.main(["--runs", "2", "--concurrency", "1"]) == 2
    [checkpoint] = (tmp_path / "results").glob("handoff_eval_checkpoint_*.jsonl")
    done_before = len(checkpoint.read_text().splitlines())
    assert 0 < done_before < 2 * 4  # some units finished and were saved, not all
    assert not list((tmp_path / "results").glob("handoff_eval_*Z.json"))  # no partial report

    budget["left"] = 10_000  # credits added
    calls.clear()
    assert await run_handoff_eval.main(["--runs", "2", "--resume"]) == 0
    assert len(checkpoint.read_text().splitlines()) == 2 * 4
    report = json.loads(next((tmp_path / "results").glob("handoff_eval_*Z.json")).read_text())
    assert report["results"]["none"]["accuracy_per_run"] == [0.0, 0.0]
    # Finished units weren't redone: no more judge calls than the missing units need.
    assert len([c for c in calls if c["model"] == "claude-opus-5-5"]) == 2 * 4 - done_before


def _respond_like(
    _client: Any, calls: list[dict[str, Any]], body: dict[str, Any]
) -> dict[str, Any]:
    """The fake API's answer for one request body (shared with fake_anthropic)."""
    system, prompt = body["system"], body["messages"][0]["content"]
    calls.append({"model": body["model"], "system": system[:40]})
    if system.startswith("Write a summary"):
        text = "Summary: made the report faster with an index on orders.created_at."
    elif system.startswith("You write handoff"):
        text = json.dumps({**CONTENT, "current_state": "Index on orders.created_at added."})
    elif system.startswith("You are taking over"):
        answer = (
            "an index on orders.created_at"
            if "orders.created_at" in prompt
            else "not in the briefing"
        )
        text = json.dumps({"answers": [{"id": "q1", "answer": answer}]})
    else:
        right = "Answer to grade: an index" in prompt
        text = json.dumps(
            {"verdicts": [{"id": "q1", "verdict": "correct" if right else "wrong", "reason": "r"}]}
        )
    return {
        "id": "m", "type": "message", "role": "assistant", "model": body["model"],
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
        "stop_sequence": None, "usage": {"input_tokens": len(prompt) // 4, "output_tokens": 50},
    }  # fmt: skip
