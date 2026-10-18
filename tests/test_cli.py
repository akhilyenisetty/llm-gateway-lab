"""The `gw` command's logic (not the processes it starts)."""

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from gateway import cli


def test_plan_mode_uses_your_login_and_identifies_you_by_header() -> None:
    base = {
        "PATH": "/bin",
        "ANTHROPIC_API_KEY": "sk-should-be-removed",
        "ANTHROPIC_AUTH_TOKEN": "x",
    }
    env = cli.claude_env(base, "gw_mine", api=False, budget=None)
    assert env["ANTHROPIC_BASE_URL"] == cli.GATEWAY_URL
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Gateway-Key: gw_mine"
    # An API key in the environment would silently replace the claude.ai login.
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["CLAUDE_CODE_GATEWAY_HINT_HEADERS"] == "1"
    assert env["PATH"] == "/bin" and base["ANTHROPIC_API_KEY"]  # the caller's dict is untouched


def test_api_mode_uses_the_gateway_key_as_api_key() -> None:
    env = cli.claude_env({"ANTHROPIC_CUSTOM_HEADERS": "Old: 1"}, "gw_mine", api=True, budget=None)
    assert env["ANTHROPIC_API_KEY"] == "gw_mine"
    assert "ANTHROPIC_CUSTOM_HEADERS" not in env  # no stale headers left behind


def test_budget_header_is_added_in_either_mode() -> None:
    plan = cli.claude_env({}, "gw_mine", api=False, budget="2")
    assert plan["ANTHROPIC_CUSTOM_HEADERS"] == "X-Gateway-Key: gw_mine\nX-Session-Budget-Usd: 2"
    api = cli.claude_env({}, "gw_mine", api=True, budget="0.5")
    assert api["ANTHROPIC_CUSTOM_HEADERS"] == "X-Session-Budget-Usd: 0.5"


