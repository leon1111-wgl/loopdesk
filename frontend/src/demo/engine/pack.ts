/**
 * The data the browser engine answers from, generated from the backend (backend/app/ops/demo_pack.py): the
 * regular expressions behind the money rules, fixed texts, the demo tenants' orders and limits, knowledge chunks
 * and a word list. Rules are data so they cannot drift: backend/tests/test_demo_pack.py fails when this file no
 * longer matches the backend.
 */
import raw from "./pack.json";

interface Rule {
  source: string;
  flags: string;
}

export interface OrderItem {
  name: string;
  qty: number;
  price: number;
  virtual?: boolean;
  no_online_refund?: string;
}

export interface SeedOrder {
  id: string;
  customer: string;
  amount: number;
  status: string;
  items: OrderItem[];
  region: string | null;
  paid_ago_h: number | null;
  delivered_ago_h: number | null;
  shipment: { carrier: string; tracking_no: string; events: { ago_h: number; desc: string }[] } | null;
}

export interface SeedRefund {
  order_id: string;
  customer: string;
  amount: number;
  status: string;
  ago_h: number | null;
  reason: string;
}

export interface KbChunk {
  id: number;
  doc: string;
  doc_id: number;
  heading: string;
  text: string;
  visibility: string;
}

export interface TenantConfig {
  name: string;
  industry: string;
  auto_refund_limit: number;
  approval_admin_threshold: number;
  ticket_sla_hours: number;
  return_window_days: number;
  quality_window_days: number;
  official_domains: string[];
}

export interface Account {
  id: number;
  name: string;
  role: "admin" | "agent" | "customer";
  tenant: string;
}

export interface TenantTool {
  name: string;
  label: string;
  risk: "read" | "write" | "money";
  description: string;
  parameters: { properties?: Record<string, { type?: string; description?: string; pattern?: string; enum?: unknown[]; default?: unknown }>; required?: string[] };
  binding: { result?: { message?: string } };
  approval?: { mode?: string; amount_field?: string; auto_limits?: Record<string, number>; admin_above?: Record<string, number>; approver?: string; customer_note?: string };
  hints?: { intents?: string[]; requires?: string[] };
  enabled: boolean;
}

export interface Pack {
  tenants: Record<string, TenantConfig>;
  accounts: Record<string, Account>;
  orders: Record<string, SeedOrder[]>;
  refunds: Record<string, SeedRefund[]>;
  kb: Record<string, KbChunk[]>;
  tools: Record<string, TenantTool[]>;
  members: Record<string, { id: string; owner: string; points: number; tier: string }[]>;
  settings: {
    min_relevance: number;
    retrieval_top_k: number;
    refund_window_days: number;
    refund_window_max_count: number;
    refund_window_max_amount: number;
    max_agent_steps: number;
  };
  rules: {
    text: Record<string, Rule>;
    mock: Record<string, Rule>;
    rewrite: Record<string, Rule>;
    tools: Record<string, Rule>;
    handoff: Record<string, Rule>;
    outcome: Record<string, Rule>;
    verifier: Record<string, Rule>;
    confirm: Record<string, Rule>;
    injection: [string, Rule][];
    pii: [string, Rule][];
  };
  texts: {
    mock: Record<string, string>;
    tools: Record<string, string> & { STATUS_TEXT: Record<string, string>; PRIORITY_TEXT: Record<string, string>; REFUND_STATUS_TEXT: Record<string, string> };
    handoff: Record<string, string>;
    query_synonyms: Record<string, string>;
    stop_tokens: string[];
    typos: [string, string][];
    leading: string[];
    mixed_words: string[];
    yes_words: string[];
    ba_ok: string[];
  };
  words: string;
  t2s: string;
}

export const pack = raw as unknown as Pack;

function compile(group: Record<string, Rule>): Record<string, RegExp> {
  return Object.fromEntries(Object.entries(group).map(([name, rule]) => [name, new RegExp(rule.source, rule.flags)]));
}

export const T = compile(pack.rules.text); // app/textutil.py
export const M = compile(pack.rules.mock); // app/llm/mock.py
export const W = compile(pack.rules.rewrite); // app/agent/rewrite.py
export const TOOLS = compile(pack.rules.tools); // app/agent/business_tools.py
export const OUTCOME = compile(pack.rules.outcome); // app/agent/outcome.py
export const INJECTION: [string, RegExp][] = pack.rules.injection.map(([name, rule]) => [name, new RegExp(rule.source, rule.flags)]);
export const PII: [string, RegExp][] = pack.rules.pii.map(([name, rule]) => [name, new RegExp(rule.source, rule.flags)]);
