import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  // GitHub Pages serves the static demo under /loopdesk/ (VITE_BASE, set by .github/workflows/pages.yml).
  base: process.env.VITE_BASE ?? "/",
  plugins: [react()],
  // The policy (backend/policies/*.cedar) and the simulation cards live outside this folder.
  optimizeDeps: { exclude: ["@cedar-policy/cedar-wasm"] },  // keeps its new URL("…wasm", import.meta.url) intact in dev
  server: {
    fs: { allow: [".."] },
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", ws: true } },  // ws: the voice channel
  },
});
