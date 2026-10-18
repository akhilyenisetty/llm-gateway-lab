import { Link, useSearchParams } from "react-router-dom";

import type { Overview } from "../api";
import { ChartCard, Filters, Meter, QueryView, StatTile, TimeBars, type Series } from "../components";
import { compact, dateTime, int, ms, pct, plural, shortId, usd } from "../format";
import { useMe, useMyUsage, useMySessions } from "../queries";

const SPEND: Series[] = [{ key: "spend", label: "spend", color: "var(--series)", format: usd }];
const PAGE_SIZE = 50;

/** A user's own usage: totals, spend over time, and their sessions. Everything here comes
 * from the client API, which is scoped to the signed-in gateway key. */
export function MyUsagePage() {
  const me = useMe();
  const usage = useMyUsage();
  const [params, setParams] = useSearchParams();
  const page = Math.max(0, Number(params.get("page") ?? 0) || 0);
  const sessions = useMySessions({ sort: "recent", limit: PAGE_SIZE, offset: page * PAGE_SIZE });
  const setPage = (n: number) =>
    setParams((current) => {
      const next = new URLSearchParams(current);
      if (n) next.set("page", String(n));
      else next.delete("page");
      return next;
    });

  return (
    <>
      <h1 className="page-title">
        My usage{me.data && <span style={{ color: "var(--text-muted)", fontWeight: 400 }}> · {me.data.name}</span>}
      </h1>
      <Filters showUser={false} />
      <QueryView query={usage}>{(data) => <UsageSummary data={data} />}</QueryView>
      <section className="card" style={{ marginTop: 16 }}>
        <div className="card-head">
          <h2>My sessions</h2>
          <span className="sub">open one for its calls, budget, and handoff</span>
        </div>
        <QueryView query={sessions} empty={(d) => d.total === 0}>
          {(data) => (
            <>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Session</th>
                      <th>Category</th>
                      <th className="num">Calls</th>
                      <th className="num">Spend</th>
                      <th>Budget used</th>
                      <th className="num">Peak context</th>
                      <th>Last call</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map((s) => (
                      <tr key={s.session_id}>
                        <td>
                          <Link to={`/sessions/${encodeURIComponent(s.session_id)}`} className="mono" title={s.session_id}>
                            {shortId(s.session_id, 18)}
                          </Link>
                          {s.app && <div style={{ color: "var(--text-muted)", fontSize: 12 }}>{s.app}</div>}
                        </td>
                        <td>
                          <span className="tag">{s.category}</span>
                        </td>
                        <td className="num">{int(s.calls)}</td>
                        <td className="num">
                          {usd(s.spend_usd)}
                          {Number(s.api_equivalent_usd) > 0 && (
                            <div>
                              <span className="tag">own plan</span>
                            </div>
                          )}
                        </td>
                        <td>
                          <Meter fraction={Number(s.spend_fraction)} label={`Budget used of ${usd(s.budget_usd)}`} />
                        </td>
                        <td className="num">{pct(s.peak_context_fraction, 0)}</td>
                        <td>{dateTime(s.last_call_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="form-row" style={{ justifyContent: "flex-end", marginTop: 12 }}>
                <button className="link-button" disabled={page === 0} onClick={() => setPage(page - 1)}>
                  ← Previous
                </button>
                <button
                  className="link-button"
                  disabled={(page + 1) * PAGE_SIZE >= data.total}
                  onClick={() => setPage(page + 1)}
                >
                  Next →
                </button>
              </div>
            </>
          )}
        </QueryView>
      </section>
    </>
  );
}

function UsageSummary({ data }: { data: Overview }) {
  const series = data.series.map((p) => ({ start: p.start, spend: Number(p.spend_usd) }));
  return (
    <>
      <div className="grid tiles">
        <StatTile
          label="Spend"
          value={usd(data.spend_usd)}
          note={
            Number(data.api_equivalent_usd) > 0
              ? `incl. ${usd(data.api_equivalent_usd)} API-equivalent on your own plan`
              : `${int(data.sessions)} sessions`
          }
        />
        <StatTile label="Calls" value={compact(data.calls)} note={`${int(data.sessions)} sessions`} />
        <StatTile label="Error rate" value={pct(data.error_rate)} note={plural(data.errors, "failed call")} />
        <StatTile label="Latency p50" value={ms(data.latency_p50_ms)} note={`p95 ${ms(data.latency_p95_ms)}`} />
      </div>
      <ChartCard
        title="My spend"
        sub={data.bucket === "hour" ? "per hour" : "per day (UTC)"}
        chart={<TimeBars data={series} bucket={data.bucket} series={SPEND} yFormat={usd} />}
        table={
          <table>
            <thead>
              <tr>
                <th>{data.bucket === "hour" ? "Hour" : "Day (UTC)"}</th>
                <th className="num">Calls</th>
                <th className="num">Spend</th>
              </tr>
            </thead>
            <tbody>
              {data.series
                .filter((p) => p.calls > 0)
                .map((p) => (
                  <tr key={p.start}>
                    <td>{new Date(p.start).toLocaleString()}</td>
                    <td className="num">{int(p.calls)}</td>
                    <td className="num">{usd(p.spend_usd)}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        }
      />
    </>
  );
}
