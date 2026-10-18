import { Link } from "react-router-dom";

import type { AlertRow } from "../api";
import { QueryView } from "../components";
import { dateTime, int, pct, shortId, usd } from "../format";
import { useAlerts, useKeys } from "../queries";

function describe(alert: AlertRow): string {
  const d = alert.detail;
  if (alert.kind === "spend") {
    return `${usd(d.spent_usd as string)} of ${usd(d.budget_usd as string)} budget (${String(d.budget_source)})`;
  }
  return `${int(d.context_tokens as number)} of ${int(d.context_window as number)} tokens · ${String(d.model)}`;
}

export function AlertsPage() {
  const alerts = useAlerts();
  const keys = useKeys();
  const names = new Map(keys.data?.map((k) => [k.id, k.name]));
  return (
    <>
      <h1 className="page-title">Alerts</h1>
      <p style={{ color: "var(--text-muted)", marginTop: -8 }}>
        Recorded the first time a session reaches each threshold of its spend budget or of a
        model's context window. Alerts never block calls.
      </p>
      <section className="card">
        <QueryView query={alerts} empty={(rows) => rows.length === 0}>
          {(rows) => (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Session</th>
                    <th>User</th>
                    <th>Kind</th>
                    <th className="num">Reached</th>
                    <th className="num">Threshold</th>
                    <th>Detail</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((a) => (
                    <tr key={a.id}>
                      <td>{dateTime(a.created_at)}</td>
                      <td>
                        <Link
                          className="mono"
                          to={`/sessions/${a.api_key_id}/${encodeURIComponent(a.session_id)}`}
                          title={a.session_id}
                        >
                          {shortId(a.session_id, 18)}
                        </Link>
                      </td>
                      <td>{names.get(a.api_key_id) ?? "—"}</td>
                      <td>
                        <span className="tag">{a.kind}</span>
                      </td>
                      <td className={`num ${Number(a.observed) >= 1 ? "status-critical" : ""}`}>
                        {Number(a.observed) >= 1 ? "⛔ " : "⚠ "}
                        {pct(a.observed)}
                      </td>
                      <td className="num">{pct(a.threshold, 0)}</td>
                      <td>{describe(a)}</td>
                      <td>
                        <Link to={`/sessions/${a.api_key_id}/${encodeURIComponent(a.session_id)}#handoff`}>
                          Handoff →
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </QueryView>
      </section>
    </>
  );
}
