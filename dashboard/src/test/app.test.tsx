import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { App } from "../App";
import { AuthProvider } from "../auth";

const OVERVIEW = {
  calls: 6, sessions: 3, spend_usd: "0.47", errors: 1, error_rate: 1 / 6,
  prompt_tokens: 500, output_tokens: 50, cache_read_tokens: 0,
  latency_p50_ms: 350, latency_p95_ms: 575, ttft_p50_ms: 100, ttft_p95_ms: 145,
  bucket: "day",
  series: [{ start: "2026-10-01T00:00:00Z", calls: 6, spend_usd: "0.47", errors: 1 }],
  by_model: [{ model: "claude-haiku-5-5", calls: 1, spend_usd: "0.4", errors: 0, latency_p50_ms: 400, latency_p95_ms: 400 }],
};

const ME = { name: "alice", key_prefix: "gw_alice12", created_at: "2026-10-01T00:00:00Z" };
const MY_SESSIONS = { total: 0, items: [] };

/** A fake gateway: the admin key opens /admin/..., a user key opens /gateway/... only. */
function mockApi(adminKey: string, userKey = "gw_user-key") {
  return vi.fn(async (url: string, init?: RequestInit) => {
    const auth = (init?.headers as Record<string, string>)?.Authorization;
    const path = url.split("?")[0] ?? "";
    const isAdmin = auth === `Bearer ${adminKey}`;
    const isUser = auth === `Bearer ${userKey}`;
    if (path.startsWith("/admin/") && !isAdmin) return new Response("{}", { status: 401 });
    if (path.startsWith("/gateway/") && !isUser) return new Response("{}", { status: 401 });
    const bodies: Record<string, unknown> = {
      "/admin/stats/overview": OVERVIEW,
      "/gateway/usage": OVERVIEW,
      "/gateway/me": ME,
      "/gateway/usage/sessions": MY_SESSIONS,
    };
    return new Response(JSON.stringify(bodies[path] ?? []), { status: 200 });
  });
}

function renderApp() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <MemoryRouter>
          <App />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

const KEY = "gateway-dashboard-key";
const ROLE = "gateway-dashboard-role";

beforeEach(() => sessionStorage.clear());
afterEach(() => vi.unstubAllGlobals());

it("rejects a wrong admin key and stays on sign-in", async () => {
  vi.stubGlobal("fetch", mockApi("right-key"));
  renderApp();
  await userEvent.type(screen.getByLabelText("Admin key or your gateway key"), "wrong-key");
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("rejected");
  expect(sessionStorage.getItem(KEY)).toBeNull();
});

it("signs in and shows the overview tiles from the API", async () => {
  const fetchMock = mockApi("right-key");
  vi.stubGlobal("fetch", fetchMock);
  renderApp();
  await userEvent.type(screen.getByLabelText("Admin key or your gateway key"), "right-key");
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

  expect(await screen.findByText("$0.470")).toBeInTheDocument(); // spend tile
  expect(screen.getByText("16.7%")).toBeInTheDocument(); // error rate
  expect(screen.getByText("350 ms")).toBeInTheDocument(); // p50 latency
  expect(sessionStorage.getItem(KEY)).toBe("right-key");

  // The overview request carries a time window and the admin key.
  const call = fetchMock.mock.calls.find(([url]) => String(url).startsWith("/admin/stats/overview"));
  expect(String(call?.[0])).toMatch(/from=.*&to=/);
});

it("signs out when the key stops working (401)", async () => {
  sessionStorage.setItem(KEY, "revoked-key");
  sessionStorage.setItem(ROLE, "admin");
  vi.stubGlobal("fetch", mockApi("right-key"));
  renderApp();
  await waitFor(() => expect(screen.getByLabelText("Admin key or your gateway key")).toBeInTheDocument());
  expect(sessionStorage.getItem(KEY)).toBeNull();
});

it("a gateway key opens My usage and never touches the admin API", async () => {
  const fetchMock = mockApi("admin-key", "gw_user-key");
  vi.stubGlobal("fetch", fetchMock);
  renderApp();
  await userEvent.type(screen.getByLabelText("Admin key or your gateway key"), "gw_user-key");
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

  const heading = await screen.findByRole("heading", { name: /My usage/ });
  await waitFor(() => expect(heading).toHaveTextContent("alice")); // from /gateway/me
  expect(screen.queryByText("Overview")).not.toBeInTheDocument(); // no admin pages
  expect(sessionStorage.getItem(ROLE)).toBe("user");
  await screen.findByText("$0.470"); // usage tiles from /gateway/usage

  // Only the sign-in check probes /admin/keys; after that, nothing goes to /admin/.
  const adminCalls = fetchMock.mock.calls.map(([url]) => String(url)).filter((u) => u.startsWith("/admin/"));
  expect(adminCalls).toEqual(["/admin/keys"]);
  expect(sessionStorage.getItem(KEY)).toBe("gw_user-key"); // still signed in
});

it("the admin key still opens the full dashboard", async () => {
  vi.stubGlobal("fetch", mockApi("admin-key"));
  renderApp();
  await userEvent.type(screen.getByLabelText("Admin key or your gateway key"), "admin-key");
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
  expect(await screen.findByRole("link", { name: "Users" })).toBeInTheDocument();
  expect(sessionStorage.getItem(ROLE)).toBe("admin");
});
