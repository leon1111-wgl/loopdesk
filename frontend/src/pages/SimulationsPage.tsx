import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Check, Dices, Flag, RotateCcw, Send, X } from "lucide-react";
import { useAuth } from "../auth";
import { Drawer, Empty, PageHeader, Segmented, TOOL_TEXT } from "../components/ui";
import { isDemo, REPO_URL } from "../demo/replay";
import type { DemoEngine } from "../demo/engine";
import type { Diff, Grade, Persona, Scenario, State, Table } from "../demo/engine/endstate";
import type { SeedOrder } from "../demo/engine/pack";
import type { CustomOutcome, RefundRule } from "../demo/engine/customgoal";
import type { AgentEvent } from "../types";
import devSet from "../../../backend/evals/simulations/dev.json";
import blindSet from "../../../backend/evals/simulations/blind_v1.json";

/**
 * 模拟顾客: a customer with a hidden goal talks to the agent, and the conversation is graded by the state it leaves
 * (app/evals/simulate.py; τ²-bench's method). Two halves:
 * - the game: the visitor plays a card against the browser rule engine, in a fresh copy of the demo data, graded in the tab
 *   by the same rules as the backend (demo/engine/endstate.ts);
 * - the recorded runs: an open-weight model played every card against the real backend on GitHub Actions
 *   (.github/workflows/simulate.yml); the transcripts and diffs are a committed recording, never a live run.
 */

const PERSONA_TEXT: Record<Persona, string> = {
  plain: "直接", colloquial: "口语", withholding: "不主动给细节", indecisive: "犹豫", pushy: "着急施压", adversarial: "钻空子",
};
const TABLE_TEXT: Record<Table, string> = { orders: "订单状态", refunds: "已退款（台账）", approvals: "审批队列", tickets: "售后工单", handoff: "转人工" };
const ORDER_STATUS: Record<string, string> = { delivered: "已签收", shipped: "已发货", paid: "已付款", refunding: "退款中", refunded: "已退款", cancelled: "已取消" };
const APPROVAL_STATUS: Record<string, string> = { pending: "待审批", approved: "已通过", rejected: "已驳回", withdrawn: "已撤回" };
const OUTCOME_TEXT: Record<string, [string, string]> = {
  pass: ["通过", "badge-success"],
  wrong_money: ["动钱错误", "badge-danger"],
  missed_action: ["该办没办", "badge-warning"],
  not_said: ["没说到要点", "badge-warning"],
  other_state: ["终态不一致", "badge-warning"],
  rule_mismatch: ["目标和规则不符", "badge-info"],
};
const ACCOUNTS: Record<string, { name: string; tenant: string; shop: string }> = {
  "customer@leon.demo": { name: "Emma", tenant: "leon", shop: "Leon优选" },
  "customer2@leon.demo": { name: "Jack", tenant: "leon", shop: "Leon优选" },
  "customer@echo.demo": { name: "Lily", tenant: "echo", shop: "Echo音乐" },
  "customer2@echo.demo": { name: "Ryan", tenant: "echo", shop: "Echo音乐" },
};
const TAU2 = "https://arxiv.org/abs/2506.07982";

interface Card extends Scenario { set: "dev" | "blind_v1" | "custom" }
const CARDS: Card[] = [
  ...(devSet.scenarios as Scenario[]).map((s) => ({ ...s, set: "dev" as const })),
  ...(blindSet.scenarios as Scenario[]).map((s) => ({ ...s, set: "blind_v1" as const })),
];
const SET_TEXT: Record<Card["set"], string> = { dev: "开发集", blind_v1: "盲测集", custom: "自定义" };

function show(table: Table, value: unknown): string {
  if (table === "orders") {
    const entries = Object.entries(value as Record<string, string>);
    return entries.length ? entries.map(([k, v]) => `${k} ${ORDER_STATUS[v] ?? v}`).join("；") : "都没变";
  }
  if (table === "refunds") {
    const entries = Object.entries(value as Record<string, number>);
    return entries.length ? entries.map(([k, v]) => `${k} ¥${Number(v).toFixed(2)}`).join("；") : "没有";
  }
  if (table === "approvals") {
    const entries = Object.entries(value as Record<string, number>);
    return entries.length ? entries.map(([k, v]) => `${APPROVAL_STATUS[k] ?? k} ${v} 笔`).join("；") : "没有";
  }
  if (table === "tickets") return `${value} 张`;
  return value ? "是" : "否";
}

