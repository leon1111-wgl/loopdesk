/**
 * The policy decision point of the static demo: backend/policies/loopdesk.cedar evaluated with cedar-wasm, the browser's
 * copy of app/agent/policy.py. Both sides pin Cedar 4.12.0 and read the same two files; policy_cases.json holds them to
 * the same answers. The gateway (tools.ts, index.ts) gathers the facts, this asks Cedar, the gateway enforces.
 *
 * Outcomes, named by the @id of the policies that decided them: "allow" (a permit matched, no forbid did), "review"
 * (only review.* forbids matched and a permit would have matched without them), "deny" (a deny.* forbid matched, or
 * nothing permitted: deny by default). Until the engine is loaded, or when a request cannot be evaluated, it is "deny".
 */
import type * as CedarModule from "@cedar-policy/cedar-wasm/web";
import policyText from "../../../../backend/policies/loopdesk.cedar?raw";
import schemaText from "../../../../backend/policies/loopdesk.cedarschema?raw";

type Cedar = Pick<typeof CedarModule, "statefulIsAuthorized" | "preparsePolicySet" | "preparseSchema" | "validate" | "policySetTextToParts" | "getCedarVersion">;
type Json = Record<string, unknown>;
export type Effect = "allow" | "review" | "deny";

export const POLICY_TEXT = policyText;
export const SCHEMA_TEXT = schemaText;
export const NOT_LOADED = "策略引擎（cedar-wasm）没有加载：默认拒绝";

export interface Decision {
  action: string;
  effect: Effect;
  reasons: string[];
  errors: string[];
  micros: number;
  context: Json;
}

export interface PolicyInfo {
  id: string;
  text: string;
  effect: "permit" | "forbid";
  label: string;
  stage: "request" | "approval";
  locked: boolean;
}

let cedar: Cedar | null = null;
let loading: Promise<void> | null = null;
let schemaReady = false;
let sets = 0;
const engines = new Map<string, PolicyEngine>();

/** The tests install the Node build of cedar-wasm; the page loads the web build (loadCedar). */
export function useCedar(api: Cedar | null): void {
  cedar = api;
  schemaReady = false;
  engines.clear();
}

/** Parse once, then every decision reuses the parsed schema and policy set (about 5 ms a decision otherwise). */
function preparsed(api: Cedar, policies: Record<string, string>): string {
  if (!schemaReady) {
    const schema = api.preparseSchema("loopdesk", SCHEMA_TEXT);
    if (schema.type !== "success") throw new Error(schema.errors.map((e) => e.message).join("; "));
    schemaReady = true;
  }
  const id = `set-${++sets}`;
  const parsed = api.preparsePolicySet(id, { staticPolicies: policies });
  if (parsed.type !== "success") throw new Error(parsed.errors.map((e) => e.message).join("; "));
  return id;
}

export const cedarLoaded = (): boolean => cedar !== null;
export const cedarVersion = (): string | null => cedar?.getCedarVersion() ?? null;

/** Fetch and start the WebAssembly engine (about 1.4 MB compressed). A failure leaves every money call denied. */
export function loadCedar(): Promise<void> {
  if (cedar) return Promise.resolve();
  loading ??= import("@cedar-policy/cedar-wasm/web").then(async (module) => {
    await module.default();
    useCedar(module);
  }).catch((error) => {
    loading = null;
    throw error;
  });
  return loading;
}

export const fen = (yuan: number): number => Math.round(yuan * 100);
export const uid = (type: string, id: string | number) => ({ type: `LoopDesk::${type}`, id: String(id) });
export const entity = (type: string, id: string | number, attrs: Json) => ({ uid: uid(type, id), attrs, parents: [] });

const ANNOTATION = /@(\w+)\("((?:[^"\\]|\\.)*)"\)/g;

/** The policy file as its policies, in file order: the order the gateway shows reasons in. */
export function parsePolicies(text: string, api: Cedar): PolicyInfo[] {
  const parts = api.policySetTextToParts(text);
  if (parts.type !== "success") throw new Error(parts.errors.map((e) => e.message).join("; "));
  const at = (part: string) => text.indexOf(/@id\("((?:[^"\\]|\\.)*)"\)/.exec(part)?.[0] ?? "\u0000");
  return [...parts.policies].sort((a, b) => at(a) - at(b)).map((part) => {
    const notes = Object.fromEntries([...part.matchAll(ANNOTATION)].map((m) => [m[1], m[2]]));
    if (!notes.id) throw new Error(`every policy needs an @id: ${part.slice(0, 80)}`);
    const effect = /^\s*(?:@\w+\("(?:[^"\\]|\\.)*"\)\s*)*(permit|forbid)/.exec(part)?.[1] as "permit" | "forbid";
    return { id: notes.id, text: part, effect, label: notes.label ?? notes.id, stage: notes.stage === "approval" ? "approval" : "request", locked: notes.locked === "true" };
  });
}

export class PolicyEngine {
  readonly policies: PolicyInfo[];
  private all: string;
  private withoutReview: string;

