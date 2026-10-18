import type { Overview as OverviewData } from "../api";
import { ChartCard, Filters, QueryView, RankBars, StatTile, TimeBars, type Series } from "../components";
import { compact, int, ms, pct, usd } from "../format";
import { useOverview } from "../queries";

const SPEND: Series[] = [{ key: "spend", label: "spend", color: "var(--series)", format: usd }];
const CALLS: Series[] = [
  { key: "ok", label: "Succeeded", color: "var(--series)", format: int },
  { key: "failed", label: "Failed", color: "var(--status-critical)", format: int },
];

export function OverviewPage() {
  const overview = useOverview();
  return (
    <>
      <h1 className="page-title">Overview</h1>
      <Filters />
      <QueryView query={overview}>{(data) => <OverviewBody data={data} />}</QueryView>
    </>
  );
}

function OverviewBody({ data }: { data: OverviewData }) {
  const unit = data.bucket === "hour" ? "per hour" : "per day (UTC)";
  const series = data.series.map((p) => ({
    start: p.start,
    spend: Number(p.spend_usd),
    ok: p.calls - p.errors,
    failed: p.errors,
  }));
  const models = data.by_model.map((m) => ({ ...m, name: m.model ?? "(unknown)" }));
  return (
    <>
      <div className="grid tiles">
        <StatTile label="Spend" value={usd(data.spend_usd)} note={`${int(data.sessions)} sessions`} />
        <StatTile label="Calls" value={compact(data.calls)} note={`${compact(data.output_tokens)} output tokens`} />
        <StatTile
          label="Error rate"
          value={pct(data.error_rate)}
          note={`${int(data.errors)} failed calls`}
        />
        <StatTile
          label="Latency p50"
          value={`${ms(data.latency_p50_ms)}`}
          note={`p95 ${ms(data.latency_p95_ms)}`}
        />
        <StatTile
          label="Time to first token p50"
          value={ms(data.ttft_p50_ms)}
          note={`p95 ${ms(data.ttft_p95_ms)} · streamed calls`}
        />
      </div>

      <div className="grid two-col">
        <ChartCard
          title="Spend"
          sub={unit}
          chart={<TimeBars data={series} bucket={data.bucket} series={SPEND} yFormat={usd} />}
          table={<SeriesTable data={data} />}
        />
        <ChartCard
          title="Calls"
          sub={`${unit} · failed = HTTP error or stream failure`}
          chart={<TimeBars data={series} bucket={data.bucket} series={CALLS} yFormat={compact} />}
          table={<SeriesTable data={data} />}
        />
      </div>

      <div className="grid" style={{ marginTop: 16 }}>
        <ChartCard
          title="By model"
          sub="spend"
          chart={<RankBars data={models.map((m) => ({ name: m.name, value: Number(m.spend_usd) }))} format={usd} label="spend" />}
          table={
            <table>
              <thead>
                <tr>
                  <th>Model</th>
                  <th className="num">Calls</th>
                  <th className="num">Spend</th>
                  <th className="num">Errors</th>
                  <th className="num">Latency p50</th>
                  <th className="num">p95</th>
                </tr>
              </thead>
              <tbody>
                {models.map((m) => (
                  <tr key={m.name}>
                    <td className="mono">{m.name}</td>
                    <td className="num">{int(m.calls)}</td>
                    <td className="num">{usd(m.spend_usd)}</td>
                    <td className="num">{int(m.errors)}</td>
                    <td className="num">{ms(m.latency_p50_ms)}</td>
                    <td className="num">{ms(m.latency_p95_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          }
        />
      </div>
    </>
  );
}

function SeriesTable({ data }: { data: OverviewData }) {
  return (
    <table>
      <thead>
        <tr>
          <th>{data.bucket === "hour" ? "Hour" : "Day (UTC)"}</th>
          <th className="num">Calls</th>
          <th className="num">Failed</th>
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
              <td className="num">{int(p.errors)}</td>
              <td className="num">{usd(p.spend_usd)}</td>
            </tr>
          ))}
      </tbody>
    </table>
  );
}
