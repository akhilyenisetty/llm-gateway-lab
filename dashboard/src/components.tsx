import { useState, type ReactNode } from "react";
import { NavLink, Outlet, useSearchParams } from "react-router-dom";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
} from "recharts";
import type { UseQueryResult } from "@tanstack/react-query";

import { ApiError } from "./api";
import { useAuth } from "./authContext";
import { bucketLabel, pct } from "./format";
import { RANGES, useFilters, useKeys, type RangeKey } from "./queries";

// ---- Layout ---------------------------------------------------------------------------

const PAGES = [
  ["/", "Overview"],
  ["/categories", "Categories"],
  ["/users", "Users"],
  ["/sessions", "Sessions"],
  ["/alerts", "Alerts"],
] as const;

export function Layout() {
  const { signOut } = useAuth();
  // Keep the shared filters (range, user) when moving between pages.
  const [params] = useSearchParams();
  const shared = new URLSearchParams();
  for (const name of ["range", "user"]) {
    const value = params.get(name);
    if (value) shared.set(name, value);
  }
  const search = shared.size ? `?${shared}` : "";
  return (
    <div className="shell">
      <nav className="nav" aria-label="Main">
        <h1>LLM Gateway</h1>
        {PAGES.map(([to, label]) => (
          <NavLink key={to} to={{ pathname: to, search }} end={to === "/"}>
            {label}
          </NavLink>
        ))}
        <button className="link-button signout" onClick={signOut}>
          Sign out
        </button>
      </nav>
      <main>
        <Outlet />
      </main>
    </div>
  );
}

/** One row of filters above the content it scopes: the time range, then the user. */
export function Filters({ showUser = true, children }: { showUser?: boolean; children?: ReactNode }) {
  const { range, setRange, userId, setUserId } = useFilters();
  const keys = useKeys();
  return (
    <div className="filters">
      <div className="segmented" role="group" aria-label="Time range">
        {(Object.keys(RANGES) as RangeKey[]).map((key) => (
          <button key={key} aria-pressed={range === key} onClick={() => setRange(key)}>
            {RANGES[key].label}
          </button>
        ))}
      </div>
      {showUser && (
        <select aria-label="User" value={userId} onChange={(e) => setUserId(e.target.value)}>
          <option value="">All users</option>
          {keys.data?.map((k) => (
            <option key={k.id} value={k.id}>
              {k.name} ({k.key_prefix}…){k.revoked_at ? " · revoked" : ""}
            </option>
          ))}
        </select>
      )}
      {children}
    </div>
  );
}

// ---- Loading / error / empty ------------------------------------------------------------

/** Renders the query's data, or its first-load / error state. While refetching, the last
 * data stays on screen, dimmed (no skeleton flash). */
export function QueryView<T>({
  query,
  children,
  empty,
}: {
  query: UseQueryResult<T>;
  children: (data: T) => ReactNode;
  empty?: (data: T) => boolean;
}) {
  if (query.isPending) return <div className="empty">Loading…</div>;
  if (query.isError && !query.data) {
    const message = query.error instanceof ApiError ? query.error.message : String(query.error);
    return <div className="error-box" role="alert">Couldn't load: {message}</div>;
  }
  const data = query.data as T;
  if (empty?.(data)) return <div className="empty">No data in this range.</div>;
  return <div className={query.isPlaceholderData ? "stale" : undefined}>{children(data)}</div>;
}

// ---- Figures ----------------------------------------------------------------------------

export function StatTile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="card tile">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {note && <div className="note">{note}</div>}
    </div>
  );
}

/** A budget meter. The fill carries severity at the alert thresholds (80%, 100%), and the
 * percentage is always written next to it, so color is never the only signal. */
export function Meter({ fraction, label }: { fraction: number; label?: string }) {
  const severity = fraction >= 1 ? "critical" : fraction >= 0.8 ? "warning" : "";
  const icon = fraction >= 1 ? "⛔ " : fraction >= 0.8 ? "⚠ " : "";
  return (
    <div className="meter">
      <div
        className="meter-track"
        role="meter"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(fraction * 100)}
        aria-label={label ?? "Budget used"}
      >
        <div className={`meter-fill ${severity}`} style={{ width: `${Math.min(fraction, 1) * 100}%` }} />
      </div>
      <span className="meter-label">
        {icon}
        {pct(fraction, 0)}
      </span>
    </div>
  );
}

