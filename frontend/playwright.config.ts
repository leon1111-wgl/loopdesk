import { defineConfig } from "@playwright/test";

// End-to-end walkthroughs of the built console against the real API (FastAPI serves
// frontend/dist), with the offline model and a fresh SQLite database seeded with the
// synthetic demo tenants. Run `pnpm build` first, then `pnpm e2e`.
//   E2E_PYTHON   interpreter with the backend requirements (default: python3)
//   E2E_CHROME   use an installed Chrome instead of Playwright's bundled Chromium
const PORT = 8765;
const python = process.env.E2E_PYTHON ?? "python3";

export default defineConfig({
  testDir: "e2e",
  // The flows share one database and one handoff queue: run them in order.
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: process.env.E2E_CHROME ? { executablePath: process.env.E2E_CHROME } : {},
  },
  webServer: {
    command: `rm -f .e2e.db .e2e.db-wal .e2e.db-shm && ${python} -m uvicorn app.main:app --host 127.0.0.1 --port ${PORT}`,
    cwd: "../backend",
    url: `http://127.0.0.1:${PORT}/api/ready`,
    timeout: 120_000,
    reuseExistingServer: false,
    env: {
      LOOPDESK_DATABASE_URL: "sqlite:///./.e2e.db",
      LOOPDESK_LLM_PROVIDER: "mock",
      LOOPDESK_SEED_DEMO: "1",
      LOOPDESK_LOG_LEVEL: "WARNING",
      // No pre-seeded conversations: every queue and approval in the walkthrough is created by it.
      LOOPDESK_SEED_DEMO_TRAFFIC: "0",
      // The voice channel with its test engines (demo mode only): no speech models in CI.
      LOOPDESK_VOICE_ASR: "scripted",
      LOOPDESK_VOICE_TTS: "tone",
      LOOPDESK_VOICE_SCRIPTED_TEXT: "怎么取消自动续费",
    },
  },
});
