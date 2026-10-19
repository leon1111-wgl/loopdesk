import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import { Logo, ROLE_TEXT } from "../components/ui";

interface DemoAccounts {
  accounts: { email: string; name: string; role: string; tenant: string }[];
  password: string | null;
}

const CAPABILITIES: [string, string][] = [
  ["证据绑定", "回答逐句核对知识库与业务数据，数字对不上的句子会被标出或拦截。"],
  ["人工复核", "超出额度的退款先进入审批队列，审批人能看到 Agent 提交的全部依据。"],
  ["纠错闭环", "客服的一次纠正，会同时变成知识补丁和一条回归测试用例。"],
  ["回归评测", "每次改动自动跑评测集，哪条用例从通过变成失败一目了然。"],
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

  return (
    <div className="login">
      <section className="login-intro">
        <div className="row" style={{ gap: 10 }}>
          <Logo size={28} />
          <b style={{ fontSize: 16 }}>LoopDesk</b>
        </div>
        <div className="headline">
          <h1>企业智能客服工作台</h1>
          <p className="lead">
            知识问答、订单处理和人工审批放在同一个工作台里。每条回答都能追溯到出处，每次纠错都会沉淀下来。
          </p>
          <div className="capabilities">
            {CAPABILITIES.map(([title, text], i) => (
              <div className="capability" key={title}>
                <span className="no">0{i + 1}</span>
                <b>{title}</b>
                <span className="desc">{text}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="foot">Leon Wang · 个人作品 · 页面中的企业、订单与客户信息均为虚构</div>
      </section>
      <section className="login-panel">
        <div className="login-card">
          <div>
            <h1>登录</h1>
            <p className="muted" style={{ marginTop: 4 }}>使用工作邮箱登录，或选择下方的演示账号</p>
          </div>
          <form className="stack" style={{ gap: 14 }} onSubmit={submit}>
            <label className="field">
              邮箱
              <input className="input" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" placeholder="name@company.com" />
            </label>
            <label className="field">
              密码
              <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
            </label>
            {error && <div className="error-banner">{error}</div>}
            <button className="btn btn-primary" disabled={busy || !email || !password} style={{ width: "100%" }}>
              {busy ? "登录中…" : "登录"}
            </button>
          </form>
          {demo && demo.accounts.length > 0 && (
            <div className="stack">
              <div className="faint">演示账号 · 点击即可进入，不同角色看到的功能不同</div>
              <div className="account-list">
                {tenants.map((tenant) => (
                  <div key={tenant}>
                    <div className="account-group">{tenant}</div>
                    {demo.accounts.filter((a) => a.tenant === tenant).map((a) => (
                      <button key={a.email} className="account" disabled={busy}
                        onClick={() => submit(undefined, { email: a.email, password: demo.password ?? "" })}>
                        <span className="avatar me">{a.name.slice(0, 1)}</span>
                        <span style={{ minWidth: 0, lineHeight: 1.35 }}>
                          <div style={{ fontSize: 13 }}>{a.name} <span className="badge">{ROLE_TEXT[a.role]}</span></div>
                          <div className="faint ellipsis">{a.email}</div>
                        </span>
                        <span className="go">进入 →</span>
                      </button>
                    ))}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
