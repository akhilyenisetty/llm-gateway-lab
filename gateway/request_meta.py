"""Request metadata: which session, category, and app a call belongs to.

Clients tag calls with optional headers:
    X-Session-Id   groups calls into a session
    X-Category     a client-chosen category tag
    X-App          which application sent the call

Session ID derivation when X-Session-Id is absent:
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
from typing import Any

from starlette.datastructures import Headers

MAX_SESSION_ID_LEN = 200
MAX_TAG_LEN = 100


@dataclass(frozen=True)
class RequestMeta:
    session_id: str
    session_id_source: str  # "header" | "derived"
    category: str | None
    app: str | None


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
    if session_id := _clean(headers.get("x-session-id"), MAX_SESSION_ID_LEN):
        return RequestMeta(session_id, "header", category, app)
    return RequestMeta(derive_session_id(api_key_id, app, body), "derived", category, app)
