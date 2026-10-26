import { useEffect, useState, type FormEvent } from "react";
import { ArrowRight, BookOpenCheck, Check, ChevronRight, Github, Headset, ShieldCheck } from "lucide-react";
import { REPO_URL, isDemo, recording } from "../demo/replay";
import { api } from "../api";
import { useAuth } from "../auth";
import { Logo, ROLE_TEXT } from "../components/ui";

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

export default function LoginPage() {
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
      <button className="btn btn-primary" disabled={busy || !email || !password}>
        {busy ? "登录中…" : "登录工作台"}<ArrowRight size={16} aria-hidden="true" />
      </button>
    </form>
  );

  return (
    <div className="login">
      <section className="login-intro">
        <div className="login-brand">
          <Logo size={32} />
          <b>LoopDesk</b>
          <span className="login-brand-label">CUSTOMER OPERATIONS</span>
        </div>
        <div className="headline">
          <div className="eyebrow">让每一次服务，都有据可循</div>
          <h1>更好的服务，<br /><span>从容发生。</span></h1>
          <p className="lead">
            把智能客服、业务处理与团队协作，连接在一个清晰的工作台。让回答有出处，让复杂问题有人跟进。
          </p>
          <div className="service-flow" aria-label="服务处理流程示意">
            <div className="flow-heading"><span>从提问，到解决</span><span>服务流程</span></div>
            <div className="flow-steps">
              <div><span className="flow-icon"><Headset size={20} /></span><b>接住问题</b><small>统一服务入口</small></div>
              <ChevronRight size={16} className="flow-arrow" aria-hidden="true" />
              <div><span className="flow-icon"><BookOpenCheck size={20} /></span><b>找到依据</b><small>知识与业务数据</small></div>
              <ChevronRight size={16} className="flow-arrow" aria-hidden="true" />
              <div><span className="flow-icon"><ShieldCheck size={20} /></span><b>妥善解决</b><small>校验与人工协作</small></div>
            </div>
          </div>
          <div className="capabilities">
            {CAPABILITIES.map(([title, text]) => (
              <div className="capability" key={title}>
                <Check size={16} aria-hidden="true" />
                <b>{title}</b>
                <span className="desc">{text}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="foot"><span>DESIGNED FOR THOUGHTFUL SERVICE</span><span>Leon Wang · 个人作品 · 所有企业与业务数据均为虚构</span></div>
      </section>
      <section className="login-panel">
        <div className="login-card">
          <div className="login-panel-top"><span className="eyebrow">LOOPDESK WORKSPACE</span><a href={REPO_URL} target="_blank" rel="noreferrer"><Github size={16} /> 源码 <ArrowRight size={14} /></a></div>
          <div className="login-heading">
            <h2>{isDemo ? "选择你的工作视角" : "欢迎回到工作台"}</h2>
            <p className="muted">{isDemo ? "从顾客、坐席或管理员的视角，体验一次完整服务。" : "使用工作邮箱登录，开始今天的服务。"}</p>
          </div>
          {error && <div className="error-banner" role="alert">{error}</div>}
          {!isDemo && loginForm}
          {demo && demo.accounts.length > 0 && (
            <div className="stack login-accounts">
              <div className="login-section-label"><span>演示空间</span><span>选择角色，即刻进入 <ArrowRight size={13} /></span></div>
              <div className="account-list">
                {tenants.map((tenant, i) => (
                  <section className="account-tenant" key={tenant} aria-label={tenant}>
                    <div className="account-group"><span className={`tenant-monogram tenant-${i}`}>{tenant.slice(0, 1)}</span><b>{tenant}</b><span className="faint">{i === 0 ? "主场景" : "跨行业场景"}</span></div>
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
            </div>
          )}
          {isDemo && <div className="login-disclosure"><ShieldCheck size={17} aria-hidden="true" /><p>{demoSource()}。可浏览与回放示例，提交类操作不会保存。</p></div>}
          {isDemo && <details className="login-other"><summary>使用邮箱登录</summary>{loginForm}</details>}
        </div>
      </section>
    </div>
  );
}
