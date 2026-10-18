// Display formatting. Values arrive exact (money as decimal strings); rounding happens
// only here, at the last moment.

export function usd(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const n = typeof value === "string" ? Number(value) : value;
  if (n === 0) return "$0";
  const abs = Math.abs(n);
  // Below the smallest shown digit, say so instead of printing a misleading $0.0000.
  if (abs < 0.0001) return n > 0 ? "<$0.0001" : ">-$0.0001";
  // Small amounts need more digits: a $0.0003 classification shouldn't show as $0.00.
  const digits = abs >= 100 ? 0 : abs >= 1 ? 2 : abs >= 0.01 ? 3 : 4;
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

export function compact(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString("en-US", { notation: "compact", maximumFractionDigits: 1 });
}

export function int(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : n.toLocaleString("en-US");
}

export function pct(fraction: string | number | null | undefined, digits = 1): string {
  if (fraction === null || fraction === undefined) return "—";
  const n = typeof fraction === "string" ? Number(fraction) : fraction;
  return `${(n * 100).toFixed(digits)}%`;
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Axis/tooltip label for a time bucket. Day buckets are UTC days, so they're labeled in
 * UTC (labeling them in local time would shift the date west of UTC). */
export function bucketLabel(iso: string, bucket: "hour" | "day"): string {
  const d = new Date(iso);
  return bucket === "day"
    ? d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" })
    : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/** "1 failed call", "3 failed calls". */
export function plural(n: number, word: string): string {
  return `${int(n)} ${word}${n === 1 ? "" : "s"}`;
}

export function shortId(id: string, length = 14): string {
  return id.length > length ? `${id.slice(0, length)}…` : id;
}
