import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Play } from "lucide-react";
import { api } from "../api";
import { useAuth } from "../auth";
import { Empty, ErrorBanner, Kpi, StatusBadge, ms, pct, time, useLoad } from "../components/ui";
import type { EvalCase, EvalRunRow } from "../types";

const CHECK_TEXT: Record<string, string> = {
  tools: "工具调用",
  forbidden_tools: "禁用工具",
  must_include: "必含内容",
  must_not_include: "禁含内容",
  refusal: "拒答判断",
  approval: "审批触发",
  retrieval_hit: "检索命中",
  evidence: "证据校验",
};

interface RunDetail extends EvalRunRow {
  results: { case_id: number; case_key: string; passed: boolean; checks: Record<string, boolean>; answer: string; tools_called: string[]; latency_ms: number; run_id: string | null }[];
}

export default function EvalsPage() {
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const runs = useLoad(() => api.get<EvalRunRow[]>("/api/evals/runs"));
  const cases = useLoad(() => api.get<EvalCase[]>("/api/evals/cases"));
  const [selected, setSelected] = useState<number | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"results" | "cases">("results");
  const [promptVersion, setPromptVersion] = useState("");
  const prompts = useLoad(() => api.get<{ default: string; versions: { version: string; characters: number }[] }>("/api/evals/prompts"));

  useEffect(() => {
    const id = selected ?? runs.data?.[0]?.id;
    if (id) api.get<RunDetail>(`/api/evals/runs/${id}`).then(setDetail).catch((e) => setError(e.message));
  }, [selected, runs.data]);

  async function runSuite() {
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<EvalRunRow>("/api/evals/runs", { suite: "default", prompt_version: promptVersion || null });
      await runs.reload();
      setSelected(r.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function setStatus(c: EvalCase, status: string) {
    await api.patch(`/api/evals/cases/${c.id}`, { status });
    cases.reload();
  }

  const m = detail?.metrics;
  return (
    <div className="page stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <h1>评测中心</h1>
          <p>每条用例都走真实 Agent 循环（演练模式，不写业务数据），按轨迹打分：工具是否调对、是否触发审批、检索是否命中、回答是否通过证据校验。CI 中通过率低于阈值即阻止合并。</p>
        </div>
        {admin && (
          <div className="row">
            <label className="row small muted">
              Prompt 版本
              <select className="select" style={{ width: 150 }} value={promptVersion} onChange={(e) => setPromptVersion(e.target.value)} aria-label="评测使用的 Prompt 版本">
                <option value="">默认（{prompts.data?.default ?? "1.00"}）</option>
                {prompts.data?.versions.map((v) => <option key={v.version} value={v.version}>{v.version} · {v.characters} 字</option>)}
              </select>
            </label>
            <button className="btn btn-primary" disabled={busy} onClick={runSuite}><Play size={15} /> {busy ? "运行中…" : "运行回归评测"}</button>
          </div>
        )}
      </div>
      <ErrorBanner error={error ?? runs.error} />
      {m && detail && (
        <>
          <div className="grid grid-4">
            <Kpi label="通过率" value={pct(m.pass_rate, 1)} sub={`${detail.passed}/${detail.total} · 运行 #${detail.id} · ${time(detail.created_at)}`} />
            <Kpi label="平均证据覆盖率" value={pct(m.avg_evidence_coverage, 1)} sub="通过逐句校验的事实句占比" />
            <Kpi label="延迟 p50 / p95" value={`${ms(m.latency_p50_ms)} / ${ms(m.latency_p95_ms)}`} sub={detail.model} />
            <Kpi label="Token 消耗" value={m.total_tokens.toLocaleString()} sub={`估算成本 ¥${m.estimated_cost.toFixed(4)}`} />
          </div>
          {m.diff && (
            <div className="card card-pad row row-wrap">
              <b>对比运行 #{m.diff.against_run}：</b>
              {m.diff.regressions.length > 0
                ? <span className="badge badge-danger">回归 {m.diff.regressions.join("、")}</span>
                : <span className="badge badge-success">无回归</span>}
              {m.diff.fixes.length > 0 && <span className="badge badge-success">修复 {m.diff.fixes.join("、")}</span>}
              {m.diff.new_cases.length > 0 && <span className="badge badge-info">新增 {m.diff.new_cases.join("、")}</span>}
            </div>
          )}
          <div className="card card-pad row row-wrap" style={{ gap: 16 }}>
            {Object.entries(m.checks).map(([k, v]) => (
              <span key={k} className="small">{CHECK_TEXT[k] ?? k} <b>{pct(v)}</b></span>
            ))}
            <span className="spacer" />
            {Object.entries(m.by_tag).map(([k, v]) => <span key={k} className="badge">{k} {pct(v)}</span>)}
          </div>
        </>
      )}
      <div className="row">
        <div className="tabs">
          <button className={tab === "results" ? "active" : ""} onClick={() => setTab("results")}>运行结果</button>
          <button className={tab === "cases" ? "active" : ""} onClick={() => setTab("cases")}>用例库（{cases.data?.length ?? 0}）</button>
        </div>
        <span className="spacer" />
        {tab === "results" && runs.data && runs.data.length > 0 && (
          <select className="select" style={{ width: 260 }} value={detail?.id ?? ""} onChange={(e) => setSelected(Number(e.target.value))}>
            {runs.data.map((r) => (
              <option key={r.id} value={r.id}>#{r.id} · {pct(r.metrics.pass_rate)} · Prompt {r.metrics.prompt_version ?? "1.00"} · {r.triggered_by} · {time(r.created_at)}</option>
            ))}
          </select>
        )}
      </div>
      {tab === "results" ? (
        <div className="card table-wrap">
          {!detail ? <Empty>还没有评测记录{admin ? "，点击右上角运行一次" : ""}。</Empty> : (
            <table className="table">
              <thead><tr><th>用例</th><th>结果</th><th>检查项</th><th>工具轨迹</th><th>回答</th><th>耗时</th></tr></thead>
              <tbody>
                {detail.results.map((r) => (
                  <tr key={r.case_id}>
                    <td className="mono nowrap">{r.case_key}</td>
                    <td>{r.passed ? <span className="badge badge-success">通过</span> : <span className="badge badge-danger">失败</span>}</td>
                    <td style={{ minWidth: 180 }}>
                      {Object.entries(r.checks).map(([k, ok]) => (
                        <span key={k} className={`badge ${ok ? "" : "badge-danger"}`} style={{ margin: "0 4px 4px 0" }}>{ok ? "✓" : "✗"} {CHECK_TEXT[k] ?? k}</span>
                      ))}
                    </td>
                    <td className="small mono">{r.tools_called.join(" → ") || "—"}</td>
                    <td style={{ maxWidth: 360 }} className="small">{r.answer}</td>
                    <td className="mono">{r.run_id ? <Link to={`/traces/${r.run_id}`}>{ms(r.latency_ms)}</Link> : ms(r.latency_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : (
        <div className="card table-wrap">
          <table className="table">
            <thead><tr><th>用例</th><th>问题</th><th>期望</th><th>来源</th><th>状态</th></tr></thead>
            <tbody>
              {cases.data?.map((c) => (
                <tr key={c.id}>
                  <td className="mono nowrap">{c.key}<div className="faint">{c.role}</div></td>
                  <td style={{ maxWidth: 280 }}>{c.question}</td>
                  <td className="small" style={{ maxWidth: 320 }}>
                    {c.expected_tools.length > 0 && <div>工具：{c.expected_tools.join(" → ")}</div>}
                    {c.must_include.length > 0 && <div>必含：{c.must_include.join("、")}</div>}
                    {c.must_not_include.length > 0 && <div>禁含：{c.must_not_include.join("、")}</div>}
                    {c.expect_refusal != null && <div>拒答：{c.expect_refusal ? "是" : "否"}</div>}
                    {c.expect_approval != null && <div>审批：{c.expect_approval ? "需要" : "不需要"}</div>}
                    {c.expected_doc && <div>命中文档：{c.expected_doc}</div>}
                  </td>
                  <td>{c.source === "flywheel" ? <span className="badge badge-warning">数据飞轮</span> : <span className="badge">{c.source}</span>}</td>
                  <td className="stack">
                    <StatusBadge status={c.status} />
                    {admin && c.status === "candidate" && <button className="btn btn-sm" onClick={() => setStatus(c, "active")}>设为生效</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
