"""Generate synthetic handoff-eval cases with Claude Sonnet (see handoff_cases.py).

    python -m evals.generate_handoff_cases            # 12 cases
    python -m evals.generate_handoff_cases --count 2  # a quick trial

Three calls per case (fact sheet, transcript, questions). Cases alternate between two
lengths, measured as rendered transcript size (what every briefing is built from):
"medium" (~10K tokens) and "long" (~25-30K tokens), so results can show whether a
handoff's value grows with session length. A transcript shorter than its length's
minimum is regenerated. Refuses to overwrite existing cases unless --force is given.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import anthropic

from evals.dataset import read_jsonl, write_jsonl
from evals.handoff_cases import (
    SECTIONS,
    SYNTHETIC_CASES_PATH,
    HandoffCase,
    Question,
    evidence_found,
    turns_to_messages,
)
from gateway.pricing import PriceTable, Usage
from gateway.settings import get_settings
from gateway.transcript import build_transcript

GENERATOR_MODEL = "claude-sonnet-5-5"
MIN_QUESTIONS = 8

# Length targets. Sizes are rendered-transcript characters (about 4 per token); tool
# results longer than the renderer's clip (2,000 characters) still count only up to it.
LENGTHS: dict[str, dict[str, Any]] = {
    "medium": {"min_chars": 25_000, "segments": 2},  # was 30_000; one theme kept landing at 26-28K
    "long": {"min_chars": 85_000, "segments": 6},
}

# What each segment of a session must contain. A single call reliably writes only
# ~20-30K characters, so long sessions are written in parts that continue each other;
# assigning facts here also controls where they land (the reversal comes late, one
# constraint appears only mid-session).
SEGMENT_BRIEFS = {
    2: [
        "Opening: the goal, the first constraint, the original (later reversed) decision, the "
        "first decision with its reason, exploring the key files, the first failed attempt.",
        "Continuation: the other constraints (one mentioned only once, in passing), the "
        "remaining decisions with reasons, the reversal of the original decision with why, "
        "the second failed attempt, progress to the current state; end unfinished, with the "
        "open tasks remaining and the next step implied, no recap.",
    ],
    6: [
        "Opening: the goal, the first constraint, the original (later reversed) decision, "
        "exploring the key files.",
        "Exploration: reading more of the codebase, an unrelated question the user asks, a "
        "dead end that leads nowhere; no new facts beyond what the opening established.",
        "Early work: the first decision with its reason, the first failed attempt shown "
        "through real tool output and the discussion after it.",
        "A side task the user asks for along the way; one constraint mentioned only once, "
        "in passing; the second decision with its reason.",
        "The reversal of the original decision with the reason, the second failed attempt, "
        "the remaining decisions with reasons.",
        "Late: progress to the current state; open tasks come up; end on an assistant reply "
        "with work unfinished and the next step implied, no recap.",
    ],
}

# One theme per case, so the scenarios differ in language, stack, and kind of work.
THEMES = [
    "adding rate limiting to a Django REST API",
    "fixing a flaky React form-validation test suite",
    "writing a Rust CLI that parses and validates log files",
    "migrating an Airflow data pipeline to a new schema",
    "setting up Terraform for an AWS Lambda + API Gateway service",
    "adding retries and timeouts to a Go gRPC client",
    "speeding up a slow PostgreSQL report query",
    "adding OAuth login to a Node.js Express app",
    "refactoring a Python ML training script into a package",
    "debugging a memory leak in a long-running Python worker",
    "building a GitHub Actions CI pipeline for a monorepo",
    "adding offline caching to a TypeScript mobile web app",
]

FACTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": {"type": "string"},
        "goal": {"type": "string"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"decision": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["decision", "reason"],
                "additionalProperties": False,
            },
        },
        "reversed_decision": {
            "type": "object",
            "properties": {
                "original": {"type": "string"},
                "replacement": {"type": "string"},
                "why_reversed": {"type": "string"},
            },
            "required": ["original", "replacement", "why_reversed"],
            "additionalProperties": False,
        },
        "failed_attempts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"attempt": {"type": "string"}, "why_it_failed": {"type": "string"}},
                "required": ["attempt", "why_it_failed"],
                "additionalProperties": False,
            },
        },
        "constraints": {"type": "array", "items": {"type": "string"}},
        "key_files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "role": {"type": "string"}},
                "required": ["path", "role"],
                "additionalProperties": False,
            },
        },
        "current_state": {"type": "string"},
        "open_tasks": {"type": "array", "items": {"type": "string"}},
        "next_step": {"type": "string"},
    },
    "required": [
        "project", "goal", "decisions", "reversed_decision", "failed_attempts",
        "constraints", "key_files", "current_state", "open_tasks", "next_step",
    ],
    "additionalProperties": False,
}  # fmt: skip

TRANSCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "turns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "enum": ["user", "assistant"]},
                    "text": {"type": "string"},
                    "tool_calls": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "input_json": {"type": "string"},
                                "result": {"type": "string"},
                                "is_error": {"type": "boolean"},
                            },
                            "required": ["name", "input_json", "result", "is_error"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["role", "text", "tool_calls"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["turns"],
    "additionalProperties": False,
}

QUESTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "enum": SECTIONS},
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["section", "question", "answer", "evidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


async def structured(
    client: anthropic.AsyncAnthropic, system: str, prompt: str, schema: dict[str, Any]
) -> tuple[dict[str, Any], Usage]:
    async with client.messages.stream(
        model=GENERATOR_MODEL,
        max_tokens=100_000,  # long transcripts are ~40K output tokens; streaming avoids timeouts
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},  # type: ignore[typeddict-item]
    ) as stream:
        message = await stream.get_final_message()
    if message.stop_reason != "end_turn":
        raise RuntimeError(f"generation stopped with {message.stop_reason}")
    text = next(b.text for b in message.content if b.type == "text")
    return json.loads(text), Usage.from_api(message.usage.model_dump())


async def make_case(
    client: anthropic.AsyncAnthropic, theme: str, index: int, length: str, prices: PriceTable
) -> tuple[HandoffCase | None, Decimal, str]:
    cost = Decimal(0)

    facts, usage = await structured(
        client,
        "You design realistic software engineering scenarios for testing how well a summary "
        "preserves a work session's context.",
        f"Design the ground truth for a coding-agent session about: {theme}.\n"
        "Make every fact specific (exact file paths, function names, numbers, error messages, "
        "commands). Give 3-4 decisions, each with a concrete reason. The reversed decision is "
        "one the user and assistant first agree on and later replace for a stated reason. Give "
        "2 failed attempts with the exact reason each failed. Give 2-3 constraints, 4-6 key "
        "files, a precise current state (what works, what is half-done), 2-3 open tasks, and "
        "the single next step.",
        FACTS_SCHEMA,
    )
    cost += prices.cost(GENERATOR_MODEL, usage).cost_usd or 0

    turns: list[dict[str, Any]] = []
    briefs = SEGMENT_BRIEFS[LENGTHS[length]["segments"]]
    for n, brief in enumerate(briefs, start=1):
        so_far = ""
        if turns:
            request, response = turns_to_messages(
                turns + [{"role": "assistant", "text": "", "tool_calls": []}]
            )
            tail = build_transcript(request, None, max_tokens=150_000).text[-6_000:]
            so_far = f"The session so far ends like this:\n<so_far>\n{tail}\n</so_far>\n\n"
        segment, usage = await structured(
            client,
            "You write realistic transcripts of a developer working with an AI coding agent "
            "that uses tools (Read, Edit, Write, Bash, Grep). Tool results look like real "
            "output: file contents, full test runs, stack traces.",
            f"Facts of the whole session:\n{json.dumps(facts, indent=2)}\n\n{so_far}"
            f"Write part {n} of {len(briefs)} of the session"
            f"{', continuing exactly where it left off' if turns else ''}. This part covers: "
            f"{brief}\n\nRequirements: 8 to 10 user turns and 15 to 20 tool calls in this "
            "part; tool results are long and realistic (30-60 lines of code, full test "
            "output); facts appear in passing, never in a recap; every turn has text; "
            "input_json is a JSON object as a string. "
            + (
                "Start with a user turn."
                if not turns
                else "Start with a user turn that follows on naturally."
            ),
            TRANSCRIPT_SCHEMA,
        )
        cost += prices.cost(GENERATOR_MODEL, usage).cost_usd or 0
        turns += segment["turns"]
    transcript = {"turns": turns}
    turns = transcript["turns"]
    if not turns or turns[0]["role"] != "user":
        return None, cost, "transcript does not start with a user turn"
    request, response = turns_to_messages(turns)
    case = HandoffCase(
        id=f"hc-syn-{index:02d}", source="synthetic", request=request, response=response,
        questions=[], facts=facts,
    )  # fmt: skip
    rendered = case.transcript()
    if len(rendered) < LENGTHS[length]["min_chars"]:
        return None, cost, f"transcript too short for {length}: {len(rendered):,} chars"

    questions, usage = await structured(
        client,
        "You write quiz questions that test whether someone continuing a work session knows "
        "what they need to know.",
        f"Facts of the session:\n{json.dumps(facts, indent=2)}\n\n"
        f"The session transcript:\n<transcript>\n{rendered}\n</transcript>\n\n"
        "Write 10 to 12 questions that someone taking over this work must be able to answer. "
        "Cover every section: goal, decisions (ask why, not just what), the reversed decision "
        "(ask what the approach is now and why it changed), failed attempts (ask why they "
        "failed), constraints, key files, current state, open tasks, next step. Each answer "
        "is short and specific. For evidence, copy an exact passage of 8 to 30 words from the "
        "transcript above that supports the answer; copy it character for character.",
        QUESTIONS_SCHEMA,
    )
    cost += prices.cost(GENERATOR_MODEL, usage).cost_usd or 0
    kept = [q for q in questions["questions"] if evidence_found(q["evidence"], rendered)]
    case.questions = [Question(id=f"q{i + 1}", **q) for i, q in enumerate(kept)]
    dropped = len(questions["questions"]) - len(kept)
    if len(case.questions) < MIN_QUESTIONS:
        return None, cost, f"only {len(case.questions)} questions with verified evidence"
    case.generator = {
        "model": GENERATOR_MODEL,
        "theme": theme,
        "length": length,
        "transcript_chars": len(rendered),
        "questions_dropped_for_evidence": dropped,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    return (
        case,
        cost,
        (
            f"{length}: {len(rendered):,} chars, {len(turns)} turns, "
            f"{len(case.questions)} questions ({dropped} dropped)"
        ),
    )


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.generate_handoff_cases")
    parser.add_argument("--count", type=int, default=len(THEMES))
    parser.add_argument("--force", action="store_true", help="replace all existing cases")
    parser.add_argument(
        "--resume", action="store_true", help="keep existing cases and generate only missing ones"
    )
    args = parser.parse_args(argv)
    if SYNTHETIC_CASES_PATH.exists() and not (args.force or args.resume):
        print(f"{SYNTHETIC_CASES_PATH} exists; use --resume or --force.", file=sys.stderr)
        return 1
    existing = (
        [HandoffCase.from_json(r) for r in read_jsonl(SYNTHETIC_CASES_PATH)] if args.resume else []
    )
    done = {c.id for c in existing}

    settings = get_settings()
    prices = PriceTable.load(settings.price_table_path)
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
    gate = asyncio.Semaphore(4)

    async def one(i: int, theme: str) -> tuple[HandoffCase | None, Decimal, str]:
        length = "medium" if i % 2 else "long"  # alternate, so both lengths span the themes
        spent = Decimal(0)
        async with gate:
            for attempt in range(2):  # one retry for a case that fails validation
                try:
                    case, cost, note = await make_case(client, theme, i, length, prices)
                except (anthropic.APIError, RuntimeError, ValueError) as exc:
                    # One failed call shouldn't lose the cases that succeeded. (Its spend
                    # before the failure isn't counted here.)
                    case, cost, note = (
                        None,
                        Decimal(0),
                        f"failed: {type(exc).__name__}: {exc}"[:300],
                    )
                spent += cost
                print(f"  case {i:02d} ({theme}): {note}", file=sys.stderr)
                if case is not None or attempt == 1:
                    return case, spent, note
            raise AssertionError("unreachable")

    results = await asyncio.gather(
        *(
            one(i, theme)
            for i, theme in enumerate(THEMES[: args.count], start=1)
            if f"hc-syn-{i:02d}" not in done
        )
    )
    cases = sorted(existing + [c for c, _, _ in results if c is not None], key=lambda c: c.id)
    total = sum((cost for _, cost, _ in results), Decimal(0))
    if not cases:
        print("No case succeeded; the existing cases file was left unchanged.", file=sys.stderr)
        return 1
    write_jsonl(SYNTHETIC_CASES_PATH, [c.to_json() for c in cases])
    print(
        f"Wrote {len(cases)} cases, {sum(len(c.questions) for c in cases)} questions to "
        f"{SYNTHETIC_CASES_PATH} (generation cost ${total:.4f})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
