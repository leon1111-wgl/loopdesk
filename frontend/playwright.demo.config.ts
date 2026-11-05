import { defineConfig } from "@playwright/test";

// The static demo as GitHub Pages serves it: replay mode, under /loopdesk/, no backend behind it. Build it first:
//   VITE_DEMO=1 VITE_BASE=/loopdesk/ pnpm build && mkdir -p dist/demo && cp ../demo/real-model/fixtures.json dist/demo/fixtures.json
//   pnpm e2e:demo
//   DEMO_URL opens a published site instead, and nothing is started here.
const PORT = 8766;

export default defineConfig({
  testDir: "e2e-demo",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: process.env.DEMO_URL ?? `http://127.0.0.1:${PORT}/loopdesk/`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: process.env.E2E_CHROME ? { executablePath: process.env.E2E_CHROME } : {},
  },
  webServer: process.env.DEMO_URL ? undefined : {
    command: `pnpm exec vite preview --port ${PORT} --strictPort --host 127.0.0.1 --base /loopdesk/`,
    url: `http://127.0.0.1:${PORT}/loopdesk/`,
    timeout: 60_000,
    reuseExistingServer: false,
    env: { VITE_BASE: "/loopdesk/" },
  },
});
