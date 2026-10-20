import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, test, type Browser, type Page } from "@playwright/test";

// The roles' critical paths, each crossing from one role to another the way the
// demo is meant to be used. Every page also fails the test on a console error, an
// uncaught exception or a 5xx from the API, so a page that "renders" but is broken
// underneath does not pass. All accounts and orders are the synthetic demo data.

const problems = new Map<Page, string[]>();

async function signIn(browser: Browser, email: string, options: { fakeMicrophone?: boolean } = {}): Promise<Page> {
  const context = await browser.newContext();
  if (options.fakeMicrophone) {
    // A tone instead of a microphone: no device, no permission prompt, the same on every platform.
    await context.addInitScript(() => {
      navigator.mediaDevices.getUserMedia = async () => {
        const ctx = new AudioContext();
        const tone = ctx.createOscillator();
        const destination = ctx.createMediaStreamDestination();
        tone.frequency.value = 220;
        tone.connect(destination);
        tone.start();
        return destination.stream;
      };
    });
  }
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

/** A refund the payment system never answered, filed 26 hours ago: past the 24 hours the
 * reconciliation job sends it again. The demo tables answer every refund at once, so the walkthrough
 * writes this one into the server's database itself (the same row a timed-out payment call leaves). */
function stuckRefund(orderId: string, email: string) {
  const db = fileURLToPath(new URL("../../backend/.e2e.db", import.meta.url));
  const script = `
import sqlite3, sys, uuid
db = sqlite3.connect(sys.argv[1])
db.execute("""INSERT INTO refunds (tenant_id, order_id, customer_id, amount, reason, status, idempotency_key,
                                   error, attempts, created_at)
              SELECT tenant_id, ?, id, 268.0, '不想要了', 'pending', ?, 'unanswered: HTTP 503 after 3 attempt(s)', 3,
                     datetime('now', '-26 hours') FROM users WHERE email = ?""", (sys.argv[2], uuid.uuid4().hex, sys.argv[3]))
db.commit()
`;
  execFileSync(process.env.E2E_PYTHON ?? "python3", ["-c", script, db, orderId, email]);
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

test("Echo音乐: an annual-card refund waits for an agent; the member sees it go through", async ({ browser }) => {
  // The main demo (a fictional music service): memberships are virtual orders, refunds count from the charge.
  const lily = await signIn(browser, "customer@echo.demo");
  await expect(lily.getByText("音乐流媒体 · 演示租户")).toBeVisible();
  await expect(lily.getByText(/会员与自动续费/)).toBeVisible(); // not the shop's "查询订单和物流"
  await ask(lily, "SO20261015102 年卡我要退款");
  await expect(lily.getByText("已提交人工审核，结果出来后会自动显示在这里")).toBeVisible();
  const answers = lily.locator(".msg.assistant");
  const before = await answers.count();

  const chloe = await signIn(browser, "agent@echo.demo");
  await chloe.goto("/approvals");
  const card = chloe.locator(".card").filter({ hasText: "SO20261015102" }).filter({ hasText: "待审批" });
  await expect(card).toHaveCount(1);
  await card.getByPlaceholder("审批意见（会告知顾客）").fill("年卡未使用会员权益，同意退款");
  await card.getByRole("button", { name: "通过并执行" }).click();
  await expect(chloe.getByText(/审批单 #\d+ 已通过并执行/)).toBeVisible();

  await expect(answers).toHaveCount(before + 1, { timeout: 20_000 });
  await expect(answers.last()).toContainText("198");
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

test("a refund the payment system never answered: the admin checks it and settles it from the refund console", async ({ browser }) => {
  stuckRefund("SO20261012003", "customer@leon.demo");

  // Agents see it and why it waits for a person; only an administrator records the outcome.
  const mia = await signIn(browser, "agent@leon.demo");
  await expect(mia.getByRole("link", { name: /退款对账\s*1/ })).toBeVisible();
  await mia.goto("/refunds");
  const row = (page: Page) => page.getByRole("row").filter({ hasText: "SO20261012003" });
  await expect(row(mia)).toContainText("需要人工确认");
  await expect(row(mia)).toContainText("超过 24 小时，不再自动重发");
  await expect(row(mia)).toContainText("发送 3 次");
  await expect(row(mia)).toContainText("无回音：HTTP 503 after 3 attempt(s)");
  await expect(row(mia).getByRole("button", { name: /确认/ })).toHaveCount(0);

  const leon = await signIn(browser, "admin@leon.demo");
  await leon.goto("/refunds");
  const settle = row(leon).getByRole("button", { name: /确认退款 #\d+ 的结果/ });
  // Keyboard: the dialog opens on the first choice, Escape closes it and returns to the button.
  await settle.focus();
  await leon.keyboard.press("Enter");
  const dialog = leon.getByRole("dialog");
  await expect(dialog.getByRole("radio", { name: /已退款/ })).toBeFocused();
  await expect(dialog).toContainText("在支付后台查找用的幂等键"); // the one handle a refund nobody answered has
  await expect(dialog.getByRole("radio", { name: /没有这笔退款/ })).toBeEnabled(); // past 24 hours: no re-send can pay it
  await leon.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(settle).toBeFocused();

  await leon.keyboard.press("Enter");
  await leon.keyboard.press("Space");
  await expect(dialog.getByRole("radio", { name: /已退款/ })).toBeChecked();
  const confirm = dialog.getByRole("button", { name: "确认结果" });
  await expect(confirm).toBeDisabled(); // the note is required
  await dialog.getByLabel("支付系统退款号（可选）").fill("RF-E2E-0001");
  await dialog.getByLabel(/核实说明/).fill("支付后台查到已原路退回");
  await confirm.click();
  await expect(leon.getByRole("status").filter({ hasText: "已记为已退款" })).toBeFocused();
  await expect(row(leon)).toHaveCount(0);
  await expect(leon.getByText("没有等待支付系统结果的退款")).toBeVisible();
  await leon.getByRole("button", { name: "全部" }).click();
  await expect(row(leon)).toContainText("已退款");
  await expect(row(leon)).toContainText("RF-E2E-0001");

  await leon.goto("/audit");
  const entry = leon.getByRole("row").filter({ hasText: "人工确认退款结果" });
  await expect(entry).toContainText("结果：已退款");
  await expect(entry).toContainText("支付后台查到已原路退回");
});

test("Echo音乐 by voice: push to talk, the answer is spoken and kept in the conversation", async ({ browser }) => {
  // Test engines (see playwright.config.ts): the microphone is a tone, the ASR returns a fixed
  // transcript, the TTS plays a tone. What is exercised is the real channel: the WebSocket,
  // push-to-talk, the agent run, playback and the played-position reports.
  const lily = await signIn(browser, "customer@echo.demo", { fakeMicrophone: true });
  await lily.getByRole("button", { name: "语音", exact: true }).click();
  const ptt = lily.getByRole("button", { name: "按住说话" });
  await expect(ptt).toBeEnabled();
  const box = (await ptt.boundingBox())!;
  await lily.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await lily.mouse.down();
  await lily.waitForTimeout(800);
  await lily.mouse.up();
  await expect(lily.locator(".msg.user .bubble-text").filter({ hasText: "怎么取消自动续费" })).toBeVisible();
  // Saved once played (the client reports each sentence's end): the saved message has the feedback
  // buttons, the live bubble does not.
  const saved = lily.locator(".msg.assistant").filter({ hasText: "管理自动续费" }).getByRole("button", { name: "有帮助" });
  await expect(saved).toBeVisible({ timeout: 40_000 });
  await lily.getByRole("button", { name: "退出语音" }).click();
  await expect(lily.getByRole("button", { name: "语音", exact: true })).toBeVisible();
});
