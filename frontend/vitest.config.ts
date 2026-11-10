import { defineConfig } from "vitest/config";

// Unit tests of the static demo's browser engine; the Playwright walkthroughs in e2e/ run on their own.
// The engine reads backend/policies (the Cedar files the backend evaluates too), outside this folder.
export default defineConfig({
  server: { fs: { allow: [".."] } },
  test: { include: ["src/**/*.test.ts"], setupFiles: ["src/demo/engine/cedar.setup.ts"] },
});
