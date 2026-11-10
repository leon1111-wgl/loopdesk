/**
 * The what-if panel (pages/PolicyPage.tsx): run every journey and every recorded simulated conversation through the
 * browser engine twice, under the shipped policy and under the visitor's variant (another automatic-approval limit,
 * some policies switched off), and say which end states flip and which policy line made the difference.
 *
 * Each replay is a fresh copy of the demo data (a new DemoEngine): nothing the visitor does here touches the rest of the
 * tab. Recorded conversations are replayed line by line as the customer sent them; the customer does not react to the
 * new answers, so a flip says "these words would now end here", not what a customer would do next.
 */
import { DemoEngine, EngineError } from ".";
import { endState, TABLES, type Full, type Table } from "./endstate";
import { pack } from "./pack";
import { capture, POLICY_TEXT, policyEngine, type Decision } from "./policy";

export interface Journey { key: string; tenant: string; steps: { as?: string; say?: string; decide?: string; comment?: string }[] }
export interface Transcript { key: string; set: string; seed: number; tenant: string; as: string; lines: string[] }
export interface Variant { limits: Record<string, number>; disabled: string[] }

export interface Step { action: string; effect: string; reasons: string[]; order: string | null; amount: number | null }
export interface Replay { key: string; kind: "journey" | "transcript"; tenant: string; end: Full; steps: Step[] }
export interface Flip {
  key: string;
  kind: Replay["kind"];
  tenant: string;
  tables: Table[]; // empty: the decisions changed, the end state did not
  before: Full;
  after: Full;
  at: number; // the first decision that differs (-1: none did, the flip came from somewhere else)
  was: Step | null;
  now: Step | null;
  stopped: string[]; // policies that decided before and no longer do (switched off, or the limit moved)
  started: string[]; // policies that decide now and did not before
}

export const SHIPPED: Variant = { limits: {}, disabled: [] };

function step(d: Decision): Step {
  const refund = (d.context.refund ?? null) as { amount?: number } | null;
  return { action: d.action, effect: d.effect, reasons: d.reasons, order: null, amount: refund?.amount === undefined ? null : refund.amount / 100 };
}

function engineFor(variant: Variant): DemoEngine {
  const engine = new DemoEngine((slug) => (variant.limits[slug] === undefined ? undefined : { auto_refund_limit: variant.limits[slug] }));
  // An invariant (@locked) cannot be switched off here either, whatever the caller asks.
  const locked = new Set((policyEngine()?.policies ?? []).filter((p) => p.locked).map((p) => p.id));
  const disabled = variant.disabled.filter((id) => !locked.has(id));
  engine.world.policy = disabled.length ? policyEngine(POLICY_TEXT, new Set(disabled)) : null;
  return engine;
}

function full(engine: DemoEngine, tenant: string, ids: number[]): Full {
  const { last_message: _, ...rest } = endState(engine, tenant, ids);
  void _;
  return rest;
}

export function replayJourney(journey: Journey, variant: Variant): Replay {
  const engine = engineFor(variant);
  const conversations = new Map<string, number>();
  let next = 1;
  const [, decisions] = capture(() => {
    for (const s of journey.steps) {
      if (s.say !== undefined) {
        if (!conversations.has(s.as!)) conversations.set(s.as!, next++);
        engine.ask(s.as!, conversations.get(s.as!)!, s.say);
      } else if (s.decide) {
        const ids = [...conversations.values()];
        const target = [...engine.world.approvals].reverse().find((a) => a.conversationId !== null && ids.includes(a.conversationId) && a.status === "pending");
        if (!target) continue; // under this variant nothing waits for a person
        try {
          engine.decide(target.id, s.as!, s.decide, s.comment ?? "");
        } catch (error) {
          if (!(error instanceof EngineError)) throw error;
        }
      }
    }
  });
  return { key: journey.key, kind: "journey", tenant: journey.tenant, end: full(engine, journey.tenant, [...conversations.values()]), steps: decisions.map(step) };
}

export function replayTranscript(t: Transcript, variant: Variant): Replay {
  const engine = engineFor(variant);
  const [, decisions] = capture(() => {
    for (const line of t.lines) engine.ask(t.as, 1, line);
  });
  return { key: `${t.set}/${t.key}/种子${t.seed}`, kind: "transcript", tenant: t.tenant, end: full(engine, t.tenant, [1]), steps: decisions.map(step) };
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const sortedEntries = (o: Record<string, unknown>) => Object.entries(o).sort(([x], [y]) => x.localeCompare(y));

export function changedTables(a: Full, b: Full): Table[] {
  return TABLES.filter((t) => {
    const x = a[t];
    const y = b[t];
    return typeof x === "object" && x !== null ? !same(sortedEntries(x as Record<string, unknown>), sortedEntries(y as Record<string, unknown>)) : x !== y;
  });
}

/** How a replay changed: the end-state tables that flipped (none: the decisions changed but ended in the same place), and
 * the first decision that differs, with the policies that stopped or started deciding it. Null: nothing changed. */
export function compareReplays(before: Replay, after: Replay): Flip | null {
  const tables = changedTables(before.end, after.end);
  if (!tables.length && same(before.steps, after.steps)) return null;
  const n = Math.max(before.steps.length, after.steps.length);
  let at = -1;
  for (let i = 0; i < n; i += 1) {
    const x = before.steps[i];
    const y = after.steps[i];
    if (!x || !y || x.action !== y.action || x.effect !== y.effect || !same(x.reasons, y.reasons)) {
      at = i;
      break;
    }
  }
  const was = at >= 0 ? before.steps[at] ?? null : null;
  const now = at >= 0 ? after.steps[at] ?? null : null;
  return {
    key: before.key, kind: before.kind, tenant: before.tenant, tables, before: before.end, after: after.end, at, was, now,
    stopped: (was?.reasons ?? []).filter((r) => !(now?.reasons ?? []).includes(r)),
    started: (now?.reasons ?? []).filter((r) => !(was?.reasons ?? []).includes(r)),
  };
}

export function flipOf(before: Replay, after: Replay): Flip | null {
  const change = compareReplays(before, after);
  return change && change.tables.length ? change : null;
}

/** The tenants' shipped limits (the pack's, overridden by the recording's /api/tenant in the page). */
export const shippedLimit = (tenant: string): number => pack.tenants[tenant]?.auto_refund_limit ?? 0;
