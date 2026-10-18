import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { ApiError, type CallRow, type SessionStatus } from "../api";
import { ChartCard, Meter, QueryView } from "../components";
import { dateTime, int, ms, pct, usd } from "../format";
import { useSessionActions, useSessionCalls, useSessionStatus } from "../queries";

export function SessionDetailPage() {
  const { keyId = "", sessionId = "" } = useParams();
  const status = useSessionStatus(keyId, sessionId);
  const calls = useSessionCalls(keyId, sessionId);
  return (
    <>
      <p style={{ margin: "0 0 8px" }}>
        <Link to="/sessions">← Sessions</Link>
      </p>
      <h1 className="page-title mono" style={{ fontSize: 16 }}>
        {sessionId}
      </h1>
      <QueryView query={status}>
        {(s) => <SessionSummary status={s} keyId={keyId} sessionId={sessionId} />}
      </QueryView>
      <div style={{ marginTop: 16 }}>
        <QueryView query={calls} empty={(rows) => rows.length === 0}>
          {(rows) => <Timeline calls={rows} />}
        </QueryView>
      </div>
    </>
  );
}

function SessionSummary({ status, keyId, sessionId }: { status: SessionStatus; keyId: string; sessionId: string }) {
  const { setBudget, categorize } = useSessionActions(keyId, sessionId);
  const [budget, setBudgetInput] = useState("");
  const category = status.category;
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (budget) setBudget.mutate(budget, { onSuccess: () => setBudgetInput("") });
  };
  const actionError = (setBudget.error ?? categorize.error) as Error | null;

  return (
    <div className="grid two-col">
      <section className="card">
        <div className="card-head">
          <h2>Budget</h2>
          <span className="sub">source: {status.budget_source}</span>
        </div>
        <Meter fraction={Number(status.spend_fraction)} />
        <p style={{ margin: "8px 0 12px" }}>
          {usd(status.spent_usd)} of {usd(status.budget_usd)} · {int(status.calls)} calls
        </p>
        <form className="form-row" onSubmit={onSubmit}>
          <label htmlFor="budget">Set budget (USD)</label>
          <input
            id="budget"
            inputMode="decimal"
            placeholder={status.budget_usd}
            value={budget}
            onChange={(e) => setBudgetInput(e.target.value)}
            style={{ width: 100 }}
          />
          <button className="primary" disabled={!budget || setBudget.isPending}>
            {setBudget.isPending ? "Saving…" : "Save"}
          </button>
        </form>
        {status.alerts.length > 0 && (
          <>
            <h2 style={{ marginTop: 16 }}>Alerts</h2>
            <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
              {status.alerts.map((a) => (
                <li key={a.id} className="status-critical">
                  ⚠ {a.kind} reached {pct(a.observed)} (threshold {pct(a.threshold, 0)}) ·{" "}
                  <span style={{ color: "var(--text-muted)" }}>{dateTime(a.created_at)}</span>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      <section className="card">
        <div className="card-head">
          <h2>Category</h2>
          <button className="link-button" onClick={() => categorize.mutate()} disabled={categorize.isPending}>
            {categorize.isPending ? "Categorizing…" : "Categorize now"}
          </button>
        </div>
        {category ? (
          <dl className="kv">
            <dt>Category</dt>
            <dd>
              <span className="tag">{category.category ?? "none"}</span>
            </dd>
            <dt>How</dt>
            <dd>{category.status}</dd>
            {category.auto_reason && (
              <>
                <dt>Reason</dt>
                <dd>{category.auto_reason}</dd>
              </>
            )}
            {category.classifier_model && (
              <>
                <dt>Classifier</dt>
                <dd className="mono">
                  {category.classifier_model} · prompt {category.prompt_version} ·{" "}
                  {usd(category.classifier_cost_usd)}
                </dd>
              </>
            )}
            {category.error && (
              <>
                <dt>Error</dt>
                <dd className="status-critical">{category.error}</dd>
              </>
            )}
            <dt>As of</dt>
            <dd>
              {int(category.classified_calls)} calls, {dateTime(category.updated_at)}
            </dd>
          </dl>
        ) : (
          <p className="sub" style={{ color: "var(--text-muted)" }}>
            Not categorized yet.
          </p>
        )}
      </section>
      {actionError && (
        <div className="error-box" role="alert">
          {actionError instanceof ApiError ? actionError.message : String(actionError)}
        </div>
      )}
    </div>
  );
}

function Timeline({ calls }: { calls: CallRow[] }) {
  const points = calls.map((c, i) => ({
    n: i + 1,
    context: c.context_tokens && c.context_window ? c.context_tokens / c.context_window : null,
    time: dateTime(c.created_at),
  }));
  return (
    <>
      <ChartCard
        title="Context use per call"
        sub="prompt + output tokens as a share of the model's context window"
        chart={
          <ResponsiveContainer width="100%" height={200}>
            <LineChart data={points} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
              <CartesianGrid vertical={false} stroke="var(--grid)" />
              <XAxis dataKey="n" stroke="var(--axis)" tick={{ fill: "var(--text-muted)", fontSize: 11 }} tickLine={false} />
              <YAxis
                domain={[0, 1]}
                stroke="var(--axis)"
                tick={{ fill: "var(--text-muted)", fontSize: 11 }}
                tickLine={false}
                axisLine={false}
                width={44}
                tickFormatter={(v: number) => pct(v, 0)}
              />
              <Tooltip
                content={({ active, payload }) =>
                  active && payload?.[0] ? (
                    <div className="chart-tooltip">
                      <div className="t-title">
                        Call {payload[0].payload.n} · {payload[0].payload.time}
                      </div>
                      <strong>{pct(payload[0].value as number)}</strong> of context window
                    </div>
                  ) : null
                }
              />
              <Line
                dataKey="context"
                stroke="var(--series)"
                strokeWidth={2}
                dot={{ r: 4, fill: "var(--series)", stroke: "var(--surface)", strokeWidth: 2 }}
                connectNulls
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        }
        table={<CallsTable calls={calls} />}
      />
      <section className="card" style={{ marginTop: 16 }}>
        <div className="card-head">
          <h2>Calls</h2>
          <span className="sub">{int(calls.length)} calls, oldest first</span>
        </div>
        <div className="table-wrap">
          <CallsTable calls={calls} />
        </div>
      </section>
    </>
  );
}

function CallsTable({ calls }: { calls: CallRow[] }) {
  return (
    <table>
      <thead>
        <tr>
          <th>Time</th>
          <th>Model</th>
          <th>Status</th>
          <th className="num">Input</th>
          <th className="num">Cache read</th>
          <th className="num">Output</th>
          <th className="num">Context</th>
          <th className="num">Cost</th>
          <th className="num">TTFT</th>
          <th className="num">Latency</th>
        </tr>
      </thead>
      <tbody>
        {calls.map((c) => {
          const failed = c.status_code >= 400 || (c.error_type && c.error_type !== "client_disconnected");
          return (
            <tr key={c.id}>
              <td>{dateTime(c.created_at)}</td>
              <td className="mono">{c.model ?? "—"}</td>
              <td className={failed ? "status-critical" : undefined}>
                {failed ? "⛔ " : ""}
                {c.status_code}
                {c.error_type ? ` · ${c.error_type}` : c.stop_reason ? ` · ${c.stop_reason}` : ""}
                {c.is_stream ? " · stream" : ""}
              </td>
              <td className="num">{int(c.input_tokens)}</td>
              <td className="num">{int(c.cache_read_tokens)}</td>
              <td className="num">{int(c.output_tokens)}</td>
              <td className="num">
                {c.context_tokens && c.context_window ? pct(c.context_tokens / c.context_window) : "—"}
              </td>
              <td className="num" title={c.cost_note ?? undefined}>
                {usd(c.cost_usd)}
                {c.cost_note ? " *" : ""}
              </td>
              <td className="num">{ms(c.ttft_ms)}</td>
              <td className="num">{ms(c.latency_ms)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
