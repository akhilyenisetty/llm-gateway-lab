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

function mockApi(validKey: string) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    const auth = (init?.headers as Record<string, string>)?.Authorization;
    if (auth !== `Bearer ${validKey}`) return new Response("{}", { status: 401 });
    const path = url.split("?")[0];
    const body = path === "/admin/stats/overview" ? OVERVIEW : [];
    return new Response(JSON.stringify(body), { status: 200 });
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

beforeEach(() => sessionStorage.clear());
afterEach(() => vi.unstubAllGlobals());

it("rejects a wrong admin key and stays on sign-in", async () => {
  vi.stubGlobal("fetch", mockApi("right-key"));
  renderApp();
  await userEvent.type(screen.getByLabelText("Admin key"), "wrong-key");
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("rejected");
  expect(sessionStorage.getItem("gateway-admin-key")).toBeNull();
});

it("signs in and shows the overview tiles from the API", async () => {
  const fetchMock = mockApi("right-key");
  vi.stubGlobal("fetch", fetchMock);
  renderApp();
  await userEvent.type(screen.getByLabelText("Admin key"), "right-key");
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

  expect(await screen.findByText("$0.470")).toBeInTheDocument(); // spend tile
  expect(screen.getByText("16.7%")).toBeInTheDocument(); // error rate
  expect(screen.getByText("350 ms")).toBeInTheDocument(); // p50 latency
  expect(sessionStorage.getItem("gateway-admin-key")).toBe("right-key");

  // The overview request carries a time window and the admin key.
  const call = fetchMock.mock.calls.find(([url]) => String(url).startsWith("/admin/stats/overview"));
  expect(String(call?.[0])).toMatch(/from=.*&to=/);
});

it("signs out when the key stops working (401)", async () => {
  sessionStorage.setItem("gateway-admin-key", "revoked-key");
  vi.stubGlobal("fetch", mockApi("right-key"));
  renderApp();
  await waitFor(() => expect(screen.getByLabelText("Admin key")).toBeInTheDocument());
  expect(sessionStorage.getItem("gateway-admin-key")).toBeNull();
});
