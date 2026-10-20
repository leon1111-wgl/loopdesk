import { expect, test, type Browser, type Page } from "@playwright/test";

// The roles' critical paths, each crossing from one role to another the way the
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
  await expect(answer.locator(".cite").first()).toBeVisible();
  // Customers see the citations, not the test bench: no verifier badge, tool names, trace link
  // or scenario tags on the suggestions (N-046).
  await expect(answer.getByTitle("逐句证据校验结果")).toHaveCount(0);
  await expect(answer.locator(".bubble-meta .badge")).toHaveCount(0);
  await expect(emma.getByRole("link", { name: "链路" })).toHaveCount(0);
  await emma.getByRole("button", { name: "新建对话" }).first().click();
  await expect(emma.locator(".suggestion").first()).toBeVisible();
  await expect(emma.locator(".suggestion .tag")).toHaveCount(0);

  const leon = await signIn(browser, "admin@leon.demo");
  await leon.goto("/chat");
  await ask(leon, "七天无理由退货有什么条件？");
  await expect(leon.locator(".msg.assistant").last().getByTitle("逐句证据校验结果")).toHaveText(/证据充分/);
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

test("the agent opens a ticket; staff claim and close it; the customer reads the outcome", async ({ browser }) => {
  const emma = await signIn(browser, "customer@leon.demo");
  await ask(emma, "我买的扫地机器人坏了开不了机，马上给我处理！");
  await expect(emma.locator(".msg.assistant").last()).toContainText(/已为您创建工单 #\d+/);

  const mia = await signIn(browser, "agent@leon.demo");
  await mia.goto("/tickets");
  const row = mia.getByRole("row").filter({ hasText: "扫地机器人" });
  await row.getByRole("button", { name: "接手" }).click();
  await expect(row).toContainText("处理中");
  await row.getByRole("button", { name: "处理完成" }).click();
  await mia.getByPlaceholder("例如：已安排上门检修，明天上午到达").fill("已安排上门检修，明天上午 10 点前到达。");
  await mia.getByRole("button", { name: "完成并告知顾客" }).click();
  await expect(mia.getByText("没有待处理的工单")).toBeVisible();

  // Tickets take hours, so the customer's page does not poll for them: the outcome is in the
  // conversation the next time they open it.
  await emma.reload();
  await expect(emma.locator(".msg.agent").last()).toContainText("已安排上门检修，明天上午 10 点前到达。");
});

test("an administrator edits a document; the knowledge page and the audit log show 1.00 → 1.01", async ({ browser }) => {
  const leon = await signIn(browser, "admin@leon.demo");
  await leon.goto("/knowledge");
  const row = leon.getByRole("row").filter({ hasText: "发票开具指南" });
  await expect(row.getByRole("cell", { name: "1.00", exact: true })).toBeVisible();
  await row.click();
  await leon.getByRole("dialog").getByRole("button", { name: "编辑" }).click();
  const content = leon.getByRole("textbox", { name: /内容/ });
  await content.fill(`${await content.inputValue()}\n\n纸质发票寄出后，可在订单详情查看物流单号。`);
  await leon.getByRole("button", { name: "保存为 1.01" }).click();
  await expect(row.getByRole("cell", { name: "1.01", exact: true })).toBeVisible();

  // Stored as 1 and 2 (the edit lock compares them); shown the way the knowledge page shows them.
  await leon.goto("/audit");
  const entry = leon.getByRole("row").filter({ hasText: "原版本" }).first();
  await expect(entry).toContainText("原版本：1.00");
  await expect(entry).toContainText("新版本：1.01");
});