  constructor(private api: Cedar, text = POLICY_TEXT, disabled: ReadonlySet<string> = new Set()) {
    this.policies = parsePolicies(text, api).filter((p) => !disabled.has(p.id));
    this.all = preparsed(api, Object.fromEntries(this.policies.map((p) => [p.id, p.text])));
    this.withoutReview = preparsed(api, Object.fromEntries(this.policies.filter((p) => !p.id.startsWith("review.")).map((p) => [p.id, p.text])));
  }

  info(id: string): PolicyInfo | undefined {
    return this.policies.find((p) => p.id === id);
  }

  private ask(set: string, call: Omit<CedarModule.StatefulAuthorizationCall, "preparsedPolicySetId">) {
    return this.api.statefulIsAuthorized({ ...call, preparsedPolicySetId: set });
  }

  decide(principal: [string, string | number], action: string, resource: [string, string | number], context: Json, entities: Json[]): Decision {
    const started = performance.now();
    const call = {
      principal: uid(principal[0], principal[1]), action: { type: "LoopDesk::Action", id: action }, resource: uid(resource[0], resource[1]),
      context: context as CedarModule.Context, entities: entities as unknown as CedarModule.EntityJson[], preparsedSchemaName: "loopdesk", validateRequest: true,
    };
    const answer = this.ask(this.all, call);
    if (answer.type !== "success") return { action, effect: "deny", reasons: [], errors: answer.errors.map((e) => e.message), micros: Math.round((performance.now() - started) * 1000), context };
    const order = (id: string) => this.policies.findIndex((p) => p.id === id);
    let reasons = [...answer.response.diagnostics.reason].sort((a, b) => order(a) - order(b));
    const errors = answer.response.diagnostics.errors.map((e) => `${e.policyId}: ${e.error.message}`);
    let effect: Effect;
    if (errors.length) effect = "deny"; // a policy that cannot be evaluated is skipped by Cedar; here it fails the call closed
    else if (answer.response.decision === "allow") effect = "allow";
    else if (reasons.length && reasons.every((r) => r.startsWith("review."))) {
      const without = this.ask(this.withoutReview, call);
      effect = without.type === "success" && without.response.decision === "allow" ? "review" : "deny";
      if (effect === "deny") reasons = [];
    } else effect = "deny";
    return { action, effect, reasons: effect === "allow" ? [] : reasons, errors, micros: Math.round((performance.now() - started) * 1000), context };
  }

  label(id: string): string {
    return this.info(id)?.label ?? id;
  }

  stage(id: string): "request" | "approval" {
    return this.info(id)?.stage ?? "request";
  }
}

/** The engine for this policy text with these policies switched off (the what-if panel); the shipped one by default. */
export function policyEngine(text = POLICY_TEXT, disabled: ReadonlySet<string> = new Set()): PolicyEngine | null {
  if (!cedar) return null;
  const key = `${text.length}:${[...disabled].sort().join(",")}:${text === POLICY_TEXT ? "" : text}`;
  let engine = engines.get(key);
  if (!engine) {
    engine = new PolicyEngine(cedar, text, disabled);
    engines.set(key, engine);
  }
  return engine;
}

const captures: Decision[][] = [];

/** Every decision the gateway takes while ``run`` runs (the what-if panel lines two replays up by them). The approval
 * queue's "who may approve" probes (principal ~agent) are not decisions about a call and are left out. */
export function capture<T>(run: () => T): [T, Decision[]] {
  const found: Decision[] = [];
  captures.push(found);
  try {
    return [run(), found];
  } finally {
    captures.splice(captures.indexOf(found), 1);
  }
}

/** What the gateway asks. ``engine``: the what-if panel's variant; the shipped policy otherwise. */
export function decide(engine: PolicyEngine | null | undefined, principal: [string, string | number], action: string, resource: [string, string | number], context: Json, entities: Json[]): Decision {
  const e = engine ?? policyEngine();
  const decision: Decision = e ? e.decide(principal, action, resource, context, entities) : { action, effect: "deny", reasons: [], errors: [NOT_LOADED], micros: 0, context };
  if (!String(principal[1]).startsWith("~")) captures.at(-1)?.push(decision);
  return decision;
}

/** The first deny.* reason checked at this stage. */
export function denial(decision: Decision, engine: PolicyEngine | null | undefined, stage: "request" | "approval"): string | null {
  const e = engine ?? policyEngine();
  return decision.reasons.find((r) => r.startsWith("deny.") && (e?.stage(r) ?? "request") === stage) ?? null;
}

export function validatePolicies(text = POLICY_TEXT): string[] {
  if (!cedar) return [NOT_LOADED];
  const answer = cedar.validate({ schema: SCHEMA_TEXT, policies: { staticPolicies: text } });
  if (answer.type !== "success") return answer.errors.map((e) => e.message);
  return answer.validationErrors.map((e) => `${e.policyId}: ${e.error.message}`);
}
