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
  UnauthorizedError,
  type AlertRow,
  type CallRow,
  type CategoryRow,
  type KeyRow,
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
  const { adminKey, signOut } = useAuth();
  const { range, userId } = useFilters();
  const fullQuery = windowed ? { ...query, key_id: userId || undefined } : query;
  return useQuery({
    queryKey: [path, fullQuery, windowed ? range : null],
    queryFn: async () => {
      try {
        return await request<T>(adminKey ?? "", path, {
          query: windowed ? { ...fullQuery, ...windowFor(range) } : fullQuery,
        });
      } catch (error) {
        if (error instanceof UnauthorizedError) signOut();
        throw error;
      }
    },
    enabled: enabled && !!adminKey,
    refetchInterval: REFRESH_MS,
    placeholderData: keepPreviousData, // keep the last render while refetching
    retry: (count, error) => !(error instanceof UnauthorizedError) && count < 2,
  });
}

export const useOverview = () => useAdminQuery<Overview>("/admin/stats/overview", {});
export const useCategories = () => useAdminQuery<CategoryRow[]>("/admin/stats/categories", {});
export const useUsers = () => useAdminQuery<UserRow[]>("/admin/stats/users", {});
export const useKeys = () => useAdminQuery<KeyRow[]>("/admin/keys", {}, { windowed: false });
export const useAlerts = (limit = 200) =>
  useAdminQuery<AlertRow[]>("/admin/alerts", { limit }, { windowed: false });

export function useSessions(query: { category?: string; sort?: string; limit: number; offset: number }) {
  return useAdminQuery<SessionPage>("/admin/sessions", query);
}

export function sessionPath(keyId: string, sessionId: string): string {
  return `/admin/keys/${keyId}/sessions/${encodeURIComponent(sessionId)}`;
}

export const useSessionStatus = (keyId: string, sessionId: string) =>
  useAdminQuery<SessionStatus>(sessionPath(keyId, sessionId), {}, { windowed: false });

export const useSessionCalls = (keyId: string, sessionId: string) =>
  useAdminQuery<CallRow[]>(`${sessionPath(keyId, sessionId)}/calls`, {}, { windowed: false });

/** Actions on one session: set its budget, or categorize it now. Both refresh the
 * session's data on success. */
export function useSessionActions(keyId: string, sessionId: string) {
  const { adminKey } = useAuth();
  const client = useQueryClient();
  const base = sessionPath(keyId, sessionId);
  const refresh = () => client.invalidateQueries({ predicate: (q) => String(q.queryKey[0]).startsWith(base) || q.queryKey[0] === "/admin/sessions" });
  const setBudget = useMutation({
    mutationFn: (budgetUsd: string) =>
      request<SessionStatus>(adminKey ?? "", `${base}/budget`, {
        method: "PUT",
        body: { budget_usd: budgetUsd },
      }),
    onSuccess: refresh,
  });
  const categorize = useMutation({
    mutationFn: () => request<SessionStatus>(adminKey ?? "", `${base}/categorize`, { method: "POST" }),
    onSuccess: refresh,
  });
  return useMemo(() => ({ setBudget, categorize }), [setBudget, categorize]);
}
