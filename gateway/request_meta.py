"""Request metadata: which session, category, and app a call belongs to.

Clients tag calls with optional headers:
    X-Session-Id   groups calls into a session
    X-Category     a client-chosen category tag
    X-App          which application sent the call
    X-Session-Budget-Usd  spend budget for this session (see budgets.py)

Session ID, in order of preference:
1. X-Session-Id, set by the client.
2. x-claude-code-session-id, which Claude Code sends on every request of a session
   (main turns, subagents, titles, compaction). Claude Code also sends its own
   `x-app: cli`; that built-in value is recorded as "claude-code" (a custom X-App set
   via ANTHROPIC_CUSTOM_HEADERS is kept as is).
3. Derived, as below.

Session ID derivation when neither header is present:
The Messages API is stateless, so every turn of a conversation resends the whole
history, and the system prompt plus the first user message stay the same for the
whole conversation. Hashing (gateway key, app, system prompt, first message) gives a
stable ID per conversation without storing any content. Known limitation: two
conversations that start with identical system prompt and first message (from the
same key and app) are grouped into one session; send X-Session-Id to avoid that.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from starlette.datastructures import Headers

from gateway.budgets import parse_budget

MAX_SESSION_ID_LEN = 200
MAX_TAG_LEN = 100
CLAUDE_CODE_BUILTIN_APP = "cli"


@dataclass(frozen=True)
class RequestMeta:
    session_id: str
    session_id_source: str  # "header" | "claude-code" | "derived"
    category: str | None
    app: str | None
    budget_usd: Decimal | None = None
    # Claude Code's labels for the request (see models.RequestLog.request_class).
    request_class: str | None = None
    agent_id: str | None = None


def _clean(value: str | None, max_len: int) -> str | None:
    value = (value or "").strip()
    return value[:max_len] or None


def derive_session_id(api_key_id: uuid.UUID, app: str | None, body: dict[str, Any]) -> str:
    messages = body.get("messages")
    first_message = messages[0] if isinstance(messages, list) and messages else None
    fingerprint = {
        "key": str(api_key_id),
        "app": app,
        "system": body.get("system"),
        "first_message": first_message,
    }
    # Canonical JSON so key order or whitespace differences don't change the hash.
    canonical = json.dumps(fingerprint, sort_keys=True, separators=(",", ":"), default=str)
    return "derived-" + hashlib.sha256(canonical.encode()).hexdigest()[:24]


def read_meta(headers: Headers, api_key_id: uuid.UUID, body: dict[str, Any]) -> RequestMeta:
    app = _clean(headers.get("x-app"), MAX_TAG_LEN)
    category = _clean(headers.get("x-category"), MAX_TAG_LEN)
    budget = parse_budget(headers.get("x-session-budget-usd"))
    hints = {
        "request_class": _clean(headers.get("x-claude-code-request-class"), 20),
        "agent_id": _clean(headers.get("x-claude-code-agent-id"), 100),
    }
    if session_id := _clean(headers.get("x-session-id"), MAX_SESSION_ID_LEN):
        return RequestMeta(session_id, "header", category, app, budget, **hints)
    if session_id := _clean(headers.get("x-claude-code-session-id"), MAX_SESSION_ID_LEN):
        if app in (None, CLAUDE_CODE_BUILTIN_APP):
            app = "claude-code"
        return RequestMeta(session_id, "claude-code", category, app, budget, **hints)
    session_id = derive_session_id(api_key_id, app, body)
    return RequestMeta(session_id, "derived", category, app, budget, **hints)
