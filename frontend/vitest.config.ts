import { defineConfig } from "vitest/config";

// Unit tests of the static demo's browser engine; the Playwright walkthroughs in e2e/ run on their own.
export default defineConfig({
  test: { include: ["src/**/*.test.ts"] },
});
