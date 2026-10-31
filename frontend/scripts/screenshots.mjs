// The README screenshots, taken from the static demo (the console replaying a recording; no server,
// no model calls), so they show exactly what the published demo shows.
//
//   VITE_DEMO=1 pnpm build --outDir dist-demo && cp <fixtures.json> dist-demo/demo/fixtures.json
//   node scripts/screenshots.mjs dist-demo ../docs/assets          # E2E_CHROME: an installed Chrome
import { chromium } from "@playwright/test";
import { createReadStream, existsSync, statSync } from "node:fs";
import http from "node:http";
import path from "node:path";

const [root = "dist-demo", out = "../docs/assets", only] = process.argv.slice(2);
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml" };

const server = http.createServer((req, res) => {
  const url = decodeURIComponent(new URL(req.url, "http://x").pathname);
  let file = path.join(root, url);
  if (!file.startsWith(path.resolve(root)) && !path.resolve(file).startsWith(path.resolve(root))) return res.writeHead(403).end();
  if (!existsSync(file) || statSync(file).isDirectory()) file = path.join(root, "index.html"); // deep links
  res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] ?? "application/octet-stream" });
  createReadStream(file).pipe(res);
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch(process.env.E2E_CHROME ? { executablePath: process.env.E2E_CHROME } : {});

// The console opens in its dark theme; light: true picks the light one, as the theme toggle would.
async function signedIn(email, { mobile = false, light = false } = {}) {
  const context = await browser.newContext({
    viewport: mobile ? { width: 390, height: 844 } : { width: 1440, height: 900 },
    deviceScaleFactor: mobile ? 2 : 1, colorScheme: light ? "light" : "dark", locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
  });
  if (light) await context.addInitScript(() => localStorage.setItem("loopdesk.theme", "light"));
  const page = await context.newPage();
  page.on("pageerror", (err) => console.error(`page error: ${err.message}`));
  await page.goto(base + "/");
  await page.getByRole("button", { name: new RegExp(email.replace(".", "\\.")) }).click();
  await page.waitForURL(/\/(dashboard|chat)/);
  return page;
}

async function shot(page, name) {
  await page.waitForTimeout(600); // fonts, the last replayed event
  await page.screenshot({ path: path.join(out, name) });
  console.log(`wrote ${path.join(out, name)}`);
}

async function ask(page, question) {
  await page.getByPlaceholder("输入问题，Enter 发送，Shift+Enter 换行").fill(question);
  await page.getByRole("button", { name: "发送" }).click();
  await page.getByRole("button", { name: "发送" }).waitFor();
  await page.waitForFunction(() => !document.querySelector(".typing, .streaming"), null, { timeout: 30_000 }).catch(() => {});
  await page.waitForTimeout(4000);
}

const SHOTS = {
  "login.png": async () => {
    // The intro page: the floating bar, the headline and the example run.
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: "dark", locale: "zh-CN" });
    const page = await context.newPage();
    await page.goto(base + "/");
    await page.getByRole("button", { name: /admin@echo.demo/ }).waitFor();
    await shot(page, "login.png");
    await context.close();
  },
  // The README's first picture: a knowledge answer with its citations, the evidence check and, for
  // staff, the run on the right (model, tokens).
  "chat-real-model.png": async () => {
    const page = await signedIn("admin@echo.demo");
    await page.goto(base + "/chat");
    await ask(page, "怎么取消自动续费？");
    await shot(page, "chat-real-model.png");
  },
  "approvals.png": async () => {
    const page = await signedIn("admin@echo.demo");
    await page.goto(base + "/approvals");
    // the evidence package opens in a side sheet
    await page.getByRole("button", { name: /审核|查看/ }).first().click();
    await page.getByRole("dialog").waitFor();
    await shot(page, "approvals.png");
  },
  "trace.png": async () => {
    const page = await signedIn("admin@echo.demo");
    await page.goto(base + "/traces");
    // the member's own request, which paused for an agent's approval
    await page.locator("tbody tr").filter({ hasText: "年卡" }).filter({ hasText: "审批" }).first().click();
    await shot(page, "trace.png");
  },
  "evals.png": async () => {
    const page = await signedIn("admin@echo.demo");
    await page.goto(base + "/evals");
    await shot(page, "evals.png");
  },
  "handoffs.png": async () => {
    const page = await signedIn("agent@echo.demo");
    await page.goto(base + "/handoffs");
    await shot(page, "handoffs.png");
  },
  "handoff-customer.png": async () => {
    const page = await signedIn("customer2@echo.demo");
    await page.goto(base + "/chat");
    await page.getByText("退款一直不到账").first().click();
    await shot(page, "handoff-customer.png");
  },
  "knowledge-conflicts.png": async () => {
    const page = await signedIn("admin@echo.demo");
    await page.goto(base + "/knowledge?tab=conflicts");
    await shot(page, "knowledge-conflicts.png");
  },
  "audit.png": async () => {
    const page = await signedIn("admin@echo.demo");
    await page.goto(base + "/audit");
    await shot(page, "audit.png");
  },
  "dashboard.png": async () => {
    const page = await signedIn("admin@echo.demo");
    await page.goto(base + "/dashboard");
    await shot(page, "dashboard.png");
  },
  "dashboard-light.png": async () => {
    const page = await signedIn("admin@echo.demo", { light: true });
    await page.goto(base + "/dashboard");
    await shot(page, "dashboard-light.png");
  },
  "mobile-approvals.png": async () => {
    const page = await signedIn("admin@echo.demo", { mobile: true });
    await page.goto(base + "/approvals");
    await page.getByRole("button", { name: /审核|查看/ }).first().click();
    await page.getByRole("dialog").waitFor();
    await shot(page, "mobile-approvals.png");
  },
  "mobile-nav.png": async () => {
    const page = await signedIn("admin@echo.demo", { mobile: true });
    await page.goto(base + "/dashboard");  // the section tabs as a bottom tab bar
    await shot(page, "mobile-nav.png");
  },
};

try {
  for (const [name, take] of Object.entries(SHOTS)) {
    if (!only || only.split(",").includes(name)) await take();
  }
} finally {
  await browser.close();
  server.close();
}
