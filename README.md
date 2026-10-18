# llm-gateway-lab

An LLM gateway for the Anthropic API. Apps send their Claude calls through the gateway
instead of directly to Anthropic (changing only the base URL); the gateway forwards each
call and records exact tokens, cost, latency, and status per call in Postgres.

**Status:** Stages 1-6 (gateway core, streaming, budgets and alerts, categories, ops
dashboard, context handoff). The handoff eval is planned next.

## How it works

```
client (Anthropic SDK, gateway key) ──► gateway ──► Anthropic API (real key)
                                          │
                                          ▼
                              Postgres: one row per call
```

- **Same API as Anthropic.** The gateway serves `POST /v1/messages`, streaming and
  non-streaming, so the Anthropic SDK and Claude Code work unchanged. Other `/v1/*` paths (`/v1/messages/count_tokens`, `/v1/models`, ...)
  are forwarded but not recorded.
- **Key swap.** Clients authenticate with gateway-issued keys (`x-api-key` or
  `Authorization: Bearer`). The gateway replaces them with the real `ANTHROPIC_API_KEY`
  upstream, so clients never see it. Only SHA-256 hashes of gateway keys are stored.
- **Request bodies are forwarded byte-for-byte**, so new API fields pass through untouched.
- **Errors keep Anthropic's format.** Upstream errors pass through as-is; if Anthropic is
  unreachable the gateway returns 502 (504 on timeout) in Anthropic's error shape.
- **Recording never breaks a call.** If the database write fails, the client still gets
  its response and the failure is logged.

### Request metadata headers

