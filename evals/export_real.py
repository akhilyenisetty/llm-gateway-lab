"""Export real sessions (with stored content) from the gateway database for labeling.

    python -m evals.export_real          # writes evals/data/real/sessions.jsonl

Only sessions with stored content can be exported, so STORE_CONTENT=true must have been
on while they ran. The output holds real conversations and is gitignored.
"""

import asyncio

from sqlalchemy import select

from evals.dataset import REAL_PATH, EvalSession, write_jsonl
from gateway.db import make_engine, make_session_factory
from gateway.models import ApiKey, RequestContent, RequestLog
from gateway.settings import get_settings


async def main() -> int:
    engine = make_engine(get_settings().database_url)
    try:
        async with make_session_factory(engine)() as session:
            rows = (
                await session.execute(
                    select(
                        ApiKey.key_prefix,
                        RequestLog.session_id,
                        RequestContent.request_json,
                        RequestContent.response_json,
                    )
                    .join(RequestLog, RequestLog.id == RequestContent.request_id)
                    .join(ApiKey, ApiKey.id == RequestLog.api_key_id)
                    .order_by(RequestLog.created_at)
                )
            ).all()
    finally:
        await engine.dispose()

    grouped: dict[str, EvalSession] = {}
    for prefix, session_id, request, response in rows:
        sid = f"real-{prefix}-{session_id}"
        grouped.setdefault(sid, EvalSession(id=sid, source="real", requests=[], responses=[]))
        grouped[sid].requests.append(request)
        grouped[sid].responses.append(response)
    write_jsonl(REAL_PATH, [s.to_json() for s in grouped.values()])
    print(f"Exported {len(grouped)} sessions ({len(rows)} calls) to {REAL_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
