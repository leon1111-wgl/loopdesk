import { Link } from "react-router-dom";
import { Download } from "lucide-react";
import { api } from "../api";
import { Empty, ErrorBanner, Kpi, TOOL_TEXT, ms, pct, useLoad, TIER_TEXT } from "../components/ui";

interface Overview {
  window_days: number;
  timezone: string;
  runs: number;
  status: Record<string, number>;
  conversations: number;
  resolution_rate: number | null; // per conversation: no handoff, no refusal, no negative feedback
  handoff_rate: number | null;
  refusal_rate: number | null;
  pending_approvals: number;
  handoff_queue: { waiting: number; in_progress: number; longest_wait_s: number; handoffs: number; avg_first_response_s: number | null };
  knowledge_conflicts: { open: number; oldest_open_s: number; handoffs: number };
  unsafe_answers: { answers: number; without_source: number; documents: { id: number; title: string; answers: number }[] };
  tickets: { open: number; overdue: number };
  failures: { runs: number; rate: number | null; last_15m: number; kinds: Record<string, number>; last_error: string | null; last_kind: string | null };
  latency_p50_ms: number;
  latency_p95_ms: number;
  tokens: number;
  cost: number;
  avg_evidence_coverage: number | null;
  verdicts: Record<string, number>;
  tools: Record<string, number>;
  route_tiers: Record<string, number>;
  cache_hits: number;
  cache_hit_rate: number | null;
  prompt_versions: Record<string, { runs: number; handoff_rate: number; blocked: number; avg_evidence_coverage: number | null; avg_tokens: number }>;
  feedback: Record<string, number>;
  daily: { day: string; runs: number; handoff: number }[];
  last_eval: { id: number; pass_rate: number; total: number } | null;
}

function Bars({ data, labels }: { data: Record<string, number>; labels?: Record<string, string> }) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map(([, v]) => v));
  if (!entries.length) return <Empty>暂无数据</Empty>;
  return (
    <div className="stack">
      {entries.map(([k, v]) => (
        <div key={k}>
          <div className="row small"><span>{labels?.[k] ?? k}</span><span className="spacer" /><span className="mono">{v}</span></div>
          <div className="bar-track"><div className="bar-fill" style={{ width: `${(v / max) * 100}%` }} /></div>
        </div>
      ))}
    </div>
  );
}

function DailyChart({ daily }: { daily: Overview["daily"] }) {
  if (!daily.length) return <Empty>暂无数据</Empty>;
  const max = Math.max(1, ...daily.map((d) => d.runs));
  const w = 100 / daily.length;
  return (
    <svg viewBox="0 0 100 60" preserveAspectRatio="none" style={{ width: "100%", height: 160 }} role="img" aria-label="每日运行量">
      {daily.map((d, i) => {
        const h = (d.runs / max) * 50;
        const hh = (d.handoff / max) * 50;
        return (
          <g key={d.day}>
            <rect x={i * w + w * 0.2} y={52 - h} width={w * 0.6} height={h} fill="var(--primary)" rx={0.8}><title>{`${d.day}：${d.runs} 次运行`}</title></rect>
            <rect x={i * w + w * 0.2} y={52 - hh} width={w * 0.6} height={hh} fill="var(--info)" rx={0.8}><title>{`${d.day}：转人工 ${d.handoff}`}</title></rect>
            <text x={i * w + w / 2} y={59} fontSize={3.2} textAnchor="middle" fill="var(--text-3)">{d.day}</text>
          </g>
        );
      })}
    </svg>
  );
}

// Why runs hit an error (N-037). Customers were served either way (quoted policy or a person, N-031).
const FAILURE_KIND: Record<string, string> = {
  unavailable: "模型服务不可用", rejected: "请求被拒绝（多半是配置或参数）", timeout: "超时",
  breaker: "熔断中", disconnected: "顾客断开", interrupted: "进程中断", embedding: "向量服务故障", rerank: "重排服务故障", truncated: "输出被截断", other: "其他",
};