| Header | Meaning |
|---|---|
| `X-Session-Id` | Groups calls into a session. |
| `X-Category` | Optional category tag (see [Categories](#categories)). |
| `X-App` | Which application sent the call. |
| `X-Session-Budget-Usd` | Spend budget for this session (see [Budgets and alerts](#budgets-and-alerts)). |

`X-Session-Id`, `X-Category`, and `X-Session-Budget-Usd` are stripped before forwarding; `X-App` is forwarded,
because Claude Code sends `x-app: cli` to Anthropic itself. The session ID comes from, in
order: `X-Session-Id`; Claude Code's own `x-claude-code-session-id` header (its built-in
`x-app: cli` is then recorded as `claude-code`); or a derived ID. The derived ID is
`derived-` + a hash of (gateway key, app, system prompt, first message). Every turn of a
conversation resends the same first message, so all turns share the ID, and no content is
stored to compute it. Two conversations with an identical system prompt and first message
(same key and app) would be grouped together; send `X-Session-Id` when that matters.

### Streaming

With `"stream": true`, the gateway forwards Anthropic's server-sent events to the client
byte for byte as they arrive (pings and unknown event types included) and parses a copy
on the side. Usage comes from `message_start` plus the cumulative `message_delta`
events. `latency_ms` is the time to the last byte; `ttft_ms` is the time to the first
content delta. Each streamed call is recorded once, when it ends, with an outcome in
`error_type`:

| `error_type` | Meaning |
|---|---|
| empty | Stream completed (`message_stop` received) |
| an API error type, e.g. `overloaded_error` | Anthropic sent an error event mid-stream |
| `upstream_timeout`, `upstream_disconnected` | The connection to Anthropic stalled or broke; the client gets a final SSE `error` event |
| `incomplete_stream` | The stream ended without `message_stop` |
| `client_disconnected` | The client left; the gateway closes the upstream connection so Anthropic stops generating |

For the last three, usage is what had arrived so far, so output tokens and cost are a
lower bound (noted in `cost_note`). Errors before the stream starts (e.g. a 429) are
plain JSON responses, recorded like non-streaming errors.

### Cost

Cost is computed when each call is recorded, from `config/prices.yaml` (USD per million
tokens, copied from Anthropic's pricing page; the file records the date it was checked).
It covers uncached input, output, cache reads, and 5-minute and 1-hour cache writes, with
tiered pricing for models priced by prompt length and the 1.1x multiplier for
`inference_geo: "us"`. Calls to models missing from the table, and fast-mode calls, are
recorded with an empty cost and a `cost_note` explaining why, rather than a guessed number.

### Budgets and alerts

A session is identified by (gateway key, session ID). The same session ID sent with two
keys is two separate sessions, and one key can never affect another key's sessions.

Each session has two budgets, and the gateway records an alert the first time a session
reaches each threshold (default 80% and 100%, `ALERT_THRESHOLDS`). Alerts are recorded and
logged; they never block calls.

- **Spend:** the session's total cost against its budget. Who can set it:

  | Who | How | Scope |
  |---|---|---|
  | Operator | `DEFAULT_SESSION_BUDGET_USD` in `.env` ($5.00), restart | Every session without its own budget |
  | Operator | Admin API `PUT .../budget` | One session; a client header never overrides it |
  | Client (gateway key) | `X-Session-Budget-Usd` header | Its own sessions only; ignored when `ALLOW_CLIENT_BUDGETS=false` |

  Lowering a budget below current spend alerts immediately.
- **Context:** each call's context use (input + cache read + cache write + output tokens,
  all of which count toward the window) against its model's context window, from
  `config/prices.yaml`. This is checked per call, not on "the session's latest call",
  because one session can interleave conversations of very different sizes: Claude Code
  sends small title and summary calls between ~70K-token main turns. A call near its
  window is the conversation that needs a handoff.

Each (session, kind, threshold) alert fires once. A unique constraint with
`INSERT ... ON CONFLICT DO NOTHING` enforces that in the database, so concurrent calls
crossing a threshold together still produce one alert. Budget writes and alert checks run
after the call's own row is committed, so they can never lose the record of a call.

Spend is checked after each call commits its own row, so the last of several concurrent
calls always sees all of them and a crossed threshold isn't missed. Known limitation: a
threshold that already fired doesn't fire again if the budget is raised and crossed a
second time.

To give a Claude Code session its own budget:

```bash
ANTHROPIC_CUSTOM_HEADERS="X-Session-Budget-Usd: 2.00" ANTHROPIC_BASE_URL=... claude
```

### Admin API

Enabled by setting `ADMIN_API_KEY`; requests send it as `Authorization: Bearer <key>`.
It is separate from client gateway keys, so a client can't raise its own budget.

| Endpoint | Purpose |
|---|---|
| `GET /admin/keys` | Gateway keys: id, prefix, name (never the key or its hash) |
| `GET /admin/keys/{key_id}/sessions/{session_id}` | Spend, budget (and its source), spend fraction, alerts |
| `PUT /admin/keys/{key_id}/sessions/{session_id}/budget` | Set a budget: `{"budget_usd": "2.50"}` |
| `POST /admin/keys/{key_id}/sessions/{session_id}/categorize` | Categorize the session now |
| `GET /admin/keys/{key_id}/sessions/{session_id}/calls` | Every call in the session, oldest first |
| `GET /admin/alerts?key_id=&session_id=&limit=` | Recent alerts, newest first |
| `POST /admin/keys/{key_id}/sessions/{session_id}/handoffs` | Generate a handoff document |
| `GET /admin/keys/{key_id}/sessions/{session_id}/handoffs` | The session's handoffs (all versions) |
| `GET /admin/handoffs/{id}`, `/admin/handoffs/{id}/markdown` | One handoff as JSON, or as a `.md` download |
| `GET /admin/stats/overview?from=&to=&key_id=&bucket=` | Totals, percentiles, time series, by model |
| `GET /admin/stats/categories`, `/admin/stats/users` | Spend and calls by category, by gateway key |
| `GET /admin/sessions?category=&sort=&limit=&offset=` | Sessions with budget, category, peak context |

### Categories

Each session gets one category, so spend can be broken down by kind of work. The fixed
list and its definitions are in [`config/categories.yaml`](config/categories.yaml):
`coding`, `writing`, `analysis`, `question_answering`, `other`.

1. **Client tag.** If the session's calls carry `X-Category`, the most frequent tag is
   the category. No model call.
2. **Automatic.** Otherwise Claude Haiku classifies a digest of the stored conversation:
   the user's messages, the start of the assistant's replies, and the tools used (system
   prompts, tool results, and repeated history are dropped; see `gateway/digest.py`).
   Structured outputs restrict the answer to the category list. The classifier calls
   Anthropic directly, not through the gateway, and its cost is recorded with each
   result (about $0.0001 per session).
3. **No content.** Without client tags or stored content (`STORE_CONTENT=false`), the
   category stays empty.

A background worker categorizes sessions after `CATEGORIZE_IDLE_S` (10 minutes) without
new calls, again if a session grows, and retries failures after 30 minutes. The admin API
can categorize a session immediately.

**Measured accuracy: 88.4%** on 43 hand-labeled sessions (3 runs, all agreeing); details,
method, and limitations in [docs/EVALUATION.md](docs/EVALUATION.md).

### Context handoff

When a session nears its budget or context window, the gateway offers to write a
**handoff document** that a fresh session (or person) can continue from: goal, current
state, next step, decisions and why, open tasks, constraints, key files and code, what
was tried and failed, and open questions.

- **Input:** the session's main conversation. Every call resends the conversation so
  far, so the request with the most messages already holds it in full. Tool calls and
  results are kept (they carry paths, commands, and exact errors); thinking and injected
  reminders are dropped; long blocks are clipped; past `HANDOFF_MAX_INPUT_TOKENS`
  (150K) the start and the end are kept.
- **Writer:** `HANDOFF_MODEL` (Claude Sonnet 5.5) with structured outputs, so every
  section is always present; the gateway renders the Markdown in a fixed layout.
- **Offered, not automatic:** alerts link to "Generate handoff", highlighted on the
  session page once it has an alert; the button works any time. Each generation costs
  a few cents, so there is a per-session cooldown (`HANDOFF_COOLDOWN_S`, 60s).
- **Needs `STORE_CONTENT=true`** while the session runs.

The session's owner can also get it with their gateway key, scoped to their own
sessions:

```bash
curl -s -H "x-api-key: $GW_KEY" localhost:8000/gateway/sessions            # your recent sessions
curl -s -X POST -H "x-api-key: $GW_KEY" localhost:8000/gateway/sessions/<id>/handoffs
curl -s -H "x-api-key: $GW_KEY" -o handoff.md localhost:8000/gateway/sessions/<id>/handoffs/latest.md
```

### Privacy: `STORE_CONTENT`

- `false` (default): only metrics are stored (tokens, cost, latency, status, tags).
- `true`: full request and response bodies are also stored in `request_contents`.
  Automatic categorization and handoff documents need this; client tags work either
  way. Anyone with database access, including the gateway operator, can then read every
  user's prompts and responses, so tell your users.

## Running it

Requires Python 3.11 and Docker (for Postgres).

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env              # then set ANTHROPIC_API_KEY

docker compose up -d postgres
.venv/bin/alembic upgrade head    # create tables

.venv/bin/python -m gateway.keys create --name my-app    # prints a gw_... key once
.venv/bin/uvicorn gateway.main:app --port 8000
```

Point a client at it:

```python
import anthropic

client = anthropic.Anthropic(api_key="gw_...", base_url="http://localhost:8000")
message = client.messages.create(
    model="claude-haiku-5-5",
    max_tokens=100,
    messages=[{"role": "user", "content": "Hello"}],
    extra_headers={"X-App": "demo", "X-Session-Id": "demo-1"},
)
```

### Claude Code through the gateway

```bash
ANTHROPIC_BASE_URL=http://localhost:8000 ANTHROPIC_API_KEY=gw_... claude
```

Calls are grouped by Claude Code's session ID automatically. Note that Claude Code then
bills per token to the Anthropic API key behind the gateway, not to a claude.ai
subscription.

Manage keys with `python -m gateway.keys list` and `python -m gateway.keys revoke <prefix>`.

## Dashboard

A React app (`dashboard/`, Vite + TypeScript, TanStack Query, Recharts) over the admin
API:

| Page | Shows |
|---|---|
| Overview | spend and calls over time, error rate, p50/p95 latency and time to first token, spend by model |
| Categories | spend, sessions, and calls by session category |
| Users | spend, calls, sessions, and errors per gateway key |
| Sessions | every session with a budget meter, category, peak context use, and alerts; filter by user and category |
| Session detail | context use per call, every call's tokens, cost, latency, and status; set the budget or categorize now |
| Alerts | budget and context alerts, linked to their sessions |

Filters (time range, user) live in the URL, so a view can be bookmarked or shared. Pages
refresh every 30 seconds. Sign in with the gateway's `ADMIN_API_KEY`; it is kept in
`sessionStorage` (this tab only).

```bash
cd dashboard
npx npm@11 install        # npm 10.x has a dependency-resolver bug with this project
npm run dev               # http://localhost:5173, forwards /admin to the gateway on :8000
npm test && npm run typecheck && npm run lint
```

`GATEWAY_URL=http://host:port npm run dev` points the dev server at another gateway.

Stats definitions: an error is an HTTP status >= 400 or a failure during a stream (a
client disconnecting is not an error); latency is time to the last byte; percentiles use
linear interpolation (Postgres `percentile_cont`); time buckets are UTC.

## Evaluating the classifier

```bash
.venv/bin/python -m evals.generate_synthetic   # synthetic sessions (once; ~$0.15)
.venv/bin/python -m evals.export_real          # optional: real sessions with stored content
.venv/bin/python -m evals.label                # label them by hand, blind
.venv/bin/python -m evals.run_classifier_eval  # 3 runs; report in results/
```

## Tests

```bash
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Tests run without an API key or network: Anthropic is mocked with `respx`, the database
is a temporary SQLite file, and the main test drives the real Anthropic SDK through the
gateway in-process. To run the same tests against Postgres (production's database, which
SQLite doesn't fully imitate), point `TEST_DATABASE_URL` at a database whose name ends in
`_test`; it is wiped for every test:

```bash
docker compose exec postgres createdb -U gateway gateway_test
TEST_DATABASE_URL=postgresql+asyncpg://gateway:gateway@localhost:5432/gateway_test .venv/bin/pytest
```
