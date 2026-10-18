"""Managing gateway keys: the admin API (for a deployed gateway, whose database is
private), `gw keys --url` on top of it, and the local `python -m gateway.keys`."""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from gateway import cli, keys
from gateway.settings import Settings

ADMIN = {"authorization": "Bearer test-admin-key"}


async def test_created_key_works_and_revoked_key_stops(http: httpx.AsyncClient) -> None:
    resp = await http.post("/admin/keys", headers=ADMIN, json={"name": "  alice  "})
    assert resp.status_code == 201
    created = resp.json()
    assert created["name"] == "alice" and created["key"].startswith(created["key_prefix"])
    user = {"x-api-key": created["key"]}
    assert (await http.get("/gateway/me", headers=user)).json()["name"] == "alice"

    listed = (await http.get("/admin/keys", headers=ADMIN)).json()
    assert [k["name"] for k in listed] == ["alice"] and "key" not in listed[0]  # never again

    revoked = await http.post(f"/admin/keys/{created['id']}/revoke", headers=ADMIN)
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]
    assert (await http.get("/gateway/me", headers=user)).status_code == 401
    again = await http.post(f"/admin/keys/{created['id']}/revoke", headers=ADMIN)
    assert again.status_code == 409


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
async def test_key_names_are_validated(http: httpx.AsyncClient, name: str) -> None:
    resp = await http.post("/admin/keys", headers=ADMIN, json={"name": name})
    assert resp.status_code == 422


async def test_only_the_admin_can_create_keys(http: httpx.AsyncClient) -> None:
    created = (await http.post("/admin/keys", headers=ADMIN, json={"name": "bob"})).json()
    as_user = await http.post(
        "/admin/keys", headers={"x-api-key": created["key"]}, json={"name": "mallory"}
    )
    assert as_user.status_code == 401
    unknown = "/admin/keys/00000000-0000-0000-0000-000000000000/revoke"
    assert (await http.post(unknown, headers=ADMIN)).status_code == 404


# --- gw keys --url (admin API calls stubbed) ---------------------------------------------

ROWS = [
    {"id": "1", "key_prefix": "gw_aaaa", "name": "alice", "created_at": "2026-10-18T00:00:00Z",
     "revoked_at": None},
    {"id": "2", "key_prefix": "gw_bbbb", "name": "bob", "created_at": "2026-10-18T00:00:00Z",
     "revoked_at": "2026-10-18T01:00:00Z"},
]  # fmt: skip


@pytest.fixture
def admin_api(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, Any]]:
    monkeypatch.setenv("GW_ADMIN_KEY", "the-admin-key")
    calls: list[tuple[str, str, Any]] = []

    def fake_api(url: str, key: str, method: str, path: str, **kw: Any) -> tuple[int, bytes]:
        import json

        assert (url, key) == ("https://gw.example.com", "the-admin-key")
        calls.append((method, path, kw.get("body")))
        if (method, path) == ("POST", "/admin/keys"):
            created = {**ROWS[0], "name": kw["body"]["name"], "key": "gw_new"}
            return 201, json.dumps(created).encode()
        if method == "GET":
            return 200, json.dumps(ROWS).encode()
        return 200, b"{}"

    monkeypatch.setattr(cli, "api_call", fake_api)
    return calls


def test_remote_create_prints_the_key_once(
    admin_api: list[tuple[str, str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["keys", "--url", "https://gw.example.com/", "create", "carol"]) == 0
    assert admin_api == [("POST", "/admin/keys", {"name": "carol"})]
    assert "gw_new" in capsys.readouterr().out


def test_remote_revoke_finds_the_active_key_by_prefix(
    admin_api: list[tuple[str, str, Any]],
) -> None:
    assert cli.main(["keys", "--url", "https://gw.example.com", "revoke", "gw_aaaa"]) == 0
    assert admin_api[-1] == ("POST", "/admin/keys/1/revoke", None)
    assert cli.main(["keys", "--url", "https://gw.example.com", "revoke", "gw_bbbb"]) == 1
    assert cli.main(["keys", "--url", "https://gw.example.com", "create"]) == 1  # no name


# --- python -m gateway.keys (local database) ---------------------------------------------


@pytest.mark.parametrize("argv", [["create", "dave"], ["create", "--name", "dave"]])
async def test_local_create_accepts_both_spellings(
    app: FastAPI,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    monkeypatch.setattr(keys, "get_settings", lambda: settings)
    assert await keys.main(argv) == 0
    assert "Created key 'dave'" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        await keys.main(["create"])
