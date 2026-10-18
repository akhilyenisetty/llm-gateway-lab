"""A short, readable digest of a session, which is what the classifier reads.

Raw requests are a poor input: every call resends the whole history (so turn 1 appears in
every request), and Claude Code requests are mostly system prompt and tool definitions
(~60K tokens). The digest keeps what says what the session is about:

- user text, in order, each distinct message once
- the start of the assistant's text replies (context for terse user messages)
- the names of the tools that were used (Edit/Bash strongly suggest coding)

and drops system prompts, tool results, thinking, and injected <system-reminder> blocks.
It is capped in size, keeping the beginning (the intent) and the end (where it went).
"""

import re
from collections.abc import Iterable
from typing import Any

MAX_MESSAGE_CHARS = 800
MAX_ASSISTANT_CHARS = 300
MAX_DIGEST_CHARS = 8_000

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_WHITESPACE = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WHITESPACE.sub(" ", _REMINDER.sub(" ", text)).strip()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _lines_from_messages(messages: Any, tools: set[str]) -> list[str]:
    lines = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        for block in _blocks(message.get("content")):
            kind = block.get("type")
            if kind == "tool_use" and block.get("name"):
                tools.add(str(block["name"]))
            if kind != "text":
                continue  # tool results, images, thinking, documents
            text = _clean(str(block.get("text", "")))
            if not text:
                continue
            if role == "user":
                lines.append("User: " + _clip(text, MAX_MESSAGE_CHARS))
            elif role == "assistant":
                lines.append("Assistant: " + _clip(text, MAX_ASSISTANT_CHARS))
    return lines


def build_digest(
    requests: Iterable[dict[str, Any]], responses: Iterable[dict[str, Any] | None] = ()
) -> str:
    """Digest of a session from its request bodies (oldest first) and, optionally, the
    matching response bodies (whose assistant text isn't in any request yet)."""
    seen: set[str] = set()
    lines: list[str] = []
    tools: set[str] = set()
    responses = list(responses)
    for i, body in enumerate(requests):
        candidates = _lines_from_messages(body.get("messages"), tools)
        response = responses[i] if i < len(responses) else None
        if response:
            candidates += _lines_from_messages(
                [{"role": "assistant", "content": response.get("content")}], tools
            )
        for line in candidates:
            if line not in seen:
                seen.add(line)
                lines.append(line)

    if tools:
        lines.append("[tools used: " + ", ".join(sorted(tools)) + "]")
    return _fit(lines)


def _fit(lines: list[str]) -> str:
    """Join lines within MAX_DIGEST_CHARS, keeping the start and the end if too long."""
    text = "\n".join(lines)
    if len(text) <= MAX_DIGEST_CHARS:
        return text
    head: list[str] = []
    tail: list[str] = []
    budget = MAX_DIGEST_CHARS - 40  # room for the omission marker
    i, j = 0, len(lines) - 1
    while i <= j:
        take_head = len(head) <= len(tail)
        line = lines[i] if take_head else lines[j]
        if len(line) + 1 > budget:
            break
        budget -= len(line) + 1
        if take_head:
            head.append(line)
            i += 1
        else:
            tail.append(line)
            j -= 1
    omitted = j - i + 1
    return "\n".join([*head, f"[… {omitted} messages omitted …]", *reversed(tail)])
