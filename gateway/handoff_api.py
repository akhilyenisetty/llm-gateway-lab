"""Shared pieces of the handoff endpoints (admin and client): response models, the call
into handoff_service with its errors mapped to HTTP, and Markdown downloads."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from gateway.handoff_service import CooldownError, NoContentError, create_handoff
from gateway.models import Handoff


class HandoffSummary(BaseModel):
    id: uuid.UUID
    created_at: datetime
    session_id: str
    requested_by: str
    status: str
    error: str | None
    session_calls: int
    transcript_messages: int
    omitted_blocks: int
    model: str
    effort: str
    prompt_version: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None

    model_config = {"from_attributes": True}


class HandoffOut(HandoffSummary):
    content: dict[str, Any] | None
    markdown: str | None


async def generate_or_http_error(
    request: Request, key_id: uuid.UUID, session_id: str, requested_by: str
) -> HandoffOut:
    state = request.app.state
    if state.anthropic is None:
        raise HTTPException(503, "handoffs need ANTHROPIC_API_KEY on the gateway")
    try:
        handoff = await create_handoff(
            state.session_factory,
            state.anthropic,
            state.prices,
            state.settings,
            key_id=key_id,
            session_id=session_id,
            requested_by=requested_by,
        )
    except NoContentError as exc:
        raise HTTPException(409, str(exc)) from exc
    except CooldownError as exc:
        raise HTTPException(429, str(exc), headers={"retry-after": str(exc.retry_after_s)}) from exc
    if handoff.status != "ready":
        raise HTTPException(502, f"handoff generation failed: {handoff.error}")
    return HandoffOut.model_validate(handoff)


def markdown_download(handoff: Handoff) -> PlainTextResponse:
    if handoff.status != "ready" or handoff.markdown is None:
        raise HTTPException(404, "this handoff has no document (generation failed)")
    stamp = handoff.created_at.strftime("%Y%m%d-%H%M")
    safe_session = "".join(c if c.isalnum() or c in "-_" else "_" for c in handoff.session_id)
    return PlainTextResponse(
        handoff.markdown,
        media_type="text/markdown; charset=utf-8",
        headers={
            "content-disposition": f'attachment; filename="handoff-{safe_session[:40]}-{stamp}.md"'
        },
    )
