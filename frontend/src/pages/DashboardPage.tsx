import { Link } from "react-router-dom";
import { api } from "../api";
import { Empty, ErrorBanner, Kpi, TOOL_TEXT, ms, pct, useLoad } from "../components/ui";

interface Overview {
  runs: number;
  status: Record<string, number>;
  resolution_rate: number | null;
  handoff_rate: number | null;
  pending_approvals: number;
  latency_p50_ms: number;
  latency_p95_ms: number;
  tokens: number;
  cost: number;
  avg_evidence_coverage: number | null;
  verdicts: Record<string, number>;
  tools: Record<string, number>;
  route_tiers: Record<string, number>;
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

export default function DashboardPage() {
  const { data, error } = useLoad(() => api.get<Overview>("/api/metrics/overview?days=7"));
  return (
    <div className="page stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <h1>运营看板</h1>
          <p>近 7 天的真实对话，不含评测运行。成本按配置的模型单价估算，离线模型的单价仅作示例。</p>
        </div>
      </div>
      <ErrorBanner error={error} />
      {!data ? <Empty>加载中…</Empty> : (
        <>
          <div className="grid grid-4">
            <Kpi label="Agent 运行" value={data.runs} sub={`已解决 ${data.status.completed ?? 0} · 转人工 ${data.status.handoff ?? 0}`} />
            <Kpi label="自助解决率" value={pct(data.resolution_rate)} sub={`转人工率 ${pct(data.handoff_rate)}`} />
            <Kpi label="平均证据覆盖率" value={pct(data.avg_evidence_coverage)}
              sub={`充分 ${data.verdicts.pass ?? 0} · 部分 ${data.verdicts.warn ?? 0} · 冲突 ${data.verdicts.blocked ?? 0}`} />
            <Kpi label="待审批" value={<Link to="/approvals">{data.pending_approvals}</Link>} sub="超额退款等待人工" />
            <Kpi label="延迟 p50 / p95" value={`${ms(data.latency_p50_ms)} / ${ms(data.latency_p95_ms)}`} />
            <Kpi label="Token / 估算成本" value={data.tokens.toLocaleString()} sub={`¥${data.cost.toFixed(4)}`} />
            <Kpi label="用户反馈" value={(data.feedback.up ?? 0) + (data.feedback.down ?? 0) + (data.feedback.correction ?? 0)}
              sub={`有用 ${data.feedback.up ?? 0} · 无用 ${data.feedback.down ?? 0} · 纠错 ${data.feedback.correction ?? 0}`} />
            <Kpi label="最近一次回归评测" value={data.last_eval ? pct(data.last_eval.pass_rate, 1) : "—"}
              sub={data.last_eval ? <Link to="/evals">运行 #{data.last_eval.id} · {data.last_eval.total} 条用例</Link> : <Link to="/evals">去运行</Link>} />
          </div>
          <div className="grid grid-3">
            <div className="card">
              <div className="card-head"><h3>每日运行量</h3><span className="faint">蓝：全部 · 紫：转人工</span></div>
              <div className="card-pad"><DailyChart daily={data.daily} /></div>
            </div>
            <div className="card">
              <div className="card-head"><h3>工具调用分布</h3></div>
              <div className="card-pad"><Bars data={data.tools} labels={TOOL_TEXT} /></div>
            </div>
            <div className="card">
              <div className="card-head"><h3>模型路由</h3><span className="faint">简单问题走快模型</span></div>
              <div className="card-pad"><Bars data={data.route_tiers} labels={{ fast: "快模型", strong: "强模型" }} /></div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
