import { expect, test, type Page } from "@playwright/test";

// The whole visitor flow on a production build: the intro, entering, every tab of each role, at desktop and phone width, in both
// themes. Nothing may raise a page error, log a console error or overflow sideways.
const SIZES = [
  { name: "desktop", viewport: { width: 1280, height: 800 } },
  { name: "phone", viewport: { width: 375, height: 812 } },
];
const ROLES = [
  ["customer", "customer@leon.demo"],
  ["agent", "agent@leon.demo"],
  ["admin", "admin@leon.demo"],
  ["admin of the main demo tenant", "admin@echo.demo"],
];

function watch(page: Page) {
  const problems: string[] = [];
  page.on("pageerror", (error) => problems.push(`page error: ${error.message}`));
  page.on("console", (message) => { if (message.type() === "error") problems.push(`console: ${message.text().slice(0, 200)}`); });
  return problems;
}

async function noSideScroll(page: Page, where: string) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow, `${where} scrolls sideways`).toBeLessThanOrEqual(0);
}

async function setTheme(page: Page, theme: string) {
  await page.addInitScript((value) => { try { localStorage.setItem("loopdesk.theme", value); } catch { /* the test will show it */ } }, theme);
}

for (const size of SIZES) {
  for (const theme of ["dark", "light"]) {
    test.describe(`${size.name}, ${theme}`, () => {
      test.use({ viewport: size.viewport });

      test("the intro works before signing in", async ({ page }) => {
        const problems = watch(page);
        await setTheme(page, theme);
        await page.goto("./");
        await expect(page.getByRole("button", { name: /customer@leon\.demo/ }).first()).toBeVisible();
        await noSideScroll(page, "intro");
        expect(problems).toEqual([]);
      });

      for (const [role, email] of ROLES) {
        test(`every page opens as ${role}`, async ({ page }) => {
          const problems = watch(page);
          await setTheme(page, theme);
          await page.goto("./");
          await page.getByRole("button", { name: new RegExp(email.replace(".", "\\.")) }).first().click();
          await expect(page.getByRole("button", { name: "账号菜单" })).toBeVisible();
          await noSideScroll(page, "first page");
          // Every section, then every page tab inside it: the navigation is what a visitor can reach.
          const seen = new Set<string>();
          for (let round = 0; round < 3; round++) {
            const hrefs = await page.locator("nav a[href]").evaluateAll((links) => links.map((a) => a.getAttribute("href") ?? ""));
            const next = [...new Set(hrefs)].filter((href) => href.includes("/loopdesk/") && !seen.has(href) && !/github/.test(href));
            if (!next.length) break;
            for (const href of next) {
              seen.add(href);
              await page.goto(href);
              await expect(page.getByRole("button", { name: "账号菜单" })).toBeVisible();
              await page.waitForLoadState("networkidle");
              await expect(page.getByText("静态演示没有录制这一页的数据")).toHaveCount(0);
              await noSideScroll(page, href);
            }
          }
          if (!email.startsWith("customer")) expect(seen.size).toBeGreaterThan(3);
          expect(problems).toEqual([]);
        });
      }
    });
  }
}

const QUESTIONS: { name: string; email: string; question: string; says: RegExp; not?: RegExp; cites?: boolean; simulated?: boolean }[] = [
  { name: "order status", email: "customer@leon.demo", question: "帮我看下 SO20261012003 现在什么情况", says: /已付款，待发货/ },
  { name: "shipping, by item", email: "customer@leon.demo", question: "那个扫地机器人的快递到哪了", says: /AT1402938475/ },
  { name: "a policy", email: "customer@leon.demo", question: "定制的东西能七天无理由吗", says: /不支持/, cites: true },
  { name: "another tenant's policy", email: "customer@echo.demo", question: "连续包月怎么关掉自动续费", says: /自动续费/, cites: true },
  { name: "an explicit refund, automatic", email: "customer@leon.demo", question: "SO20260918001 抱枕不想要了，帮我退款", says: /已受理/, simulated: true },
  { name: "an explicit refund, reviewed", email: "customer@leon.demo", question: "SO20261012003 帮我退款", says: /已提交人工审核/, simulated: true },
  { name: "a question about a refund", email: "customer@leon.demo", question: "SO20261012003 退款的话多久到账", says: /SO20261012003 申请退款/, not: /已受理|已提交人工审核/ },
  { name: "a hedge", email: "customer@leon.demo", question: "SO20261012003 我可能会退款 看情况", says: /SO20261012003/, not: /已受理|已提交人工审核/ },
  { name: "a conditional", email: "customer@leon.demo", question: "SO20261012003 如果退款的话优惠券还在吗", says: /SO20261012003/, not: /已受理|已提交人工审核/ },
  { name: "taken back", email: "customer@leon.demo", question: "SO20261012003 我要退款 算了不退了", says: /SO20261012003/, not: /已受理|已提交人工审核/ },
  { name: "a report", email: "customer@leon.demo", question: "SO20261012003 客服说已经退钱了", says: /SO20261012003/, not: /已受理|已提交人工审核/ },
  { name: "an unknown order", email: "customer@leon.demo", question: "SO20990101999 到哪了", says: /不存在或无权查看/ },
  { name: "someone else's order", email: "customer@leon.demo", question: "SO20261003005 到哪了", says: /不存在或无权查看/, not: /降噪耳机/ },
  { name: "nonsense", email: "customer@leon.demo", question: "asdfgh 123 啦啦啦 qwer", says: /没有找到/ },
  { name: "out of scope", email: "customer@leon.demo", question: "今天上海天气怎么样", says: /没有找到/ },
];

