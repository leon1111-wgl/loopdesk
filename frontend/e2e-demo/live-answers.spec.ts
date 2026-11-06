import { expect, test, type Page } from "@playwright/test";

// A visitor types what nobody recorded, on the static demo, with no server behind it. Every page also fails on a console
// error, and nothing may reach for the model's weights before a click.

const SIZES = [
  { name: "desktop", viewport: { width: 1280, height: 800 } },
  { name: "phone", viewport: { width: 375, height: 812 } },
];
const MODEL_REQUEST = /career-atlas\/(models|vendor)/;

async function noWebGpu(page: Page) {
  await page.addInitScript(() => Object.defineProperty(navigator, "gpu", { value: undefined, configurable: true }));
}

async function signIn(page: Page, email: string) {
  const problems: string[] = [];
  page.on("pageerror", (error) => problems.push(error.message));
  await page.goto("./");
  await page.getByRole("button", { name: new RegExp(email.replace(".", "\\.")) }).first().click();
  await expect(page.getByRole("button", { name: "账号菜单" })).toBeVisible();
  return problems;
}

async function ask(page: Page, text: string) {
  const box = page.getByRole("textbox", { name: /输入|消息/ }).or(page.locator("textarea")).first();
  await box.fill(text);
  await box.press("Enter");
}

async function switchAccount(page: Page, email: string) {
  await page.getByRole("button", { name: "账号菜单" }).click();
  await page.getByRole("button", { name: "退出登录" }).click();
  await page.getByRole("button", { name: new RegExp(email.replace(".", "\\.")) }).first().click();
  await expect(page.getByRole("button", { name: "账号菜单" })).toBeVisible();
}

const lastAnswer = (page: Page) => page.locator(".msg.assistant").last();

for (const size of SIZES) {
  test.describe(size.name, () => {
    test.use({ viewport: size.viewport });

    test("a question nobody recorded gets an answer with its source and engine", async ({ page }) => {
      await noWebGpu(page);
      const models: string[] = [];
      page.on("request", (request) => { if (MODEL_REQUEST.test(request.url())) models.push(request.url()); });
      const problems = await signIn(page, "customer@leon.demo");
      await page.goto("./chat");
      await ask(page, "退货的时候快递费谁出啊");
      await expect(lastAnswer(page)).toContainText("运费", { timeout: 20_000 });
      await expect(lastAnswer(page).locator(".cite-chip").first()).toBeVisible();
      await expect(lastAnswer(page).locator(".engine-tag")).toHaveText(/规则引擎/);
      await expect(page.locator(".demo-engine")).toHaveCount(0); // no WebGPU in this browser: nothing is offered
      const scroll = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(scroll).toBeLessThanOrEqual(0);
      expect(models).toEqual([]);
      expect(problems).toEqual([]);
    });

    test("order status comes from the tools", async ({ page }) => {
      await signIn(page, "customer@leon.demo");
      await page.goto("./chat");
      await ask(page, "我那个扫地机器人现在到哪儿了");
      await expect(lastAnswer(page)).toContainText("AT1402938475", { timeout: 20_000 });
      await expect(lastAnswer(page).locator(".engine-tag")).toHaveText(/规则引擎/);
    });
  });
}

test("a scripted question still replays the recording, and says so", async ({ page }) => {
  await signIn(page, "customer@leon.demo");
  await page.goto("./chat");
  await page.getByRole("button", { name: "退款多久能到账？" }).click();
  await expect(lastAnswer(page).locator(".engine-tag")).toHaveText(/录制/, { timeout: 20_000 });
});

