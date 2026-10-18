"""`gw`: one short command for running and using the gateway.

    gw up                 start Postgres, apply migrations, start the gateway (background)
    gw down [--db]        stop the gateway and dashboard (--db: Postgres too)
    gw restart            down, then up (e.g. after editing .env)
    gw status             what's running, URLs, keys
    gw logs [dashboard]   follow a log
    gw dashboard          start the dashboard and open it in the browser
    gw claude [...]       Claude Code through the gateway; other arguments go to claude
                            --api        bill the gateway's API key instead of your plan
                            --budget N   session budget in USD
    gw admin-key [--copy] the dashboard sign-in key (generated into .env if missing)
    gw keys list | create NAME | revoke PREFIX

For users of a gateway (any machine; only a URL and a gateway key are needed):

    gw login [URL]        save the gateway and your gw_ key (asked for at a hidden prompt)
    gw whoami | logout    which key and gateway you use / forget them
    gw claude [...]       as above, against the gateway you logged in to
    gw sessions           your recent sessions
    gw handoff [SESSION]  write a handoff for a session (default: latest) to handoff-*.md
    eval "$(gw env)"      point Anthropic SDK apps at the gateway in this shell

`gw claude` creates your own gateway key the first time and keeps it in
~/.config/llm-gateway/client.json (readable only by you), so you never copy keys around.
Runtime files (pids, logs) live in .gateway/ in the project (gitignored).
"""

import argparse
import asyncio
import getpass
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from gateway.settings import PROJECT_ROOT

GATEWAY_PORT = 8000
DASHBOARD_PORT = 5173
GATEWAY_URL = f"http://localhost:{GATEWAY_PORT}"
DASHBOARD_URL = f"http://localhost:{DASHBOARD_PORT}"
STATE_DIR = PROJECT_ROOT / ".gateway"
CONFIG_PATH = Path.home() / ".config" / "llm-gateway" / "client.json"
ENV_PATH = PROJECT_ROOT / ".env"
PYTHON = sys.executable


# ---- small helpers ------------------------------------------------------------------


def say(message: str) -> None:
    print(message, flush=True)


def fail(message: str) -> int:
    print(f"gw: {message}", file=sys.stderr)
    return 1


def healthy(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except OSError:
        return False


def wait_until(check, seconds: float) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.3)
    return False


def pid_alive(name: str) -> int | None:
    """The pid of a process gw started, if it is still running."""
    path = STATE_DIR / f"{name}.pid"
    try:
        pid = int(path.read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def start_background(
    name: str, command: list[str], cwd: Path, env: dict[str, str] | None = None
) -> int:
    STATE_DIR.mkdir(exist_ok=True)
    log = (STATE_DIR / f"{name}.log").open("a")
    log.write(f"\n--- gw: starting {name} at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    log.flush()
    process = subprocess.Popen(
        command, cwd=cwd, stdout=log, stderr=log, env=env, start_new_session=True
    )
    (STATE_DIR / f"{name}.pid").write_text(str(process.pid))
    return process.pid


def stop_background(name: str) -> bool:
    pid = pid_alive(name)
    if pid is None:
        return False
    os.killpg(os.getpgid(pid), signal.SIGTERM)  # the whole group (vite spawns children)
    wait_until(lambda: pid_alive(name) is None, 10)
    (STATE_DIR / f"{name}.pid").unlink(missing_ok=True)
    return True


def read_env_value(name: str, path: Path | None = None) -> str:
    # Defaults are resolved at call time (a `= ENV_PATH` default would be fixed at import).
    path = path or ENV_PATH
    if not path.exists():
        return ""
    for line in path.read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return ""


def write_env_value(name: str, value: str, path: Path | None = None) -> None:
    """Set NAME=value in .env, replacing an existing line or appending one."""
    path = path or ENV_PATH
    lines = path.read_text().splitlines() if path.exists() else []
    for i, line in enumerate(lines):
        if line.startswith(f"{name}="):
            lines[i] = f"{name}={value}"
            break
    else:
        lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n")


# ---- your own gateway key (for gw claude) -------------------------------------------


def load_client_config(path: Path | None = None) -> dict[str, str]:
    path = path or CONFIG_PATH
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_client_config(config: dict[str, str], path: Path | None = None) -> None:
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2))
    path.chmod(0o600)  # the key is a secret: readable only by you


