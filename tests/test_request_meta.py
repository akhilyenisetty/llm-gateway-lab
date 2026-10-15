import uuid

from starlette.datastructures import Headers

from gateway.request_meta import derive_session_id, read_meta

KEY_A = uuid.UUID(int=1)
KEY_B = uuid.UUID(int=2)
FIRST = {"role": "user", "content": "Plan my week"}
BODY = {"system": "You are helpful.", "messages": [FIRST]}


def test_header_session_id_wins() -> None:
    meta = read_meta(Headers({"x-session-id": " abc ", "x-category": "docs"}), KEY_A, BODY)
    assert (meta.session_id, meta.session_id_source, meta.category) == ("abc", "header", "docs")


def test_blank_headers_are_ignored() -> None:
    meta = read_meta(Headers({"x-session-id": "  ", "x-category": "", "x-app": " "}), KEY_A, BODY)
    assert meta.session_id_source == "derived"
    assert meta.category is None
    assert meta.app is None


def test_long_header_values_are_truncated() -> None:
    meta = read_meta(Headers({"x-session-id": "s" * 500, "x-app": "a" * 500}), KEY_A, BODY)
    assert len(meta.session_id) == 200
    assert len(meta.app or "") == 100


def test_later_turns_keep_the_same_derived_session() -> None:
    turn_3 = {
        **BODY,
        "messages": [
            FIRST,
            {"role": "assistant", "content": "Sure"},
            {"role": "user", "content": "Add gym on Tuesday"},
        ],
    }
    assert derive_session_id(KEY_A, None, BODY) == derive_session_id(KEY_A, None, turn_3)


def test_derived_session_ignores_key_order() -> None:
    reordered = {
        "messages": [{"content": "Plan my week", "role": "user"}],
        "system": "You are helpful.",
    }
    assert derive_session_id(KEY_A, None, BODY) == derive_session_id(KEY_A, None, reordered)


def test_derived_session_differs_by_key_app_system_and_first_message() -> None:
    base = derive_session_id(KEY_A, "app1", BODY)
    assert derive_session_id(KEY_B, "app1", BODY) != base
    assert derive_session_id(KEY_A, "app2", BODY) != base
    assert derive_session_id(KEY_A, "app1", {**BODY, "system": "Other"}) != base
    other_first = {**BODY, "messages": [{"role": "user", "content": "Different"}]}
    assert derive_session_id(KEY_A, "app1", other_first) != base


def test_derived_session_handles_missing_messages() -> None:
    assert derive_session_id(KEY_A, None, {}).startswith("derived-")
