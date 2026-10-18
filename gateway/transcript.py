"""A session's main conversation, rendered as a transcript for the handoff writer.

Every Messages API call resends the conversation so far, so the request with the most
messages already contains the session's main conversation in full (in Claude Code, the
main thread; title, summary, and subagent calls are shorter). That request plus its
response is the whole story; no stitching across requests is needed.

Unlike the classifier's digest, a handoff needs details, so the transcript keeps tool calls
(name and input) and tool results, which carry file paths, commands, and exact errors.
It drops thinking and injected <system-reminder> blocks, clips long blocks, and fits a
character budget by keeping the start (the goal) and, preferentially, the end (the
current state).
"""

import json
import re
from dataclasses import dataclass
from typing import Any

MAX_SYSTEM_CHARS = 3_000
MAX_TEXT_CHARS = 6_000
MAX_TOOL_INPUT_CHARS = 1_500
MAX_TOOL_RESULT_CHARS = 2_000
# Characters per token used to turn the token budget into a character budget. Code and
# JSON tokenize denser than prose, so this errs on the side of a smaller transcript.
CHARS_PER_TOKEN = 3.5

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


@dataclass(frozen=True)
class Transcript:
    text: str
    messages: int  # messages in the conversation used
    omitted_blocks: int  # blocks dropped to fit the budget


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 40].rstrip() + f"\n[… {len(text) - limit + 40} characters clipped]"


def _text_of(content: Any) -> str:
    """Plain text of a tool result's content (a string or a list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, dict):
                parts.append(f"[{block.get('type', 'block')}]")
        return "\n".join(parts)
    return ""


def _render_block(role: str, block: dict[str, Any]) -> str | None:
    kind = block.get("type")
    if kind == "text":
        text = _REMINDER.sub("", str(block.get("text", ""))).strip()
        return f"**{role}:** {_clip(text, MAX_TEXT_CHARS)}" if text else None
    if kind == "tool_use":
        tool_input = json.dumps(block.get("input", {}), ensure_ascii=False)
        return f"→ tool call `{block.get('name')}`: {_clip(tool_input, MAX_TOOL_INPUT_CHARS)}"
    if kind == "tool_result":
        status = " (error)" if block.get("is_error") else ""
        result = _REMINDER.sub("", _text_of(block.get("content"))).strip()
        return f"← tool result{status}: {_clip(result, MAX_TOOL_RESULT_CHARS)}"
    if kind in ("image", "document"):
        return f"[{role} attached a {kind}]"
    return None  # thinking, redacted thinking, and unknown block types


def _blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def pick_main(requests: list[dict[str, Any]]) -> int | None:
    """Index of the request with the most messages (the main conversation); the latest
    one on ties, since it has the newest response."""
    best: int | None = None
    for i, request in enumerate(requests):
        count = len(request.get("messages") or [])
        if best is None or count >= len(requests[best].get("messages") or []):
            best = i
    return best


def build_transcript(
    request: dict[str, Any], response: dict[str, Any] | None, *, max_tokens: int
) -> Transcript:
    messages = list(request.get("messages") or [])
    if response and response.get("content"):
        messages.append({"role": "assistant", "content": response["content"]})

    rendered: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = "User" if message.get("role") == "user" else "Assistant"
        for block in _blocks(message.get("content")):
            line = _render_block(role, block)
            if line:
                rendered.append(line)

    system = request.get("system")
    system_text = system if isinstance(system, str) else _text_of(system)
    header = (
        f"[System prompt of the app, clipped]\n{_clip(system_text.strip(), MAX_SYSTEM_CHARS)}"
        if system_text.strip()
        else ""
    )
    text, omitted = _fit(rendered, int(max_tokens * CHARS_PER_TOKEN) - len(header))
    return Transcript(
        text=f"{header}\n\n{text}".strip(), messages=len(messages), omitted_blocks=omitted
    )


def _fit(blocks: list[str], budget: int) -> tuple[str, int]:
    """Join blocks within `budget` characters. If they don't fit, keep the first quarter
    of the budget from the start and the rest from the end, marking the gap."""
    total = sum(len(b) + 2 for b in blocks)
    if total <= budget:
        return "\n\n".join(blocks), 0
    head_budget = budget // 4
    head: list[str] = []
    used = 0
    for block in blocks:
        if used + len(block) + 2 > head_budget:
            break
        head.append(block)
        used += len(block) + 2
    tail: list[str] = []
    used = 0
    tail_budget = budget - sum(len(b) + 2 for b in head) - 80
    for block in reversed(blocks[len(head) :]):
        if used + len(block) + 2 > tail_budget:
            break
        tail.append(block)
        used += len(block) + 2
    omitted = len(blocks) - len(head) - len(tail)
    marker = f"[… {omitted} earlier messages and tool calls omitted to fit the budget …]"
    return "\n\n".join([*head, marker, *reversed(tail)]), omitted