async def _ensure_key(saved: str | None, name: str) -> tuple[str, bool]:
    """A working gateway key: the saved one if it's still active, else a new one."""
    from gateway.auth import create_key, find_active_key
    from gateway.db import make_engine, make_session_factory
    from gateway.settings import get_settings

    engine = make_engine(get_settings().database_url)
    try:
        async with make_session_factory(engine)() as session:
            if saved and await find_active_key(session, saved):
                return saved, False
            _, plaintext = await create_key(session, name)
            return plaintext, True
    finally:
        await engine.dispose()


def own_key() -> str:
    config = load_client_config()
    name = f"cli-{getpass.getuser()}"
    key, created = asyncio.run(_ensure_key(config.get("key"), name))
    if created:
        config.update(key=key, name=name, gateway_url=config.get("gateway_url") or GATEWAY_URL)
        save_client_config(config)
        say(f"gw: created your gateway key '{name}' ({key[:10]}…), saved in {CONFIG_PATH}")
    return key


def is_local(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


def connection(*, start_local: bool = True) -> tuple[str, str]:
    """The gateway URL and gateway key that user commands use: the gateway you logged in
    to (gw login), or else the local gateway with your own key, created on first use."""
    config = load_client_config()
    url = config.get("gateway_url") or GATEWAY_URL
    if not is_local(url):
        if not config.get("key"):
            raise SystemExit(fail(f"no key saved for {url}; run: gw login {url}"))
        return url, config["key"]
    if not healthy(f"{url}/health"):
        if not start_local or url != GATEWAY_URL:
            raise SystemExit(fail(f"no gateway answering at {url} (gw up starts the local one)"))
        say("gateway not running; starting it …")
        if cmd_up(argparse.Namespace()):
            raise SystemExit(1)
    return url, own_key()


def api_call(
    url: str,
    key: str,
    method: str,
    path: str,
    *,
    timeout: float = 120,
    body: dict[str, Any] | None = None,
) -> tuple[int, bytes]:
    headers = {"x-api-key": key, "accept": "*/*"}
    data = None
    if body is not None:
        headers["content-type"] = "application/json"
        data = json.dumps(body).encode()
    request = urllib.request.Request(f"{url}{path}", method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except OSError as exc:
        raise SystemExit(fail(f"couldn't reach {url}: {exc}")) from exc


def error_detail(body: bytes) -> str:
    try:
        data = json.loads(body)
        return str(data.get("detail") or data.get("error", {}).get("message") or data)
    except ValueError:
        return body.decode(errors="replace")[:200]


def claude_env(
    base: dict[str, str],
    key: str,
    *,
    api: bool,
    budget: str | None,
    url: str = GATEWAY_URL,
) -> dict[str, str]:
    """Environment for Claude Code through the gateway.

    Plan mode (default): your claude.ai login is used; the gateway knows you by the
    X-Gateway-Key header and forwards your login unchanged. API mode: the gateway key is
    the API key, and the gateway bills its own Anthropic key."""
    env = dict(base)
    env["ANTHROPIC_BASE_URL"] = url
    env["CLAUDE_CODE_GATEWAY_HINT_HEADERS"] = "1"
    headers = []
    if api:
        env["ANTHROPIC_API_KEY"] = key
    else:
        # Any API key in the environment would replace the claude.ai login.
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        headers.append(f"X-Gateway-Key: {key}")
    if budget:
        headers.append(f"X-Session-Budget-Usd: {budget}")
    if headers:
        env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(headers)
    else:
        env.pop("ANTHROPIC_CUSTOM_HEADERS", None)
    return env


# ---- commands -----------------------------------------------------------------------


def cmd_up(_: argparse.Namespace) -> int:
    if healthy(f"{GATEWAY_URL}/health"):
        say(f"gateway already running at {GATEWAY_URL}")
        return 0
    if not shutil.which("docker"):
        return fail("docker not found: install and start Docker Desktop")
    say("starting Postgres …")
    if subprocess.run(["docker", "compose", "up", "-d", "postgres"], cwd=PROJECT_ROOT).returncode:
        return fail("couldn't start Postgres (is Docker Desktop running?)")
    ready = wait_until(
        lambda: subprocess.run(
            ["docker", "compose", "exec", "-T", "postgres", "pg_isready", "-U", "gateway", "-q"],
            cwd=PROJECT_ROOT, capture_output=True,
        ).returncode == 0,
        30,
    )  # fmt: skip
    if not ready:
        return fail("Postgres didn't become ready")
    say("applying migrations …")
    if subprocess.run([PYTHON, "-m", "alembic", "upgrade", "head"], cwd=PROJECT_ROOT).returncode:
        return fail("migrations failed")
    say("starting the gateway …")
    start_background(
        "gateway",
        [PYTHON, "-m", "uvicorn", "gateway.main:app", "--port", str(GATEWAY_PORT)],
        PROJECT_ROOT,
    )
    if not wait_until(lambda: healthy(f"{GATEWAY_URL}/health"), 20):
        return fail(f"the gateway didn't start; see {STATE_DIR / 'gateway.log'} (gw logs)")
    say(f"gateway running at {GATEWAY_URL}  (logs: gw logs)")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    for name in ("dashboard", "gateway"):
        say(f"{name}: {'stopped' if stop_background(name) else 'not running (from gw)'}")
    if healthy(f"{GATEWAY_URL}/health"):
        say(f"note: a gateway not started by gw is still answering on port {GATEWAY_PORT}")
    if args.db:
        subprocess.run(["docker", "compose", "stop", "postgres"], cwd=PROJECT_ROOT)
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    args.db = False
    cmd_down(args)
    return cmd_up(args)


def cmd_status(_: argparse.Namespace) -> int:
    gateway = healthy(f"{GATEWAY_URL}/health")
    dashboard = healthy(DASHBOARD_URL)
    postgres = subprocess.run(
        ["docker", "compose", "ps", "--status", "running", "-q", "postgres"],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    ).stdout.strip() if shutil.which("docker") else ""  # fmt: skip
    config = load_client_config()
    say(f"gateway    {'up    ' + GATEWAY_URL if gateway else 'down  (gw up)'}")
    say(f"dashboard  {'up    ' + DASHBOARD_URL if dashboard else 'down  (gw dashboard)'}")
    say(f"postgres   {'up' if postgres else 'down  (gw up)'}")
    admin = "set (gw admin-key)" if read_env_value("ADMIN_API_KEY") else "not set (gw admin-key)"
    if config.get("key"):
        mine = f"{config['key'][:10]}…  {config.get('name', '')}"
    else:
        mine = "none yet (gw claude creates it)"
    say(f"store content: {read_env_value('STORE_CONTENT') or 'false'}")
    say(f"admin key:     {admin}")
    say(f"your key:      {mine}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    path = STATE_DIR / f"{args.which}.log"
    if not path.exists():
        return fail(f"no log yet at {path}")
    os.execvp("tail", ["tail", "-n", "60", "-f", str(path)])
    return 0  # unreachable


def cmd_dashboard(args: argparse.Namespace) -> int:
    if not healthy(f"{GATEWAY_URL}/health") and cmd_up(args):
        return 1
    if not healthy(DASHBOARD_URL):
        dashboard_dir = PROJECT_ROOT / "dashboard"
        if not (dashboard_dir / "node_modules").exists():
            say("installing dashboard dependencies (first time) …")
            # npm 10.x has a dependency-resolver bug with this project; use npm 11.
            if subprocess.run(
                ["npx", "-y", "npm@11", "install", "--no-fund", "--no-audit"], cwd=dashboard_dir
            ).returncode:
                return fail("npm install failed")
        say("starting the dashboard …")
        start_background(
            "dashboard",
            ["npx", "vite", "--port", str(DASHBOARD_PORT), "--strictPort"],
            dashboard_dir,
        )
        if not wait_until(lambda: healthy(DASHBOARD_URL), 30):
            return fail(f"the dashboard didn't start; see {STATE_DIR / 'dashboard.log'}")
    say(f"dashboard at {DASHBOARD_URL}  (sign in with the key from: gw admin-key --copy)")
    webbrowser.open(DASHBOARD_URL)
    return 0


def cmd_admin_key(args: argparse.Namespace) -> int:
    key = read_env_value("ADMIN_API_KEY")
    if not key:
        key = secrets.token_urlsafe(32)
        write_env_value("ADMIN_API_KEY", key)
        say("generated a new admin key into .env; restart the gateway to use it: gw restart")
    if args.copy and shutil.which("pbcopy"):
        subprocess.run(["pbcopy"], input=key.encode())
        say("admin key copied to the clipboard")
    else:
        say(key)
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    """Locally: straight to the database (python -m gateway.keys). With --url: through
    that gateway's admin API, for a deployed gateway whose database is private."""
    if not args.url:
        from gateway import keys

        return asyncio.run(keys.main(args.rest))
    return remote_keys(args.url.rstrip("/"), args.rest)


def remote_keys(url: str, rest: list[str]) -> int:
    usage = "usage: gw keys --url URL {list | create NAME | revoke PREFIX}"
    command, params = (rest[0], rest[1:]) if rest else ("", [])
    if (command, len(params)) not in (("list", 0), ("create", 1), ("revoke", 1)):
        return fail(usage)
    admin = os.environ.get("GW_ADMIN_KEY") or getpass.getpass(f"admin key for {url}: ")

    def call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        status, data = api_call(url, admin, method, path, body=body)
        if status >= 400:
            raise SystemExit(fail(f"{method} {path}: {status} {error_detail(data)}"))
        return json.loads(data)

    if command == "create":
        row = call("POST", "/admin/keys", {"name": params[0]})
        say(f"Created key '{row['name']}' ({row['key_prefix']}...)")
        say(f"\n  {row['key']}\n")
        say("Store it now; the gateway keeps only a hash and cannot show it again.")
        return 0
    rows = call("GET", "/admin/keys")
    if command == "list":
        for row in rows:
            status = f"revoked {row['revoked_at'][:10]}" if row["revoked_at"] else "active"
            say(f"{row['key_prefix']}...  {row['name']:<30} {row['created_at'][:10]}  {status}")
        return 0
    matches = [r for r in rows if r["key_prefix"] == params[0] and not r["revoked_at"]]
    if len(matches) != 1:
        return fail(f"expected one active key with prefix {params[0]!r}, found {len(matches)}")
    call("POST", f"/admin/keys/{matches[0]['id']}/revoke")
    say(f"Revoked {params[0]}... ({matches[0]['name']})")
    return 0


def cmd_claude(args: argparse.Namespace, extra: list[str]) -> int:
    if not shutil.which("claude"):
        return fail("claude not found: install Claude Code first")
    url, key = connection()
    env = claude_env(dict(os.environ), key, api=args.api, budget=args.budget, url=url)
    mode = "the gateway's API key" if args.api else "your claude.ai plan"
    say(f"gw: Claude Code via {url}, billed to {mode}")
    os.execvpe("claude", ["claude", *extra], env)
    return 0  # unreachable


def cmd_login(args: argparse.Namespace) -> int:
    url = (args.url or GATEWAY_URL).rstrip("/")
    key = args.key or getpass.getpass(f"gateway key for {url} (gw_…, input hidden): ").strip()
    if not key:
        return fail("no key entered")
    status, body = api_call(url, key, "GET", "/gateway/me", timeout=10)
    if status != 200:
        return fail(f"{url} rejected the key ({status}: {error_detail(body)})")
    me = json.loads(body)
    save_client_config({"gateway_url": url, "key": key, "name": me["name"]})
    say(f"logged in to {url} as '{me['name']}' ({me['key_prefix']}…); saved in {CONFIG_PATH}")
    return 0


def cmd_logout(_: argparse.Namespace) -> int:
    CONFIG_PATH.unlink(missing_ok=True)
    say("forgot the saved gateway and key")
    return 0


def cmd_whoami(_: argparse.Namespace) -> int:
    url, key = connection(start_local=False)
    status, body = api_call(url, key, "GET", "/gateway/me", timeout=10)
    if status != 200:
        return fail(f"{url}: {error_detail(body)}")
    me = json.loads(body)
    say(f"{me['name']} ({me['key_prefix']}…) on {url}")
    return 0


def _my_sessions(url: str, key: str, limit: int = 100) -> list[dict]:  # type: ignore[type-arg]
    status, body = api_call(url, key, "GET", f"/gateway/sessions?limit={limit}", timeout=15)
    if status != 200:
        raise SystemExit(fail(error_detail(body)))
    return json.loads(body)


def cmd_sessions(args: argparse.Namespace) -> int:
    url, key = connection(start_local=False)
    sessions = _my_sessions(url, key, args.limit)
    if not sessions:
        say("no sessions yet")
        return 0
    say(f"{'LAST CALL':17} {'SESSION':38} {'APP':12} {'CALLS':>5} {'SPEND':>9}  HANDOFF")
    for s in sessions:
        last = s["last_call_at"][:16].replace("T", " ")
        handoff = "yes" if s["has_handoff"] else ""
        say(f"{last:17} {s['session_id'][:38]:38} {(s['app'] or '')[:12]:12} "
            f"{s['calls']:>5} ${float(s['spend_usd']):>8.4f}  {handoff}")  # fmt: skip
    return 0


def cmd_handoff(args: argparse.Namespace) -> int:
    url, key = connection(start_local=False)
    sessions = _my_sessions(url, key)
    if args.session:
        matches = [s for s in sessions if s["session_id"].startswith(args.session)]
        if len(matches) != 1:
            return fail(f"{len(matches)} of your recent sessions start with {args.session!r}")
        session = matches[0]
    elif sessions:
        session = sessions[0]  # the most recent
    else:
        return fail("no sessions yet")
    sid = session["session_id"]
    quoted = urllib.parse.quote(sid, safe="")
    if not args.existing:
        say(f"writing a handoff for {sid} ({session['calls']} calls) … (up to a minute)")
        status, body = api_call(url, key, "POST", f"/gateway/sessions/{quoted}/handoffs")
        if status == 429:
            say(f"a handoff was generated moments ago; downloading that one ({error_detail(body)})")
        elif status != 201:
            return fail(f"couldn't generate it ({status}): {error_detail(body)}")
    status, body = api_call(url, key, "GET", f"/gateway/sessions/{quoted}/handoffs/latest.md")
    if status != 200:
        return fail(f"no handoff to download ({status}): {error_detail(body)}")
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")[:24].rstrip("-_")
    path = Path(args.output or f"handoff-{safe}.md")
    path.write_bytes(body)
    say(f"saved {path}")
    return 0


def cmd_env(args: argparse.Namespace) -> int:
    url, key = connection(start_local=False)
    # Printed for `eval "$(gw env)"`: these lines contain your key.
    print(f"export ANTHROPIC_BASE_URL={url}")
    print(f"export ANTHROPIC_API_KEY={key}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gw", description="Run and use the LLM gateway.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("up", help="start Postgres, migrate, start the gateway")
    down = sub.add_parser("down", help="stop the gateway and dashboard")
    down.add_argument("--db", action="store_true", help="also stop Postgres")
    sub.add_parser("restart", help="restart the gateway")
    sub.add_parser("status", help="what's running")
    logs = sub.add_parser("logs", help="follow a log")
    logs.add_argument("which", nargs="?", default="gateway", choices=["gateway", "dashboard"])
    sub.add_parser("dashboard", help="start and open the dashboard")
    admin = sub.add_parser("admin-key", help="print the dashboard sign-in key")
    admin.add_argument("--copy", action="store_true", help="copy it to the clipboard")
    keys = sub.add_parser("keys", help="manage gateway keys (list, create NAME, revoke PREFIX)")
    keys.add_argument(
        "--url", help="a deployed gateway, via its admin API (admin key: GW_ADMIN_KEY or a prompt)"
    )
    keys.add_argument("rest", nargs=argparse.REMAINDER)
    login = sub.add_parser("login", help="save a gateway URL and your gateway key")
    login.add_argument("url", nargs="?", help=f"gateway URL (default {GATEWAY_URL})")
    login.add_argument("--key", help="the key (prefer the hidden prompt: keeps it out of history)")
    sub.add_parser("logout", help="forget the saved gateway and key")
    sub.add_parser("whoami", help="which key and gateway you use")
    sessions = sub.add_parser("sessions", help="your recent sessions")
    sessions.add_argument("--limit", type=int, default=20)
    handoff = sub.add_parser("handoff", help="write a session's handoff to a Markdown file")
    handoff.add_argument("session", nargs="?", help="session id or its start (default: latest)")
    handoff.add_argument("-o", "--output", help="file to write (default handoff-<session>.md)")
    handoff.add_argument(
        "--existing", action="store_true", help="download the latest, don't generate"
    )
    sub.add_parser("env", help='shell exports for SDK apps: eval "$(gw env)"')
    claude = sub.add_parser("claude", help="Claude Code through the gateway")
    claude.add_argument("--api", action="store_true", help="bill the gateway's API key")
    claude.add_argument("--budget", help="session budget in USD")

    args, extra = parser.parse_known_args(argv)
    if args.command == "claude":
        return cmd_claude(args, extra)
    if extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    commands = {
        "up": cmd_up, "down": cmd_down, "restart": cmd_restart, "status": cmd_status,
        "logs": cmd_logs, "dashboard": cmd_dashboard, "admin-key": cmd_admin_key,
        "keys": cmd_keys, "login": cmd_login, "logout": cmd_logout, "whoami": cmd_whoami,
        "sessions": cmd_sessions, "handoff": cmd_handoff, "env": cmd_env,
    }  # fmt: skip
    return commands[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
