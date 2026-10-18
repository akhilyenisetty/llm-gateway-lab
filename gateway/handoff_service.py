"""Generating and storing a session's handoff (shared by the admin and client APIs).

Read (cooldown, the session's stored calls, the transcript), generate (no database
connection held during the model call), write (the handoff, or the failure).
"""

import uuid
from datetime import timedelta

import anthropic
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from gateway.handoff import PROMPT_VERSION, HandoffError, generate, to_markdown
from gateway.models import Handoff, RequestContent, RequestLog, utcnow
from gateway.pricing import PriceTable
from gateway.settings import Settings
from gateway.transcript import build_transcript, pick_main


class NoContentError(Exception):
    """The session has no stored content (STORE_CONTENT was off), so there's nothing to
    write a handoff from."""


class CooldownError(Exception):
    def __init__(self, retry_after_s: int) -> None:
        super().__init__(f"a handoff was generated recently; retry in {retry_after_s}s")
        self.retry_after_s = retry_after_s


async def create_handoff(
    session_factory: async_sessionmaker,
    client: anthropic.AsyncAnthropic,
    prices: PriceTable,
    settings: Settings,
    *,
    key_id: uuid.UUID,
    session_id: str,
    requested_by: str,
) -> Handoff:
    """Generate a handoff for one session and store it. Raises NoContentError or
    CooldownError before spending anything; a generation failure is stored and returned
    with status "error"."""
    async with session_factory() as session:
        last = (
            await session.execute(
                select(func.max(Handoff.created_at)).where(
                    Handoff.api_key_id == key_id, Handoff.session_id == session_id
                )
            )
        ).scalar_one()
        if last is not None:
            cooldown = timedelta(seconds=settings.handoff_cooldown_s)
            last = last if last.tzinfo else last.replace(tzinfo=utcnow().tzinfo)
            if utcnow() - last < cooldown:
                raise CooldownError(int((cooldown - (utcnow() - last)).total_seconds()) + 1)

        calls = (
            await session.execute(
                select(func.count()).where(
                    RequestLog.api_key_id == key_id, RequestLog.session_id == session_id
                )
            )
        ).scalar_one()
        rows = (
            await session.execute(
                select(RequestLog.id, RequestContent.request_json, RequestContent.response_json)
                .join(RequestContent, RequestContent.request_id == RequestLog.id)
                .where(RequestLog.api_key_id == key_id, RequestLog.session_id == session_id)
                .order_by(RequestLog.created_at)
            )
        ).all()
    if not rows:
        raise NoContentError("no stored content for this session (STORE_CONTENT was off)")

    main = pick_main([r.request_json for r in rows])
    assert main is not None  # rows is not empty
    source_id, request_body, response_body = rows[main]
    transcript = build_transcript(
        request_body, response_body, max_tokens=settings.handoff_max_input_tokens
    )

    handoff = Handoff(
        id=uuid.uuid4(),
        created_at=utcnow(),
        api_key_id=key_id,
        session_id=session_id,
        requested_by=requested_by,
        source_request_id=source_id,
        session_calls=calls,
        transcript_messages=transcript.messages,
        omitted_blocks=transcript.omitted_blocks,
        model=settings.handoff_model,
        effort=settings.handoff_effort,
        prompt_version=PROMPT_VERSION,
    )
    try:
        result = await generate(
            client,
            prices,
            transcript.text,
            model=settings.handoff_model,
            effort=settings.handoff_effort,
        )
    except (HandoffError, anthropic.APIError) as exc:
        handoff.status, handoff.error = "error", f"{type(exc).__name__}: {exc}"[:500]
    else:
        handoff.status = "ready"
        handoff.model, handoff.prompt_version = result.model, result.prompt_version
        handoff.input_tokens, handoff.output_tokens = result.input_tokens, result.output_tokens
        handoff.cost_usd = result.cost_usd
        handoff.content = result.content
        handoff.markdown = to_markdown(
            result.content,
            {
                "session_id": session_id,
                "generated_at": handoff.created_at,
                "model": result.model,
                "prompt_version": result.prompt_version,
                "calls": calls,
                "omitted_blocks": transcript.omitted_blocks,
            },
        )

    async with session_factory() as session:
        session.add(handoff)
        await session.commit()
    return handoff