test.describe("questions nobody recorded", () => {
  for (const q of QUESTIONS) {
    test(q.name, async ({ page }) => {
      const problems = watch(page);
      await page.goto("./");
      await page.getByRole("button", { name: new RegExp(q.email.replace(".", "\\.")) }).first().click();
      await expect(page.getByRole("button", { name: "账号菜单" })).toBeVisible();
      await page.goto("./chat");
      const box = page.locator("textarea").first();
      await box.fill(q.question);
      await box.press("Enter");
      const answer = page.locator(".msg.assistant").last();
      await expect(answer.locator(".engine-tag")).toHaveText(/规则引擎/, { timeout: 20_000 });
      await expect(answer).toContainText(q.says);
      if (q.not) await expect(answer).not.toContainText(q.not);
      if (q.cites) await expect(answer.locator(".cite").first()).toBeVisible();
      if (q.simulated) await expect(answer).toContainText("（模拟）");
      expect(problems).toEqual([]);
    });
  }

  test("a member of staff can open the trace of an answer", async ({ page }) => {
    const problems = watch(page);
    await page.goto("./");
    await page.getByRole("button", { name: /agent@leon\.demo/ }).first().click();
    await expect(page.getByRole("button", { name: "账号菜单" })).toBeVisible();
    await page.goto("./chat");
    const box = page.locator("textarea").first();
    await box.fill("退货的时候快递费谁出啊");
    await box.press("Enter");
    await page.getByRole("link", { name: /查看完整链路/ }).click();
    await expect(page.getByText("执行时间线")).toBeVisible();
    await expect(page.getByText("search_knowledge").first()).toBeVisible();
    await expect(page.locator(".badge", { hasText: "模拟" }).first()).toBeVisible();
    expect(problems).toEqual([]);
  });

  test("a follow-up after a recorded answer knows what was asked", async ({ page }) => {
    const problems = watch(page);
    await page.goto("./");
    await page.getByRole("button", { name: /customer@leon\.demo/ }).first().click();
    await expect(page.getByRole("button", { name: "账号菜单" })).toBeVisible();
    await page.goto("./chat");
    await page.getByRole("button", { name: "SO20261008002 的快递到哪了？" }).click();
    await expect(page.locator(".msg.assistant").last().locator(".engine-tag")).toHaveText(/录制/, { timeout: 20_000 });
    const box = page.locator("textarea").first();
    await box.fill("为什么这么慢");
    await box.press("Enter");
    await expect(page.locator(".msg.assistant").last().locator(".engine-tag")).toHaveText(/规则引擎/, { timeout: 20_000 });
    await expect(page.locator(".msg.assistant").last()).toContainText("AT1402938475");
    expect(problems).toEqual([]);
  });

  test("going on in a recorded conversation keeps what it said", async ({ page }) => {
    const problems = watch(page);
    await page.goto("./");
    await page.getByRole("button", { name: /customer@leon\.demo/ }).first().click();
    await expect(page.getByRole("button", { name: "账号菜单" })).toBeVisible();
    await page.goto("./chat");
    await page.locator(".convo-item", { hasText: "SO20261008002 的快递到哪了" }).first().click({ force: true }).catch(() => undefined);
    const before = await page.locator(".msg").count();
    const box = page.locator("textarea").first();
    await box.fill("为什么这么慢");
    await box.press("Enter");
    await expect(page.locator(".msg.assistant").last().locator(".engine-tag")).toHaveText(/规则引擎/, { timeout: 20_000 });
    expect(await page.locator(".msg").count()).toBeGreaterThanOrEqual(before + 2);
    expect(problems).toEqual([]);
  });

  test("reloading says the simulated conversation is gone, and the chat still works", async ({ page }) => {
    await page.goto("./");
    await page.getByRole("button", { name: /customer@leon\.demo/ }).first().click();
    await expect(page.getByRole("button", { name: "账号菜单" })).toBeVisible();
    await page.goto("./chat");
    const box = page.locator("textarea").first();
    await box.fill("退货的时候快递费谁出啊");
    await box.press("Enter");
    await expect(page.locator(".msg.assistant").last().locator(".engine-tag")).toHaveText(/规则引擎/, { timeout: 20_000 });
    await page.reload();
    await expect(page.getByText("这段模拟对话只保存在当前页面")).toBeVisible();
    await page.getByRole("button", { name: "新建对话" }).first().click();
    await page.locator("textarea").first().fill("积分可以怎么抵扣");
    await page.locator("textarea").first().press("Enter");
    await expect(page.locator(".msg.assistant").last().locator(".engine-tag")).toHaveText(/规则引擎/, { timeout: 20_000 });
  });

  test("the model is not offered in a build that has not released it, even with WebGPU", async ({ page }) => {
    await page.addInitScript(() => Object.defineProperty(navigator, "gpu", { value: { requestAdapter: async () => ({ features: new Set(["shader-f16"]) }) }, configurable: true }));
    await page.goto("./");
    await page.getByRole("button", { name: /customer@leon\.demo/ }).first().click();
    await expect(page.getByRole("button", { name: "账号菜单" })).toBeVisible();
    await page.goto("./chat");
    await page.waitForTimeout(500);
    await expect(page.locator(".demo-engine")).toHaveCount(0);
    await page.goto("./chat?tier2=1");
    await page.waitForTimeout(500);
    await expect(page.locator(".demo-engine")).toHaveCount(0);
  });
});
