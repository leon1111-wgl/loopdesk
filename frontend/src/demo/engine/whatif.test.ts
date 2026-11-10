import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { pack } from "./pack";
import { policyEngine } from "./policy";
import { compareReplays, flipOf, replayJourney, replayTranscript, SHIPPED, type Journey, type Transcript, type Variant } from "./whatif";

/** The what-if panel's arithmetic: same policy, no flips; a change flips what it should and names the policy line. */
const json = (path: string) => JSON.parse(readFileSync(new URL(`../../../../${path}`, import.meta.url), "utf-8"));
const journeys: Journey[] = json("backend/evals/journeys.json");
const cards = new Map([...json("backend/evals/simulations/dev.json").scenarios, ...json("backend/evals/simulations/blind_v1.json").scenarios].map((c: { key: string; as: string; tenant: string }) => [c.key, c]));
const transcripts: Transcript[] = json("demo/simulations/report.json").runs.map((r: { key: string; set: string; seed: number; tenant: string; transcript: { role: string; text: string; sent?: boolean }[] }) => ({
  key: r.key, set: r.set, seed: r.seed, tenant: r.tenant, as: cards.get(r.key)!.as, lines: r.transcript.filter((t) => t.role === "customer" && t.sent).map((t) => t.text),
}));

function flips(variant: Variant) {
  return [
    ...journeys.map((j) => flipOf(replayJourney(j, SHIPPED), replayJourney(j, variant))),
    ...transcripts.map((t) => flipOf(replayTranscript(t, SHIPPED), replayTranscript(t, variant))),
  ].filter((f) => f !== null);
}

describe("what if the policy were different", () => {
  it("flips nothing when nothing changed", () => {
    expect(flips(SHIPPED)).toEqual([]);
  });

  it("raising Leon's automatic limit to ¥2000 changes decisions but no end state: the velocity limits send the same orders to a person", () => {
    const variant: Variant = { limits: { leon: 2000 }, disabled: [] };
    expect(flips(variant)).toEqual([]);
    const j = journeys.find((x) => x.key === "over_the_limit_waits_then_approved")!;
    const change = compareReplays(replayJourney(j, SHIPPED), replayJourney(j, variant))!;
    expect(change.tables).toEqual([]);
    expect(change.stopped).toEqual(["review.above_auto_limit"]);
    expect(change.started).toEqual(["review.velocity_total"]);
  });

  it("raising Echo's limit to ¥2000 pays the annual plan without a person, and names review.above_auto_limit", () => {
    const found = flips({ limits: { echo: 2000 }, disabled: [] });
    expect(found.length).toBeGreaterThan(0);
    expect(found.every((f) => f.tenant === "echo" && f.stopped.includes("review.above_auto_limit") && f.was?.effect === "review" && f.now?.effect === "allow")).toBe(true);
  });

  it("switching off review.above_auto_limit flips the journeys that waited for a person", () => {
    const found = flips({ limits: {}, disabled: ["review.above_auto_limit"] });
    const journey = found.find((f) => f.key === "over_the_limit_waits_then_approved")!;
    expect(journey.tables).toEqual(["approvals"]);
    expect(journey.stopped).toEqual(["review.above_auto_limit"]);
  });

  it("cannot switch off an invariant, and the loosest variant still refunds no order above what was paid", () => {
    const locked = policyEngine()!.policies.filter((p) => p.locked).map((p) => p.id);
    expect(flips({ limits: {}, disabled: locked })).toEqual([]);
    const loosest: Variant = { limits: { leon: 100000, echo: 100000 }, disabled: policyEngine()!.policies.filter((p) => !p.locked && p.id.startsWith("review.")).map((p) => p.id) };
    for (const f of flips(loosest)) {
      for (const [order, amount] of Object.entries(f.after.refunds)) {
        const paid = pack.orders[f.tenant].find((o) => o.id === order)!.amount;
        expect(amount).toBeLessThanOrEqual(paid);
      }
    }
  });
});
