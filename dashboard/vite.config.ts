/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dashboard calls the gateway's admin API (/admin/...) and client API (/gateway/...).
// In development, Vite's dev
// server forwards those requests to the gateway, so the browser only ever talks to one
// origin (no CORS setup needed). GATEWAY_URL overrides the default.
const gateway = process.env.GATEWAY_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/admin": gateway, "/gateway": gateway },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
  },
});
