/**
 * The second tier of the demo's answers: Qwen3-0.6B in the visitor's browser, downloaded only after a click.
 *
 * The model never writes the answer. Under a JSON schema it picks what the customer wants, which of their orders and which
 * knowledge chunks; the program checks each choice against what it may be (their own orders, the chunks it offered) and
 * then runs the same tools and rules as tier 1, so every fact comes from the data. A write or a refund still has to pass
 * the rules on the customer's own words: the model can only choose among reads.
 */
import { M, T } from "./pack";
import { search } from "./retrieval";
import type { ModelChoice } from "./runtime";
import { canonical, findAll, normalizeWidth, refundRequested, refundWithdrawRequested } from "./text";
import { World } from "./tools";

export const INTENTS = ["order_status", "shipment", "refund_request", "refund_question", "refund_progress", "refund_withdraw", "policy_question", "fault_report", "human", "other"] as const;
export type Intent = (typeof INTENTS)[number];
export const NO_ORDER = "none";
const MAX_CANDIDATES = 6;
const CANDIDATE_CHARS = 110;

export interface Candidate {
  n: number;
  chunk: number;
  doc: string;
  heading: string;
  text: string;
}

export interface ModelPrompt {
  messages: { role: "system" | "user"; content: string }[];
  schema: Record<string, unknown>;
  candidates: Candidate[];
  orders: string[];
}

const SYSTEM = `你是客服系统的路由器，只做选择，不回答问题，不编写任何事实。
读顾客这句话，输出 JSON：
- intent：order_status 查订单状态；shipment 查物流；refund_request 顾客明确要求退款；refund_question 只是咨询退款、假设、犹豫或已撤回；refund_progress 问退款进度；refund_withdraw 撤回退款申请；policy_question 问政策规则；fault_report 报告商品故障；human 要人工；other 其他。
- order_id：顾客说的是哪一单，从给出的订单里选，说不清就选 none。
- evidence：哪些资料编号能回答这个问题，最多 3 个；都不能回答就给空数组。不要猜。`;

/** The question the model reads, and the closed choices it may give. */
export function buildPrompt(world: World, tenant: string, email: string, role: string, question: string): ModelPrompt {
  const text = canonical(normalizeWidth(question));
  const orders = role === "customer" ? world.ordersOf(tenant, email).slice(0, 5) : [];
  const scope = new Set(role === "customer" ? ["public"] : ["public", "internal"]);
  const candidates: Candidate[] = search(tenant, text, scope, MAX_CANDIDATES, false).map((hit, i) => ({
    n: i + 1, chunk: hit.chunk.id, doc: hit.chunk.doc, heading: hit.chunk.heading, text: hit.chunk.text.slice(0, CANDIDATE_CHARS),
  }));
  const mentioned = findAll(T.ORDER_ID_RE, text).map((m) => m[0].toUpperCase());
  const allowed = [...new Set([...orders.map((o) => o.id), ...mentioned])];
  const orderLines = orders.map((o) => `${o.id}：${o.items.map((i) => i.name).join("、")}`).join("\n") || "（无）";
  const evidenceLines = candidates.map((c) => `${c.n}. ${c.heading}：${c.text}`).join("\n") || "（无）";
  return {
    messages: [
      { role: "system", content: SYSTEM },
      { role: "user", content: `顾客的订单：\n${orderLines}\n\n资料：\n${evidenceLines}\n\n顾客说：${text}` },
    ],
    schema: {
      type: "object",
      properties: {
        intent: { type: "string", enum: INTENTS },
        order_id: { type: "string", enum: [...allowed, NO_ORDER] },
        evidence: { type: "array", items: { type: "integer", enum: candidates.length ? candidates.map((c) => c.n) : [0] }, maxItems: 3 },
      },
      required: ["intent", "order_id", "evidence"],
    },
    candidates,
    orders: orders.map((o) => o.id),
  };
}

const QUESTION_FOR: Partial<Record<Intent, (order: string) => string>> = {
  order_status: (o) => `${o} 查一下`,
  shipment: (o) => `${o} 到哪了`,
  refund_progress: (o) => `${o} 退款进度`,
};

/**
 * What the program keeps of the model's answer. Null: nothing usable, tier 1 answers on its own.
 * An order must be the customer's or named in their words; evidence must be one of the chunks offered; and anything that
 * writes or moves money goes back through the rules on the customer's own text, so the model cannot talk them into it.
 */
