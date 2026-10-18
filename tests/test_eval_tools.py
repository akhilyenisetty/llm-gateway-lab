"""The eval's own code: metrics math, dataset handling, and transcript conversion."""

import pytest

from evals import metrics
from evals.dataset import EvalSession, split
from evals.generate_synthetic import is_valid, requests_for


def test_accuracy_and_confusion() -> None:
    gold = ["coding", "coding", "writing", "analysis"]
    pred = ["coding", "writing", "writing", "coding"]
    assert metrics.accuracy(gold, pred) == 0.5
    assert metrics.confusion(gold, pred) == {
        "coding": {"coding": 1, "writing": 1},
        "writing": {"writing": 1},
        "analysis": {"coding": 1},
    }


def test_per_class_marks_undefined_values_as_none() -> None:
    gold = ["coding", "coding", "writing"]
    pred = ["coding", "writing", "writing"]
    out = metrics.per_class(gold, pred, ["coding", "writing", "other"])
    assert out["coding"] == {"precision": 1.0, "recall": 0.5, "support": 2}
    assert out["writing"] == {"precision": 0.5, "recall": 1.0, "support": 1}
    assert out["other"] == {"precision": None, "recall": None, "support": 0}


def test_consistency_counts_items_all_runs_agree_on() -> None:
    runs = [["a", "b", "c"], ["a", "b", "x"], ["a", "y", "c"]]
    assert metrics.consistency(runs) == pytest.approx(1 / 3)


def test_split_is_deterministic_and_roughly_balanced() -> None:
    ids = [f"s{i}" for i in range(400)]
    assert [split(i) for i in ids] == [split(i) for i in ids]
    dev = sum(split(i) == "dev" for i in ids)
    assert 160 < dev < 240


def test_transcript_becomes_growing_requests_with_tool_calls() -> None:
    turns = [
        {"role": "user", "text": "My test fails", "tools_used": []},
        {"role": "assistant", "text": "Let me look.", "tools_used": ["Bash", "Read"]},
        {"role": "user", "text": "Thanks, now fix it", "tools_used": []},
    ]
    requests = requests_for(turns)
    assert [len(r["messages"]) for r in requests] == [1, 3]
    assistant = requests[1]["messages"][1]["content"]
    assert [b["type"] for b in assistant] == ["text", "tool_use", "tool_use"]

    session = EvalSession(id="x", source="synthetic", requests=requests)
    assert session.digest.splitlines() == [
        "User: My test fails",
        "Assistant: Let me look.",
        "User: Thanks, now fix it",
        "[tools used: Bash, Read]",
    ]


async def test_eval_runner_end_to_end_with_fake_classifier(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    import dataclasses
    import json as _json

    from evals import dataset, run_classifier_eval
    from evals.dataset import write_jsonl
    from tests.test_classifier import api_message, classifier_with

    sessions = [
        EvalSession(
            id=f"syn-coding-0{i}",
            source="synthetic",
            requests=[{"messages": [{"role": "user", "content": f"fix bug {i}"}]}],
            generator={"asked_for": "coding"},
        ).to_json()
        for i in range(3)
    ]
    email = [{"messages": [{"role": "user", "content": "draft an email"}]}]
    sessions.append(EvalSession(id="real-x-1", source="real", requests=email).to_json())
    write_jsonl(tmp_path / "syn.jsonl", sessions)
    v = fake_version = 7
    write_jsonl(tmp_path / "labels.jsonl", [
        {"id": "syn-coding-00", "label": "coding", "categories_version": v},
        {"id": "syn-coding-01", "label": "coding", "categories_version": v},
        {"id": "syn-coding-02", "label": "coding", "categories_version": v},
        {"id": "real-x-1", "label": "writing", "categories_version": v},
        # Made under other definitions: must be ignored.
        {"id": "syn-coding-00", "label": "writing", "categories_version": v - 1},
    ])  # fmt: skip
    monkeypatch.setattr(dataset, "SYNTHETIC_PATH", tmp_path / "syn.jsonl")
    monkeypatch.setattr(dataset, "REAL_PATH", tmp_path / "missing.jsonl")
    monkeypatch.setattr(dataset, "LABELS_PATH", tmp_path / "labels.jsonl")
    monkeypatch.setattr(run_classifier_eval, "RESULTS_DIR", tmp_path / "results")

    # The fake says "coding" for everything: 3 of 4 right.
    out = _json.dumps({"category": "coding", "reason": "r"})
    fake = classifier_with(lambda _: api_message(out), [])
    fake.categories = dataclasses.replace(fake.categories, version=fake_version)
    monkeypatch.setattr(run_classifier_eval, "make_classifier", lambda *a: fake)

    assert await run_classifier_eval.main(["--runs", "2"]) == 0
    [report_path] = (tmp_path / "results").glob("*.json")
    report = _json.loads(report_path.read_text())
    assert report["overall"]["accuracy_mean"] == 0.75
    assert report["overall"]["consistency"] == 1.0
    assert report["slices"]["real"]["accuracy_mean"] == 0.0
    assert report["slices"]["synthetic"]["accuracy_mean"] == 1.0
    assert report["config"]["prompt_version"] == "v1" and report["config"]["runs"] == 2
    assert report["mistakes"] == [
        {"id": "real-x-1", "label": "writing", "predicted": ["coding", "coding"]}
    ]
    md = next((tmp_path / "results").glob("*.md")).read_text()
    assert "| all | 4 | 75.0% |" in md and "| **writing** | 2 |" in md


def test_generated_sessions_are_validated() -> None:
    good = {"scenario": "asks about TCP", "turns": [{"role": "user", "text": "what is TCP"}]}
    assert is_valid(good)
    assert not is_valid({"scenario": "placeholder", "turns": []})
    assert not is_valid({"scenario": "x", "turns": [{"role": "assistant", "text": "hi"}]})
    assert not is_valid({"scenario": "x", "turns": [{"role": "user", "text": "  "}]})
    placeholder_turn = {"scenario": "x", "turns": [{"role": "user", "text": "Placeholder text"}]}
    assert not is_valid(placeholder_turn)


def test_labels_are_filtered_by_categories_version(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    from evals import dataset
    from evals.dataset import load_labels, write_jsonl

    write_jsonl(tmp_path / "labels.jsonl", [
        {"id": "a", "label": "coding"},  # no version field: made under v1
        {"id": "a", "label": "analysis", "categories_version": 2},
        {"id": "b", "label": "writing", "categories_version": 2},
        {"id": "b", "label": "other", "categories_version": 2},  # later line wins
    ])  # fmt: skip
    monkeypatch.setattr(dataset, "LABELS_PATH", tmp_path / "labels.jsonl")
    assert load_labels(1) == {"a": "coding"}
    assert load_labels(2) == {"a": "analysis", "b": "other"}