function DiffTable({ tables, caption, target = "目标" }: { tables: Diff; caption: string; target?: string }) {
  return (
    <div className="table-wrap">
      <table className="table sim-diff">
        <caption className="sr-only">{caption}</caption>
        <thead><tr><th scope="col">表</th><th scope="col">{target}</th><th scope="col">实际</th><th scope="col">结果</th></tr></thead>
        <tbody>
          {(Object.keys(TABLE_TEXT) as Table[]).map((t) => (
            <tr key={t} className={tables[t].ok ? "" : "sim-miss"}>
              <th scope="row">{TABLE_TEXT[t]}</th>
              <td className="small">{show(t, tables[t].expected)}</td>
              <td className="small">{show(t, tables[t].actual)}</td>
              <td>{tables[t].ok ? <span className="sim-mark ok"><Check size={14} aria-hidden="true" /><span className="sim-mark-text">一致</span></span> : <span className="sim-mark bad"><X size={14} aria-hidden="true" /><span className="sim-mark-text">不一致</span></span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CardFace({ card }: { card: Card }) {
  const who = ACCOUNTS[card.as];
  return (
    <div className="sim-card">
      <div className="row row-wrap" style={{ gap: 6 }}>
        {card.set !== "custom" && <span className="badge badge-primary">{PERSONA_TEXT[card.persona]}</span>}
        <span className="badge">{SET_TEXT[card.set]}</span>
        {card.set !== "custom" && <span className="faint small mono">{card.key}</span>}
      </div>
      <p className="sim-card-who">你是 <b>{card.name ?? who?.name}</b> · {who?.shop ?? card.tenant} 的顾客</p>
      <p className="sim-card-goal">{card.card}</p>
      {!!card.facts?.length && (
        <ul className="sim-facts" aria-label="你手上的信息">{card.facts.map((f) => <li key={f}>{f}</li>)}</ul>
      )}
      {!!card.behaviour?.length && <p className="faint small">说话方式：{card.behaviour.join("；")}</p>}
    </div>
  );
}

const CLI = "cd backend && python -m app.evals.simulate --play leon_mug_cracked";

export default function SimulationsPage() {
  const [tab, setTab] = useState<"play" | "recorded">(isDemo ? "play" : "recorded");
  return (
    <div className="page stack sim-page" style={{ gap: 16 }}>
      <PageHeader title="模拟顾客"
        description="顾客带着隐藏目标来对话，结束后只看数据停在哪里：订单、退款台账、审批队列、售后工单、转人工五张表都和目标一致才算通过，卡上没提到的必须保持原样。不评回答写得好不好。" />
      <p className="sim-source small">
        做法对应 <a href={TAU2} target="_blank" rel="noreferrer">τ²-bench</a>（Sierra 研究团队，2025-06）的模拟用户与数据终态检查；业界怎么用见<a href={`${REPO_URL}/blob/main/docs/FRONTIER_REVIEW.md#what-large-companies-run-in-production大公司在生产里怎么做`} target="_blank" rel="noreferrer">前沿评审</a>。
      </p>
      <div className="tabs" aria-label="模拟顾客">
        <button aria-pressed={tab === "play"} className={tab === "play" ? "active" : ""} onClick={() => setTab("play")}>试一个目标</button>
        <button aria-pressed={tab === "recorded"} className={tab === "recorded" ? "active" : ""} onClick={() => setTab("recorded")}>录制的模拟运行</button>
      </div>
      {tab === "play" ? (isDemo ? <Game /> : (
        <div className="card card-pad stack">
          <p>本地运行时，用这条命令扮演任意一张卡里的顾客，对话走真实后端，结束后打印五张表的对比：</p>
          <pre className="json">{CLI}</pre>
          <p className="faint small">在线演示里同一个玩法由浏览器内的规则引擎回答。</p>
        </div>
      )) : <Recorded />}
    </div>
  );
}

// ------------------------------------------------------------------ the game

type Phase = "pick" | "play" | "graded";
interface Line { who: "you" | "agent"; text: string; tools?: string[]; approval?: boolean }

function Game() {
  const { user } = useAuth();
  const [phase, setPhase] = useState<Phase>("pick");
  const [card, setCard] = useState<Card | null>(null);
  const [engine, setEngine] = useState<DemoEngine | null>(null);
  const [start, setStart] = useState<State | null>(null);
  const [lines, setLines] = useState<Line[]>([]);
  const [draft, setDraft] = useState("");
  const [result, setResult] = useState<Grade | null>(null);
  const [error, setError] = useState<string | null>(null);
  const resultRef = useRef<HTMLHeadingElement>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // braces: newer browsers return a Promise from scrollIntoView, and an effect may return only its cleanup
    bottom.current?.scrollIntoView?.({ block: "nearest" });
  }, [lines.length]);
  useEffect(() => {
    if (phase === "graded") resultRef.current?.focus();
  }, [phase]);

  async function begin(next: Card) {
    setError(null);
    try {
      const [{ freshEngine }, { endState }] = await Promise.all([import("../demo/replay"), import("../demo/engine/endstate")]);
      const fresh = await freshEngine();
      setEngine(fresh);
      setStart(endState(fresh, next.tenant, [1]));
      setCard(next);
      setLines([]);
      setDraft("");
      setResult(null);
      setPhase("play");
    } catch (e) {
      setError(`规则引擎加载失败：${e instanceof Error ? e.message : String(e)}，请刷新页面重试`);
    }
  }

  function send(event: FormEvent) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || !engine || !card || phase !== "play") return;
    const events = engine.ask(card.as, 1, text);
    const final = events.find((e): e is Extract<AgentEvent, { type: "final" }> => e.type === "final");
    const tools = events.filter((e): e is Extract<AgentEvent, { type: "tool_call" }> => e.type === "tool_call").map((e) => e.name);
    setLines((now) => [...now, { who: "you", text }, { who: "agent", text: final?.text ?? "（没有回答）", tools, approval: events.some((e) => e.type === "approval_required") }]);
    setDraft("");
  }

  async function finish() {
    if (!engine || !card || !start) return;
    const { endState, grade } = await import("../demo/engine/endstate");
    setResult(grade(card, start, endState(engine, card.tenant, [1]), lines.filter((l) => l.who === "agent").map((l) => l.text)));
    setPhase("graded");
  }

  if (phase === "pick") return <Picker onPick={begin} error={error} defaultAccount={user?.email} />;
  return (
    <div className="sim-game">
      <aside className="card card-pad sim-side">
        <h2 className="sim-h">任务卡</h2>
        {card && <CardFace card={card} />}
        <p className="faint small">别把卡念给客服，按这位顾客的说法来；觉得办完了或办不成了，就点“结束并评分”。</p>
        <div className="row row-wrap">
          <button className="btn btn-sm" onClick={() => card && begin(card)}><RotateCcw size={14} aria-hidden="true" /> 重来</button>
          <button className="btn btn-sm" onClick={() => setPhase("pick")}>换一张卡</button>
        </div>
      </aside>
      <section className="card sim-chat" aria-label="对话">
        <div className="sim-lines" aria-live="polite">
          <div className="msg assistant"><div className="avatar bot">AI</div><div className="bubble"><div className="bubble-text">您好，这里是{card ? ACCOUNTS[card.as]?.shop : ""}在线客服，请问有什么可以帮您？</div></div></div>
          {lines.map((l, i) => (
            <div key={i} className={`msg ${l.who === "you" ? "user" : "assistant"}`}>
              <div className={`avatar ${l.who === "you" ? "me" : "bot"}`}>{l.who === "you" ? (card?.name ?? "我").slice(0, 1) : "AI"}</div>
              <div className="bubble">
                <div className="bubble-text">{l.text}</div>
                {l.who === "agent" && (
                  <div className="bubble-meta">
                    <span className="badge engine-tag">规则引擎 · 模拟</span>
                    {l.approval && <span className="badge badge-warning">已提交人工审核</span>}
                    {!!l.tools?.length && <span className="faint small">工具：{l.tools.map((t) => TOOL_TEXT[t] ?? t).join(" → ")}</span>}
                  </div>
                )}
              </div>
            </div>
          ))}
          <div ref={bottom} />
        </div>
        {phase === "play" ? (
          <form className="composer sim-composer" onSubmit={send}>
            <label className="sr-only" htmlFor="sim-input">以顾客身份发送</label>
            <input id="sim-input" className="input" value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="以顾客身份输入…" autoComplete="off" maxLength={500} />
            <button className="btn btn-primary" type="submit" disabled={!draft.trim()}><Send size={15} aria-hidden="true" /> 发送</button>
            <button className="btn" type="button" onClick={finish} disabled={!lines.length}><Flag size={15} aria-hidden="true" /> 结束并评分</button>
          </form>
        ) : result && (
          <div className="sim-result">
            <h3 ref={resultRef} tabIndex={-1} className="row" style={{ gap: 8 }}>
              评分 <span className={`badge ${OUTCOME_TEXT[result.outcome][1]}`}>{OUTCOME_TEXT[result.outcome][0]}</span>
            </h3>
            {result.outcome === "rule_mismatch" && card?.rule ? (
              <>
                <p className="small">你选的结局是“{card.rule.chosen}”，但按这单的规则，直接申请退款会{card.rule.note}。所以这一轮不判客服的对错，下表按规则的终态对比：{result.ruleKept ? "对话停在了规则会到的地方。" : "对话没有停在规则会到的地方（比如还没有提出申请）。"}</p>
                <DiffTable tables={result.tables} caption="按规则的终态和对话结束时的实际数据" target="按规则" />
              </>
            ) : <DiffTable tables={result.tables} caption="这张卡要求的终态和对话结束时的实际数据" />}
            {result.notSaid.length > 0 && <p className="small">客服没有说到：{result.notSaid.map((s) => s.split("|")[0]).join("、")}</p>}
            <details className="small">
              <summary>这个评分说明什么</summary>
              <p>只比较对话结束时的数据：五张表都和这张卡的目标一致才算通过，卡上没提到的必须保持原样。“动钱错误”是退了或提交了目标里没有的钱，这是发布前必须为 0 的一项；“该办没办”是少办了，顾客还得再说一句。回答由浏览器里的规则引擎给出，不是模型，不代表真实模型的质量；改动只在这一轮的模拟数据里，不影响页面上其他地方。</p>
            </details>
            <div className="row row-wrap">
              <button className="btn btn-primary" onClick={() => card && begin(card)}><RotateCcw size={14} aria-hidden="true" /> 再玩一次</button>
              <button className="btn" onClick={() => setPhase("pick")}>换一张卡</button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

const PERSONA_FILTER: ["all" | Persona, string][] = [["all", "全部"], ...(Object.keys(PERSONA_TEXT) as Persona[]).map((p): [Persona, string] => [p, PERSONA_TEXT[p]])];

const OUTCOMES: { value: CustomOutcome; label: string }[] = [
  { value: "refund", label: "自动退款，钱退回" },
  { value: "review", label: "提交人工审核（先不退钱）" },
  { value: "ticket", label: "开一张售后工单" },
  { value: "handoff", label: "转人工" },
  { value: "nothing", label: "什么都不改变" },
];

function Picker({ onPick, error, defaultAccount }: { onPick: (card: Card) => void; error: string | null; defaultAccount?: string }) {
  const [persona, setPersona] = useState<"all" | Persona>("all");
  const [custom, setCustom] = useState(false);
  const shown = CARDS.filter((c) => persona === "all" || c.persona === persona);
  return (
    <div className="stack" style={{ gap: 12 }}>
      {error && <div className="error-banner" role="alert">{error}</div>}
      <div className="row row-wrap" style={{ gap: 8 }}>
        <Segmented label="顾客类型" options={PERSONA_FILTER} value={persona} onChange={setPersona} />
        <span className="spacer" />
        <button className="btn" onClick={() => onPick(shown[Math.floor(Math.random() * shown.length)])}><Dices size={15} aria-hidden="true" /> 随机一张</button>
        <button className="btn" aria-expanded={custom} onClick={() => setCustom(!custom)}>自己写目标</button>
      </div>
      {custom && <CustomCard onPick={onPick} defaultAccount={defaultAccount} />}
      <ul className="sim-pick" aria-label="任务卡">
        {shown.map((c) => (
          <li key={`${c.set}-${c.key}`}>
            <button className="sim-pick-item" onClick={() => onPick(c)}>
              <span className="row" style={{ gap: 6 }}>
                <span className="badge badge-primary">{PERSONA_TEXT[c.persona]}</span>
                <span className="faint small">{ACCOUNTS[c.as]?.shop} · {c.name ?? ACCOUNTS[c.as]?.name} · {SET_TEXT[c.set]}</span>
              </span>
              <span className="clamp-2">{c.card}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function CustomCard({ onPick, defaultAccount }: { onPick: (card: Card) => void; defaultAccount?: string }) {
  const [account, setAccount] = useState(defaultAccount && ACCOUNTS[defaultAccount] ? defaultAccount : "customer@leon.demo");
  const [orders, setOrders] = useState<SeedOrder[]>([]);
  const [order, setOrder] = useState("");
  const [outcome, setOutcome] = useState<CustomOutcome>("refund");
  const [goal, setGoal] = useState("");
  const [rule, setRule] = useState<RefundRule | null>(null);
  const tenant = ACCOUNTS[account].tenant;
  useEffect(() => {
    import("../demo/engine/pack").then(({ pack }) => {
      const mine = (pack.orders[tenant] ?? []).filter((o) => o.customer === account);
      setOrders(mine);
      setOrder(mine[0]?.id ?? "");
    });
  }, [account, tenant]);
  // What the rules do with a plain request on this order: the engine itself answers it, in its own fresh copy of the data.
  useEffect(() => {
    let current = true;
    setRule(null);
    if (!order) return;
    Promise.all([import("../demo/replay"), import("../demo/engine/customgoal")]).then(async ([{ freshEngine }, { rulesForRefund }]) => {
      const answer = rulesForRefund(await freshEngine(), account, order, tenant);
      if (current) setRule(answer);
    }).catch(() => undefined);
    return () => {
      current = false;
    };
  }, [account, order, tenant]);
  const chosen = useMemo(() => orders.find((o) => o.id === order), [orders, order]);
  const needsOrder = outcome === "refund" || outcome === "review";
  const fits = (value: string) => !rule || !((value === "refund" && rule.kind !== "auto") || (value === "review" && rule.kind !== "review"));

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (needsOrder && !chosen) return;
    const { customScenario } = await import("../demo/engine/customgoal");
    const label = OUTCOMES.find((o) => o.value === outcome)!.label;
    const card = customScenario({
      email: account, tenant, name: ACCOUNTS[account].name, outcome, goal, label,
      order: chosen ? { id: chosen.id, amount: chosen.amount, items: chosen.items.map((i) => i.name).join("、") } : undefined,
    }, chosen ? rule : null);
    onPick({ ...card, set: "custom" });
  }

  return (
    <form className="card card-pad sim-custom" onSubmit={submit}>
      <label className="field"><span>顾客</span>
        <select className="select" value={account} onChange={(e) => setAccount(e.target.value)}>
          {Object.entries(ACCOUNTS).map(([email, a]) => <option key={email} value={email}>{a.name} · {a.shop}</option>)}
        </select>
      </label>
      <label className="field"><span>订单</span>
        <select className="select" value={order} onChange={(e) => setOrder(e.target.value)}>
          {orders.map((o) => <option key={o.id} value={o.id}>{o.id} · {o.items.map((i) => i.name).join("、")} · ¥{o.amount}</option>)}
        </select>
      </label>
      <label className="field"><span>你认为正确的结局</span>
        <select className="select" value={outcome} onChange={(e) => setOutcome(e.target.value as CustomOutcome)}>
          {OUTCOMES.map((o) => <option key={o.value} value={o.value}>{o.label}{fits(o.value) ? "" : "（按规则这单不会）"}</option>)}
        </select>
      </label>
      <label className="field sim-custom-goal"><span>目标（选填，只给你自己看）</span>
        <input className="input" value={goal} onChange={(e) => setGoal(e.target.value)} maxLength={200} placeholder="例如：杯子裂了想退钱，但先不给订单号" />
      </label>
      <p className="small sim-custom-note" role="status">
        {rule ? <>按这单的规则，直接说“{order} 申请退款”会：<b>{rule.note}</b>。</> : "正在查这单按规则会怎么办…"}
        {rule && !fits(outcome) && <> 你选的结局和规则不符：这一轮会按规则的终态对比，并说明规则会怎么办，不会把客服照规则办事算成错误。</>}
      </p>
      <p className="faint small sim-custom-note">评分按你选的结局判：选“自动退款”就要求这单按实付金额退回；其他订单和表必须保持原样。</p>
      <div className="row"><button className="btn btn-primary" type="submit" disabled={needsOrder && !chosen}>用这张卡开始</button></div>
    </form>
  );
}

// ------------------------------------------------------------------ recorded runs

interface RunRow {
  key: string;
  set: string;
  seed: number;
  persona: Persona;
  passed: boolean;
  outcome: string;
  stop: string;
  turns: number;
  handoff: boolean;
  tables: Diff;
  not_said: string[];
  transcript: { role: "customer" | "agent"; text: string; sent?: boolean; tools?: { name: string; error: boolean }[]; approval?: boolean }[];
}
interface Block { runs: number; scenarios: number; passed: number; pass_all_seeds: number; handoffs: number; wrong_money: number; missed_action: number; other_state: number; not_said: number; hit_max_turns: number; customer_errors: number }
interface Report {
  meta: { commit: string; date: string; seeds: number[]; sets: string[]; customer_model: string; temperature: number; agent: string; run_url: string };
  summary: Record<string, { all: Block; by_persona: Record<string, Block> }>;
  runs: RunRow[];
  audit?: { sample: string; checked: number; errors: Record<string, number>; note: string };
}

function Recorded() {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => {
    import("../../../demo/simulations/report.json").then((m) => setReport(m.default as unknown as Report)).catch((e) => setError(String(e)));
  }, []);
  if (error) return <div className="error-banner" role="alert">录制结果加载失败：{error}</div>;
  if (!report) return <Empty>加载录制结果…</Empty>;
  const { meta } = report;
  const keys = [...new Set(report.runs.map((r) => `${r.set}/${r.key}`))];
  const cardOf = (setKey: string) => CARDS.find((c) => `${c.set}/${c.key}` === setKey);
  return (
    <div className="stack" style={{ gap: 12 }}>
      <div className="notice sim-meta" role="note">
        <p>
          <b>录制结果，不是实时运行。</b>扮演顾客的是 {meta.customer_model}（llama.cpp，GitHub Actions 的 CPU），温度 {meta.temperature}，种子 {meta.seeds.join("、")}；
          被测的是 LoopDesk 后端（{meta.agent === "offline" ? "离线模型，即规则和工具" : meta.agent}），不是这个页面的浏览器引擎。
          提交 <a href={`${REPO_URL}/commit/${meta.commit}`} target="_blank" rel="noreferrer" className="mono">{meta.commit.slice(0, 7)}</a>，{meta.date.slice(0, 10)}，
          <a href={meta.run_url} target="_blank" rel="noreferrer">Actions 运行记录</a>。
        </p>
      </div>
      <div className="card table-wrap">
        <table className="table">
          <caption className="sr-only">按顾客类型汇总</caption>
          <thead><tr><th scope="col">集合</th><th scope="col">顾客类型</th><th scope="col">终态通过</th><th scope="col">pass^{meta.seeds.length}</th><th scope="col">动钱错误</th><th scope="col">该办没办</th><th scope="col">转人工</th></tr></thead>
          <tbody>
            {Object.entries(report.summary).flatMap(([set, s]) => [["all", s.all] as const, ...Object.entries(s.by_persona)].map(([p, b]) => (
              <tr key={`${set}-${p}`} className={p === "all" ? "sim-total" : ""}>
                <td className="nowrap">{SET_TEXT[set as Card["set"]] ?? set}</td>
                <td className="nowrap">{p === "all" ? "全部" : PERSONA_TEXT[p as Persona] ?? p}</td>
                <td className="mono">{b.passed}/{b.runs}</td>
                <td className="mono">{b.pass_all_seeds}/{b.scenarios}</td>
                <td className={`mono ${b.wrong_money ? "sim-bad" : ""}`}>{b.wrong_money}</td>
                <td className="mono">{b.missed_action}</td>
                <td className="mono">{b.handoffs}</td>
              </tr>
            )))}
          </tbody>
        </table>
      </div>
      {report.audit && <p className="small">模拟器自身的错误（人工审计 {report.audit.sample}，{report.audit.checked} 段）：{Object.entries(report.audit.errors).map(([k, v]) => `${k} ${v}`).join("，")}。{report.audit.note}</p>}
      <ul className="sim-runs" aria-label="每张卡的结果">
        {keys.map((setKey) => {
          const runs = report.runs.filter((r) => `${r.set}/${r.key}` === setKey).sort((a, b) => a.seed - b.seed);
          const card = cardOf(setKey);
          return (
            <li key={setKey}>
              <button className="sim-run-item" onClick={() => setOpen(setKey)}>
                <span className="row" style={{ gap: 6, minWidth: 0 }}>
                  <span className="badge">{SET_TEXT[runs[0].set as Card["set"]] ?? runs[0].set}</span>
                  <span className="badge badge-primary">{PERSONA_TEXT[runs[0].persona]}</span>
                  <span className="mono small ellipsis">{runs[0].key}</span>
                </span>
                <span className="clamp-2 small">{card?.card}</span>
                <span className="sim-seeds" aria-label={`各种子：${runs.map((r) => `种子 ${r.seed} ${OUTCOME_TEXT[r.outcome]?.[0] ?? r.outcome}`).join("，")}`}>
                  {runs.map((r) => <i key={r.seed} className={`sim-dot ${r.passed ? "ok" : r.outcome === "wrong_money" ? "bad" : "miss"}`} title={`种子 ${r.seed}：${OUTCOME_TEXT[r.outcome]?.[0] ?? r.outcome}`} />)}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {open && (
        <RunDrawer card={cardOf(open)} runs={report.runs.filter((r) => `${r.set}/${r.key}` === open).sort((a, b) => a.seed - b.seed)} onClose={() => setOpen(null)} />
      )}
    </div>
  );
}

function RunDrawer({ card, runs, onClose }: { card?: Card; runs: RunRow[]; onClose: () => void }) {
  const [seed, setSeed] = useState(runs[0]?.seed ?? 1);
  const run = runs.find((r) => r.seed === seed) ?? runs[0];
  return (
    <Drawer title={`${run.key} · 录制`} onClose={onClose} width={720}>
      <div className="stack" style={{ gap: 12 }}>
        {card && <CardFace card={card} />}
        <Segmented label="种子" options={runs.map((r): [string, string] => [String(r.seed), `种子 ${r.seed} · ${OUTCOME_TEXT[r.outcome]?.[0] ?? r.outcome}`])} value={String(seed)} onChange={(v) => setSeed(Number(v))} />
        <p className="faint small">顾客发了 {run.turns} 条；结束原因：{run.stop === "done" ? "顾客说办完了" : run.stop === "max_turns" ? "到了 8 条上限" : run.stop}</p>
        <div className="sim-lines">
          {run.transcript.map((t, i) => (
            <div key={i} className={`msg ${t.role === "customer" ? "user" : "assistant"}`}>
              <div className={`avatar ${t.role === "customer" ? "me" : "bot"}`}>{t.role === "customer" ? "顾" : "AI"}</div>
              <div className="bubble">
                <div className="bubble-text">{t.text}</div>
                {t.role === "customer" && t.sent === false && <div className="faint small">（结束语，没有发给客服）</div>}
                {t.role === "agent" && (
                  <div className="bubble-meta">
                    {t.approval && <span className="badge badge-warning">已提交人工审核</span>}
                    {!!t.tools?.length && <span className="faint small">工具：{t.tools.map((x) => `${TOOL_TEXT[x.name] ?? x.name}${x.error ? "（被拒）" : ""}`).join(" → ")}</span>}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
        <DiffTable tables={run.tables} caption="目标终态和这次运行结束时的实际数据" />
        {run.not_said.length > 0 && <p className="small">客服没有说到：{run.not_said.map((s) => s.split("|")[0]).join("、")}</p>}
        {card?.why && <p className="faint small">判定依据：{card.why}</p>}
        <p className="faint small">在真实后端上自己扮演这位顾客：<code>python -m app.evals.simulate --play {run.key}{run.set === "blind_v1" ? " --set blind_v1" : ""}</code>（在 backend 目录运行）。</p>
      </div>
    </Drawer>
  );
}
