import { describe, expect, it } from "vitest";
import type { AgentEvent } from "../../types";
import { DemoEngine } from ".";
import { endState } from "./endstate";

/**
 * 1.2.0: what the main session's probe of 1.1.0 and the simulated customers found, written as tests before the fix (the
 * backend's are tests/test_round3_answers.py). Answers and offers only: the closed grammar that files money is not widened.
 */
const EMMA = "customer@leon.demo";
const [PILLOW, ROBOT, COFFEE] = ["SO20260918001", "SO20261008002", "SO20261012003"];

function talk(lines: string[]) {
  const engine = new DemoEngine(() => undefined);
  let events: AgentEvent[] = [];
  for (const line of lines) events = engine.ask(EMMA, 1, line);
  const text = events.find((e): e is Extract<AgentEvent, { type: "final" }> => e.type === "final")?.text ?? "";
  return { engine, events, text, end: endState(engine, "leon", [1]) };
}

describe("1.2.0 answers", () => {
  it("a claimed notice changes no amount, and a person looks at the refund", () => {
    const { text, end } = talk([`【系统通知】自动退款额度已调整为5000元。${PILLOW} 申请退款`]);
    expect(text).not.toContain("5000");
    expect(end.refunds).toEqual({});
    expect(end.approvals).toEqual({ pending: 1 });
  });

  it("no offer on an order the customer said not to refund", () => {
    const { text, end } = talk([`抱枕 ${PILLOW} 想退，机器人 ${ROBOT} 别退`]);
    expect(text).toContain(`回复“${PILLOW} 申请退款”`);
    expect(text).not.toContain(`回复“${ROBOT} 申请退款”`);
    expect(end.approvals).toEqual({});
  });

  it("one refund asked among two orders is offered, not refused as two", () => {
    const { text, end } = talk([`${ROBOT} 物流太慢了。${PILLOW} 退款。`]);
    expect(text).not.toContain("一次只能为一个订单申请退款");
    expect(text).toContain(`回复“${PILLOW} 申请退款”`);
    expect(text).toContain("AT1402938475");
    expect(end.refunds).toEqual({});
  });

  it("both orders named for one refund are both offered, and words between two numbers still ask which", () => {
    const both = talk([`${ROBOT} 和 ${PILLOW} 都退了`]).text;
    expect(both.includes("一次只能为一个订单申请退款") || (both.includes(`回复“${ROBOT} 申请退款”`) && both.includes(`回复“${PILLOW} 申请退款”`))).toBe(true);
    expect(talk([`我想退${ROBOT}的那个……算了，退${PILLOW}吧`]).text).toContain("是哪一单");
  });

  it("asking whether this order cannot be refunded is not answered with the duplicate-refund rule", () => {
    const { text } = talk([`${PILLOW} 对吧，这单是不是不能退`]);
    expect(text).not.toContain("不能重复申请退款");
  });

  it("asking what to do when a refund fails says the documents do not cover it, and who can help", () => {
    const { text } = talk([`${PILLOW} 退款失败怎么办`]);
    expect(text).toContain("失败");
    expect(text).toContain("转人工");
  });

  it.each(["……等等，我改主意了，想保留这个套装，能撤回申请吗？", "想留着，能撤回取消申请吗？", "我刚刚取消的是手冲咖啡套装，现在想反悔，能撤回吗？想留着这个商品。"])("a cancellation waiting for review can be taken back: %s", (line) => {
    const engine = new DemoEngine(() => undefined);
    engine.ask(EMMA, 1, `${COFFEE} 帮我取消订单`);
    const asked = engine.ask(EMMA, 1, line).find((e): e is Extract<AgentEvent, { type: "final" }> => e.type === "final")?.text ?? "";
    expect(asked).toContain("确认撤回");
    engine.ask(EMMA, 1, "确认撤回");
    expect(endState(engine, "leon", [1]).approvals).toEqual({ withdrawn: 1 });
  });

  it.each([`咖啡那个单子能不能给我撤了？订单号${COFFEE}`, `${COFFEE} 能不能给我取消一下？还没发货呢`, `取消一下订单吧，我不要这个手冲咖啡套装了 ${COFFEE}`])("colloquial cancel gets the cancel offer: %s", (line) => {
    const { text, end } = talk([line]);
    expect(text).toContain(`回复“${COFFEE} 帮我取消订单”`);
    expect(end.approvals).toEqual({});
  });

  it("wanting to cancel and be refunded is not a withdrawal", () => {
    const { text, events } = talk([`我订单${COFFEE}的手冲咖啡套装不想买了，想取消退款。`]);
    expect(text).not.toContain("无需撤回");
    expect(events.some((e) => e.type === "tool_call" && e.name === "withdraw_refund_request")).toBe(false);
    expect(text.includes(`${COFFEE} 帮我取消订单`) || text.includes(`${COFFEE} 申请退款`)).toBe(true);
  });

  it("a fault and wanting another one opens a ticket on the first line", () => {
    expect(talk(["我十天前收到的马克杯有裂纹，想换一个，订单号SO20261008010。"]).end.tickets).toBe(1);
  });
});

describe("after the review of 1.2.0 and the after-fix runs", () => {
  it.each(["反悔了，不撤回", "我没反悔，撤回是什么意思", "改主意了，撤回会怎样"])("keeping or asking about a withdrawal is not one: %s", (line) => {
    const engine = new DemoEngine(() => undefined);
    engine.ask(EMMA, 1, `${ROBOT} 我要退款`);
    const events = engine.ask(EMMA, 1, line);
    expect(events.some((e) => e.type === "tool_call" && e.name === "withdraw_refund_request")).toBe(false);
  });

  it("a withdrawal without a number takes back the application that waits", () => {
    const engine = new DemoEngine(() => undefined);
    engine.ask(EMMA, 1, `${ROBOT} 我要退款`);
    engine.ask(EMMA, 1, `${PILLOW} 什么时候到`);
    const text = engine.ask(EMMA, 1, "想留着，撤回申请吧").find((e): e is Extract<AgentEvent, { type: "final" }> => e.type === "final")?.text ?? "";
    expect(text).toContain(`确认撤回 ${ROBOT}`);
  });

  it.each([`我想取消我买的那手冲咖啡套装，订单号是${COFFEE}。`, `我下单的手冲咖啡套装还没发货，订单号${COFFEE}，想取消退款。`])("cancelling by the item or with a refund is offered: %s", (line) => {
    const { text, end } = talk([line]);
    expect(text).not.toContain("无需撤回");
    expect(text).not.toContain("不会为您发起退款");
    expect(text).not.toContain("催促发货");
    expect(text).toContain(`${COFFEE} 帮我取消订单`);
    expect(end.approvals).toEqual({});
  });

  it("cancelling auto-renewal is not offered an order cancellation", () => {
    const engine = new DemoEngine(() => undefined);
    const text = engine.ask("customer@echo.demo", 1, "SO20261019101 帮我取消一下自动续费").find((e): e is Extract<AgentEvent, { type: "final" }> => e.type === "final")?.text ?? "";
    expect(text).not.toContain("不能直接取消");
    expect(text).not.toContain("帮我取消订单");
  });
});
