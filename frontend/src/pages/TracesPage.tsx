import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { api } from "../api";
import { Empty, ErrorBanner, Json, Kpi, ROLE_TEXT, StatusBadge, VerdictBadge, ms, pressable, time, useLoad, tierText } from "../components/ui";
import type { EvidenceItem, RunRow, SpanRow } from "../types";

const KIND_TEXT: Record<string, string> = {
  guardrail: "护栏", router: "路由", llm: "模型", tool: "工具", verify: "校验", approval: "审批", notify: "通知",
  cache: "缓存",
  rewrite: "改写",
};

interface RunDetail extends RunRow {
  spans: SpanRow[];
  evidence: EvidenceItem[];
  approvals: { id: number; status: string; tool_name: string; reason: string }[];
}

function RunDetailView({ runId }: { runId: string }) {
  const navigate = useNavigate();
  const { data, error } = useLoad(() => api.get<RunDetail>(`/api/runs/${runId}`), [runId]);
  const [open, setOpen] = useState<number | null>(null);
  if (error) return <ErrorBanner error={error} />;
  if (!data) return <Empty>加载中…</Empty>;
  const maxLatency = Math.max(1, ...data.spans.map((s) => s.latency_ms));
  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="row">
        <button className="btn btn-sm" onClick={() => navigate("/traces")}><ArrowLeft size={14} /> 返回列表</button>
        <span className="mono faint">trace {data.id}</span>
        {data.is_eval && <span className="badge badge-info">评测运行</span>}
      </div>
      <div className="grid grid-4">
        <Kpi label="状态" value={<StatusBadge status={data.status} />} sub={`${data.steps} 步 · ${tierText(data.route_tier)}${data.route_tier === "cache" ? "" : ` ${data.model}`}`} />
        <Kpi label="端到端耗时" value={ms(data.latency_ms)} sub={time(data.created_at)} />
        <Kpi label="Token" value={(data.prompt_tokens + data.completion_tokens).toLocaleString()} sub={`输入 ${data.prompt_tokens} / 输出 ${data.completion_tokens} · ¥${data.cost.toFixed(5)}`} />
        <Kpi label="证据校验" value={<VerdictBadge verdict={data.verdict} coverage={data.evidence_coverage} />} sub="逐句校验覆盖率" />
      </div>
      <div className="grid grid-2">
        <div className="card card-pad stack">
          <div className="faint">用户输入（已脱敏）· {data.user?.name}（{ROLE_TEXT[data.user?.role ?? ""]}）</div>
          <div>{data.input_text}</div>
        </div>
        <div className="card card-pad stack">
          <div className="faint">最终回答</div>
          <div style={{ whiteSpace: "pre-wrap" }}>{data.output_text}</div>
          {data.error && <ErrorBanner error={data.error} />}
        </div>
      </div>
      <div className="card">
        <div className="card-head"><h3>执行时间线（{data.spans.length} 个 span）</h3><span className="faint">点击展开输入输出</span></div>
        {data.spans.map((s) => (
          <div key={s.id}>
            <div className="span-row" role="button" aria-expanded={open === s.id} {...pressable(() => setOpen(open === s.id ? null : s.id))}>
              <span className={`badge ${s.status === "ok" ? "" : s.status === "rejected" ? "badge-warning" : "badge-danger"}`}>{KIND_TEXT[s.kind] ?? s.kind}</span>
              <span className="ellipsis"><b>{s.name}</b> {s.prompt_tokens + s.completion_tokens > 0 && <span className="faint">· {s.prompt_tokens + s.completion_tokens} tokens</span>}</span>
              <div className="bar-track"><div className={`span-bar kind-${s.kind}`} style={{ width: `${(s.latency_ms / maxLatency) * 100}%` }} /></div>
              <span className="mono faint">{ms(s.latency_ms)}</span>
            </div>
            {open === s.id && (
              <div className="grid grid-2" style={{ padding: 12, borderBottom: "1px solid var(--border)" }}>
                <div><div className="faint">input</div><Json value={s.input} /></div>
                <div><div className="faint">output</div><Json value={s.output} /></div>
              </div>
            )}
          </div>
        ))}
      </div>
      {data.evidence.length > 0 && (
        <div className="card">
          <div className="card-head"><h3>证据清单</h3><span className="faint">回答中的 [n] 对应这里的编号</span></div>
          <div className="card-pad stack">
            {data.evidence.map((e) => (
              <div key={e.ref} className="json small">
                <b>[{e.ref}] {e.kind === "kb" ? `《${e.title}》` : `工具：${e.title}`}</b> <span className="faint">{e.meta.heading}</span>
                <div className="mono" style={{ whiteSpace: "pre-wrap" }}>{e.text}</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default function TracesPage() {
  const { runId } = useParams();
  const navigate = useNavigate();
  const [status, setStatus] = useState("");
  const [includeEval, setIncludeEval] = useState(false);
  const { data, error } = useLoad(
    () => api.get<{ items: RunRow[] }>(`/api/runs?limit=100${status ? `&status=${status}` : ""}${includeEval ? "&include_eval=true" : ""}`),
    [status, includeEval],
  );
  if (runId) return <div className="page"><RunDetailView runId={runId} /></div>;
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>链路追踪</h1>
          <p>每次 Agent 运行都是一条 trace：路由、模型调用、工具、审批、证据校验，全部可回放。日志中的手机号、身份证等信息已脱敏。</p>
        </div>
        <div className="row">
          <select className="select" style={{ width: 140 }} value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">全部状态</option>
            <option value="completed">已解决</option>
            <option value="handoff">已转人工</option>
            <option value="waiting_approval">等待审批</option>
            <option value="failed">失败</option>
          </select>
          <label className="row small muted"><input type="checkbox" checked={includeEval} onChange={(e) => setIncludeEval(e.target.checked)} /> 含评测运行</label>
        </div>
      </div>
      <ErrorBanner error={error} />
      <div className="card table-wrap">
        <table className="table">
          <thead><tr><th>时间</th><th>用户</th><th>输入</th><th>状态</th><th>模型</th><th>耗时</th><th>Token</th><th>证据</th></tr></thead>
          <tbody>
            {data?.items.map((r) => (
              <tr key={r.id} className="clickable" {...pressable(() => navigate(`/traces/${r.id}`))}>
                <td className="faint nowrap">{time(r.created_at)}</td>
                <td className="nowrap">{r.user?.name}</td>
                <td style={{ maxWidth: 380 }}><div className="ellipsis">{r.input_text}</div></td>
                <td><StatusBadge status={r.status} /></td>
                <td className="nowrap"><span className={`badge ${r.route_tier === "strong" ? "badge-info" : r.route_tier === "cache" ? "badge-success" : ""}`}>{r.route_tier === "strong" ? "强" : r.route_tier === "cache" ? "缓存" : "快"}</span> <span className="faint">{r.model}</span></td>
                <td className="mono">{ms(r.latency_ms)}</td>
                <td className="mono">{r.prompt_tokens + r.completion_tokens}</td>
                <td><VerdictBadge verdict={r.verdict} coverage={r.evidence_coverage} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        {data?.items.length === 0 && <Empty>还没有运行记录，先去 <Link to="/chat">对话测试台</Link> 问一个问题。</Empty>}
      </div>
    </div>
  );
}
