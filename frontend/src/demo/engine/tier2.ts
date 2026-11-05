/**
 * The second tier of the demo's answers: Qwen3-0.6B in the visitor's browser, downloaded only after a click.
 *
 * It comes in when the rules are about to say the documents have no answer, and does one thing: under a JSON schema it picks
 * which of the nearest knowledge chunks answer the question, or none. The program checks each pick against what it offered and
 * quotes those chunks. It writes nothing, and it never reaches the tools: letting it choose the intent and the order measured
 * worse than the rules alone and sometimes named the wrong order (docs/EVALUATION.md), so that was taken out.
 */
import { search } from "./retrieval";
import type { ModelChoice } from "./runtime";
import { canonical, normalizeWidth } from "./text";

const MAX_CANDIDATES = 6;
const MAX_EVIDENCE = 2;
const CANDIDATE_CHARS = 110;
const MIN_RELEVANCE = 0.1; // nothing shared with the question at all: not offered

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
}

const SYSTEM = `你是客服资料筛选器，只做选择，不回答问题，不编写任何内容。
读顾客的问题和下面的资料，输出 JSON：
- can_answer：资料里确实写了这个问题的答案才为 true；只是话题相近、或者要靠猜，都为 false。
- evidence：能回答问题的资料编号，最多 2 个；can_answer 为 false 时给空数组。

例子一。资料：1. 配送：满99元包邮。2. 发票：可开电子发票。顾客问：多少钱免运费。输出：{"can_answer":true,"evidence":[1]}
例子二。资料：1. 配送：满99元包邮。2. 发票：可开电子发票。顾客问：能用信用卡分期吗。输出：{"can_answer":false,"evidence":[]}`;

/** The question the model reads, and the closed choices it may give. */
export function buildPrompt(tenant: string, role: string, question: string): ModelPrompt {
  const text = canonical(normalizeWidth(question));
  const scope = new Set(role === "customer" ? ["public"] : ["public", "internal"]);
  const candidates: Candidate[] = search(tenant, text, scope, MAX_CANDIDATES, false).filter((hit) => hit.relevance >= MIN_RELEVANCE).map((hit, i) => ({
    n: i + 1, chunk: hit.chunk.id, doc: hit.chunk.doc, heading: hit.chunk.heading, text: hit.chunk.text.slice(0, CANDIDATE_CHARS),
  }));
  const lines = candidates.map((c) => `${c.n}. ${c.heading}：${c.text}`).join("\n") || "（无）";
  return {
    messages: [
      { role: "system", content: SYSTEM },
      { role: "user", content: `资料：\n${lines}\n\n顾客问：${text}` },
    ],
    schema: {
      type: "object",
      properties: {
        can_answer: { type: "boolean" },
        evidence: { type: "array", items: { type: "integer", enum: candidates.length ? candidates.map((c) => c.n) : [0] }, maxItems: MAX_EVIDENCE },
      },
      required: ["can_answer", "evidence"],
    },
    candidates,
  };
}

/** What the program keeps of the model's answer. Null: nothing usable, and the rules' refusal stands. */
export function validateChoice(raw: string, prompt: ModelPrompt, question: string): ModelChoice | null {
  let parsed: { can_answer?: unknown; evidence?: unknown };
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || parsed.can_answer !== true) return null;
  const offered = new Map(prompt.candidates.map((c) => [c.n, c.chunk]));
  const picked = Array.isArray(parsed.evidence) ? parsed.evidence : [];
  const evidence = [...new Set(picked.filter((n) => offered.has(n as number)).map((n) => offered.get(n as number)!))].slice(0, MAX_EVIDENCE);
  if (!evidence.length) return null;
  return { question: canonical(normalizeWidth(question)), evidence, raw: raw.slice(0, 400), note: picked.length === evidence.length ? "通过校验" : "丢弃了不在候选范围内的依据编号" };
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

/**
 * Released separately from the rules: a build offers the model only with VITE_DEMO_TIER2=1. ?tier2=1 lets whoever is checking it on
 * a real GPU reach it in a published build before that; no page links to it.
 */
const TIER2_RELEASED = import.meta.env.VITE_DEMO_TIER2 === "1";
const TIER2_ASKED_FOR = () => new URLSearchParams(window.location.search).get("tier2") === "1";

export async function webgpuAvailable(): Promise<boolean> {
  if (!TIER2_RELEASED && !TIER2_ASKED_FOR()) return false;
  const gpu = (navigator as unknown as { gpu?: { requestAdapter(): Promise<{ features: Set<string> } | null> } }).gpu;
  if (!globalThis.isSecureContext || !gpu) return false;
  if (window.matchMedia?.("(pointer: coarse)").matches || window.innerWidth < 720) return false; // phones mostly cannot run it
  try {
    const adapter = await gpu.requestAdapter();
    return Boolean(adapter?.features.has("shader-f16")); // the shared weights are q4f16: without it the load could only fail
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

