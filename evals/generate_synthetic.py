"""Generate synthetic sessions for the classifier eval with Claude Sonnet.

    python -m evals.generate_synthetic            # writes evals/data/synthetic_sessions.jsonl

One request per category asks for varied, realistic sessions (different domains, lengths,
phrasing, and tool use), plus one request for deliberately mixed or borderline sessions,
which are where classifiers fail. What each request asked for is kept in the session's
`generator` field; it is not a label (sessions are labeled blind by a person).

Generated sessions are validated (at least one user turn, no placeholder content) and a
request that returns fewer valid sessions than asked is retried: structured outputs
guarantee valid JSON, not useful content.

Costs roughly $0.15 with the default model. The script refuses to overwrite an existing
file unless --force is given, since labels refer to these session ids; --only regenerates
one request's sessions and keeps the rest.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any

import anthropic

from evals.dataset import SYNTHETIC_PATH, EvalSession, read_jsonl, write_jsonl
from gateway.classifier import Categories
from gateway.pricing import PriceTable, Usage
from gateway.settings import get_settings

GENERATOR_MODEL = "claude-sonnet-5-5"
PER_CATEGORY = 7
MIXED = 8
MAX_ATTEMPTS = 3

SYSTEM = (
    "You write realistic transcripts of people using AI assistants, for testing a session "
    "classifier. Vary the domain, the person's expertise, message length, tone, and how "
    "clearly they state what they want; include typos and terse follow-ups like real "
    "users. Sessions can come from a chat assistant or from a coding agent that uses tools "
    "(Read, Edit, Bash, Grep, WebSearch). Keep each message under 120 words and each "
    "assistant reply under 60 words."
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sessions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scenario": {"type": "string"},
                    "turns": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string", "enum": ["user", "assistant"]},
                                "text": {"type": "string"},
                                "tools_used": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["role", "text", "tools_used"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["scenario", "turns"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sessions"],
    "additionalProperties": False,
}


def requests_for(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn a transcript into the request bodies a client would have sent: one request per
    user turn, each carrying the history so far (as the Messages API requires)."""
    messages: list[dict[str, Any]] = []
    requests = []
    for n, turn in enumerate(turns):
        if turn["role"] == "assistant":
            content: list[dict[str, Any]] = [{"type": "text", "text": turn["text"]}]
            content += [
                {"type": "tool_use", "id": f"toolu_{n}_{i}", "name": name, "input": {}}
                for i, name in enumerate(turn.get("tools_used") or [])
            ]
            messages.append({"role": "assistant", "content": content})
        else:
            messages.append({"role": "user", "content": turn["text"]})
            requests.append({"model": "claude-sonnet-5-5", "messages": list(messages)})
    return requests


def is_valid(item: dict[str, Any]) -> bool:
    users = [
        t for t in item.get("turns", []) if t.get("role") == "user" and t.get("text", "").strip()
    ]
    text = item.get("scenario", "") + " ".join(t.get("text", "") for t in item.get("turns", []))
    return bool(users) and "placeholder" not in text.lower()


async def generate_valid(
    client: anthropic.AsyncAnthropic, instruction: str, wanted: int
) -> tuple[list[dict[str, Any]], list[anthropic.types.Usage]]:
    """Generate until `wanted` valid sessions arrive in one response, up to MAX_ATTEMPTS."""
    usages = []
    best: list[dict[str, Any]] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        generated, usage = await generate(client, instruction)
        usages.append(usage)
        valid = [g for g in generated if is_valid(g)]
        if len(valid) > len(best):
            best = valid
        if len(valid) >= wanted:
            return valid[:wanted], usages
        print(
            f"  attempt {attempt}: {len(valid)}/{wanted} valid sessions, retrying", file=sys.stderr
        )
    return best, usages


async def generate(
    client: anthropic.AsyncAnthropic, instruction: str
) -> tuple[list[dict[str, Any]], anthropic.types.Usage]:
    response = await client.messages.create(
        model=GENERATOR_MODEL,
        max_tokens=16_000,
        system=SYSTEM,
        messages=[{"role": "user", "content": instruction}],
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": SCHEMA},
        },
    )
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"generation stopped with {response.stop_reason}")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)["sessions"], response.usage


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.generate_synthetic")
    parser.add_argument("--force", action="store_true", help="overwrite the existing file")
    parser.add_argument("--only", help="regenerate one request (a category or 'mixed')")
    args = parser.parse_args(argv)
    if SYNTHETIC_PATH.exists() and not (args.force or args.only):
        print(f"{SYNTHETIC_PATH} exists; labels refer to its ids. Use --force to replace it.")
        return 1

    settings = get_settings()
    categories = Categories.load(settings.categories_path)
    prices = PriceTable.load(settings.price_table_path)
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
    definitions = "\n".join(f"- {k}: {v}" for k, v in categories.definitions.items())

    jobs: dict[str, str] = {
        name: (
            f"Write {PER_CATEGORY} different sessions that clearly belong to this category:\n"
            f"{name}: {categories.definitions[name]}\n"
            "Use 1 to 6 user turns, alternating with assistant replies."
        )
        for name in categories.names
    }
    jobs["mixed"] = (
        f"Write {MIXED} sessions that are hard to classify into these categories:\n"
        f"{definitions}\n"
        "Make some mix two kinds of work (for example, a question that turns into a "
        "coding task, or analysis that ends in a written report), and make some borderline "
        "cases between two categories. Use 2 to 6 user turns."
    )

    wanted = {name: PER_CATEGORY for name in categories.names} | {"mixed": MIXED}
    if args.only:
        if args.only not in jobs:
            print(f"--only must be one of: {', '.join(jobs)}", file=sys.stderr)
            return 1
        jobs = {args.only: jobs[args.only]}

    print(f"Generating with {GENERATOR_MODEL}: {len(jobs)} requests …", file=sys.stderr)
    results = await asyncio.gather(
        *(generate_valid(client, text, wanted[name]) for name, text in jobs.items())
    )

    # With --only, keep every other request's sessions (labels refer to their ids).
    kept = [
        EvalSession(**r)
        for r in read_jsonl(SYNTHETIC_PATH)
        if args.only and r["generator"]["asked_for"] != args.only
    ]
    sessions: list[EvalSession] = []
    total_cost = 0
    created = datetime.now(UTC).isoformat(timespec="seconds")
    for (intent, _), (generated, usages) in zip(jobs.items(), results, strict=True):
        for usage in usages:
            cost = prices.cost(GENERATOR_MODEL, Usage.from_api(usage.model_dump())).cost_usd
            total_cost += cost or 0
        if len(generated) < wanted[intent]:
            print(f"  {intent}: only {len(generated)}/{wanted[intent]} valid", file=sys.stderr)
        for i, item in enumerate(generated, start=1):
            sessions.append(
                EvalSession(
                    id=f"syn-{intent}-{i:02d}",
                    source="synthetic",
                    requests=requests_for(item["turns"]),
                    generator={
                        "asked_for": intent,
                        "scenario": item["scenario"],
                        "model": GENERATOR_MODEL,
                        "categories_version": categories.version,
                        "created_at": created,
                    },
                )
            )
    write_jsonl(SYNTHETIC_PATH, [s.to_json() for s in kept + sessions])
    print(
        f"Wrote {len(sessions)} new sessions ({len(kept)} kept) to {SYNTHETIC_PATH} "
        f"(cost ${total_cost:.4f})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
