"""Cases for the handoff eval: a session plus quiz questions with known answers.

To keep the eval from being circular, the ground truth comes before any summary:

1. **Fact sheet.** A realistic coding scenario as structured facts: goal, decisions with
   reasons (one of them reversed later in the session), failed attempts, constraints, key
   files, current state, open tasks, next step.
2. **Transcript.** A long coding-agent session that contains those facts, scattered
   (some only in tool output, one buried mid-session), stored as Messages API request and
   response bodies so it goes through the gateway's own transcript code.
3. **Questions.** 10-12 questions built from the fact sheet, each with its answer and a
   verbatim quote from the session as evidence. A question whose evidence isn't in the
   rendered transcript (what every briefing is built from) is dropped: everything asked
   must be answerable from the session.

    evals/data/handoff_cases.jsonl        synthetic cases (committed)
    evals/data/real/handoff_cases.jsonl   cases from real sessions (gitignored)
"""

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from evals.dataset import DATA_DIR, read_jsonl
from gateway.transcript import build_transcript

SYNTHETIC_CASES_PATH = DATA_DIR / "handoff_cases.jsonl"
REAL_CASES_PATH = DATA_DIR / "real" / "handoff_cases.jsonl"
TRANSCRIPT_MAX_TOKENS = 150_000  # same budget the gateway uses for handoffs

SECTIONS = [
    "goal",
    "decision",
    "reversed_decision",
    "failed_attempt",
    "constraint",
    "key_file",
    "current_state",
    "open_task",
    "next_step",
]


@dataclass
class Question:
    id: str
    section: str
    question: str
    answer: str
    evidence: str  # verbatim quote from the session supporting the answer


@dataclass
class HandoffCase:
    id: str
    source: str  # "synthetic" | "real"
    request: dict[str, Any]  # the session's main request (all messages but the last)
    response: dict[str, Any] | None  # the last assistant message
    questions: list[Question]
    facts: dict[str, Any] | None = None  # synthetic only
    generator: dict[str, Any] = field(default_factory=dict)

    def transcript(self) -> str:
        return build_transcript(self.request, self.response, max_tokens=TRANSCRIPT_MAX_TOKENS).text

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "HandoffCase":
        questions = [Question(**q) for q in data.pop("questions")]
        return cls(questions=questions, **data)


def load_cases() -> list[HandoffCase]:
    return [
        HandoffCase.from_json(r)
        for r in read_jsonl(SYNTHETIC_CASES_PATH) + read_jsonl(REAL_CASES_PATH)
    ]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def evidence_found(evidence: str, transcript: str) -> bool:
    """Whether the quote appears in the transcript (ignoring whitespace and case)."""
    quote = _normalize(evidence)
    return len(quote) >= 8 and quote in _normalize(transcript)


def turns_to_messages(turns: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """A generated transcript (user/assistant turns, assistant tool calls with results) as
    the request a client would have sent last, plus the final response.

    Each assistant tool call becomes a tool_use block, and its result a tool_result block
    in the next user message, as the Messages API requires.
    """
    messages: list[dict[str, Any]] = []
    for n, turn in enumerate(turns):
        if turn["role"] == "user":
            messages.append({"role": "user", "content": turn["text"]})
            continue
        content: list[dict[str, Any]] = []
        if turn.get("text"):
            content.append({"type": "text", "text": turn["text"]})
        results: list[dict[str, Any]] = []
        for i, call in enumerate(turn.get("tool_calls") or []):
            tool_id = f"toolu_{n:03d}_{i}"
            try:
                tool_input = json.loads(call.get("input_json") or "{}")
            except ValueError:
                tool_input = {"raw": call.get("input_json")}
            content.append(
                {"type": "tool_use", "id": tool_id, "name": call["name"], "input": tool_input}
            )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": call.get("result", ""),
                    "is_error": bool(call.get("is_error")),
                }
            )
        messages.append({"role": "assistant", "content": content})
        if results:
            messages.append({"role": "user", "content": results})

    # The session ends on an assistant reply: that's the response; the rest is the request.
    if messages and messages[-1]["role"] == "assistant":
        final = messages.pop()
        response = {"type": "message", "role": "assistant", "content": final["content"]}
    else:
        response = {"type": "message", "role": "assistant", "content": []}
    request = {"model": "claude-sonnet-5-5", "messages": messages}
    return request, response