def test_env_file_values_are_read_replaced_and_appended(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("STORE_CONTENT=true\nADMIN_API_KEY=\n")
    assert cli.read_env_value("STORE_CONTENT", env) == "true"
    assert cli.read_env_value("ADMIN_API_KEY", env) == ""
    cli.write_env_value("ADMIN_API_KEY", "abc", env)
    cli.write_env_value("NEW_SETTING", "1", env)
    assert env.read_text() == "STORE_CONTENT=true\nADMIN_API_KEY=abc\nNEW_SETTING=1\n"
    assert cli.read_env_value("MISSING", tmp_path / "nope") == ""


def test_saved_key_is_private(tmp_path: Path) -> None:
    path = tmp_path / "llm-gateway" / "client.json"
    cli.save_client_config({"key": "gw_secret"}, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert cli.load_client_config(path) == {"key": "gw_secret"}
    assert cli.load_client_config(tmp_path / "missing.json") == {}


def test_admin_key_is_generated_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / ".env"
    env.write_text("STORE_CONTENT=false\n")
    monkeypatch.setattr(cli, "ENV_PATH", env)
    args = type("Args", (), {"copy": False})()
    assert cli.cmd_admin_key(args) == 0
    first = cli.read_env_value("ADMIN_API_KEY", env)
    assert len(first) >= 40 and first in capsys.readouterr().out
    assert cli.cmd_admin_key(args) == 0
    assert cli.read_env_value("ADMIN_API_KEY", env) == first  # not regenerated


def test_extra_arguments_go_to_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_claude(args: Any, extra: list[str]) -> int:
        seen.update(api=args.api, budget=args.budget, extra=extra)
        return 0

    monkeypatch.setattr(cli, "cmd_claude", fake_claude)
    assert cli.main(["claude", "--budget", "2", "--model", "haiku", "-p", "hi"]) == 0
    assert seen == {"api": False, "budget": "2", "extra": ["--model", "haiku", "-p", "hi"]}


def test_unknown_arguments_are_rejected_for_other_commands() -> None:
    with pytest.raises(SystemExit):
        cli.main(["status", "--nope"])


# --- user commands (gateway calls stubbed) ----------------------------------------------

REMOTE = "https://gateway.example.com"
SESSIONS = [
    {"session_id": "cc-new-1234", "app": "claude-code", "calls": 4, "spend_usd": "0.02",
     "last_call_at": "2026-10-18T10:00:00Z", "has_handoff": False},
    {"session_id": "cc-old-9999", "app": "claude-code", "calls": 9, "spend_usd": "0.10",
     "last_call_at": "2026-10-17T10:00:00Z", "has_handoff": True},
]  # fmt: skip


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Logged in to a remote gateway; records (method, path) of every gateway call."""
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "client.json")
    cli.save_client_config({"gateway_url": REMOTE, "key": "gw_remote", "name": "alice"})
    calls: list[tuple[str, str]] = []

    def fake_api(url: str, key: str, method: str, path: str, **_: Any) -> tuple[int, bytes]:
        assert (url, key) == (REMOTE, "gw_remote")
        calls.append((method, path))
        if path.startswith("/gateway/sessions?"):
            return 200, json.dumps(SESSIONS).encode()
        if method == "POST":
            return 201, b"{}"
        if path.endswith("latest.md"):
            return 200, b"# Handoff: test\n"
        if path == "/gateway/me":
            return 200, json.dumps({"name": "alice", "key_prefix": "gw_remote"}).encode()
        return 404, b'{"detail": "nope"}'

    monkeypatch.setattr(cli, "api_call", fake_api)
    return calls


def test_remote_gateway_without_a_key_says_to_log_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "client.json")
    cli.save_client_config({"gateway_url": REMOTE})
    with pytest.raises(SystemExit):
        cli.connection()


def test_login_saves_only_a_working_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "client.json")
    answers = {"gw_good": (200, json.dumps({"name": "alice", "key_prefix": "gw_good"}).encode())}
    monkeypatch.setattr(
        cli, "api_call", lambda url, key, *a, **k: answers.get(key, (401, b'{"detail": "invalid"}'))
    )
    assert cli.main(["login", REMOTE + "/", "--key", "gw_bad"]) == 1
    assert cli.load_client_config() == {}
    assert cli.main(["login", REMOTE, "--key", "gw_good"]) == 0
    assert cli.load_client_config() == {"gateway_url": REMOTE, "key": "gw_good", "name": "alice"}


def test_handoff_defaults_to_the_latest_session(
    remote: list[tuple[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["handoff"]) == 0
    assert ("POST", "/gateway/sessions/cc-new-1234/handoffs") in remote
    assert (tmp_path / "handoff-cc-new-1234.md").read_text() == "# Handoff: test\n"


def test_handoff_by_prefix_and_existing_only(
    remote: list[tuple[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["handoff", "cc-old", "--existing", "-o", "h.md"]) == 0
    assert not any(method == "POST" for method, _ in remote)  # --existing: no generation
    assert (tmp_path / "h.md").exists()
    assert cli.main(["handoff", "cc-"]) == 1  # ambiguous prefix


def test_handoff_falls_back_to_existing_during_cooldown(
    remote: list[tuple[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    real = cli.api_call

    def cooling_down(url: str, key: str, method: str, path: str, **k: Any) -> tuple[int, bytes]:
        if method == "POST":
            return 429, b'{"detail": "retry in 40s"}'
        return real(url, key, method, path)

    monkeypatch.setattr(cli, "api_call", cooling_down)
    assert cli.main(["handoff"]) == 0
    assert (tmp_path / "handoff-cc-new-1234.md").exists()


def test_env_prints_exports_for_sdk_apps(
    remote: list[tuple[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["env"]) == 0
    assert capsys.readouterr().out == (
        f"export ANTHROPIC_BASE_URL={REMOTE}\nexport ANTHROPIC_API_KEY=gw_remote\n"
    )


def test_claude_uses_the_logged_in_gateway(
    remote: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: dict[str, Any] = {}
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/local/bin/claude")
    monkeypatch.setattr(
        cli.os, "execvpe", lambda prog, argv, env: launched.update(argv=argv, env=env)
    )
    cli.main(["claude", "--model", "haiku"])
    assert launched["argv"] == ["claude", "--model", "haiku"]
    assert launched["env"]["ANTHROPIC_BASE_URL"] == REMOTE
    assert launched["env"]["ANTHROPIC_CUSTOM_HEADERS"] == "X-Gateway-Key: gw_remote"
