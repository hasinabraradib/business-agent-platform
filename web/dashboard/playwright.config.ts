import { defineConfig } from "@playwright/test";

/** End-to-end tests and screenshots against the e2e stack (scripts/e2e-stack.sh up): the real
 * dashboard and API in Docker Compose, with the offline chat model and embeddings. */
export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  fullyParallel: false, // one shared demo tenant: run in order
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL: process.env.DASHBOARD_URL ?? "http://localhost:13001",
    channel: "chromium", // the full browser Playwright already has (no separate headless shell)
    trace: "retain-on-failure",
    viewport: { width: 1360, height: 900 },
  },
  projects: [
    { name: "e2e", testIgnore: /screenshots\.spec\.ts/ },
    { name: "screenshots", testMatch: /screenshots\.spec\.ts/ },
  ],
});
