// Typed client for the gateway's admin API. Money arrives as exact decimal strings and
// times as ISO 8601 UTC; formatting happens at display time (format.ts).

export type Money = string;

export interface ModelRow {
  model: string | null;
  calls: number;
  spend_usd: Money;
  errors: number;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
}

export interface SeriesPoint {
  start: string;
  calls: number;
  spend_usd: Money;
  errors: number;
}

export interface Overview {
  calls: number;
  sessions: number;
  spend_usd: Money;
  /** Part of spend_usd from calls on clients' own credentials (API-equivalent, not paid). */
  api_equivalent_usd: Money;
  errors: number;
  error_rate: number | null;
  prompt_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  ttft_p50_ms: number | null;
  ttft_p95_ms: number | null;
  bucket: "hour" | "day";
  series: SeriesPoint[];
  by_model: ModelRow[];
}

export interface CategoryRow {
  category: string;
  calls: number;
  spend_usd: Money;
  errors: number;
  sessions: number;
}

export interface UserRow {
  key_id: string;
  name: string;
  key_prefix: string;
  revoked: boolean;
  calls: number;
  spend_usd: Money;
  errors: number;
  sessions: number;
  last_call_at: string;
}

export interface SessionRow {
  key_id: string;
  key_name: string;
  key_prefix: string;
  session_id: string;
  app: string | null;
  category: string;
  category_status: string | null;
  calls: number;
  calls_in_window: number;
  first_call_at: string;
  last_call_at: string;
  spend_usd: Money;
  api_equivalent_usd: Money;
  budget_usd: Money;
  budget_source: string;
  spend_fraction: string;
  peak_context_fraction: number | null;
  alerts: number;
}

export interface SessionPage {
  total: number;
  items: SessionRow[];
}

export interface AlertRow {
  id: string;
  created_at: string;
  api_key_id: string;
  session_id: string;
  kind: "spend" | "context";
  threshold: string;
  observed: string;
  request_id: string | null;
  detail: Record<string, unknown>;
}

export interface CategoryInfo {
  category: string | null;
  status: string;
  client_category: string | null;
  auto_category: string | null;
  auto_reason: string | null;
  classifier_model: string | null;
  prompt_version: string | null;
  classifier_cost_usd: Money | null;
  error: string | null;
  classified_calls: number;
  updated_at: string;
}

export interface SessionStatus {
  api_key_id: string;
  key_prefix: string;
  session_id: string;
  calls: number;
  spent_usd: Money;
  api_equivalent_usd: Money;
  budget_usd: Money;
  budget_source: string;
  spend_fraction: string;
  category: CategoryInfo | null;
  alerts: AlertRow[];
}

export interface CallRow {
  id: string;
  created_at: string;
  model: string | null;
  is_stream: boolean;
  status_code: number;
  error_type: string | null;
  stop_reason: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  context_tokens: number | null;
  context_window: number | null;
  cost_usd: Money | null;
  cost_note: string | null;
  latency_ms: number;
  ttft_ms: number | null;
  category_tag: string | null;
  app: string | null;
  billing: "gateway_key" | "client_credential";
  request_class: string | null;
}

export interface HandoffSummary {
  id: string;
  created_at: string;
  session_id: string;
  requested_by: string;
  status: "ready" | "error";
  error: string | null;
  session_calls: number;
  transcript_messages: number;
  omitted_blocks: number;
  model: string;
  effort: string;
  prompt_version: string;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: Money | null;
}

export interface Handoff extends HandoffSummary {
  content: Record<string, unknown> | null;
  markdown: string | null;
}

export interface KeyRow {
  id: string;
  key_prefix: string;
  name: string;
  created_at: string;
  revoked_at: string | null;
}

/** Thrown for 401: the admin key is missing or wrong. The app signs out on it. */
export class UnauthorizedError extends Error {}

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export type Query = Record<string, string | number | undefined>;

export async function request<T>(
  adminKey: string,
  path: string,
  { query, method = "GET", body }: { query?: Query; method?: string; body?: unknown } = {},
): Promise<T> {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== "") params.set(k, String(v));
  }
  const url = params.size ? `${path}?${params}` : path;
  const response = await fetch(url, {
    method,
    headers: {
      Authorization: `Bearer ${adminKey}`,
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (response.status === 401) throw new UnauthorizedError("invalid admin key");
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const detail = (await response.json()).detail;
      if (detail) message = typeof detail === "string" ? detail : JSON.stringify(detail);
    } catch {
      // not JSON: keep the status text
    }
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as T;
}

/** Download a text file from an authenticated endpoint. A plain <a href> can't send the
 * admin key header, so fetch it, then save it through a temporary object URL. */
export async function downloadText(adminKey: string, path: string, filename: string): Promise<void> {
  const response = await fetch(path, { headers: { Authorization: `Bearer ${adminKey}` } });
  if (response.status === 401) throw new UnauthorizedError("invalid admin key");
  if (!response.ok) throw new ApiError(response.status, `${response.status} ${response.statusText}`);
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
