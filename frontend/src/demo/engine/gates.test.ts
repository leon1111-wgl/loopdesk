import { readFileSync, writeFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { scoreCase, type GateCase } from "./gate-harness";
import phrases from "./money_phrases.json";
import { asksForPerson, cancelRequested, classifyReply, handoffCancelled, refundBareDecline, refundDeclined, refundRequested, refundWithdrawRequested, statedRefundAmount, withdrawConfirmed } from "./text";

/** The port against the backend: backend/app/ops/demo_pack.py writes what the Python functions say on every phrase. */
const NAMES = ["refund_requested", "refund_declined", "cancel_requested", "refund_withdraw_requested", "withdraw_confirmed", "refund_bare_decline", "stated_refund_amount", "asks_for_person", "handoff_cancelled", "classify_reply"];

describe("money rules match the backend", () => {
  const table = phrases as unknown as Record<string, unknown[]>;
  it(`agree on all ${Object.keys(table).length} phrases`, () => {
    const wrong: string[] = [];
    for (const [text, expected] of Object.entries(table)) {
      const got = [refundRequested(text), refundDeclined(text), cancelRequested(text), refundWithdrawRequested(text), withdrawConfirmed(text),
        refundBareDecline(text), statedRefundAmount(text), asksForPerson(text), handoffCancelled(text)[0], classifyReply(text)];
      got.forEach((value, i) => {
        if (value !== expected[i]) wrong.push(`${NAMES[i]}(${text}) = ${value}, backend ${expected[i]}`);
      });
    }
    expect(wrong).toEqual([]);
  });
});

function cases(name: string): GateCase[] {
  return readFileSync(new URL(`../../../../backend/evals/${name}`, import.meta.url), "utf-8").trim().split("\n").map((line: string) => JSON.parse(line));
}

describe.each([["leon", "leon_default.jsonl"], ["echo", "echo_default.jsonl"], ["iris", "iris_default.jsonl"]])("the %s gate cases", (tenant, file) => {
  const all = cases(file);
  const results = all.map((c) => scoreCase(tenant, c));
  const failed = results.filter((r) => !r.passed);
  if (process.env.GATE_REPORT) {
    writeFileSync(`${process.env.GATE_REPORT}/${tenant}.json`, JSON.stringify(results, null, 1));
    console.log(`${tenant}: ${all.length - failed.length}/${all.length}`);
  }
  it(`answer ${all.length} cases`, () => {
    expect(failed.map((r) => `${r.key} [${r.failed.join(",")}] tools=${r.tools.join(",")}: ${r.answer}`)).toEqual([]);
  });
});
