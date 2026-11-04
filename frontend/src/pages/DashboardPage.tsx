import { Link } from "react-router-dom";
import { AlertTriangle, ArrowDownToLine, ArrowUpRight, CalendarDays, ChevronDown, ChevronRight, ClipboardCheck, Headset, Info, Ticket } from "lucide-react";
import { api } from "../api";
import { BarList, ColumnChart, Sparkline } from "../components/charts";
import { Empty, ErrorBanner, Kpi, PageHeader, Skeleton, TOOL_TEXT, ms, pct, useLoad, TIER_TEXT } from "../components/ui";

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
  cost_unpriced_models?: string[]; // their tokens are not in cost (N-225)
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

const dayLabel = (day: string) => day.replace(/^\d{4}-/, "").replace("-", "/");

export default function DashboardPage() {
  const { data, error } = useLoad(() => api.get<Overview>("/api/metrics/overview?days=7"));
  const daily = data?.daily ?? [];
  return (
    <div className="page stack dashboard-page" style={{ gap: 16 }}>
      <PageHeader title="运营看板" description="掌握服务进展，把注意力留给需要跟进的问题。"
        actions={<>
          <span className="period-label"><CalendarDays size={14} aria-hidden="true" /> 近 7 天 · 含今天{data && ` · ${zoneLabel(data.timezone)}`}</span>
          <details className="export-menu menu">
            <summary className="btn"><ArrowDownToLine size={15} /> 导出数据 <ChevronDown size={13} /></summary>
            <div className="export-options menu-panel" aria-label="导出近 30 天数据">
              <div className="menu-label">近 30 天 · CSV 文件</div>
              {EXPORTS.map(([kind, label]) => (
                <button key={kind} className="menu-item" onClick={() => api.download(`/api/export/${kind}.csv?days=30`, `loopdesk-${kind}.csv`)}>{label}<ArrowUpRight size={14} style={{ marginLeft: "auto" }} aria-hidden="true" /></button>
              ))}
            </div>
          </details>
        </>} />
      <ErrorBanner error={error} />
      {!data ? <div className="card"><Skeleton rows={6} /></div> : (
        <>
          {data.failures.last_15m > 0 && (
            <div className="error-banner" role="alert">
              <AlertTriangle size={15} aria-hidden="true" />
              <span>{`最近 15 分钟有 ${data.failures.last_15m} 次运行出错（`
                + Object.entries(data.failures.kinds).map(([kind, n]) => `${FAILURE_KIND[kind] ?? kind} ${n}`).join("、")
                + `），顾客收到的是引用原文的回复或已转人工，这些会话不计入自助解决。`
                + `最近一次：${FAILURE_KIND[data.failures.last_kind ?? ""] ?? "其他"}（${data.failures.last_error ?? "—"}）。`}
              <Link to="/traces">查看链路 →</Link></span>
            </div>
          )}
          {data.tickets.overdue > 0 && (
            <div className="error-banner" role="alert">
              <AlertTriangle size={15} aria-hidden="true" />
              <span>{`有 ${data.tickets.overdue} 张工单已超过对顾客承诺的响应时限（待处理共 ${data.tickets.open} 张）。`}
              <Link to="/tickets">去处理 →</Link></span>
            </div>
          )}
          {data.unsafe_answers.answers > 0 && (
            <div className="error-banner" role="alert">
              <AlertTriangle size={15} aria-hidden="true" />
              <span>{`近 ${data.window_days} 天有 ${data.unsafe_answers.answers} 条回答因为索要验证码、银行卡号、密码，或给出私人联系方式、要求转账，没有发给顾客，已转人工。`
                + (data.unsafe_answers.documents.length
                  ? `知识库里说了同样内容的文档：${data.unsafe_answers.documents.map((d) => `《${d.title}》（${d.answers} 次）`).join("、")}，请检查。`
                  : "")
                + (data.unsafe_answers.without_source
                  ? `${data.unsafe_answers.without_source} 条在知识库里找不到来源（模型生成或顾客诱导），请在链路里查看。`
                  : "")}
              <Link to="/knowledge">去知识库 →</Link></span>
            </div>
          )}
          {data.knowledge_conflicts.open > 0 && (
            <div className="banner banner-warning" role="status">
              <Info size={15} aria-hidden="true" />
              <span>{`知识库有 ${data.knowledge_conflicts.open} 处冲突待核对（最久 ${duration(data.knowledge_conflicts.oldest_open_s)}）。`
                + `核对之前，同时用到两种说法的问题都会转人工，近 ${data.window_days} 天已有 ${data.knowledge_conflicts.handoffs} 次。`}
              <Link to="/knowledge?tab=conflicts">去核对 →</Link></span>
            </div>
          )}
          <div className="work-queue card" aria-label="需要跟进的工作">
            <div className="work-queue-label"><b>待办工作</b><span>需要有人跟进的事项</span></div>
            <Link to="/approvals"><span className="wq-icon"><ClipboardCheck size={16} /></span><span>待审批</span><b>{data.pending_approvals}</b><ChevronRight size={14} /></Link>
            <Link to="/handoffs"><span className="wq-icon"><Headset size={16} /></span><span>等待人工</span><b>{data.handoff_queue.waiting}</b><ChevronRight size={14} /></Link>
            <Link to="/tickets"><span className="wq-icon"><Ticket size={16} /></span><span>待处理工单</span><b>{data.tickets.open}</b><ChevronRight size={14} /></Link>
          </div>
          <div className="section-heading"><h2>服务表现</h2><span>按{zoneLabel(data.timezone)}自然日统计 · 不含评测运行</span></div>
          <div className="grid grid-4 primary-metrics">
            <Kpi label="Agent 运行" value={data.runs}
              extra={<Sparkline values={daily.map((d) => d.runs)} label={`近 ${daily.length} 天每日运行：${daily.map((d) => d.runs).join("、")}`} />}
              sub={`已解决 ${data.status.completed ?? 0} · 转人工 ${data.status.handoff ?? 0} · 缓存命中 ${pct(data.cache_hit_rate)}`} />
            <Kpi label="自助解决率（按会话）" value={pct(data.resolution_rate)}
              sub={`${data.conversations} 个会话 · 转人工 ${pct(data.handoff_rate)} · 拒答 ${pct(data.refusal_rate)}`} />
            <Kpi label="平均证据覆盖率" value={pct(data.avg_evidence_coverage)}
              sub={`充分 ${data.verdicts.pass ?? 0} · 部分 ${data.verdicts.warn ?? 0} · 冲突 ${data.verdicts.blocked ?? 0}`} />
            <Kpi label="人工首次响应"
              value={duration(data.handoff_queue.avg_first_response_s)}
              sub={`接待中 ${data.handoff_queue.in_progress} · 最长等待 ${duration(data.handoff_queue.longest_wait_s)}`} />
          </div>
          <div className="grid dashboard-charts">
            <div className="card">
              <div className="card-head"><h3>每日运行量</h3><span className="faint">全部运行与其中转人工的次数</span></div>
              <div className="card-pad">
                {daily.length ? (
                  <ColumnChart label="每日运行量"
                    series={[{ key: "runs", label: "全部运行", color: "var(--chart-1)" }, { key: "handoff", label: "转人工", color: "var(--chart-2)" }]}
                    rows={daily.map((d) => ({ label: dayLabel(d.day), values: { runs: d.runs, handoff: d.handoff }, title: `${d.day}：${d.runs} 次运行，转人工 ${d.handoff} 次` }))} />
                ) : <Empty>暂无数据</Empty>}
              </div>
            </div>
            <div className="card">
              <div className="card-head"><h3>工具调用分布</h3></div>
              <div className="card-pad"><BarList data={data.tools} labels={TOOL_TEXT} empty={<Empty>暂无数据</Empty>} /></div>
            </div>
            <div className="card">
              <div className="card-head"><h3>模型路由</h3><span className="faint">重复问题走缓存，简单问题走快模型</span></div>
              <div className="card-pad"><BarList data={data.route_tiers} labels={TIER_TEXT} empty={<Empty>暂无数据</Empty>} /></div>
            </div>
          </div>
          <div className="section-heading"><h2>运行与质量</h2><span>成本按配置单价估算，离线模型单价仅作示例</span></div>
          <div className="grid grid-4 secondary-metrics">
            <Kpi label="延迟 p50 / p95" value={`${ms(data.latency_p50_ms)} / ${ms(data.latency_p95_ms)}`}
              sub={`出错 ${pct(data.failures.rate)}（${data.failures.runs} 次）· 近 15 分钟 ${data.failures.last_15m} 次`} />
            <Kpi label="Token / 估算成本" value={data.tokens.toLocaleString()}
              sub={data.cost_unpriced_models?.length
                ? <span title="设置 LOOPDESK_PRICES 后按单价估算">{data.cost > 0 ? `¥${data.cost.toFixed(4)} · ` : ""}未定价：{data.cost_unpriced_models.join("、")}</span>
                : `¥${data.cost.toFixed(4)}`} />
            <Kpi label="用户反馈" value={(data.feedback.up ?? 0) + (data.feedback.down ?? 0) + (data.feedback.correction ?? 0)}
              sub={`有用 ${data.feedback.up ?? 0} · 无用 ${data.feedback.down ?? 0} · 纠错 ${data.feedback.correction ?? 0}`} />
            <Kpi label="最近一次回归评测" value={data.last_eval ? pct(data.last_eval.pass_rate, 1) : "—"}
              sub={data.last_eval ? <Link to="/evals">运行 #{data.last_eval.id} · {data.last_eval.total} 条用例</Link> : <Link to="/evals">去运行</Link>} />
          </div>
          <div className="card">
            <div className="card-head"><h3>Prompt 版本对比</h3><span className="faint">灰度期间按会话稳定分流，比较各版本在真实对话中的表现</span></div>
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>版本</th><th className="num">运行次数</th><th className="num">转人工率</th><th className="num">证据冲突</th><th className="num">平均证据覆盖率</th><th className="num">平均 Token</th></tr></thead>
                <tbody>
                  {Object.entries(data.prompt_versions).map(([version, v]) => (
                    <tr key={version}>
                      <td className="mono">{version}</td>
                      <td className="num">{v.runs}</td>
                      <td className="num">{pct(v.handoff_rate)}</td>
                      <td className="num">{v.blocked}</td>
                      <td className="num">{pct(v.avg_evidence_coverage)}</td>
                      <td className="num mono">{v.avg_tokens}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
