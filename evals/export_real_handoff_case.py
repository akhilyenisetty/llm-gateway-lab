"""Turn a real gateway session (with stored content) into a handoff-eval case.

    python -m evals.export_real_handoff_case <session-id-prefix>

A real session has no fact sheet, so questions are written from the transcript itself
(with the same evidence check as synthetic cases) and printed for a person to review:
for a real session, the person who did the work is the ground truth. Delete or fix any
question in evals/data/real/handoff_cases.jsonl before running the eval. The file holds
the real conversation, so it is gitignored.
"""

import argparse
import asyncio
import sys

import anthropic
from sqlalchemy import select

from evals.dataset import write_jsonl
from evals.generate_handoff_cases import QUESTIONS_SCHEMA, structured
from evals.handoff_cases import REAL_CASES_PATH, HandoffCase, Question, evidence_found, load_cases
from gateway.db import make_engine, make_session_factory
from gateway.models import RequestContent, RequestLog
from gateway.pricing import PriceTable
from gateway.settings import get_settings
from gateway.transcript import pick_main


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.export_real_handoff_case")
    parser.add_argument("session_prefix")
    args = parser.parse_args(argv)
    settings = get_settings()

    engine = make_engine(settings.database_url)
    try:
        async with make_session_factory(engine)() as session:
            rows = (
                await session.execute(
                    select(
                        RequestLog.session_id,
                        RequestContent.request_json,
                        RequestContent.response_json,
                        RequestLog.request_class,
                    )
                    .join(RequestContent, RequestContent.request_id == RequestLog.id)
                    .where(RequestLog.session_id.startswith(args.session_prefix))
                    .order_by(RequestLog.created_at)
                )
            ).all()
    finally:
        await engine.dispose()
    sessions = {r.session_id for r in rows}
    if len(sessions) != 1:
        print(f"Expected one session with stored content, found {len(sessions)}.", file=sys.stderr)
        return 1
    session_id = sessions.pop()
    main_index = pick_main([r.request_json for r in rows], [r.request_class for r in rows])
    assert main_index is not None
    request, response = rows[main_index].request_json, rows[main_index].response_json

    case = HandoffCase(
        id=f"hc-real-{session_id[:8]}", source="real", request=request,
        response=response, questions=[], generator={"session_id": session_id},
    )  # fmt: skip
    rendered = case.transcript()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
    generated, usage = await structured(
        client,
        "You write quiz questions that test whether someone continuing a work session knows "
        "what they need to know.",
        f"The session transcript:\n<transcript>\n{rendered}\n</transcript>\n\n"
        "Write 10 to 12 questions that someone taking over this work must be able to answer, "
        "covering as many of these as the session supports: goal, decisions (ask why), "
        "failed attempts (ask why they failed), constraints, key files, current state, open "
        "tasks, next step. Skip a section the session has nothing about; do not invent. Each "
        "answer is short and specific. For evidence, copy an exact passage of 8 to 30 words "
        "from the transcript that supports the answer, character for character.",
        QUESTIONS_SCHEMA,
    )
    kept = [q for q in generated["questions"] if evidence_found(q["evidence"], rendered)]
    case.questions = [Question(id=f"q{i + 1}", **q) for i, q in enumerate(kept)]
    case.generator["transcript_chars"] = len(rendered)

    others = [c for c in load_cases() if c.source == "real" and c.id != case.id]
    write_jsonl(REAL_CASES_PATH, [c.to_json() for c in [*others, case]])
    cost = PriceTable.load(settings.price_table_path).cost("claude-sonnet-5-5", usage).cost_usd
    print(f"Saved {case.id}: {len(case.questions)} questions "
          f"({len(generated['questions']) - len(kept)} dropped for evidence), "
          f"transcript {len(rendered):,} chars, cost ${cost:.4f}\n")  # fmt: skip
    for q in case.questions:
        print(f"{q.id} [{q.section}] {q.question}\n   answer: {q.answer}\n")
    print(f"Review these; edit or delete any in {REAL_CASES_PATH} before running the eval.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
