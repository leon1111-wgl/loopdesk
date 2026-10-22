import { useState } from "react";
import { Link } from "react-router-dom";
import { Check, X } from "lucide-react";
import { ApiError, api } from "../api";
import { useAuth } from "../auth";
import { Empty, ErrorBanner, Kpi, StatusBadge, docVersion, pct, time, useLoad } from "../components/ui";

interface FeedbackRow {
  id: number;
  rating: string;
  question: string;
  answer: string;
  correction: string;
  reason_category: string;
  attribution: string;
  run_id: string | null;
  user: { name: string; role: string };
  created_at: string;
  patch: { id: number; status: string } | null;
  eval_case: { id: number; key: string; status: string } | null;
}

interface QuestionItem {
  heading: string;
  text_sha: string;
  questions: string[];
}

interface Patch {
  id: number;
  kind: "correction" | "questions";
  feedback_id: number | null;
  title: string;
  content: string;
  status: string;
  created_at: string;
  payload: {
    document_title?: string;
    items?: QuestionItem[];
    published?: { indexed: number; skipped: string[]; stale: number };
  } | null;
}

/** Colloquial questions proposed as index entries (W5): the reviewer unticks the wrong ones. */
function QuestionsPatch({ patch, editable, dropped, onToggle }: {
  patch: Patch; editable: boolean; dropped: Set<string>; onToggle: (q: string) => void;
}) {
  const items = patch.payload?.items ?? [];
  const published = patch.payload?.published;
  return (
    <div className="stack" style={{ gap: 6, margin: "6px 0" }}>
      <div className="small"><b>{patch.title}</b> <span className="faint">· 发布后作为片段的检索索引，不改动原文</span></div>
      {items.map((item, i) => (
        <div key={i} className="stack" style={{ gap: 2 }}>
          <div className="small muted">{item.heading}</div>
          {item.questions.map((q) => editable ? (
            <label key={q} className="row small">
              <input type="checkbox" checked={!dropped.has(q)} onChange={() => onToggle(q)} />
              <span style={dropped.has(q) ? { textDecoration: "line-through", color: "var(--text-3)" } : undefined}>{q}</span>
            </label>
          ) : (
            <div key={q} className="small">· {q}</div>
          ))}
        </div>
      ))}
      {published && (
        <div className="small faint">已索引 {published.indexed} 条{published.skipped.length ? `，跳过 ${published.skipped.length} 条` : ""}
          {published.stale ? `，${published.stale} 条因原文已修改未生效` : ""}</div>
      )}
    </div>
  );
}

interface PatchConflict {
  patch_sentence: string;
  document: string;
  existing_sentence: string;
}

interface Stats {
  feedback: Record<string, number>;
  attribution: Record<string, number>;
  reason_labels: Record<string, string>;
  patches: Record<string, number>;
  eval_cases: { total: number; from_flywheel: number; candidates: number };
  pass_rate_history: { id: number; pass_rate: number; total: number }[];
}

const RATING: Record<string, [string, string]> = {
  up: ["点赞", "badge-success"],
  down: ["点踩", "badge-danger"],
  correction: ["纠错", "badge-warning"],
};

