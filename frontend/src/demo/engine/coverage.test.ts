import { describe, expect, it } from "vitest";
import type { AgentEvent } from "../../types";
import { DemoEngine } from ".";
import coverage from "./coverage.json";
import { pack } from "./pack";
import { canDo } from "./reading";

/**
 * What this demo says it can answer: every topic in a tenant's capability text and every section of its public documents, each
 * in one plain and one colloquial wording. An answer counts only if it cites that section; an action, only if it was taken.
 * The wordings were written before they were run, and by the same author as the rules: this guards the menu, it is no blind set.
 */
interface Entry {
  tenant: string;
  kind: string;
  topic: string;
  sections?: string[];
  expect?: "orders" | "asks_which" | "ticket";
  plain: string;
  colloquial: string;
}

const ENTRIES = coverage as Entry[];
const CUSTOMER: Record<string, string> = { leon: "customer@leon.demo", echo: "customer@echo.demo", iris: "customer@iris.demo" };
const sectionOf = (heading: unknown) => String(heading ?? "").split(">").at(-1)!.trim();

function reply(tenant: string, question: string) {
  const engine = new DemoEngine(() => undefined);
  const events = engine.ask(CUSTOMER[tenant], 1, question) as AgentEvent[];
  const final = events.find((e): e is Extract<AgentEvent, { type: "final" }> => e.type === "final")!;
  const refs = new Set([...final.text.matchAll(/\[(\d+)\]/g)].map((m) => Number(m[1])));
  const cited = (final.meta.citations ?? []).filter((c) => refs.has(c.ref)).map((c) => sectionOf(c.meta?.heading));
  return { text: final.text, cited, tickets: engine.world.tickets.filter((t) => t.kind !== "handoff").length, tools: events.filter((e) => e.type === "tool_call").map((e) => (e as { name: string }).name) };
}

const cases = ENTRIES.flatMap((e) => (["plain", "colloquial"] as const).map((form) => ({ entry: e, form, question: e[form], label: `${e.tenant} ${e.kind} ${e.topic} (${form})` })));

describe("what the demo says it can answer", () => {
  it.each(cases)("$label: $question", ({ entry, question }) => {
    const got = reply(entry.tenant, question);
    let ok: boolean;
    if (entry.expect === "orders") ok = /SO\d+/.test(got.text) && got.tools.includes("list_my_orders");
    else if (entry.expect === "asks_which") ok = got.text.includes("要申请退款的是哪一单");
    else if (entry.expect === "ticket") ok = got.tickets === 1;
    else ok = got.cited.some((section) => (entry.sections ?? [entry.topic]).includes(section));
    expect(ok, `${question} -> ${got.text.slice(0, 120)}`).toBe(true);
  });

  it.each(["leon", "echo", "iris"])("%s: every public section has its two wordings", (tenant) => {
    const sections = (pack.kb[tenant] ?? []).filter((c) => c.visibility === "public" && !/SYNTHETIC/.test(c.text)).map((c) => sectionOf(c.heading));
    const written = ENTRIES.filter((e) => e.tenant === tenant && e.kind === "section").map((e) => e.topic);
    expect(written.sort()).toEqual([...sections].sort());
  });

  it.each(["leon", "echo", "iris"])("%s: every capability topic is named in its capability text", (tenant) => {
    for (const e of ENTRIES.filter((x) => x.tenant === tenant && x.kind === "capability")) expect(canDo(tenant)).toContain(e.topic === "订单" ? "查订单" : e.topic);
  });
});
