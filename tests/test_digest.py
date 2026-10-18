from gateway.digest import MAX_DIGEST_CHARS, build_digest


def turn(*messages: tuple[str, object]) -> dict[str, object]:
    return {
        "system": "You are Claude Code. " * 500,
        "messages": [{"role": r, "content": c} for r, c in messages],
    }


def test_history_resent_on_every_call_appears_once() -> None:
    first = turn(("user", "Add a /health endpoint"))
    second = turn(
        ("user", "Add a /health endpoint"),
        ("assistant", "Done, added it to main.py."),
        ("user", "Now add a test for it"),
    )
    digest = build_digest([first, second])
    assert digest.splitlines() == [
        "User: Add a /health endpoint",
        "Assistant: Done, added it to main.py.",
        "User: Now add a test for it",
    ]


def test_drops_system_prompt_tool_results_and_reminders_but_names_tools() -> None:
    body = turn(
        (
            "user",
            [
                {"type": "text", "text": "<system-reminder>env info</system-reminder>Fix the bug"},
            ],
        ),
        (
            "assistant",
            [
                {"type": "thinking", "thinking": "secret reasoning"},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest"}},
            ],
        ),
        (
            "user",
            [{"type": "tool_result", "tool_use_id": "t1", "content": "1 failed, 2 passed"}],
        ),
    )
    digest = build_digest([body])
    assert digest == "User: Fix the bug\n[tools used: Bash]"
    assert "You are Claude Code" not in digest
    assert "secret reasoning" not in digest and "1 failed" not in digest


def test_includes_final_reply_from_response() -> None:
    body = turn(("user", "What is a vector clock?"))
    response = {"content": [{"type": "text", "text": "A vector clock tracks causality."}]}
    assert build_digest([body], [response]).splitlines()[-1] == (
        "Assistant: A vector clock tracks causality."
    )


def test_long_messages_are_clipped() -> None:
    digest = build_digest([turn(("user", "x" * 5_000))])
    assert len(digest) < 900 and digest.endswith("…")


def test_long_sessions_keep_start_and_end() -> None:
    messages = [("user", f"message {i} " + "y" * 600) for i in range(40)]
    digest = build_digest([turn(*messages)])
    assert len(digest) <= MAX_DIGEST_CHARS
    assert digest.startswith("User: message 0 ")
    assert "messages omitted" in digest
    assert digest.splitlines()[-1].startswith("User: message 39 ")


def test_empty_or_odd_input_gives_empty_digest() -> None:
    assert build_digest([]) == ""
    assert build_digest([{"messages": "not a list"}, {}]) == ""
