import { writeFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

// The real thing, on GitHub Actions only (.github/workflows/tier2-eval.yml): a production build, a real download of the shared
// weights, real inference on whatever adapter the browser has. A runner has only a software GPU, so this says what happened
// rather than pretending: the report is the result, whether it got as far as an answer or not.
const REPORT = "webgpu-report.json";

test.use({
  launchOptions: { args: ["--enable-unsafe-webgpu", "--enable-features=Vulkan", "--use-vulkan=swiftshader", "--use-webgpu-adapter=swiftshader", "--ignore-gpu-blocklist"] },
});

test.skip(!process.env.REAL_WEBGPU, "real inference runs on GitHub Actions only");

test("click to load, then a question answered by the model", async ({ page }) => {
  const report: Record<string, unknown> = { url: process.env.DEMO_URL || "this commit's build", started: new Date().toISOString() };
  const save = () => writeFileSync(REPORT, JSON.stringify(report, null, 1));
  const messages: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") messages.push(m.text().slice(0, 300)); });
  try {
    await page.goto("./");
    report.adapter = await page.evaluate(async () => {
      const gpu = (navigator as unknown as { gpu?: { requestAdapter(): Promise<{ features: Set<string>; info?: Record<string, string> } | null> } }).gpu;
      if (!gpu) return { available: false };
      const adapter = await gpu.requestAdapter();
      return adapter ? { available: true, features: [...adapter.features], vendor: adapter.info?.vendor, architecture: adapter.info?.architecture } : { available: false };
    });
    save();
    await page.getByRole("button", { name: /customer@leon\.demo/ }).first().click();
    await expect(page.getByRole("button", { name: "账号菜单" })).toBeVisible();
    await page.goto("./chat");
    const load = page.getByRole("button", { name: "加载模型" });
    report.offered = await load.isVisible({ timeout: 15_000 }).catch(() => false);
    save();
    if (!report.offered) return;
    const started = Date.now();
    await load.click();
    await expect(page.getByRole("button", { name: "卸载模型" }).or(page.getByRole("button", { name: "重试" }))).toBeVisible({ timeout: 1_800_000 });
    report.load_seconds = Math.round((Date.now() - started) / 1000);
    report.bar = await page.locator(".demo-engine").innerText();
    save();
    if (await page.getByRole("button", { name: "重试" }).isVisible()) return;
    const asked = Date.now();
    const box = page.locator("textarea").first();
    await box.fill("退货的时候快递费谁出啊");
    await box.press("Enter");
    const answer = page.locator(".msg.assistant").last();
    await expect(answer.locator(".engine-tag")).toBeVisible({ timeout: 1_500_000 });
    report.answer_seconds = Math.round((Date.now() - asked) / 1000);
    report.answer = await answer.innerText();
    report.engine = await answer.locator(".engine-tag").innerText();
  } finally {
    report.console_errors = messages.slice(0, 10);
    report.finished = new Date().toISOString();
    save();
  }
});
