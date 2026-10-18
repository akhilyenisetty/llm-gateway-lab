import { Link, useSearchParams } from "react-router-dom";

import { Filters, Meter, QueryView } from "../components";
import { dateTime, int, pct, shortId, usd } from "../format";
import { useSessions } from "../queries";

const PAGE_SIZE = 50;
const CATEGORIES = ["coding", "writing", "analysis", "question_answering", "other", "uncategorized"];

export function SessionsPage() {
  const [params, setParams] = useSearchParams();
  const category = params.get("category") ?? "";
  const sort = params.get("sort") === "spend" ? "spend" : "recent";
  const page = Math.max(0, Number(params.get("page") ?? 0) || 0);
  const sessions = useSessions({
    category: category || undefined,
    sort,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  });

  const set = (key: string, value: string) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        if (value) next.set(key, value);
        else next.delete(key);
        if (key !== "page") next.delete("page");
        return next;
      },
      { replace: true },
    );

  return (
    <>
      <h1 className="page-title">Sessions</h1>
      <Filters>
        <select aria-label="Category" value={category} onChange={(e) => set("category", e.target.value)}>
          <option value="">All categories</option>
          {CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <select aria-label="Sort" value={sort} onChange={(e) => set("sort", e.target.value)}>
          <option value="recent">Most recent</option>
          <option value="spend">Highest spend</option>
        </select>
      </Filters>
      <section className="card">
        <QueryView query={sessions} empty={(d) => d.total === 0}>
          {(data) => (
            <>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Session</th>
                      <th>User</th>
                      <th>Category</th>
                      <th className="num">Calls</th>
                      <th className="num">Spend</th>
                      <th>Budget used</th>
                      <th className="num">Peak context</th>
                      <th className="num">Alerts</th>
                      <th>Last call</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map((s) => (
                      <tr key={`${s.key_id}/${s.session_id}`}>
                        <td>
                          <Link
                            to={`/sessions/${s.key_id}/${encodeURIComponent(s.session_id)}`}
                            className="mono"
                            title={s.session_id}
                          >
                            {shortId(s.session_id, 18)}
                          </Link>
                          {s.app && <div className="sub" style={{ color: "var(--text-muted)", fontSize: 12 }}>{s.app}</div>}
                        </td>
                        <td>{s.key_name}</td>
                        <td>
                          <span className="tag">{s.category}</span>
                        </td>
                        <td className="num">{int(s.calls)}</td>
                        <td
                          className="num"
                          title={
                            Number(s.api_equivalent_usd) > 0
                              ? `${usd(s.api_equivalent_usd)} of this is API-equivalent (the user's own plan or key paid)`
                              : undefined
                          }
                        >
                          {usd(s.spend_usd)}
                          {Number(s.api_equivalent_usd) > 0 && (
                            <div>
                              <span className="tag">own plan</span>
                            </div>
                          )}
                        </td>
                        <td title={`${usd(s.spend_usd)} of ${usd(s.budget_usd)} (${s.budget_source})`}>
                          <Meter fraction={Number(s.spend_fraction)} label={`Budget used of ${usd(s.budget_usd)}`} />
                        </td>
                        <td className="num">{pct(s.peak_context_fraction, 0)}</td>
                        <td className={`num ${s.alerts ? "status-critical" : ""}`}>{int(s.alerts)}</td>
                        <td>{dateTime(s.last_call_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="form-row" style={{ justifyContent: "space-between", marginTop: 12 }}>
                <span className="sub" style={{ color: "var(--text-muted)", fontSize: 12 }}>
                  {page * PAGE_SIZE + 1}–{page * PAGE_SIZE + data.items.length} of {int(data.total)} · spend
                  and budget cover each session's whole history
                </span>
                <span className="form-row">
                  <button className="link-button" disabled={page === 0} onClick={() => set("page", String(page - 1))}>
                    ← Previous
                  </button>
                  <button
                    className="link-button"
                    disabled={(page + 1) * PAGE_SIZE >= data.total}
                    onClick={() => set("page", String(page + 1))}
                  >
                    Next →
                  </button>
                </span>
              </div>
            </>
          )}
        </QueryView>
      </section>
    </>
  );
}