/** A chart with a title and a table view, so no value is reachable only by hovering. */
export function ChartCard({
  title,
  sub,
  chart,
  table,
}: {
  title: string;
  sub?: string;
  chart: ReactNode;
  table: ReactNode;
}) {
  const [showTable, setShowTable] = useState(false);
  return (
    <section className="card">
      <div className="card-head">
        <div>
          <h2>{title}</h2>
          {sub && <div className="sub">{sub}</div>}
        </div>
        <button className="link-button" onClick={() => setShowTable((v) => !v)} aria-pressed={showTable}>
          {showTable ? "Show chart" : "Show table"}
        </button>
      </div>
      {showTable ? <div className="table-wrap">{table}</div> : chart}
    </section>
  );
}

// ---- Charts -----------------------------------------------------------------------------

const AXIS = { stroke: "var(--axis)", tick: { fill: "var(--text-muted)", fontSize: 11 } };
const BAR_RADIUS: [number, number, number, number] = [4, 4, 0, 0];

export interface Series {
  key: string;
  label: string;
  color: string;
  format: (value: number) => string;
}

function SeriesTooltip({
  active,
  payload,
  label,
  series,
  labelFormat,
}: TooltipContentProps<number, string> & { series: Series[]; labelFormat: (l: string) => string }) {
  if (!active || !payload?.length) return null;
  const row = payload[0]?.payload as Record<string, number>;
  return (
    <div className="chart-tooltip">
      <div className="t-title">{labelFormat(String(label))}</div>
      {series.map((s) => (
        <div className="t-row" key={s.key}>
          <span className="t-key" style={{ background: s.color }} />
          <strong>{s.format(row[s.key] ?? 0)}</strong> {s.label}
        </div>
      ))}
    </div>
  );
}

/** Columns over time. Several series stack (e.g. succeeded + failed calls), with a legend;
 * one series needs none (the card title names it). */
export function TimeBars({
  data,
  bucket,
  series,
  yFormat,
}: {
  data: Record<string, number | string>[];
  bucket: "hour" | "day";
  series: Series[];
  yFormat: (value: number) => string;
}) {
  const labelFormat = (l: string) => bucketLabel(l, bucket) + (bucket === "day" ? " (UTC)" : "");
  return (
    <>
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }} barCategoryGap={2}>
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis
            dataKey="start"
            {...AXIS}
            tickLine={false}
            minTickGap={24}
            tickFormatter={(v: string) => bucketLabel(v, bucket)}
          />
          <YAxis {...AXIS} tickLine={false} axisLine={false} width={56} tickFormatter={yFormat} />
          <Tooltip
            cursor={{ fill: "var(--hover)" }}
            content={(props) => (
              <SeriesTooltip {...(props as TooltipContentProps<number, string>)} series={series} labelFormat={labelFormat} />
            )}
          />
          {series.map((s, i) => (
            <Bar
              key={s.key}
              dataKey={s.key}
              stackId="a"
              fill={s.color}
              maxBarSize={24}
              radius={i === series.length - 1 ? BAR_RADIUS : 0}
              isAnimationActive={false}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
      {series.length > 1 && (
        <div className="legend">
          {series.map((s) => (
            <span key={s.key} style={{ ["--key" as string]: s.color }}>
              {s.label}
            </span>
          ))}
        </div>
      )}
    </>
  );
}

/** Horizontal bars ranking items by one measure, one hue, labeled at the bar end. */
export function RankBars({
  data,
  format,
  label,
}: {
  data: { name: string; value: number }[];
  format: (value: number) => string;
  label: string;
}) {
  const height = Math.max(80, data.length * 34 + 16);
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} layout="vertical" margin={{ top: 0, right: 64, left: 0, bottom: 0 }}>
        <XAxis type="number" hide />
        <YAxis type="category" dataKey="name" {...AXIS} tickLine={false} axisLine={false} width={150} />
        <Tooltip
          cursor={{ fill: "var(--hover)" }}
          content={({ active, payload }) =>
            active && payload?.[0] ? (
              <div className="chart-tooltip">
                <div className="t-title">{String(payload[0].payload.name)}</div>
                <strong>{format(Number(payload[0].value))}</strong> {label}
              </div>
            ) : null
          }
        />
        <Bar
          dataKey="value"
          fill="var(--series)"
          maxBarSize={20}
          radius={[0, 4, 4, 0]}
          isAnimationActive={false}
          label={{ position: "right", fill: "var(--text-secondary)", fontSize: 12, formatter: (v: unknown) => format(Number(v)) }}
        />
      </BarChart>
    </ResponsiveContainer>
  );
}
