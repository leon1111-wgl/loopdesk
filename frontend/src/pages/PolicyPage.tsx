import { useEffect, useMemo, useRef, useState } from "react";
import { Lock, Play, RotateCcw } from "lucide-react";
import { Empty, PageHeader } from "../components/ui";
import { isDemo, REPO_URL } from "../demo/replay";
import type { PolicyInfo } from "../demo/engine/policy";
import type { Flip, Journey, Replay, Transcript, Variant } from "../demo/engine/whatif";
import type { Full, Table } from "../demo/engine/endstate";
import journeysData from "../../../backend/evals/journeys.json";
import devSet from "../../../backend/evals/simulations/dev.json";
import blindSet from "../../../backend/evals/simulations/blind_v1.json";

/**
 * 审批策略: the Cedar policy every change of data is decided by (backend/policies/loopdesk.cedar), and a what-if panel.
 * The visitor moves an automatic-approval limit or switches rules off; every journey and every recorded simulated
 * conversation is replayed in this tab under the shipped policy and under the variant (demo/engine/whatif.ts), and the page
 * lists the end states that flip with the policy line that made the difference. All of it is 模拟: fresh copies of the
 * demo data, never the tab's own.
 */

type Engine = typeof import("../demo/engine");
type WhatIf = typeof import("../demo/engine/whatif");
const POLICY_URL = `${REPO_URL}/blob/main/backend/policies/loopdesk.cedar`;
const AGENTCORE = "https://aws.amazon.com/about-aws/whats-new/2026/03/policy-amazon-bedrock-agentcore-generally-available";
const TENANTS = [["leon", "Leon优选"], ["echo", "Echo音乐"]] as const;
const EFFECT_TEXT: Record<string, string> = { allow: "自动通过", review: "人工审批", deny: "拒绝" };
const ASKS_BACK = new Set(["deny.not_asked", "deny.order_unconfirmed", "deny.item_not_on_order"]);
const TABLE_TEXT: Record<Table, string> = { orders: "订单状态", refunds: "已退款", approvals: "审批队列", tickets: "售后工单", handoff: "转人工" };
const ORDER_STATUS: Record<string, string> = { delivered: "已签收", shipped: "已发货", paid: "已付款", refunding: "退款中", refunded: "已退款", cancelled: "已取消" };
const APPROVAL_STATUS: Record<string, string> = { pending: "待审批", approved: "已通过", rejected: "已驳回", withdrawn: "已撤回" };
const GROUPS: [string, (p: PolicyInfo) => boolean][] = [
  ["拒绝：不办，告诉顾客为什么", (p) => p.id.startsWith("deny.") && !["deny.voice_unconfirmed", "deny.four_eyes", "deny.approver_tier"].includes(p.id)],
  ["人工审批：交给一位员工决定", (p) => p.id.startsWith("review.")],
  ["渠道与审批队列", (p) => ["deny.voice_unconfirmed", "deny.four_eyes", "deny.approver_tier"].includes(p.id)],
];

interface Card { key: string; as: string; tenant: string; card: string }
const CARDS = new Map<string, Card>([...(devSet.scenarios as Card[]), ...(blindSet.scenarios as Card[])].map((c) => [c.key, c]));
const JOURNEYS = journeysData as unknown as Journey[];

let baseline: Replay[] | null = null; // the shipped policy's replays: the same for every variant, computed once per tab

function show(table: Table, value: unknown, before?: unknown): string {
  if (table === "orders") {
    const now = value as Record<string, string>;
    const was = (before ?? {}) as Record<string, string>;
    const changed = Object.entries(now).filter(([k, v]) => was[k] !== v);
    return changed.length ? changed.map(([k, v]) => `${k} ${ORDER_STATUS[v] ?? v}`).join("；") : "—";
  }
  if (table === "refunds") {
    const entries = Object.entries(value as Record<string, number>);
    return entries.length ? entries.map(([k, v]) => `${k} ¥${Number(v).toFixed(2)}`).join("；") : "没有";
  }
  if (table === "approvals") {
    const entries = Object.entries(value as Record<string, number>);
    return entries.length ? entries.map(([k, v]) => `${APPROVAL_STATUS[k] ?? k} ${v}`).join("；") : "没有";
  }
  if (table === "handoff") return value ? "是" : "否";
  return String(value);
}

