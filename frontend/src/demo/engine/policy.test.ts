import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { cedarVersion, decide, entity, policyEngine, validatePolicies, uid, type PolicyEngine } from "./policy";

/**
 * The browser half of backend/tests/test_policy.py: cedar-wasm validates the same files against the same schema, answers
 * every shared case (backend/evals/policy/policy_cases.json, answered by cedarpy) the same way, and keeps the promised
 * properties for the variants the what-if panel can make.
 */
interface Case { principal: [string, string]; action: string; resource: [string, string]; context: Record<string, unknown>; entities: Record<string, unknown>[]; expected: { effect: string; reasons: string[]; errors: number } }
const cases: Case[] = JSON.parse(readFileSync(new URL("../../../../backend/evals/policy/policy_cases.json", import.meta.url), "utf-8"));
const shipped = policyEngine()!;
const switchable = shipped.policies.filter((p) => !p.locked).map((p) => p.id);
const variants: PolicyEngine[] = [shipped, policyEngine(undefined, new Set(switchable))!, ...switchable.map((id) => policyEngine(undefined, new Set([id]))!)];

describe("the policy in the browser", () => {
  it("is evaluated by the Cedar version the backend pins (4.12.0)", () => {
    expect(cedarVersion()).toBe("4.12.0");
  });

  it("validates against its schema with cedar-wasm", () => {
    expect(validatePolicies()).toEqual([]);
  });

  it(`answers all ${cases.length} shared cases as cedarpy did`, () => {
    const wrong = cases.map((c) => ({ c, got: decide(shipped, c.principal, c.action, c.resource, c.context, c.entities) }))
      .filter(({ c, got }) => got.effect !== c.expected.effect || JSON.stringify(got.reasons) !== JSON.stringify(c.expected.reasons) || (got.errors.length > 0) !== (c.expected.errors > 0))
      .map(({ c, got }) => ({ action: c.action, expected: c.expected, got: { effect: got.effect, reasons: got.reasons } }));
    expect(wrong.slice(0, 5)).toEqual([]);
  });

  it("locks the invariants: the panel cannot switch them off", () => {
    expect(shipped.policies.filter((p) => p.locked).map((p) => p.id).sort()).toEqual(["deny.amount_above_paid", "deny.amount_not_positive", "deny.four_eyes", "deny.no_other_reviewer", "deny.voice_unconfirmed"]);
  });

  it.each(variants.map((v, i) => [i, v] as const))("variant %i never permits a refund above what was paid, nor lets the requester approve", (_, engine) => {
    for (const c of cases.filter((x) => x.action === "request_refund" && x.context.refund)) {
      const refund = { ...(c.context.refund as Record<string, number>) };
      refund.amount = refund.paid + 1;
      for (const reviewed of [false, true]) {
        expect(decide(engine, c.principal, c.action, c.resource, { ...c.context, reviewed, refund }, c.entities).effect).toBe("deny");
      }
    }
    for (const role of ["customer", "agent", "admin"]) {
      for (const action of ["approve", "reject"]) {
        const entities = [entity("User", "u1", { role }), entity("Application", "a1", { requester: { __entity: uid("User", "u1") }, amount: 10, approver: "" })];
        expect(decide(engine, ["User", "u1"], action, ["Application", "a1"], { approval_admin_threshold: 50000 }, entities).effect).toBe("deny");
      }
    }
  });
});
