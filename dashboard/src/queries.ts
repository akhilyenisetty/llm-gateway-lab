import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

import {
  request,
  requestText,
  UnauthorizedError,
  type AlertRow,
  type CallRow,
  type CategoryRow,
  type Handoff,
  type HandoffSummary,
  type KeyRow,
  type Me,
  type Overview,
  type Query,
  type SessionPage,
  type SessionStatus,
  type UserRow,
} from "./api";
import { useAuth } from "./authContext";

export const RANGES = {
  "24h": { label: "24 hours", hours: 24 },
  "7d": { label: "7 days", hours: 24 * 7 },
  "30d": { label: "30 days", hours: 24 * 30 },
} as const;
export type RangeKey = keyof typeof RANGES;

const REFRESH_MS = 30_000;

/** Filters live in the URL (?range=7d&user=<key id>), so a view survives a refresh and
 * can be shared as a link. */
export function useFilters() {
  const [params, setParams] = useSearchParams();
  const rawRange = params.get("range");
  const range: RangeKey = rawRange && rawRange in RANGES ? (rawRange as RangeKey) : "7d";
  const userId = params.get("user") ?? "";

  const update = useCallback(
    (changes: Record<string, string>) =>
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          for (const [k, v] of Object.entries(changes)) {
            if (v) next.set(k, v);
            else next.delete(k);
          }
          return next;
        },
        { replace: true },
      ),
    [setParams],
  );
  return {
    range,
    userId,
    setRange: (value: RangeKey) => update({ range: value }),
    setUserId: (value: string) => update({ user: value }),
  };
}

/** The time window for a range, computed when a request is made (not when the query key
 * is built), so "last 24 hours" means up to now on every refresh. */
function windowFor(range: RangeKey): { from: string; to: string } {
  const to = new Date();
  const from = new Date(to.getTime() - RANGES[range].hours * 3_600_000);
  return { from: from.toISOString(), to: to.toISOString() };
}

function useAdminQuery<T>(
  path: string,
  query: Query,
  { windowed = true, enabled = true }: { windowed?: boolean; enabled?: boolean } = {},
): UseQueryResult<T> {
  const { apiKey, signOut } = useAuth();
  const { range, userId } = useFilters();
  const fullQuery = windowed ? { ...query, key_id: userId || undefined } : query;
  return useQuery({
    queryKey: [path, fullQuery, windowed ? range : null],
    queryFn: async () => {
      try {
        return await request<T>(apiKey ?? "", path, {
          query: windowed ? { ...fullQuery, ...windowFor(range) } : fullQuery,
        });
      } catch (error) {
        if (error instanceof UnauthorizedError) signOut();
        throw error;
      }
    },
    enabled: enabled && !!apiKey,
    refetchInterval: REFRESH_MS,
    placeholderData: keepPreviousData, // keep the last render while refetching
    retry: (count, error) => !(error instanceof UnauthorizedError) && count < 2,
  });
}

export const useOverview = () => useAdminQuery<Overview>("/admin/stats/overview", {});
export const useCategories = () => useAdminQuery<CategoryRow[]>("/admin/stats/categories", {});
export const useUsers = () => useAdminQuery<UserRow[]>("/admin/stats/users", {});
export const useKeys = (enabled = true) =>
  useAdminQuery<KeyRow[]>("/admin/keys", {}, { windowed: false, enabled });
export const useAlerts = (limit = 200) =>
  useAdminQuery<AlertRow[]>("/admin/alerts", { limit }, { windowed: false });

export function useSessions(query: { category?: string; sort?: string; limit: number; offset: number }) {
  return useAdminQuery<SessionPage>("/admin/sessions", query);
}

/** Where one session's data lives. Admins reach any session through the admin API; a
 * user reaches only their own, through the client API (scoped to their key). */
export interface SessionApi {
  base: string;
  markdownPath: (handoffId: string) => string;
  canCategorize: boolean;
  /** Query key of the list page to refresh after a change. */
  listPath: string;
}

export function adminSessionApi(keyId: string, sessionId: string): SessionApi {
  return {
    base: `/admin/keys/${keyId}/sessions/${encodeURIComponent(sessionId)}`,
    markdownPath: (id) => `/admin/handoffs/${id}/markdown`,
    canCategorize: true,
    listPath: "/admin/sessions",
  };
}

export function userSessionApi(sessionId: string): SessionApi {
  const base = `/gateway/sessions/${encodeURIComponent(sessionId)}`;
  return {
    base,
    markdownPath: (id) => `${base}/handoffs/${id}/markdown`,
    canCategorize: false,
    listPath: "/gateway/usage/sessions",
  };
}

export const useSessionStatus = (api: SessionApi) =>
  useAdminQuery<SessionStatus>(api.base, {}, { windowed: false });

export const useSessionCalls = (api: SessionApi) =>
  useAdminQuery<CallRow[]>(`${api.base}/calls`, {}, { windowed: false });

export const useHandoffs = (api: SessionApi) =>
  useAdminQuery<HandoffSummary[]>(`${api.base}/handoffs`, {}, { windowed: false });

/** One handoff's Markdown (fetched only when shown). */
export function useHandoffMarkdown(api: SessionApi, id: string | undefined, enabled: boolean) {
  const { apiKey } = useAuth();
  return useQuery({
    queryKey: ["handoff-markdown", id],
    queryFn: () => requestText(apiKey ?? "", api.markdownPath(id ?? "")),
    enabled: enabled && !!id && !!apiKey,
    staleTime: Infinity, // a stored handoff never changes
  });
}

// The signed-in user's own data (role "user").
export const useMe = () => useAdminQuery<Me>("/gateway/me", {}, { windowed: false });
export const useMyUsage = () => useAdminQuery<Overview>("/gateway/usage", {});
export function useMySessions(query: { sort?: string; limit: number; offset: number }) {
  return useAdminQuery<SessionPage>("/gateway/usage/sessions", query);
}

/** Actions on one session: set its budget, categorize it now (admin), write a handoff.
 * Each refreshes the session's data and the list it belongs to. */
export function useSessionActions(api: SessionApi) {
  const { apiKey } = useAuth();
  const client = useQueryClient();
  const refresh = () =>
    client.invalidateQueries({
      predicate: (q) => String(q.queryKey[0]).startsWith(api.base) || q.queryKey[0] === api.listPath,
    });
  const setBudget = useMutation({
    mutationFn: (budgetUsd: string) =>
      request<SessionStatus>(apiKey ?? "", `${api.base}/budget`, {
        method: "PUT",
        body: { budget_usd: budgetUsd },
      }),
    onSuccess: refresh,
  });
  const categorize = useMutation({
    mutationFn: () => request<SessionStatus>(apiKey ?? "", `${api.base}/categorize`, { method: "POST" }),
    onSuccess: refresh,
  });
  const generateHandoff = useMutation({
    mutationFn: () => request<Handoff>(apiKey ?? "", `${api.base}/handoffs`, { method: "POST" }),
    onSuccess: refresh,
  });
  return useMemo(
    () => ({ setBudget, categorize, generateHandoff }),
    [setBudget, categorize, generateHandoff],
  );
}
