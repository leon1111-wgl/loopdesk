import { expect, test, type Browser, type Page } from "@playwright/test";

// The three roles' critical paths, each crossing from one role to another the way the
// demo is meant to be used. Every page also fails the test on a console error, an
// uncaught exception or a 5xx from the API, so a page that "renders" but is broken
// underneath does not pass. All accounts and orders are the synthetic demo data.

const problems = new Map<Page, string[]>();

async function signIn(browser: Browser, email: string): Promise<Page> {
  const context = await browser.newContext();
  const page = await context.newPage();
  const seen: string[] = [];
  problems.set(page, seen);
  page.on("console", (msg) => { if (msg.type() === "error") seen.push(`console: ${msg.text()}`); });
  page.on("pageerror", (err) => seen.push(`page error: ${err.message}`));
  page.on("response", (r) => { if (r.status() >= 500) seen.push(`${r.status()} ${r.url()}`); });
  await page.goto("/");
  // The login page lists the demo accounts; clicking one signs in with the demo password.
  await page.getByRole("button", { name: new RegExp(email.replace(".", "\\.")) }).click();
  await expect(page.getByRole("button", { name: "退出登录" })).toBeVisible();
  return page;
}

async function ask(page: Page, text: string) {
  await page.getByPlaceholder("输入问题，Enter 发送，Shift+Enter 换行").fill(text);
  await page.getByRole("button", { name: "发送" }).click();
}

test.afterEach(async () => {
  for (const [page, seen] of problems) {
    await page.context().close();
    expect(seen, "console errors, page errors or 5xx responses").toEqual([]);
  }
  problems.clear();
});

test("customer gets a cited answer; staff can open its trace", async ({ browser }) => {
  const emma = await signIn(browser, "customer@leon.demo");
  await expect(emma).toHaveURL(/\/chat/);
  await ask(emma, "七天无理由退货有什么条件？");
  const answer = emma.locator(".msg.assistant").last();
  await expect(answer.getByTitle("逐句证据校验结果")).toHaveText(/证据充分/);
  // Customers do not see staff-only controls.
  await expect(emma.getByRole("link", { name: "链路" })).toHaveCount(0);

  const leon = await signIn(browser, "admin@leon.demo");
  await leon.goto("/traces");
  await expect(leon.getByText("七天无理由退货有什么条件？").first()).toBeVisible();
});

test("refund over the auto limit waits for an admin, then the customer sees the result", async ({ browser }) => {
  const emma = await signIn(browser, "customer@leon.demo");
  await ask(emma, "SO20261008002 我要退款");
  await expect(emma.getByText("已提交人工审核，结果出来后会自动显示在这里")).toBeVisible();
  const answers = emma.locator(".msg.assistant");
  const before = await answers.count();

  const leon = await signIn(browser, "admin@leon.demo");
  await leon.goto("/approvals");
  const card = leon.locator(".card").filter({ hasText: "SO20261008002" }).filter({ hasText: "待审批" });
  await expect(card).toHaveCount(1);
  await card.getByPlaceholder("审批意见（会告知顾客）").fill("核实无误");
  await card.getByRole("button", { name: "通过并执行" }).click();
  await expect(leon.getByText(/审批单 #\d+ 已通过并执行/)).toBeVisible();

  // The customer's page polls while an approval is pending: the resumed answer arrives
  // without a reload and the waiting notice goes away.
  await expect(answers).toHaveCount(before + 1, { timeout: 20_000 });
  await expect(emma.getByText("已提交人工审核，结果出来后会自动显示在这里")).toHaveCount(0);
  await expect(answers.last()).toContainText("退款");
});

test("customer asks for a person; the agent replies from the handoff queue", async ({ browser }) => {
  const jack = await signIn(browser, "customer2@leon.demo");
  await ask(jack, "给我转人工");
  await expect(jack.getByRole("status").filter({ hasText: "已转人工客服，当前排队第 1 位" })).toBeVisible();

  const mia = await signIn(browser, "agent@leon.demo");
  await mia.goto("/handoffs");
  await mia.getByRole("listitem").filter({ hasText: "Jack" }).click();
  await mia.getByPlaceholder("以人工客服身份回复顾客").fill("您好，我是 Mia，这就帮您看。");
  await mia.getByRole("button", { name: "回复顾客" }).click();
  await expect(mia.locator(".handoff-panel").getByText("Mia 跟进中")).toBeVisible();

  // The customer's page polls while a person is involved.
  await expect(jack.locator(".msg.agent").getByText("您好，我是 Mia，这就帮您看。")).toBeVisible({ timeout: 20_000 });
  await expect(jack.getByRole("status").filter({ hasText: "人工客服 Mia 正在为您服务" })).toBeVisible();
});
