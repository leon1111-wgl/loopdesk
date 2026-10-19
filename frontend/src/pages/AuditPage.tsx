import { api } from "../api";
import { Empty, ErrorBanner, time, useLoad } from "../components/ui";

interface AuditEntry {
  id: number;
  action: string;
  action_label: string;
  target: string;
  detail: Record<string, unknown>;
  request_id: string;
  created_at: string;
  actor: { id: number; name: string; email: string } | null;
}

const LABEL: Record<string, string> = {
  auto_refund_limit: "自动退款额度",
  ticket_sla_hours: "工单时限（小时）",
  return_window_days: "无理由退货期（天）",
  quality_window_days: "质量问题退货期（天）",
  title: "标题",
  visibility: "可见范围",
  status: "状态",
  question: "问题",
  must_include: "必含内容",
  must_not_include: "禁含内容",
  expected_tools: "期望工具",
  version: "版本",
  from_version: "原版本",
  to_version: "新版本",
  file: "文件",
  content: "内容",
  document_version: "补丁文档版本",
  acknowledged_conflicts: "确认发布的冲突",
  key: "用例",
  sentence: "句子",
  other_sentence: "另一句",
  note: "备注",
  erased: "抹除的记录",
};

function show(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.map(show).join("、");
  if (typeof value === "object") return Object.entries(value).map(([k, v]) => `${k} ${show(v)}`).join("，");
  return String(value);
}

function Detail({ detail }: { detail: Record<string, unknown> }) {
  const { changes, diff, ...rest } = detail as { changes?: Record<string, [unknown, unknown]>; diff?: string };
  return (
    <div className="stack" style={{ gap: 4 }}>
      {changes && Object.entries(changes).map(([field, [before, after]]) => (
        <div key={field}>{LABEL[field] ?? field}：<span className="faint">{show(before)}</span> → <b>{show(after)}</b></div>
      ))}
      {Object.entries(rest).map(([key, value]) => (
        <div key={key} className="small"><span className="faint">{LABEL[key] ?? key}：</span>{show(value)}</div>
      ))}
      {diff && (
        <details>
          <summary className="small">查看修改的行</summary>
          <pre className="json small" style={{ whiteSpace: "pre-wrap", margin: "6px 0 0" }}>{diff}</pre>
        </details>
      )}
    </div>
  );
}

export default function AuditPage() {
  const { data, error } = useLoad(() => api.get<AuditEntry[]>("/api/audit?limit=200"));
  return (
    <div className="page stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <h1>操作审计</h1>
          <p>管理员对风控额度、知识文档、知识补丁、评测用例和个人数据的每一次修改：谁、什么时候、改了什么。Agent 的操作见链路追踪和审批记录。</p>
        </div>
      </div>
      <ErrorBanner error={error} />
      <div className="card table-wrap">
        <table className="table">
          <thead><tr><th>时间</th><th>操作人</th><th>操作</th><th>对象</th><th>内容</th></tr></thead>
          <tbody>
            {data?.map((e) => (
              <tr key={e.id}>
                <td className="faint" title={`请求 ID：${e.request_id}`}>{time(e.created_at)}</td>
                <td>{e.actor ? <>{e.actor.name}<div className="faint small">{e.actor.email}</div></> : <span className="faint">—</span>}</td>
                <td>{e.action_label}</td>
                <td className="faint">{e.target}</td>
                <td><Detail detail={e.detail} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && data.length === 0 && <Empty>还没有管理操作记录</Empty>}
      </div>
    </div>
  );
}
