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

// A software GPU has no 16-bit floats, and the shared weights need them. WEBGPU_F32 serves the same model's 32-bit build from its
// upstream for this run only, under the shared URLs, so the path (click, worker, load, schema, validation, answer) still runs for real.
const F32_MODEL = "https://huggingface.co/mlc-ai/Qwen3-0.6B-q4f32_1-MLC/resolve/main/";
const F32_LIB = "https://raw.githubusercontent.com/mlc-ai/binary-mlc-llm-libs/main/web-llm-models/v0_2_86/base/Qwen3-0.6B-q4f32_1_cs1k-webgpu.wasm";

test("click to load, then a question answered by the model", async ({ page, context }) => {
  const report: Record<string, unknown> = { url: process.env.DEMO_URL || "this commit's build", started: new Date().toISOString() };
  const save = () => writeFileSync(REPORT, JSON.stringify(report, null, 1));
  const messages: string[] = [];
  if (process.env.WEBGPU_F32) {
    report.weights = "q4f32 build of the same model, served under the shared URLs for this run";
    await context.route("**/career-atlas/models/qwen3/resolve/main/**", async (route) => {
      const file = route.request().url().split("/resolve/main/")[1];
      await route.fulfill({ response: await route.fetch({ url: F32_MODEL + file, timeout: 600_000 }), headers: { "access-control-allow-origin": "*" } });
    });
    await context.route("**/career-atlas/vendor/qwen3-0.6b-webgpu.wasm", async (route) => {
      await route.fulfill({ response: await route.fetch({ url: F32_LIB, timeout: 300_000 }), headers: { "access-control-allow-origin": "*", "content-type": "application/wasm" } });
    });
    await page.addInitScript(() => {
      const gpu = (navigator as unknown as { gpu: { requestAdapter(...a: unknown[]): Promise<{ features: Set<string> }> } }).gpu;
      const request = gpu.requestAdapter.bind(gpu);
      gpu.requestAdapter = async (...a: unknown[]) => {
        const adapter = await request(...a);
        const features = new Set([...adapter.features, "shader-f16"]);
        return new Proxy(adapter, { get: (t, k) => (k === "features" ? features : Reflect.get(t, k)) });
      };
    });
  }
  // A software GPU is far slower than any real one: this run waits longer than a visitor's page would.
  await page.addInitScript(() => {
    const later = window.setTimeout.bind(window);
    window.setTimeout = ((run: () => void, ms?: number) => later(run, ms === 120_000 ? 3_000_000 : ms)) as typeof window.setTimeout;
  });
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
    await page.getByRole("button", { name: /agent@leon\.demo/ }).first().click(); // staff see the model's choice in the side panel
    await expect(page.getByRole("button", { name: "账号菜单" })).toBeVisible();
    await page.goto("./chat");
    const load = page.getByRole("button", { name: "加载模型" });
    report.offered = await load.waitFor({ timeout: 15_000 }).then(() => true, () => false);
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
    await box.fill("拍下之后大概多久会发货呀"); // the rules would refuse this, so the model is asked
    await box.press("Enter");
    const answer = page.locator(".msg.assistant").last();
    await expect(answer.locator(".engine-tag")).toBeVisible({ timeout: 3_000_000 });
    report.answer_seconds = Math.round((Date.now() - asked) / 1000);
    report.answer = await answer.innerText();
    report.engine = await answer.locator(".engine-tag").innerText();
    report.model_events = await page.locator(".event", { hasText: "浏览器模型的选择" }).allInnerTexts();
  } finally {
    report.console_errors = messages.slice(0, 10);
    report.finished = new Date().toISOString();
    save();
  }
});