export function validateChoice(raw: string, prompt: ModelPrompt, question: string): ModelChoice | null {
  let parsed: { intent?: unknown; order_id?: unknown; evidence?: unknown };
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || !INTENTS.includes(parsed.intent as Intent)) return null;
  const text = canonical(normalizeWidth(question));
  const intent = parsed.intent as Intent;
  const named = findAll(T.ORDER_ID_RE, text).map((m) => m[0].toUpperCase());
  const allowed = new Set([...prompt.orders, ...named]);
  const notes: string[] = [];
  let order: string | null = null;
  if (typeof parsed.order_id === "string" && parsed.order_id !== NO_ORDER) {
    if (allowed.has(parsed.order_id.toUpperCase())) order = parsed.order_id.toUpperCase();
    else notes.push(`丢弃不在可选范围内的订单 ${parsed.order_id.slice(0, 24)}`);
  }
  const offered = new Map(prompt.candidates.map((c) => [c.n, c.chunk]));
  const picked = Array.isArray(parsed.evidence) ? parsed.evidence : [];
  const evidence = [...new Set(picked.filter((n) => offered.has(n as number)).map((n) => offered.get(n as number)!))].slice(0, 3);
  if (picked.length !== evidence.length) notes.push("丢弃不在候选范围内或重复的依据编号");

  // A number the customer named wins: the model may only fill in an order they did not name.
  const target = named[0] ?? order;
  let routed = text;
  const read = QUESTION_FOR[intent];
  if (read && target) routed = read(target);
  else if (intent === "refund_request" || intent === "refund_question") {
    // The same gate as the rules, on the customer's words. An order the model picked never files anything.
    if (intent === "refund_request" && !refundRequested(text)) notes.push("模型判断要退款，但这句话没有明确要求退款：不提交");
    routed = target && !named.length ? (refundRequested(text) ? text : `${target} ${text}`) : text;
  } else if (intent === "refund_withdraw" && !refundWithdrawRequested(text)) {
    notes.push("模型判断要撤回，但这句话没有明确要求撤回：不撤回");
  }
  const usesEvidence = intent === "policy_question" && evidence.length > 0 && !named.length && !M._OWN.test(text);
  return { question: routed, evidence: usesEvidence ? evidence : [], raw: raw.slice(0, 400), note: notes.join("；") || "通过校验" };
}

// ---------------------------------------------------------------- the model, in a worker
export interface LoadProgress {
  progress: number;
  text: string;
}

/** The shared copy on this site's origin: one set of weights for every project, one browser cache. */
const SHARED = "https://leon1111-wgl.github.io/career-atlas";
const CONFIG = {
  model_id: "Qwen3-0.6B-q4f16_1-MLC",
  model: `${SHARED}/models/qwen3/resolve/main/`,
  model_lib: `${SHARED}/vendor/qwen3-0.6b-webgpu.wasm`,
  runtime: `${SHARED}/vendor/webllm.js?v=20261102`,
};
const LOAD_TIMEOUT = 900_000;
const GENERATE_TIMEOUT = 120_000;

/** Released separately from the rules: a build offers the model only with VITE_DEMO_TIER2=1. */
export const TIER2_RELEASED = import.meta.env.VITE_DEMO_TIER2 === "1";

export async function webgpuAvailable(): Promise<boolean> {
  if (!TIER2_RELEASED) return false;
  const gpu = (navigator as unknown as { gpu?: { requestAdapter(): Promise<unknown> } }).gpu;
  if (!globalThis.isSecureContext || !gpu) return false;
  if (window.matchMedia?.("(pointer: coarse)").matches || window.innerWidth < 720) return false; // phones mostly cannot run it
  try {
    return Boolean(await gpu.requestAdapter());
  } catch {
    return false;
  }
}

interface Pending {
  id: number;
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timer: number;
  onProgress?: (value: LoadProgress) => void;
}

export class ModelClient {
  private worker: Worker | null = null;
  private pending: Pending | null = null;
  private sequence = 0;
  loaded = false;

  private request(type: string, payload: Record<string, unknown>, timeout: number, onProgress?: (value: LoadProgress) => void): Promise<unknown> {
    if (this.pending) return Promise.reject(new Error("模型正忙，请稍候。"));
    if (!this.worker) {
      this.worker = new Worker(`${import.meta.env.BASE_URL}model-worker.js`, { type: "module" });
      this.worker.onmessage = ({ data }) => {
        const p = this.pending;
        if (!p || p.id !== data.id) return;
        if (data.type === "progress") {
          p.onProgress?.(data.value);
          return;
        }
        window.clearTimeout(p.timer);
        this.pending = null;
        if (data.type === "error") p.reject(new Error(data.error));
        else p.resolve(data.value);
      };
      this.worker.onerror = () => this.teardown(new Error("浏览器模型运行失败，已回到规则引擎。"));
      this.worker.onmessageerror = () => this.teardown(new Error("浏览器模型通信失败，已回到规则引擎。"));
    }
    return new Promise((resolve, reject) => {
      const id = ++this.sequence;
      const timer = window.setTimeout(() => this.teardown(new Error("浏览器模型超时，已释放资源并回到规则引擎。")), timeout);
      this.pending = { id, resolve, reject, timer, onProgress };
      this.worker!.postMessage({ id, type, ...payload });
    });
  }

  async load(onProgress: (value: LoadProgress) => void): Promise<void> {
    if (this.loaded) return;
    await this.request("load", { config: CONFIG }, LOAD_TIMEOUT, onProgress);
    this.loaded = true;
  }

  async choose(prompt: ModelPrompt): Promise<string> {
    if (!this.loaded) throw new Error("模型尚未加载。");
    return String(await this.request("generate", { messages: prompt.messages, schema: prompt.schema }, GENERATE_TIMEOUT));
  }

  /** Ending the worker also stops a download in flight and releases the GPU. */
  teardown(error: Error = new Error("已停止浏览器模型。")): void {
    this.sequence++;
    this.worker?.terminate();
    this.worker = null;
    this.loaded = false;
    if (this.pending) {
      window.clearTimeout(this.pending.timer);
      this.pending.reject(error);
      this.pending = null;
    }
  }
}