export default function PolicyPage() {
  const [engine, setEngine] = useState<Engine | null>(null);
  const [whatif, setWhatif] = useState<WhatIf | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [limits, setLimits] = useState<Record<string, string>>({});
  const [off, setOff] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState<string | null>(null);
  const [progress, setProgress] = useState<[number, number] | null>(null);
  const [result, setResult] = useState<{ variant: Variant; changes: Flip[]; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const transcripts = useRef<Transcript[] | null>(null);
  const cancelled = useRef(false);

  useEffect(() => {
    cancelled.current = false;
    Promise.all([import("../demo/engine"), import("../demo/engine/whatif")])
      .then(async ([e, w]) => {
        await e.loadCedar();
        if (!cancelled.current) {
          setEngine(e);
          setWhatif(w);
        }
      })
      .catch((err) => setLoadError(String(err)));
    return () => {
      cancelled.current = true;
    };
  }, []);

  const policies = useMemo(() => (engine ? engine.policyEngine()?.policies ?? [] : []), [engine]);
  const shippedLimit = (tenant: string) => whatif?.shippedLimit(tenant) ?? 0;

  function variantNow(): Variant | string {
    const out: Record<string, number> = {};
    for (const [slug, name] of TENANTS) {
      const raw = limits[slug];
      if (raw === undefined || raw.trim() === "") continue;
      const value = Number(raw);
      if (!Number.isFinite(value) || value < 0 || value > 100000) return `${name}的自动审批额度要在 ¥0 到 ¥100000 之间`;
      if (Math.round(value * 100) / 100 !== shippedLimit(slug)) out[slug] = Math.round(value * 100) / 100;
    }
    return { limits: out, disabled: [...off].sort() };
  }

  async function rerun() {
    if (!whatif) return;
    const variant = variantNow();
    if (typeof variant === "string") {
      setError(variant);
      return;
    }
    setError(null);
    setResult(null);
    try {
      if (!transcripts.current) {
        const report = (await import("../../../demo/simulations/report.json")).default as unknown as { runs: { key: string; set: string; seed: number; tenant: string; transcript: { role: string; text: string; sent?: boolean }[] }[] };
        transcripts.current = report.runs.map((r) => ({ key: r.key, set: r.set, seed: r.seed, tenant: r.tenant, as: CARDS.get(r.key)?.as ?? "", lines: r.transcript.filter((t) => t.role === "customer" && t.sent).map((t) => t.text) }));
      }
      const items: (Journey | Transcript)[] = [...JOURNEYS, ...transcripts.current];
      const replay = (item: Journey | Transcript, v: Variant) => ("steps" in item ? whatif.replayJourney(item, v) : whatif.replayTranscript(item, v));
      const total = items.length;
      const base: Replay[] = baseline ?? [];
      const changes: Flip[] = [];
      for (let i = 0; i < total; i += 1) {
        if (cancelled.current) return;
        if (!baseline) base.push(replay(items[i], whatif.SHIPPED));
        const change = whatif.compareReplays(base[i], replay(items[i], variant));
        if (change) changes.push(change);
        if (i % 6 === 5) {
          setProgress([i + 1, total]);
          await new Promise((resolve) => setTimeout(resolve, 0)); // let the page paint between batches
        }
      }
      baseline = base;
      setResult({ variant, changes, total });
    } catch (err) {
      setError(`重算失败：${String(err)}`);
    } finally {
      setProgress(null);
    }
  }

  function reset() {
    setLimits({});
    setOff(new Set());
    setResult(null);
    setError(null);
  }

  if (!isDemo) {
    return (
      <div className="page policy-page">
        <PageHeader title="审批策略" description="退款、审批分级、四眼原则和语音确认写成 Cedar 策略，后端用 cedarpy 判定。" />
        <Empty>“如果……会怎样”只在静态演示里运行：它用浏览器里的规则引擎重算全部对话。策略原文见仓库 backend/policies/loopdesk.cedar。</Empty>
      </div>
    );
  }

  const variant = variantNow();
  const pending = typeof variant !== "string" && (Object.keys(variant.limits).length > 0 || variant.disabled.length > 0);
  const flips = result?.changes.filter((c) => c.tables.length) ?? [];
  const quiet = result?.changes.filter((c) => !c.tables.length) ?? [];
  const label = (id: string) => policies.find((p) => p.id === id)?.label ?? id;

  return (
    <div className="page policy-page">
      <PageHeader
        title="审批策略"
        description="每一次改动数据的工具调用都先问这份策略：没有一条允许，就不办（默认拒绝）。"
      />
      <div className="notice policy-intro" role="note">
        <p>
          退款、审批分级、四眼原则和语音确认是一份 <a href={POLICY_URL} target="_blank" rel="noreferrer">Cedar 策略</a>。后端用 cedarpy、本页用 cedar-wasm 判定同一份文件（Cedar 4.12.0），
          每次判定在链路追踪里是单独一段。对应业界做法：AWS AgentCore Policy 在工具网关处默认拒绝、用 Cedar 逐次判定工具调用（<a href={AGENTCORE} target="_blank" rel="noreferrer">2026-03 正式发布</a>）。
        </p>
        <p className="faint">
          策略引擎：{loadError ? <b className="sim-bad">没有加载（{loadError}），所有改动数据的调用都会被拒绝</b> : engine ? `cedar-wasm ${engine.cedarVersion()} 已加载` : "加载中（约 1.4 MB）…"}
        </p>
      </div>

      <div className="policy-grid">
        <section className="card policy-side" aria-labelledby="policy-try">
          <div className="policy-side-head">
            <h2 id="policy-try" className="sim-h">如果这样改</h2>
            <span className="badge badge-warning">模拟</span>
          </div>
          <p className="faint small">只在本页的副本上重算，不改演示数据，也不改真实策略。</p>
          <fieldset className="policy-limits">
            <legend className="small">自动审批额度（不超过这个金额，不用人审）</legend>
            {TENANTS.map(([slug, name]) => (
              <label key={slug} className="field">
                <span>{name}<span className="faint">（上线 ¥{shippedLimit(slug)}）</span></span>
                <input className="input" type="number" inputMode="decimal" min={0} max={100000} step={1} placeholder={String(shippedLimit(slug))}
                  value={limits[slug] ?? ""} onChange={(e) => setLimits({ ...limits, [slug]: e.target.value })} aria-label={`${name}的自动审批额度（元）`} />
              </label>
            ))}
          </fieldset>
          {GROUPS.map(([title, pick]) => (
            <fieldset key={title} className="policy-rules">
              <legend className="small">{title}</legend>
              {policies.filter((p) => p.effect === "forbid" && pick(p)).map((p) => (
                <div key={p.id} className="policy-rule">
                  <label className="policy-rule-main" title={p.locked ? "不变式：不能关闭。属性测试检查任何变体都守得住它" : "取消勾选 = 在模拟里关掉这条规则"}>
                    <input type="checkbox" checked={!off.has(p.id)} disabled={p.locked}
                      onChange={(e) => { const next = new Set(off); if (e.target.checked) next.delete(p.id); else next.add(p.id); setOff(next); }} />
                    <span className="policy-rule-text">
                      <span>{p.label}</span>
                      <span className="mono faint small">{p.id}</span>
                    </span>
                    {p.locked && <Lock size={13} aria-label="不变式，不能关闭" className="policy-lock" />}
                  </label>
                  <button type="button" className="btn btn-text btn-sm" aria-expanded={open === p.id} onClick={() => setOpen(open === p.id ? null : p.id)}>
                    {open === p.id ? "收起原文" : "原文"}
                  </button>
                  {open === p.id && <pre className="policy-code">{p.text.trim()}</pre>}
                </div>
              ))}
            </fieldset>
          ))}
          <div className="row policy-actions">
            <button type="button" className="btn btn-primary" onClick={rerun} disabled={!whatif || !!progress}>
              <Play size={14} aria-hidden="true" /> 重算全部对话
            </button>
            <button type="button" className="btn" onClick={reset} disabled={!!progress}><RotateCcw size={14} aria-hidden="true" /> 恢复上线策略</button>
          </div>
          {error && <div className="error-banner" role="alert">{error}</div>}
        </section>

        <section className="card policy-result" aria-labelledby="policy-out" aria-live="polite">
          <h2 id="policy-out" className="sim-h">哪些终态会变</h2>
          {progress ? (
            <div className="stack" style={{ gap: 8 }}>
              <p className="small">重算中：{progress[0]}/{progress[1]}（每段先按上线策略、再按你的改动各跑一遍）</p>
              <progress max={progress[1]} value={progress[0]} className="policy-progress" aria-label="重算进度" />
            </div>
          ) : !result ? (
            <Empty>{pending ? "点“重算全部对话”：15 段完整对话和 144 段录制对话，各在上线策略和你的改动下跑一遍。" : "改一个额度或关掉一条规则，再点“重算全部对话”。"}</Empty>
          ) : (
            <div className="stack" style={{ gap: 12 }}>
              <p className="policy-summary">
                <b>{result.total} 段对话里，{flips.length} 段终态改变</b>，{quiet.length} 段只是决定的理由变了、终态没变。
                <span className="faint small">{describeVariant(result.variant, label)}</span>
              </p>
              {flips.length === 0 && <Empty>这个改动不让任何一段对话的结局不同。</Empty>}
              <ul className="policy-flips">
                {flips.map((f) => <FlipItem key={f.key} flip={f} label={label} policies={policies} />)}
              </ul>
              {quiet.length > 0 && (
                <details className="policy-quiet">
                  <summary className="small">理由变了、终态没变的 {quiet.length} 段</summary>
                  <ul className="policy-flips">
                    {quiet.map((f) => <FlipItem key={f.key} flip={f} label={label} policies={policies} />)}
                  </ul>
                </details>
              )}
              <p className="faint small">
                录制对话按顾客当时发出的原句重放，顾客不会因为新的回答改口，所以这里说的是“同样的话现在会停在哪里”。
                完整对话里员工审批那一步，没有待审批的申请时跳过。这些都在浏览器引擎上算，后端用同一份策略文件。
              </p>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

function describeVariant(v: Variant, label: (id: string) => string): string {
  const parts = [
    ...Object.entries(v.limits).map(([slug, value]) => `${TENANTS.find(([s]) => s === slug)?.[1] ?? slug}额度 ¥${value}`),
    ...v.disabled.map((id) => `关掉“${label(id)}”`),
  ];
  return parts.length ? `改动：${parts.join("，")}。` : "没有改动。";
}

function FlipItem({ flip, label, policies }: { flip: Flip; label: (id: string) => string; policies: PolicyInfo[] }) {
  const [code, setCode] = useState<string | null>(null);
  const title = flip.kind === "journey" ? `完整对话 ${flip.key}` : `录制对话 ${flip.key}`;
  const card = flip.kind === "transcript" ? CARDS.get(flip.key.split("/")[1]) : undefined;
  // A refund the words did not ask for plainly is offered back to the customer, not refused: say so.
  const effectText = (s: NonNullable<Flip["was"]>) => (s.effect === "deny" && s.reasons.some((r) => ASKS_BACK.has(r)) ? "不提交，先问顾客" : EFFECT_TEXT[s.effect] ?? s.effect);
  const step = (s: Flip["was"]) => (s ? `${s.order ? `${s.order} ` : ""}${effectText(s)}${s.amount !== null ? `（¥${s.amount.toFixed(2)}）` : ""}` : "没有这一步");
  const policyLine = (id: string, change: string) => (
    <li key={`${change}-${id}`}>
      <span>{change}：{label(id)} </span>
      <button type="button" className="btn btn-text btn-sm mono" aria-expanded={code === id} onClick={() => setCode(code === id ? null : id)}>{id}</button>
      {code === id && <pre className="policy-code">{policies.find((p) => p.id === id)?.text.trim() ?? id}</pre>}
    </li>
  );
  return (
    <li className="policy-flip">
      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
        <span className="badge">{flip.tenant === "echo" ? "Echo音乐" : "Leon优选"}</span>
        <span className="small ellipsis policy-flip-title">{title}</span>
      </div>
      {card && <p className="clamp-2 faint small">{card.card}</p>}
      {flip.tables.length > 0 && (
        <table className="table policy-diff">
          <caption className="sr-only">终态对比</caption>
          <thead><tr><th scope="col">表</th><th scope="col">上线策略</th><th scope="col">你的改动</th></tr></thead>
          <tbody>
            {flip.tables.map((t) => (
              <tr key={t}>
                <th scope="row">{TABLE_TEXT[t]}</th>
                <td>{show(t, flip.before[t as keyof Full], t === "orders" ? flip.after.orders : undefined)}</td>
                <td>{show(t, flip.after[t as keyof Full], t === "orders" ? flip.before.orders : undefined)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {flip.at >= 0 && (
        <div className="small policy-why">
          <p>第 {flip.at + 1} 次判定：{step(flip.was)} → {step(flip.now)}</p>
          <ul>
            {flip.stopped.map((id) => policyLine(id, "不再起作用"))}
            {flip.started.map((id) => policyLine(id, "开始起作用"))}
            {!flip.stopped.length && !flip.started.length && <li>同一条规则，结论不同（额度变了）</li>}
          </ul>
        </div>
      )}
    </li>
  );
}