function Sparkline({ values }: { values: { pass_rate: number; total: number }[] }) {
  if (values.length < 2) return <span className="faint">至少运行两次评测后显示趋势</span>;
  const w = 260, h = 56;
  const points = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - v.pass_rate * (h - 8) - 4}`).join(" ");
  return (
    <svg width={w} height={h} role="img" aria-label="评测通过率趋势">
      <polyline points={points} fill="none" stroke="var(--primary)" strokeWidth={2} />
      {values.map((v, i) => (
        <circle key={i} cx={(i / (values.length - 1)) * w} cy={h - v.pass_rate * (h - 8) - 4} r={3} fill="var(--primary)">
          <title>{`${pct(v.pass_rate)}（${v.total} 条用例）`}</title>
        </circle>
      ))}
    </svg>
  );
}

export default function FlywheelPage() {
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const stats = useLoad(() => api.get<Stats>("/api/flywheel/stats"));
  const feedback = useLoad(() => api.get<FeedbackRow[]>("/api/flywheel/feedback"));
  const patches = useLoad(() => api.get<Patch[]>("/api/flywheel/patches"));
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A patch that contradicts the knowledge the corrected answer cited waits for an explicit decision.
  const [disputed, setDisputed] = useState<{ patch: Patch; reason: string; conflicts: PatchConflict[] } | null>(null);
  // Questions the reviewer unticked, per questions patch.
  const [dropped, setDropped] = useState<Record<number, Set<string>>>({});

  function toggle(patchId: number, question: string) {
    setDropped((all) => {
      const next = new Set(all[patchId] ?? []);
      if (next.has(question)) next.delete(question); else next.add(question);
      return { ...all, [patchId]: next };
    });
  }

  async function act(patch: Patch, action: "apply" | "reject", acknowledge = false) {
    try {
      const body = action !== "apply" ? {}
        : patch.kind === "questions" ? { skip_questions: [...(dropped[patch.id] ?? [])] }
        : { acknowledge_conflicts: acknowledge };
      const r = await api.post<{ document_version?: number; conflicts?: number; eval_case_activated?: boolean;
        questions_indexed?: number; questions_stale?: number }>(`/api/flywheel/patches/${patch.id}/${action}`, body);
      setDisputed(null);
      if (patch.kind === "questions") {
        setMessage(action === "reject" ? `问法 #${patch.id} 已驳回，没有建立任何索引`
          : `已发布 ${r.questions_indexed ?? 0} 条问法到检索索引` +
            (r.questions_stale ? `；${r.questions_stale} 条对应的原文已修改，没有生效，请重新生成` : ""));
        setError(null);
        patches.reload();
        stats.reload();
        return;
      }
      const published = r.document_version ? `（版本 ${docVersion(r.document_version)}）` : "";
      setMessage(action === "reject"
        ? `补丁 #${patch.id} 已驳回`
        : r.eval_case_activated === false
          ? `补丁 #${patch.id} 已发布${published}。因为它与原文档冲突，对应评测用例保持候选状态：请先修改原文档，再到评测中心激活。`
          : `补丁 #${patch.id} 已发布到知识库${published}，对应评测用例已激活。` +
            (r.conflicts ? `注意：补丁与现有知识存在 ${r.conflicts} 处数字冲突，请到「知识库」核对。` : "去评测中心运行回归，确认它从失败变为通过。"));
      setError(null);
      patches.reload();
      stats.reload();
      feedback.reload();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && Array.isArray(e.body?.conflicts)) {
        setDisputed({ patch, reason: e.message, conflicts: e.body.conflicts as PatchConflict[] });
        setError(null);
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const s = stats.data;
  const labels = s?.reason_labels ?? {};
  const attributionTotal = Object.values(s?.attribution ?? {}).reduce((a, b) => a + b, 0);
  return (
    <div className="page stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <h1>数据飞轮</h1>
          <p>客服的一次纠错 → 失败归因 + 回归评测用例 + 知识补丁草稿。管理员发布补丁后，知识库重建索引、用例转为生效，回归评测验证闭环。</p>
        </div>
      </div>
      <ErrorBanner error={error} />
      {disputed && (
        <div className="card" role="alert">
          <div className="card-head"><h3>补丁 #{disputed.patch.id} 与现有知识冲突</h3></div>
          <div className="card-pad stack">
            <div className="small">{disputed.reason}</div>
            <div className="small faint">仍然发布时，这些冲突会列入知识库的待核对清单；核对之前，同时用到两种说法的顾客问题会转人工。</div>
            {disputed.conflicts.map((c, i) => (
              <div key={i} className="json small stack" style={{ gap: 4 }}>
                <div className="quote"><b>补丁</b> {c.patch_sentence}</div>
                <div className="quote"><b>《{c.document}》</b> {c.existing_sentence}</div>
              </div>
            ))}
            <div className="row">
              <button className="btn btn-sm btn-danger" onClick={() => act(disputed.patch, "reject")}><X size={14} /> 驳回补丁</button>
              <button className="btn btn-sm" onClick={() => act(disputed.patch, "apply", true)}>已修改原文档，仍然发布（用例不自动生效）</button>
              <button className="btn btn-sm btn-text" onClick={() => setDisputed(null)}>取消</button>
            </div>
          </div>
        </div>
      )}
      {message && <div className="notice">{message} <Link to="/evals">前往评测中心 →</Link></div>}
      <div className="grid grid-4">
        <Kpi label="收到反馈" value={(s?.feedback.up ?? 0) + (s?.feedback.down ?? 0) + (s?.feedback.correction ?? 0)}
          sub={`点赞 ${s?.feedback.up ?? 0} · 点踩 ${s?.feedback.down ?? 0} · 纠错 ${s?.feedback.correction ?? 0}`} />
        <Kpi label="知识补丁" value={s?.patches.applied ?? 0} sub={`已发布 · 待审核 ${s?.patches.proposed ?? 0} · 驳回 ${s?.patches.rejected ?? 0}`} />
        <Kpi label="评测用例" value={s?.eval_cases.total ?? 0} sub={`其中来自飞轮 ${s?.eval_cases.from_flywheel ?? 0} · 候选 ${s?.eval_cases.candidates ?? 0}`} />
        <div className="card kpi">
          <div className="kpi-label">回归通过率趋势</div>
          <Sparkline values={s?.pass_rate_history ?? []} />
        </div>
      </div>
      <div className="grid grid-2">
        <div className="card">
          <div className="card-head"><h3>失败归因分布</h3><span className="faint">来自 trace 自动分析，可被人工覆盖</span></div>
          <div className="card-pad stack">
            {attributionTotal === 0 && <Empty>暂无差评或纠错</Empty>}
            {Object.entries(s?.attribution ?? {}).map(([k, v]) => (
              <div key={k}>
                <div className="row small"><span>{labels[k] ?? k}</span><span className="spacer" /><span className="mono">{v}</span></div>
                <div className="bar-track"><div className="bar-fill" style={{ width: `${(v / attributionTotal) * 100}%` }} /></div>
              </div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="card-head"><h3>知识补丁</h3><span className="faint">{admin ? "审核后发布到“客服纠错知识补丁”文档" : "仅管理员可发布"}</span></div>
          <div className="card-pad stack">
            {patches.data?.length === 0 && <Empty>在对话里用“纠错”按钮纠正一条知识类回答，这里会出现补丁草稿。</Empty>}
            {patches.data?.map((p) => (
              <div key={p.id} className="json">
                <div className="row"><b>#{p.id}</b><StatusBadge status={p.status} /><span className="spacer" /><span className="faint">{time(p.created_at)}</span></div>
                {p.kind === "questions" ? (
                  <QuestionsPatch patch={p} editable={admin && p.status === "proposed"} dropped={dropped[p.id] ?? new Set()}
                    onToggle={(q) => toggle(p.id, q)} />
                ) : (
                  <pre style={{ fontFamily: "inherit", margin: "6px 0" }}>{p.content}</pre>
                )}
                {admin && p.status === "proposed" && (
                  <div className="row">
                    <button className="btn btn-sm btn-success" onClick={() => act(p, "apply")}><Check size={14} /> 发布</button>
                    <button className="btn btn-sm btn-danger" onClick={() => act(p, "reject")}><X size={14} /> 驳回</button>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
      <div className="card table-wrap">
        <div className="card-head"><h3>反馈记录</h3></div>
        <table className="table">
          <thead><tr><th>时间</th><th>类型</th><th>问题</th><th>原回答 → 纠正</th><th>归因</th><th>产出</th></tr></thead>
          <tbody>
            {feedback.data?.map((f) => (
              <tr key={f.id}>
                <td className="faint nowrap">{time(f.created_at)}<div>{f.user.name}</div></td>
                <td><span className={`badge ${RATING[f.rating]?.[1] ?? ""}`}>{RATING[f.rating]?.[0] ?? f.rating}</span></td>
                <td style={{ maxWidth: 200 }}>{f.question}</td>
                <td style={{ maxWidth: 380 }}>
                  <div className="small muted">{f.answer}</div>
                  {f.correction && <div className="small" style={{ color: "var(--success)" }}>→ {f.correction}</div>}
                </td>
                <td>{f.reason_category ? <span className="badge">{labels[f.reason_category] ?? f.reason_category}</span> : "—"}</td>
                <td className="stack small">
                  {f.eval_case && <span>用例 {f.eval_case.key} <StatusBadge status={f.eval_case.status} /></span>}
                  {f.patch && <span>补丁 #{f.patch.id} <StatusBadge status={f.patch.status} /></span>}
                  {f.run_id && <Link to={`/traces/${f.run_id}`}>链路</Link>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {feedback.data?.length === 0 && <Empty>暂无反馈</Empty>}
      </div>
    </div>
  );
}