// The dashboard's days are the tenant's calendar days (N-024); say which zone that is.
function zoneLabel(zone: string) {
  return zone === "Asia/Shanghai" ? "北京时间" : zone;
}

const EXPORTS: [string, string][] = [
  ["conversations", "会话全文"],
  ["tickets", "工单"],
  ["handoffs", "人工接待"],
  ["runs", "运行记录"],
  ["feedback", "反馈"],
  ["approvals", "审批"],
];

const duration = (seconds: number | null | undefined) =>
  seconds == null ? "—" : seconds < 60 ? `${seconds} 秒` : seconds < 3600 ? `${Math.round(seconds / 60)} 分钟` : `${(seconds / 3600).toFixed(1)} 小时`;

export default function DashboardPage() {
  const { data, error } = useLoad(() => api.get<Overview>("/api/metrics/overview?days=7"));
  return (
    <div className="page stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <h1>运营看板</h1>
          <p>近 7 天（按自然日计，含今天）的真实对话，不含评测运行。成本按配置的模型单价估算，离线模型的单价仅作示例。</p>
        </div>
        <div className="row row-wrap" aria-label="导出近 30 天数据">
          <span className="faint">导出近 30 天（CSV）</span>
          {EXPORTS.map(([kind, label]) => (
            <button key={kind} className="btn btn-sm" onClick={() => api.download(`/api/export/${kind}.csv?days=30`, `loopdesk-${kind}.csv`)}>
              <Download size={14} aria-hidden="true" /> {label}
            </button>
          ))}
        </div>
      </div>
      <ErrorBanner error={error} />
      {!data ? <Empty>加载中…</Empty> : (
        <>
          {data.failures.last_15m > 0 && (
            <div className="error-banner" role="alert">
              {`最近 15 分钟有 ${data.failures.last_15m} 次运行出错（`
                + Object.entries(data.failures.kinds).map(([kind, n]) => `${FAILURE_KIND[kind] ?? kind} ${n}`).join("、")
                + `），顾客收到的是引用原文的回复或已转人工，这些会话不计入自助解决。`
                + `最近一次：${FAILURE_KIND[data.failures.last_kind ?? ""] ?? "其他"}（${data.failures.last_error ?? "—"}）。`}
              <Link to="/traces">查看链路 →</Link>
            </div>
          )}
          {data.tickets.overdue > 0 && (
            <div className="error-banner" role="alert">
              {`有 ${data.tickets.overdue} 张工单已超过对顾客承诺的响应时限（待处理共 ${data.tickets.open} 张）。`}
              <Link to="/tickets">去处理 →</Link>
            </div>
          )}
          {data.unsafe_answers.answers > 0 && (
            <div className="error-banner" role="alert">
              {`近 ${data.window_days} 天有 ${data.unsafe_answers.answers} 条回答因为索要验证码、银行卡号、密码，或给出私人联系方式、要求转账，没有发给顾客，已转人工。`
                + (data.unsafe_answers.documents.length
                  ? `知识库里说了同样内容的文档：${data.unsafe_answers.documents.map((d) => `《${d.title}》（${d.answers} 次）`).join("、")}，请检查。`
                  : "")
                + (data.unsafe_answers.without_source
                  ? `${data.unsafe_answers.without_source} 条在知识库里找不到来源（模型生成或顾客诱导），请在链路里查看。`
                  : "")}
              <Link to="/knowledge">去知识库 →</Link>
            </div>
          )}
          {data.knowledge_conflicts.open > 0 && (
            <div className="notice" role="status">
              {`知识库有 ${data.knowledge_conflicts.open} 处冲突待核对（最久 ${duration(data.knowledge_conflicts.oldest_open_s)}）。`
                + `核对之前，同时用到两种说法的问题都会转人工，近 ${data.window_days} 天已有 ${data.knowledge_conflicts.handoffs} 次。`}
              <Link to="/knowledge">去核对 →</Link>
            </div>
          )}
          <div className="grid grid-4">
            <Kpi label="Agent 运行" value={data.runs} sub={`已解决 ${data.status.completed ?? 0} · 转人工 ${data.status.handoff ?? 0} · 缓存命中 ${pct(data.cache_hit_rate)}`} />
            <Kpi label="自助解决率（按会话）" value={pct(data.resolution_rate)}
              sub={`${data.conversations} 个会话 · 转人工 ${pct(data.handoff_rate)} · 拒答 ${pct(data.refusal_rate)}`} />
            <Kpi label="平均证据覆盖率" value={pct(data.avg_evidence_coverage)}
              sub={`充分 ${data.verdicts.pass ?? 0} · 部分 ${data.verdicts.warn ?? 0} · 冲突 ${data.verdicts.blocked ?? 0}`} />
            <Kpi label="待审批 / 等待人工"
              value={<><Link to="/approvals">{data.pending_approvals}</Link> / <Link to="/handoffs">{data.handoff_queue.waiting}</Link></>}
              sub={`接待中 ${data.handoff_queue.in_progress} · 最长等待 ${duration(data.handoff_queue.longest_wait_s)} · 首次响应 ${duration(data.handoff_queue.avg_first_response_s)}`} />
            <Kpi label="延迟 p50 / p95" value={`${ms(data.latency_p50_ms)} / ${ms(data.latency_p95_ms)}`}
              sub={`出错 ${pct(data.failures.rate)}（${data.failures.runs} 次）· 近 15 分钟 ${data.failures.last_15m} 次`} />
            <Kpi label="Token / 估算成本" value={data.tokens.toLocaleString()} sub={`¥${data.cost.toFixed(4)}`} />
            <Kpi label="用户反馈" value={(data.feedback.up ?? 0) + (data.feedback.down ?? 0) + (data.feedback.correction ?? 0)}
              sub={`有用 ${data.feedback.up ?? 0} · 无用 ${data.feedback.down ?? 0} · 纠错 ${data.feedback.correction ?? 0}`} />
            <Kpi label="最近一次回归评测" value={data.last_eval ? pct(data.last_eval.pass_rate, 1) : "—"}
              sub={data.last_eval ? <Link to="/evals">运行 #{data.last_eval.id} · {data.last_eval.total} 条用例</Link> : <Link to="/evals">去运行</Link>} />
          </div>
          <div className="grid grid-3">
            <div className="card">
              <div className="card-head"><h3>每日运行量</h3><span className="faint">蓝：全部 · 紫：转人工 · 按{zoneLabel(data.timezone)}分天</span></div>
              <div className="card-pad"><DailyChart daily={data.daily} /></div>
            </div>
            <div className="card">
              <div className="card-head"><h3>工具调用分布</h3></div>
              <div className="card-pad"><Bars data={data.tools} labels={TOOL_TEXT} /></div>
            </div>
            <div className="card">
              <div className="card-head"><h3>模型路由</h3><span className="faint">重复问题走缓存，简单问题走快模型</span></div>
              <div className="card-pad"><Bars data={data.route_tiers} labels={TIER_TEXT} /></div>
            </div>
          </div>
          <div className="card table-wrap">
            <div className="card-head"><h3>Prompt 版本对比</h3><span className="faint">灰度期间按会话稳定分流，比较各版本在真实对话中的表现</span></div>
            <table className="table">
              <thead><tr><th>版本</th><th>运行次数</th><th>转人工率</th><th>证据冲突</th><th>平均证据覆盖率</th><th>平均 Token</th></tr></thead>
              <tbody>
                {Object.entries(data.prompt_versions).map(([version, v]) => (
                  <tr key={version}>
                    <td className="mono">{version}</td>
                    <td>{v.runs}</td>
                    <td>{pct(v.handoff_rate)}</td>
                    <td>{v.blocked}</td>
                    <td>{pct(v.avg_evidence_coverage)}</td>
                    <td className="mono">{v.avg_tokens}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
