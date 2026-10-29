import { useId, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { AlertTriangle, Check, FilePlus2, MessageSquarePlus, Pencil, Search, Trash2 } from "lucide-react";
import { api } from "../api";
import { useAuth } from "../auth";
import { ConfirmDialog, Drawer, Empty, ErrorBanner, Modal, PageHeader, Skeleton, docVersion, pressable, time, useLoad } from "../components/ui";
import type { DocumentRow } from "../types";

interface DocChunk {
  id: number;
  ord: number;
  heading: string;
  text: string;
  page: number | null;
  flagged_injection: boolean;
  flag_reasons: { code: string; text: string }[];
  reviewed_at: string | null;
}

interface DocDetail extends DocumentRow {
  content: string;
  chunks: DocChunk[];
}

interface SearchHit {
  chunk_id: number;
  doc_title: string;
  heading: string;
  text: string;
  rrf: number;
  relevance: number;
  coverage: number;
  cosine: number;
  bm25_rank: number | null;
  vector_rank: number | null;
  flagged_injection: boolean;
  passes_threshold: boolean;
  rerank: number | null;
  matched_question: string | null;
}

function RetrievalDebugger() {
  const [query, setQuery] = useState("退款多久能到账");
  const [scope, setScope] = useState("customer");
  const [result, setResult] = useState<{
    query_tokens: string[]; coverage_tokens: string[]; min_relevance: number; hits: SearchHit[];
    reranker: string | null; min_rerank: number | null; rerank_floor: number | null; rerank_error: string | null;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  async function run() {
    try {
      setResult(await api.post("/api/kb/search", { query, scope, top_k: 6 }));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  return (
    <div className="card">
      <div className="card-head">
        <h3>检索调试器</h3>
        <span className="faint">BM25 + 向量（+ 已发布的口语问法）→ RRF 融合 → 重排（可选）→ 相关度阈值</span>
      </div>
      <div className="card-pad stack">
        <div className="row">
          <input className="input" aria-label="检索问题" value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => e.key === "Enter" && run()} />
          <select className="select" style={{ width: 140, flexShrink: 0 }} aria-label="检索范围" value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="customer">顾客可见范围</option>
            <option value="staff">员工可见范围</option>
          </select>
          <button className="btn btn-primary" style={{ flexShrink: 0 }} onClick={run}><Search size={15} /> 检索</button>
        </div>
        <ErrorBanner error={error} />
        {result && (
          <>
            <div className="faint">
              召回分词：{result.query_tokens.join(" / ") || "（无）"} · 覆盖率分词（口语已换成知识库用语）：
              {result.coverage_tokens.join(" / ") || "（无）"} · 相关度阈值 {result.min_relevance}
              {result.reranker ? ` · 重排模型 ${result.reranker}（单独放行 ≥ ${result.min_rerank ?? "未标定"}，否决 < ${result.rerank_floor ?? "未设"}）` : " · 未启用重排"}
              {result.rerank_error && ` · 重排失败：${result.rerank_error}`}
            </div>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr><th>片段</th><th>BM25 名次</th><th>向量名次</th><th>RRF</th><th>词覆盖</th><th>余弦</th><th>重排</th><th>相关度</th></tr>
                </thead>
                <tbody>
                  {result.hits.map((h) => (
                    <tr key={h.chunk_id} style={{ opacity: h.passes_threshold && !h.flagged_injection ? 1 : 0.5 }}>
                      <td style={{ maxWidth: 360 }}>
                        <div><b>{h.doc_title}</b> <span className="faint">{h.heading}</span></div>
                        <div className="small muted">{h.text.slice(0, 90)}</div>
                        {h.flagged_injection && <span className="badge badge-danger"><AlertTriangle size={12} /> 待复核，未进入检索</span>}
                        {!h.passes_threshold && <span className="badge">低于阈值，不进入上下文</span>}
                        {h.matched_question && <div className="small faint">经问法“{h.matched_question}”命中</div>}
                      </td>
                      <td>{h.bm25_rank ?? "—"}</td>
                      <td>{h.vector_rank ?? "—"}</td>
                      <td className="mono">{h.rrf.toFixed(4)}</td>
                      <td className="mono">{h.coverage.toFixed(2)}</td>
                      <td className="mono">{h.cosine.toFixed(2)}</td>
                      <td className="mono">{h.rerank == null ? "—" : h.rerank.toFixed(2)}</td>
                      <td><b className="mono">{h.relevance.toFixed(2)}</b></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {result.hits.length === 0 && <Empty>没有命中任何片段</Empty>}
          </>
        )}
      </div>
    </div>
  );
}

function AddDocModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [mode, setMode] = useState<"text" | "file">("text");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [visibility, setVisibility] = useState("public");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);
  async function submit() {
    setBusy(true);
    setError(null);
    try {
      if (mode === "text") await api.post("/api/kb/documents/text", { title, content, visibility });
      else {
        const f = file.current?.files?.[0];
        if (!f) throw new Error("请选择文件");
        const form = new FormData();
        form.append("file", f);
        form.append("visibility", visibility);
        if (title) form.append("title", title);
        await api.upload("/api/kb/documents/upload", form);
      }
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }
  return (
    <Modal title="新增知识文档" onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>取消</button><button className="btn btn-primary" disabled={busy} onClick={submit}>入库并索引</button></>}>
      <div className="tabs" style={{ width: "fit-content" }}>
        <button className={mode === "text" ? "active" : ""} onClick={() => setMode("text")}>粘贴 Markdown</button>
        <button className={mode === "file" ? "active" : ""} onClick={() => setMode("file")}>上传文件</button>
      </div>
      <label className="field">标题<input className="input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder={mode === "file" ? "可选，默认使用文件名" : ""} /></label>
      {mode === "text" ? (
        <label className="field">内容（按 ## 标题切分章节）
          <textarea className="textarea" style={{ minHeight: 180 }} value={content} onChange={(e) => setContent(e.target.value)}
            placeholder={"## 以旧换新\n- 以旧换新活动仅限手机和平板品类。"} />
        </label>
      ) : (
        <label className="field">文件（.md / .txt / .pdf，≤ 5MB）<input ref={file} type="file" accept=".md,.markdown,.txt,.pdf" /></label>
      )}
      <label className="field">可见范围
        <select className="select" value={visibility} onChange={(e) => setVisibility(e.target.value)}>
          <option value="public">公开：顾客和员工都能检索</option>
          <option value="internal">内部：只有员工能检索</option>
        </select>
      </label>
      <ErrorBanner error={error} />
    </Modal>
  );
}

function EditDocModal({ doc, onClose, onDone }: { doc: DocDetail; onClose: () => void; onDone: () => void }) {
  // A PDF's chunks carry page numbers, so its text is replaced by uploading a new file; its title
  // and visibility change here (a document published by mistake is made internal at once).
  const pdf = doc.chunks.some((c) => c.page);
  const [title, setTitle] = useState(doc.title);
  const [content, setContent] = useState(doc.content);
  const [visibility, setVisibility] = useState(doc.visibility);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function submit() {
    setBusy(true);
    setError(null);
    try {
      // The version this edit was made on: if someone saved in between, the server refuses
      // instead of overwriting their change.
      await api.put(`/api/kb/documents/${doc.id}`,
        pdf ? { title, visibility, version: doc.version } : { title, content, visibility, version: doc.version });
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }
  return (
    <Modal title={`编辑《${doc.title}》`} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>取消</button><button className="btn btn-primary" disabled={busy || !title.trim() || (!pdf && !content.trim())} onClick={submit}>保存为 {docVersion(doc.version + 1)}</button></>}>
      <label className="field">标题<input className="input" value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      {pdf ? <p className="faint small">PDF 正文带页码，不能在线修改；要改正文请删除后上传新版本文件。这里可以修改标题和可见范围，保存后立即生效。</p>
        : <label className="field">内容（保存后重新切分、索引，并重新检查知识冲突）
          <textarea className="textarea" style={{ minHeight: 280 }} value={content} onChange={(e) => setContent(e.target.value)} />
        </label>}
      <label className="field">可见范围
        <select className="select" value={visibility} onChange={(e) => setVisibility(e.target.value as DocDetail["visibility"])}>
          <option value="public">公开：顾客和员工都能检索</option>
          <option value="internal">内部：只有员工能检索</option>
        </select>
      </label>
      <ErrorBanner error={error} />
    </Modal>
  );
}

interface Conflict {
  id: number;
  sentence: string;
  other_sentence: string;
  document: { id: number; title: string };
  other_document: { id: number; title: string };
  detail: Record<string, { this: string[]; other: string[] }>;
  origin: "detected" | "flywheel";
  note: string;
  created_at: string;
}

interface Channels {
  official_domains: string[];
  official_contacts: string[];
}

interface DomainRow {
  domain: string;
  chunks: number;
  held_chunks: number;
  official: boolean;
}

const lines = (text: string) => text.split(/[\s,，、]+/).map((s) => s.trim()).filter(Boolean);

// Links and contact accounts outside these are held back in knowledge that asks for something, and
// stop an answer from reaching the customer (N-038 follow-up).
function OfficialChannelsCard({ admin, onChange }: { admin: boolean; onChange: () => void }) {
  const tenant = useLoad(() => api.get<Channels>("/api/tenant"));
  const domains = useLoad(() => api.get<DomainRow[]>("/api/kb/domains"));
  const [draft, setDraft] = useState<{ domains: string; contacts: string } | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (!tenant.data) return null;
  const current = tenant.data;

  async function save(next: Channels) {
    try {
      const r = await api.patch<Channels & { rescan?: { held: number; released: number } }>("/api/tenant", next);
      setDraft(null);
      setError(null);
      setMessage(r.rescan ? `已保存，并按新的官方渠道重新扫描知识库：新扣下 ${r.rescan.held} 个片段，放回 ${r.rescan.released} 个。` : "已保存。");
      tenant.reload();
      domains.reload();
      onChange();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  const outside = (domains.data ?? []).filter((d) => !d.official);

  return (
    <div className="card">
      <div className="card-head"><h3>官方渠道</h3></div>
      <div className="card-pad stack">
      <p className="faint small" style={{ margin: 0 }}>回答里只允许出现这些网站的链接和这些客服账号；其他链接、账号所在的回答会被拦下转人工，知识库里带着“填写、领取、核验”等要求的站外链接会被扣下复核。</p>
      <ErrorBanner error={error} />
      {message && <div className="notice" role="status">{message}</div>}
      {draft ? (
        <div className="stack">
          <label className="stack small">官方域名（含子域名，每行一个）
            <textarea className="textarea" rows={3} value={draft.domains} onChange={(e) => setDraft({ ...draft, domains: e.target.value })} />
          </label>
          <label className="stack small">官方客服账号（微信号、QQ 号或群号，每行一个）
            <textarea className="textarea" rows={2} value={draft.contacts} onChange={(e) => setDraft({ ...draft, contacts: e.target.value })} />
          </label>
          <div className="row">
            <button className="btn btn-primary btn-sm" onClick={() => save({ official_domains: lines(draft.domains), official_contacts: lines(draft.contacts) })}>
              <Check size={14} /> 保存并重新扫描
            </button>
            <button className="btn btn-sm" onClick={() => setDraft(null)}>取消</button>
          </div>
        </div>
      ) : (
        <div className="stack small">
          <div>域名：{current.official_domains.length ? current.official_domains.join("、") : <span className="faint">未设置（回答里的任何链接都会被拦下）</span>}</div>
          <div>客服账号：{current.official_contacts.length ? current.official_contacts.join("、") : <span className="faint">未设置</span>}</div>
          {admin && (
            <button className="btn btn-sm" style={{ width: "fit-content" }}
              onClick={() => setDraft({ domains: current.official_domains.join("\n"), contacts: current.official_contacts.join("\n") })}>
              <Pencil size={14} /> 编辑
            </button>
          )}
        </div>
      )}
      {outside.length > 0 && (
        <div className="stack small">
          <span className="faint">知识库里出现的其他网站（确认是自己或可信合作方的，再加入官方域名）：</span>
          {outside.map((d) => (
            <div key={d.domain} className="row">
              <code>{d.domain}</code>
              <span className="faint">{d.chunks} 个片段{d.held_chunks ? `，其中 ${d.held_chunks} 个待复核` : ""}</span>
              {admin && (
                <button className="btn btn-sm" onClick={() => save({ ...current, official_domains: [...current.official_domains, d.domain] })}>
                  加入官方域名
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      </div>
    </div>
  );
}

function ConflictsCard({ admin, data, reload, onChange }: { admin: boolean; data: Conflict[] | null; reload: () => void; onChange: () => void }) {
  if (!data) return <div className="card"><Skeleton rows={3} /></div>;
  if (data.length === 0) return <div className="card"><Empty>没有待核对的知识冲突。两份文档对同一规则写了不同数字时，会出现在这里。</Empty></div>;
  async function resolve(id: number) {
    try {
      await api.post(`/api/kb/conflicts/${id}/resolve`, { note: "已核对" });
    } catch {
      // 409: another administrator resolved it, or its document was deleted; the reload shows that
    } finally {
      reload();
      onChange();
    }
  }
  return (
    <div className="card">
      <div className="card-head">
        <h3>知识冲突（{data.length}）</h3>
        <span className="faint">同一规则在两份文档中写了不同的数字。核对之前，同时用到两种说法的顾客问题会转人工</span>
      </div>
      <div className="card-pad stack">
        {data.map((c) => (
          <div key={c.id} className="conflict-item">
            <div className="row">
              {Object.entries(c.detail).map(([unit, v]) => (
                <span key={unit} className="badge badge-warning">{unit}：{v.this.join("、")} ≠ {v.other.join("、")}</span>
              ))}
              {c.origin === "flywheel" && <span className="badge" title={c.note}>纠错补丁确认发布</span>}
              <span className="spacer" />
              <span className="faint">{time(c.created_at)}</span>
            </div>
            <div className="quote"><b>《{c.document.title}》</b> {c.sentence}</div>
            <div className="quote"><b>《{c.other_document.title}》</b> {c.other_sentence}</div>
            {admin && (
              <button className="btn btn-sm" style={{ width: "fit-content" }} onClick={() => resolve(c.id)}>
                <Check size={14} /> 已核对，标记为解决
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

type Tab = "docs" | "conflicts" | "channels" | "debug";

/** WAI-ARIA tabs: arrow keys move between tabs, each tab controls one panel. */
function Tabs({ tabs, value, onChange, label }: { tabs: [Tab, ReactNode][]; value: Tab; onChange: (t: Tab) => void; label: string }) {
  const base = useId();
  return (
    <div className="tabs" role="tablist" aria-label={label}
      onKeyDown={(e) => {
        if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
        const i = tabs.findIndex(([k]) => k === value);
        const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length][0];
        onChange(next);
        document.getElementById(`${base}-${next}`)?.focus();
      }}>
      {tabs.map(([key, text]) => (
        <button key={key} id={`${base}-${key}`} type="button" role="tab" aria-selected={value === key} tabIndex={value === key ? 0 : -1}
          className={value === key ? "active" : ""} onClick={() => onChange(key)}>{text}</button>
      ))}
    </div>
  );
}

export default function KnowledgePage() {
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const [params, setParams] = useSearchParams();
  const tab = (["docs", "conflicts", "channels", "debug"].includes(params.get("tab") ?? "") ? params.get("tab") : "docs") as Tab;
  const setTab = (t: Tab) => setParams(t === "docs" ? {} : { tab: t }, { replace: true });
  const { data, error, reload } = useLoad(() => api.get<DocumentRow[]>("/api/kb/documents"));
  const conflicts = useLoad(() => api.get<Conflict[]>("/api/kb/conflicts"));
  const [detail, setDetail] = useState<DocDetail | null>(null);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState(false);
  const [query, setQuery] = useState("");
  const [confirm, setConfirm] = useState<{ kind: "release"; chunk: DocChunk } | { kind: "remove"; id: number } | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  function changed() {
    reload();
    conflicts.reload();
  }

  async function open(id: number) {
    setDetail(await api.get<DocDetail>(`/api/kb/documents/${id}`));
  }
  async function release(chunk: DocChunk) {
    setConfirm(null);
    setDetail(await api.post<DocDetail>(`/api/kb/chunks/${chunk.id}/release`, {}));
    changed();
  }
  async function propose(id: number) {
    try {
      const r = await api.post<{ patch_id: number; sections: number; questions: number }>(`/api/kb/documents/${id}/questions`, {});
      setMessage(`已为 ${r.sections} 个片段生成 ${r.questions} 条口语问法（补丁 #${r.patch_id}），请到「数据飞轮」审核后发布；发布前不影响检索。`);
    } catch (e) {
      setMessage(e instanceof Error ? e.message : String(e));
    }
  }
  async function remove(id: number) {
    setConfirm(null);
    await api.del(`/api/kb/documents/${id}`);
    setDetail(null);
    changed();
  }

  const docs = (data ?? []).filter((d) => !query.trim() || d.title.includes(query.trim()));
  const flagged = (data ?? []).reduce((n, d) => n + d.flagged_chunks, 0);
  return (
    <div className="page stack" style={{ gap: 16 }}>
      <PageHeader title="知识库"
        description="文档按标题切分并保留章节路径。入库时扫描提示注入和诈骗话术（索要验证码或银行卡号、私人微信、转账），命中的片段不进入检索，等管理员复核。"
        actions={admin && <button className="btn btn-primary" onClick={() => setAdding(true)}><FilePlus2 size={16} /> 新增文档</button>} />
      <ErrorBanner error={error} />
      {message && <div className="notice" role="status">{message}</div>}
      <Tabs label="知识库视图" value={tab} onChange={setTab} tabs={[
        ["docs", <>文档<span className="count">{data?.length ?? ""}</span></>],
        ["conflicts", <>知识冲突{conflicts.data && conflicts.data.length > 0 && <span className="badge badge-warning tab-badge">{conflicts.data.length}</span>}</>],
        ["channels", "官方渠道"],
        ["debug", "检索调试"],
      ]} />
      <div role="tabpanel" className="stack" style={{ gap: 16 }}>
        {tab === "docs" && (
          <div className="card">
            <div className="card-toolbar">
              <label className="search-input" style={{ width: 260, maxWidth: "100%" }}>
                <Search size={14} aria-hidden="true" />
                <input className="input" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="按标题搜索" aria-label="按标题搜索文档" />
              </label>
              <span className="spacer" />
              {flagged > 0 && <span className="badge badge-danger"><AlertTriangle size={12} /> {flagged} 个片段待复核</span>}
              <span className="faint">{docs.length} 份文档</span>
            </div>
            {!data ? <Skeleton rows={5} /> : (
              <div className="table-wrap">
                <table className="table">
                  <thead><tr><th>标题</th><th>可见范围</th><th className="num">片段数</th><th>安全扫描</th><th>知识冲突</th><th>版本</th><th>更新时间</th></tr></thead>
                  <tbody>
                    {docs.map((d) => (
                      <tr key={d.id} className="clickable" {...pressable(() => open(d.id))}>
                        <td><b>{d.title}</b> <span className="faint">{d.source}</span></td>
                        <td>{d.visibility === "public" ? <span className="badge badge-primary">公开</span> : <span className="badge badge-info">仅内部</span>}</td>
                        <td className="num">{d.chunk_count}</td>
                        <td>{d.flagged_chunks ? <span className="badge badge-danger"><AlertTriangle size={12} /> {d.flagged_chunks} 个片段待复核</span> : <span className="faint">无</span>}</td>
                        <td>{d.conflicts ? <span className="badge badge-warning">{d.conflicts} 处待核对</span> : <span className="faint">无</span>}</td>
                        <td>{docVersion(d.version)}</td>
                        <td className="faint nowrap">{time(d.updated_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {docs.length === 0 && <Empty>{query ? "没有标题匹配的文档" : "知识库还没有文档"}</Empty>}
              </div>
            )}
          </div>
        )}
        {tab === "conflicts" && <ConflictsCard admin={admin} data={conflicts.data} reload={conflicts.reload} onChange={reload} />}
        {tab === "channels" && <OfficialChannelsCard admin={admin} onChange={changed} />}
        {tab === "debug" && <RetrievalDebugger />}
      </div>
      {adding && <AddDocModal onClose={() => setAdding(false)} onDone={() => { setAdding(false); changed(); }} />}
      {editing && detail && <EditDocModal doc={detail} onClose={() => setEditing(false)}
        onDone={() => { setEditing(false); changed(); open(detail.id); }} />}
      {detail && (
        <Drawer title={<div><h2>{detail.title}</h2><div className="faint">{detail.chunk_count} 个片段 · 版本 {docVersion(detail.version)} · {detail.visibility === "public" ? "公开" : "仅内部"}</div></div>}
          onClose={() => setDetail(null)}>
          <div className="stack" style={{ gap: 12 }}>
            {admin && (
              <div className="row row-wrap">
                <button className="btn" onClick={() => setEditing(true)}>
                  <Pencil size={15} /> {detail.chunks.some((c) => c.page) ? "修改标题和可见范围" : "编辑"}</button>
                <button className="btn" onClick={() => propose(detail.id)}
                  title="用模型为每个片段写 3-5 个顾客口语问法，审核发布后作为检索索引">
                  <MessageSquarePlus size={15} /> 生成口语问法</button>
                <span className="spacer" />
                <button className="btn btn-danger" onClick={() => setConfirm({ kind: "remove", id: detail.id })}><Trash2 size={15} /> 删除文档</button>
              </div>
            )}
            {detail.chunks.map((c) => (
              <div key={c.id} className={`chunk ${c.flagged_injection ? "flagged" : ""}`}>
                <div className="row faint">#{c.ord} · {c.heading || "（无标题）"}{c.page && ` · 第 ${c.page} 页`}
                  {c.flagged_injection && <span className="badge badge-danger">待复核，未进入检索</span>}
                  {!c.flagged_injection && c.reviewed_at && <span className="badge" title={time(c.reviewed_at)}>已复核放行</span>}
                </div>
                {c.flag_reasons.length > 0 && (
                  <div className="row row-wrap small">
                    <span className="faint">命中：</span>
                    {c.flag_reasons.map((r) => <span key={r.code} className="badge badge-warning">{r.text}</span>)}
                  </div>
                )}
                <div style={{ whiteSpace: "pre-wrap" }}>{c.text}</div>
                {admin && c.flagged_injection && (
                  <button className="btn btn-sm" style={{ width: "fit-content" }} onClick={() => setConfirm({ kind: "release", chunk: c })}>
                    <Check size={14} /> 复核无害，放行
                  </button>
                )}
              </div>
            ))}
          </div>
        </Drawer>
      )}
      {confirm?.kind === "release" && (
        <ConfirmDialog title="放行这个片段" confirmText="确认放行" onClose={() => setConfirm(null)} onConfirm={() => void release(confirm.chunk)}>
          <p>放行后这个片段会进入检索，顾客的问题可能用到它。确认已经读过全文，“{confirm.chunk.flag_reasons.map((r) => r.text).join("、")}”是误判吗？</p>
          {confirm.chunk.flag_reasons.some((r) => r.code === "outside_link" || r.code === "private_contact") && (
            <p className="small muted">放行不会让回答里的站外链接或客服账号通过；如果它们是你们自己的，请加到“官方渠道”。</p>
          )}
        </ConfirmDialog>
      )}
      {confirm?.kind === "remove" && (
        <ConfirmDialog title="删除文档" confirmText="删除" tone="danger" onClose={() => setConfirm(null)} onConfirm={() => void remove(confirm.id)}>
          <p>删除后该文档的所有片段会从索引中移除，确定吗？</p>
        </ConfirmDialog>
      )}
    </div>
  );
}
