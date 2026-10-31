import { useEffect, useState, type FormEvent } from "react";
import { ArrowRight, BookOpenCheck, Check, Github, Headset, ShieldCheck } from "lucide-react";
import { REPO_URL, isDemo, recording } from "../demo/replay";
import { api } from "../api";
import { useAuth } from "../auth";
import { Logo, ROLE_TEXT } from "../components/ui";

const SKIP_INTRO_KEY = "loopdesk.skip-intro"; // IntroPage's "下次直接进入"

function demoSource(): string {
  const rec = recording();
  return rec && !rec.model.offline
    ? `这是静态演示。回答由真实模型 ${rec.model.strong} 生成（${rec.date} 录制，数据虚构）`
    : "这是静态演示，数据录制自真实后端的离线运行";
}

interface DemoAccounts {
  accounts: { email: string; name: string; role: string; tenant: string }[];
  password: string | null;
}

const CAPABILITIES: [string, string][] = [
  ["回答有据可查", "逐句核对知识库与业务数据"],
  ["关键操作可复核", "高风险动作交由人工审批"],
  ["每次纠错有积累", "修正沉淀为知识与评测用例"],
  ["服务质量可追踪", "从一次对话到完整运行链路"],
];

/** The sign-in panel: demo accounts by tenant (in demo mode) and the e-mail form. The intro page
 * embeds it, so "进入控制台" lands on it without another page. */
export default function LoginPage({ embedded }: { embedded?: boolean } = {}) {
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [demo, setDemo] = useState<DemoAccounts | null>(null);

  useEffect(() => {
    api.get<DemoAccounts>("/api/auth/demo-accounts").then(setDemo).catch(() => setDemo(null));
  }, []);

  async function submit(e?: FormEvent, override?: { email: string; password: string }) {
    e?.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(override?.email ?? email, override?.password ?? password);
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setBusy(false);
    }
  }

  const tenants = demo ? [...new Set(demo.accounts.map((a) => a.tenant))] : [];

  const loginForm = (
    <form className="stack login-form" onSubmit={submit}>
      <label className="field">
        工作邮箱
        <input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" placeholder="name@company.com" />
      </label>
      <label className="field">
        密码
        <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" placeholder="输入登录密码" />
      </label>
      <button className="btn btn-primary btn-lg" disabled={busy || !email || !password}>
        {busy ? "登录中…" : "登录工作台"}<ArrowRight size={16} aria-hidden="true" />
      </button>
    </form>
  );

  const INDUSTRY: Record<string, string> = { "Echo音乐": "音乐流媒体", "Leon优选": "电商零售", "Iris云文档": "SaaS 服务" };

  const panel = (
  <section className="login-panel" aria-label="登录">
    {!embedded && <div className="login-heading">
      <h2>{isDemo || demo?.accounts.length ? "选择演示账号" : "登录工作台"}</h2>
      <p className="muted">{isDemo || demo?.accounts.length ? "从顾客、客服坐席或管理员的视角，体验一次完整服务。" : "使用工作邮箱登录，开始今天的服务。"}</p>
    </div>}
    {error && <div className="error-banner" role="alert">{error}</div>}
    {!isDemo && !demo?.accounts.length && loginForm}
    {demo && demo.accounts.length > 0 && (
      <div className="account-list">
        {tenants.map((tenant, i) => (
          <section className="account-tenant" key={tenant} aria-label={tenant}>
            <div className="account-group">
              <span className={`tenant-monogram tenant-${i}`} aria-hidden="true">{tenant.slice(0, 1)}</span>
              <b>{tenant}</b>
              {INDUSTRY[tenant] && <span className="badge">{INDUSTRY[tenant]}</span>}
              <span className="faint">{i === 0 ? "主场景" : "跨行业场景"}</span>
            </div>
            <div className="account-grid">
            {demo.accounts.filter((a) => a.tenant === tenant).map((a) => (
              <button key={a.email} className="account" disabled={busy}
                title={a.email} aria-label={`${a.name} ${ROLE_TEXT[a.role]} ${a.email} 进入`}
                onClick={() => submit(undefined, { email: a.email, password: demo.password ?? "" })}>
                <span className={`avatar role-${a.role}`}>{a.name.slice(0, 1)}</span>
                <span className="account-person"><b>{a.name}</b><span>{ROLE_TEXT[a.role]}</span></span>
                <ArrowRight size={14} className="go" aria-hidden="true" />
              </button>
            ))}
            </div>
          </section>
        ))}
      </div>
    )}
    {isDemo && <div className="login-disclosure"><ShieldCheck size={15} aria-hidden="true" /><p>{demoSource()}。可浏览与回放示例，提交类操作不会保存。</p></div>}
    {(isDemo || (demo?.accounts.length ?? 0) > 0) && <details className="login-other"><summary>使用邮箱登录</summary>{loginForm}</details>}
  </section>
  );
  if (embedded) return panel;

  return (
    <div className="login">
      <header className="login-top">
        <div className="login-brand"><Logo size={28} /><b>LoopDesk</b><span className="login-brand-label">智能客服工作台</span></div>
        <span className="row" style={{ gap: 16 }}>
          <a href={import.meta.env.BASE_URL} className="login-source" onClick={() => { try { localStorage.removeItem(SKIP_INTRO_KEY); } catch { /* ignore */ } }}>产品介绍</a>
          <a href={REPO_URL} target="_blank" rel="noreferrer" className="login-source"><Github size={15} aria-hidden="true" /> 项目源码</a>
        </span>
      </header>
      <main className="login-main">
        <div className="login-frame">
          <section className="login-intro" aria-label="产品介绍">
            <div className="eyebrow">让每一次服务，都有据可循</div>
            <h1>多租户智能客服与人工协作平台</h1>
            <p className="lead">智能客服、业务处理与团队协作在一个工作台：回答逐句核对出处，退款等高风险操作交给人工审批，每次纠错都沉淀为知识与评测用例。</p>
            <ol className="service-flow" aria-label="服务处理流程">
              <li><span className="flow-icon"><Headset size={16} aria-hidden="true" /></span><b>接住问题</b><small>统一服务入口</small></li>
              <li><span className="flow-icon"><BookOpenCheck size={16} aria-hidden="true" /></span><b>找到依据</b><small>知识与业务数据</small></li>
              <li><span className="flow-icon"><ShieldCheck size={16} aria-hidden="true" /></span><b>妥善解决</b><small>校验与人工协作</small></li>
            </ol>
            <ul className="capabilities">
              {CAPABILITIES.map(([title, text]) => (
                <li className="capability" key={title}>
                  <Check size={14} aria-hidden="true" />
                  <span><b>{title}</b><span className="desc">{text}</span></span>
                </li>
              ))}
            </ul>
            <div className="foot">Leon Wang · 个人作品 · 所有企业、人员与订单均为虚构（SYNTHETIC）</div>
          </section>
          {panel}
        </div>
      </main>
    </div>
  );
}