test("a refund request goes to review, and an agent's decision reaches the customer, all in this tab", async ({ page }) => {
  await signIn(page, "customer@leon.demo");
  await page.goto("./chat");
  await ask(page, "SO20261012003 不想要了帮我退掉吧");
  await expect(lastAnswer(page)).toContainText("（模拟）", { timeout: 20_000 });
  await expect(lastAnswer(page)).toContainText("已提交人工审核");

  // Sign in as the agent in the same tab: the simulated copy lives as long as the page does.
  await switchAccount(page, "agent@leon.demo");
  await page.getByRole("link", { name: /审批/ }).first().click();
  const row = page.locator("tr", { hasText: "模拟" }).first();
  await expect(row).toContainText("SO20261012003");
  await row.click();
  await page.getByRole("button", { name: "通过并执行" }).click();
  await page.getByRole("dialog", { name: "确认通过并退款" }).getByRole("button", { name: "确认退款 ¥268.00" }).click();
  await expect(page.getByText(/审批单 #\d+ 已通过并执行/)).toBeVisible();

  await switchAccount(page, "customer@leon.demo");
  await page.locator(".convo-item", { hasText: "SO20261012003 不想要了" }).first().click();
  await expect(lastAnswer(page)).toContainText("已受理");
  await expect(lastAnswer(page)).toContainText("（模拟）");
});

test("when the engine cannot load, the visitor is told and the recordings still play", async ({ page }) => {
  await noWebGpu(page);
  await page.route("**/assets/retrieval-*.js", (route) => route.abort()); // the chunk that carries the rules and data
  await page.goto("./");
  await page.getByRole("button", { name: /customer@leon\.demo/ }).first().click();
  await expect(page.getByRole("button", { name: "账号菜单" })).toBeVisible();
  await page.goto("./chat");
  await ask(page, "退货的时候快递费谁出啊");
  await expect(lastAnswer(page)).toContainText("没能加载", { timeout: 20_000 });
  await expect(lastAnswer(page).locator(".engine-tag")).toHaveText("未作答");
  await page.goto("./chat");
  await page.getByRole("button", { name: "退款多久能到账？" }).click();
  await expect(lastAnswer(page).locator(".engine-tag")).toHaveText(/录制/, { timeout: 20_000 });
});

test("a question is not a request: nothing is filed", async ({ page }) => {
  await signIn(page, "customer@leon.demo");
  await page.goto("./chat");
  await ask(page, "SO20261012003 如果我退款的话优惠券还在吗");
  await expect(lastAnswer(page)).toContainText("SO20261012003 申请退款", { timeout: 20_000 });
  await expect(lastAnswer(page)).not.toContainText("已提交人工审核");
});

test("no offer on a GPU without what the weights need", async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(navigator, "gpu", { value: { requestAdapter: async () => ({ features: new Set(["subgroups"]) }) }, configurable: true }));
  await signIn(page, "customer@leon.demo");
  await page.goto("./chat");
  await page.getByRole("button", { name: "退款多久能到账？" }).waitFor();
  await page.waitForTimeout(500);
  await expect(page.locator(".demo-engine")).toHaveCount(0);
});

test("no download and no offer on a phone, even with WebGPU", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.addInitScript(() => Object.defineProperty(navigator, "gpu", { value: { requestAdapter: async () => ({ features: new Set(["shader-f16"]) }) }, configurable: true }));
  await signIn(page, "customer@leon.demo");
  await page.goto("./chat");
  await page.getByRole("button", { name: "退款多久能到账？" }).waitFor();
  await page.waitForTimeout(500);
  await expect(page.locator(".demo-engine")).toHaveCount(0);
});

test.describe("the browser model, with a stand-in for the weights", () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(navigator, "gpu", { value: { requestAdapter: async () => ({ features: new Set(["shader-f16"]) }) }, configurable: true });
    });
    // The worker that would run the model: answers load and generate at once, so no weights are downloaded here.
    await page.route("**/model-worker.js", (route) => route.fulfill({
      contentType: "text/javascript",
      body: `self.onmessage = ({ data }) => {
        if (data.type === "load") { self.postMessage({ id: data.id, type: "progress", value: { progress: 0.5, text: "stand-in" } });
          setTimeout(() => self.postMessage({ id: data.id, type: "done", value: true }), 200); }
        else self.postMessage({ id: data.id, type: "done", value: JSON.stringify({ can_answer: true, evidence: [1] }) });
      };`,
    }));
  });

  test("cancelling a load keeps the rules answering", async ({ page }) => {
    await page.route("**/model-worker.js", (route) => route.fulfill({ contentType: "text/javascript", body: "self.onmessage = () => {};" }));
    await signIn(page, "customer@leon.demo");
    await page.goto("./chat");
    await page.getByRole("button", { name: "加载模型" }).click();
    await expect(page.getByRole("button", { name: "取消" })).toBeVisible();
    await ask(page, "退货的时候快递费谁出啊"); // a question while it loads: the rules answer
    await expect(lastAnswer(page).locator(".engine-tag")).toHaveText(/规则引擎/, { timeout: 20_000 });
    await page.getByRole("button", { name: "取消" }).click();
    await expect(page.getByRole("button", { name: "加载模型" })).toBeVisible();
  });

  test("loads on click, answers with its label, and can be unloaded", async ({ page }) => {
    await signIn(page, "customer@leon.demo");
    await page.goto("./chat");
    await expect(page.locator(".demo-engine")).toContainText("点击后才下载");
    await page.getByRole("button", { name: "加载模型" }).click();
    await expect(page.locator(".demo-engine")).toContainText("已加载");
    await ask(page, "支持花呗分期吗"); // the rules would refuse this: the model picks a chunk, the program quotes it
    await expect(lastAnswer(page).locator(".engine-tag")).toHaveText(/Qwen3-0\.6B/, { timeout: 20_000 });
    await expect(lastAnswer(page).locator(".cite").first()).toBeVisible();
    await ask(page, "退款多久能到账呀"); // the rules have this one: the model is not asked
    await expect(page.locator(".msg.assistant")).toHaveCount(2, { timeout: 20_000 });
    await expect(lastAnswer(page).locator(".engine-tag")).toHaveText(/规则引擎/, { timeout: 20_000 });
    await page.getByRole("button", { name: "卸载模型" }).click();
    await expect(page.getByRole("button", { name: "加载模型" })).toBeVisible();
  });
});
