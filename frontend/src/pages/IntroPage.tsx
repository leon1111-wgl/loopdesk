import { useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import {
  Activity, ArrowRight, BookOpen, BookOpenCheck, ClipboardCheck, Github, Headset, MessageSquareText, Pause, Play, RefreshCcw,
  ReceiptText, Search, ShieldCheck,
} from "lucide-react";
import { REPO_URL } from "../demo/replay";
import { isStaff, useAuth } from "../auth";
import { Logo, ROLE_TEXT, SKIP_INTRO_KEY } from "../components/ui";
import type { User } from "../types";
import LoginPage from "./LoginPage";

// The public intro page: what LoopDesk is, how one request flows through it, and the demo accounts.
// Only this page has an English version; the console itself is Chinese.

const LANG_KEY = "loopdesk.intro-lang";
type Lang = "zh" | "en";

const T = {
  zh: {
    nav: [["product", "产品"], ["how", "运作方式"], ["sign-in", "演示账号"], ["faq", "常见问题"]],
    enter: "进入控制台",
    sub: "客服工作台",
    eyebrow: "多租户 · 证据优先 · 人机协作",
    h1: ["每一句回答，", "都有据可查"],
    lead: "LoopDesk 把智能客服、业务处理和人工协作放进一个控制台：回答逐句核对知识库和订单数据，退款等高风险操作交给人工审批，每一次纠错都沉淀为知识补丁和回归用例。",
    how: "看运作方式",
    skip: "下次直接进入",
    signedIn: "已登录演示账号",
    switchAccount: "切换账号",
    cancel: "取消",
    signOut: "退出登录",
    chips: ["逐句证据校验", "超额退款人工审批", "离线可复现", "数据均为虚构"],
    liveTitle: "一次退款，从提问到受理",
    liveTag: "示例 · 虚构数据",
    live: [
      ["顾客提问", "“SO20261015102 年卡我要退款”"],
      ["检索规定", "《会员与自动续费》等 2 篇，RRF 融合 + 阈值"],
      ["查询订单", "年卡 ¥198.00，权益未使用，在退款期内"],
      ["逐句校验", "数字、订单状态和证据逐句比对，覆盖 100%"],
      ["人工审批", "超过自动额度 ¥20，坐席看证据包后通过"],
      ["退款受理", "幂等键先落库，再请求支付系统"],
    ],
    liveFoot: ["同一流程在控制台的链路追踪里逐步回放", "SYNTHETIC"],
    stats: [["3", "个虚构租户"], ["3", "种角色视角"], ["8", "个 Agent 工具"], ["14", "条端到端走查"]],
    howTitle: "运作方式",
    howLead: "一条顾客消息在 LoopDesk 里经过五步，每一步都能在控制台里看到、追溯和纠正。",
    steps: [
      ["接住问题", "网页、飞书、语音三个入口进同一个 Agent；提示注入和诈骗话术先过护栏，命中的消息只记录、不放权。", "对话测试台"],
      ["找到依据", "BM25 与向量检索各取候选，RRF 融合后按相关度阈值筛选；冲突的两份文档会先转人工。", "知识库 · 检索调试"],
      ["逐句校验", "回答拆成句子，数字、订单号和工具状态逐句对照证据；对不上的句子标红或改写，仍不行就转人工。", "链路追踪"],
      ["人工把关", "超过租户自动额度的退款暂停在审批队列，坐席看证据包、确认金额后才执行；顾客要人工时直接进接待台。", "审批队列 · 人工接待"],
      ["沉淀改进", "坐席的一次纠错生成失败归因、回归评测用例和知识补丁草稿，管理员发布后用回归评测验证。", "数据飞轮 · 评测中心"],
    ],
    play: "自动播放", pause: "暂停",
    consoleTitle: "控制台里有什么",
    consoleLead: "按角色显示：顾客只有在线客服，坐席和管理员看到三个工作区，每个工作区有自己的颜色。",
    sections: [
      ["服务运营", "运营看板、对话测试台、人工接待、审批队列、工单、退款对账"],
      ["知识与质量", "知识库（冲突、官方渠道、检索调试）、评测中心、数据飞轮"],
      ["运维审计", "每次运行的完整链路，管理员的操作审计"],
    ],
    signTitle: "选择演示账号",
    signLead: "从顾客、客服坐席或管理员的视角进入。所有企业、人员和订单都是虚构的。",
    faqTitle: "常见问题",
    faq: [
      ["演示里的数据是真的吗？", "不是。三个租户（Echo音乐、Leon优选、Iris云文档）和其中的人员、订单都是虚构的，页面上标有“演示环境 · 数据均为虚构”。"],
      ["回答来自真实模型吗？", "在线演示回放的是录制下来的回答，页面顶部写明来源：真实模型的录制或离线模型。离线模型的数字只说明流程正确，不代表模型效果。"],
      ["能在本地完整运行吗？", "可以。仓库里有 docker compose 和本地开发说明，默认用离线模型，不需要任何密钥；配置模型地址和 Key 后用真实模型。"],
    ],
    foot: "Leon Wang · 个人作品 · 所有企业、人员与订单均为虚构（SYNTHETIC）",
    source: "项目源码",
  },
  en: {
    nav: [["product", "Product"], ["how", "How it works"], ["sign-in", "Demo accounts"], ["faq", "FAQ"]],
    enter: "Open the console",
    sub: "Service workbench",
    eyebrow: "Multi-tenant · Evidence first · Human in the loop",
    h1: ["Every answer,", "backed by evidence"],
    lead: "LoopDesk puts the AI agent, business actions and human agents in one console: each sentence is checked against the knowledge base and order data, risky actions such as refunds wait for a person, and every correction becomes a knowledge patch and a regression case.",
    how: "How it works",
    skip: "Skip this page next time",
    signedIn: "Signed in as",
    switchAccount: "Switch account",
    cancel: "Cancel",
    signOut: "Sign out",
    chips: ["Sentence-level evidence check", "Human approval for large refunds", "Reproducible offline", "All data is fictional"],
    liveTitle: "One refund, from question to filing",
    liveTag: "Example · synthetic data",
    live: [
      ["Customer asks", "\"Refund my annual pass, SO20261015102\""],
      ["Retrieve rules", "2 documents, RRF fusion plus a relevance threshold"],
      ["Look up order", "Annual pass ¥198.00, unused, within the refund window"],
      ["Check sentences", "Numbers, order status and evidence compared, 100% covered"],
      ["Human approval", "Above the ¥20 auto limit; an agent approves after reading the evidence"],
      ["Refund filed", "Idempotency key stored before calling the payment system"],
    ],
    liveFoot: ["Replayed step by step in the console's trace view", "SYNTHETIC"],
    stats: [["3", "fictional tenants"], ["3", "role views"], ["8", "agent tools"], ["14", "end-to-end walkthroughs"]],
    howTitle: "How it works",
    howLead: "A customer message goes through five steps; each one can be seen, traced and corrected in the console.",
    steps: [
      ["Take the question", "Web, Feishu and voice reach the same agent; prompt injection and scam wording are screened first and never gain permissions.", "Chat test bench"],
      ["Find the evidence", "BM25 and vector search each propose candidates, fused with RRF and filtered by relevance; conflicting documents go to a person.", "Knowledge · retrieval debugger"],
      ["Check every sentence", "Numbers, order ids and tool states are compared sentence by sentence; unsupported ones are marked or rewritten, or the chat goes to a person.", "Traces"],
      ["A person decides", "Refunds above the tenant's auto limit wait in the approval queue until an agent reads the evidence and confirms the amount.", "Approvals · Handoffs"],
      ["Learn from fixes", "An agent's correction yields a failure label, a regression case and a knowledge patch draft, verified by the regression suite.", "Flywheel · Evals"],
    ],
    play: "Autoplay", pause: "Pause",
    consoleTitle: "What is in the console",
    consoleLead: "Role-aware: customers get the chat only; agents and admins get three workspaces, each with its own colour.",
    sections: [
      ["Service", "Dashboard, chat test bench, handoffs, approvals, tickets, refund reconciliation"],
      ["Knowledge & quality", "Knowledge base (conflicts, official channels, retrieval debugger), evals, flywheel"],
      ["Ops & audit", "The full trace of every run, the admin audit log"],
    ],
    signTitle: "Pick a demo account",
    signLead: "Enter as a customer, an agent or an admin. Every company, person and order is fictional. The console itself is in Chinese.",
    faqTitle: "FAQ",
    faq: [
      ["Is the demo data real?", "No. The three tenants (Echo音乐, Leon优选, Iris云文档) and their people and orders are fictional, and every page says so."],
      ["Do the answers come from a real model?", "The online demo replays recorded answers; the top of each page says whether they were recorded from a real model or the offline one. Offline numbers show the pipeline works, not model quality."],
      ["Can I run it locally?", "Yes. The repository has docker compose and local setup; the offline model needs no key, and a model URL and key switch to a real model."],
    ],
    foot: "Leon Wang · personal project · every company, person and order is fictional (SYNTHETIC)",
    source: "Source",
  },
} as const;

const LIVE_ICONS = [MessageSquareText, Search, ReceiptText, ShieldCheck, ClipboardCheck, BookOpenCheck];
const STEP_ICONS = [Headset, Search, ShieldCheck, ClipboardCheck, RefreshCcw];
const SECTION_ICONS = [Headset, BookOpen, Activity];
const SECTION_KEYS = ["service", "knowledge", "ops"];

function stored(key: string) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function store(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* storage unavailable: the choice lasts for this page only */
  }
}

const reduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const go = (id: string) => document.getElementById(id)?.scrollIntoView({ behavior: reduced() ? "auto" : "smooth", block: "start" });

/** The section nav, styled as the desk's queue tabs: the section in view carries the on-duty dot. */
function DeskNav({ items, label }: { items: readonly (readonly [string, string])[]; label: string }) {
  const [active, setActive] = useState(items[0][0]);
  useEffect(() => {
    const onScroll = () => {
      let current = items[0][0];
      for (const [id] of items) {
        const el = document.getElementById(id);
        if (el && el.getBoundingClientRect().top < 160) current = id;
      }
      setActive(current);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [items]);
  return (
    <nav className="desk-nav" aria-label={label}>
      {items.map(([id, text]) => (
        <button key={id} type="button" aria-current={active === id ? "true" : undefined}
          onClick={() => { setActive(id); go(id); }}>{text}</button>
      ))}
    </nav>
  );
}

function Stepper({ t }: { t: (typeof T)[Lang] }) {
  const [current, setCurrent] = useState(0);
  const [playing, setPlaying] = useState(() => !reduced());
  useEffect(() => {
    if (!playing) return;
    const timer = window.setInterval(() => { if (!document.hidden) setCurrent((c) => (c + 1) % t.steps.length); }, 3800);
    return () => window.clearInterval(timer);
  }, [playing, t.steps.length]);
  const [title, text, where] = t.steps[current];
  const Icon = STEP_ICONS[current];
  return (
    <div className="card how">
      <div className="how-tabs">
        <div className="how-steps" role="tablist" aria-label={t.howTitle}>
        {t.steps.map(([name], i) => (
          <button key={name} type="button" role="tab" aria-selected={i === current} aria-controls="how-panel" tabIndex={i === current ? 0 : -1}
            className={i === current ? "active" : ""} onClick={() => { setCurrent(i); setPlaying(false); }}
            onKeyDown={(e) => {
              if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
              const next = (i + (e.key === "ArrowRight" ? 1 : t.steps.length - 1)) % t.steps.length;
              setCurrent(next);
              setPlaying(false);
              (e.currentTarget.parentElement?.children[next] as HTMLElement | undefined)?.focus();
            }}>
            <span className="how-num">{String(i + 1).padStart(2, "0")}</span>{name}
            {i === current && playing && <i className="how-progress" key={`p${current}`} aria-hidden="true" />}
          </button>
        ))}
        </div>
        <button type="button" className="how-play" aria-pressed={playing} aria-label={playing ? t.pause : t.play}
          title={playing ? t.pause : t.play} onClick={() => setPlaying((p) => !p)}>
          {playing ? <Pause size={15} /> : <Play size={15} />}
        </button>
      </div>
      <div className="how-card" id="how-panel" role="tabpanel" aria-live={playing ? "off" : "polite"}>
        <span className="icon-tile lg" aria-hidden="true"><Icon size={24} /></span>
        <div>
          <h3>{title}</h3>
          <p>{text}</p>
          <span className="badge how-where">{where}</span>
        </div>
      </div>
    </div>
  );
}

/** Where an account's console starts: staff on the dashboard, customers in the chat. */
const homeOf = (user: User) => (isStaff(user) ? "/dashboard" : "/chat");

export default function IntroPage() {
  const [lang, setLang] = useState<Lang>(() => (stored(LANG_KEY) === "en" ? "en" : "zh"));
  const [skip, setSkip] = useState(() => stored(SKIP_INTRO_KEY) === "1");
  const t = T[lang];
  useEffect(() => {
    document.documentElement.lang = lang === "en" ? "en" : "zh-CN";
    return () => { document.documentElement.lang = "zh-CN"; };
  }, [lang]);
  const chooseLang = (l: Lang) => { setLang(l); store(LANG_KEY, l); };
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [switching, setSwitching] = useState(false);
  // A signed-in visitor goes straight to their console; others pick a demo account first.
  const enter = () => (user ? navigate(homeOf(user)) : go("sign-in"));
  // Signing in (or switching account, even to the same one) here opens that account's console, as the
  // sign-in page does: every sign-in yields a new user object.
  const seen = useRef(user);
  useEffect(() => {
    if (user && user !== seen.current) navigate(homeOf(user), { replace: true });
    seen.current = user;
    setSwitching(false);
  }, [user, navigate]);
  const icon = (Icon: (typeof LIVE_ICONS)[number], key?: string): ReactNode => <span className="icon-tile" data-section={key}><Icon size={18} /></span>;

  return (
    <div className="intro-page">
      <header className="intro-bar">
        <a className="brand" href="#product" onClick={(e) => { e.preventDefault(); go("product"); }}>
          <Logo size={28} /><span className="brand-name">LoopDesk</span><small className="brand-sub">{t.sub}</small>
        </a>
        <DeskNav items={t.nav} label={lang === "en" ? "Page sections" : "页面导航"} />
        <div className="intro-actions">
          <div className="segmented" role="group" aria-label="语言 / Language">
            <button type="button" className={lang === "zh" ? "active" : ""} aria-pressed={lang === "zh"} onClick={() => chooseLang("zh")}>中文</button>
            <button type="button" className={lang === "en" ? "active" : ""} aria-pressed={lang === "en"} onClick={() => chooseLang("en")}>EN</button>
          </div>
          <button type="button" className="btn btn-primary" onClick={enter}>{t.enter}<ArrowRight size={15} aria-hidden="true" /></button>
        </div>
      </header>

      <section className="hero" id="product">
        <div>
          <span className="intro-eyebrow">{t.eyebrow}</span>
          {/* The second line reads like an evidenced phrase in an answer: highlighted, with its citation. */}
          <h1>{t.h1[0]}<br /><span className="hl">{t.h1[1]}</span><sup className="hl-cite" aria-hidden="true">1</sup></h1>
          <p className="lead">{t.lead}</p>
          <div className="cta">
            <button type="button" className="btn btn-primary btn-lg" onClick={enter}>{t.enter}<ArrowRight size={16} aria-hidden="true" /></button>
            <button type="button" className="btn btn-lg" onClick={() => go("how")}><Play size={15} aria-hidden="true" />{t.how}</button>
          </div>
          {user && (
            <button type="button" className="resume" onClick={enter} aria-label={`${t.enter}：${t.signedIn} ${user.name}，${ROLE_TEXT[user.role]}，${user.tenant.name}`}>
              <small>{t.signedIn}</small>
              <b>{user.name} · {ROLE_TEXT[user.role]} · {user.tenant.name}</b>
              <ArrowRight size={18} aria-hidden="true" />
            </button>
          )}
          <label className="skip-intro">
            <input type="checkbox" checked={skip} onChange={(e) => { setSkip(e.target.checked); store(SKIP_INTRO_KEY, e.target.checked ? "1" : null); }} />
            {t.skip}
          </label>
          <div className="chips">{t.chips.map((c) => <span key={c}><ShieldCheck size={14} aria-hidden="true" />{c}</span>)}</div>
        </div>
        <section className="card live" aria-label={t.liveTitle}>
          <div className="card-head"><h2>{t.liveTitle}</h2><span className="badge badge-info">{t.liveTag}</span></div>
          <ol className="live-rows">
            {t.live.map(([name, sub], i) => (
              <li key={name} className="live-row" data-section={i === 4 ? "ops" : i < 2 ? "knowledge" : "service"}>
                <span className="step">{String(i + 1).padStart(2, "0")}</span>
                {icon(LIVE_ICONS[i])}
                <span className="live-text"><b>{name}</b><span>{sub}</span></span>
              </li>
            ))}
          </ol>
          <div className="live-foot"><span>{t.liveFoot[0]}</span><span className="mono">{t.liveFoot[1]}</span></div>
        </section>
      </section>

      <div className="intro-stats">
        {t.stats.map(([n, label]) => <div key={label}><b>{n}</b><span>{label}</span></div>)}
      </div>

      <section className="intro-section" id="how">
        <header><div><span className="intro-eyebrow">01</span><h2>{t.howTitle}</h2></div><p>{t.howLead}</p></header>
        <Stepper t={t} />
      </section>

      <section className="intro-section">
        <header><div><span className="intro-eyebrow">02</span><h2>{t.consoleTitle}</h2></div><p>{t.consoleLead}</p></header>
        <div className="intro-sections">
          {t.sections.map(([name, text], i) => {
            const Icon = SECTION_ICONS[i];
            return (
              <div key={name} className="card intro-area" data-section={SECTION_KEYS[i]}>
                <span className="icon-tile" aria-hidden="true"><Icon size={18} /></span>
                <h3>{name}</h3>
                <p>{text}</p>
              </div>
            );
          })}
        </div>
      </section>

      <section className="intro-section" id="sign-in">
        <header><div><span className="intro-eyebrow">03</span><h2>{t.signTitle}</h2></div><p>{t.signLead}</p></header>
        <div className="card intro-signin">
          {user && !switching ? (
            <section className="signed-in" aria-label={t.signedIn}>
              <span className="avatar me" aria-hidden="true">{user.name.slice(0, 1)}</span>
              <span className="signed-in-who">
                <small>{t.signedIn}</small>
                <b>{user.name}（{ROLE_TEXT[user.role]}）· {user.tenant.name}</b>
                <span className="faint">{user.email}</span>
              </span>
              <span className="signed-in-actions">
                <button type="button" className="btn btn-primary" onClick={enter}>{t.enter}<ArrowRight size={15} aria-hidden="true" /></button>
                <button type="button" className="btn" onClick={() => setSwitching(true)}>{t.switchAccount}</button>
                <button type="button" className="btn btn-text" onClick={logout}>{t.signOut}</button>
              </span>
            </section>
          ) : (
            <>
              {user && (
                <div className="switch-bar">
                  <span className="faint">{t.signedIn} {user.name}（{user.email}）</span>
                  <button type="button" className="btn btn-sm" onClick={() => setSwitching(false)}>{t.cancel}</button>
                </div>
              )}
              <LoginPage embedded />
            </>
          )}
        </div>
      </section>

      <section className="intro-section" id="faq">
        <header><div><span className="intro-eyebrow">04</span><h2>{t.faqTitle}</h2></div></header>
        <div className="faq">
          {t.faq.map(([q, a]) => (
            <details key={q} className="card"><summary>{q}</summary><p>{a}</p></details>
          ))}
        </div>
      </section>

      <footer className="intro-foot">
        <span>{t.foot}</span>
        <a href={REPO_URL} target="_blank" rel="noreferrer"><Github size={14} aria-hidden="true" /> {t.source}</a>
      </footer>
    </div>
  );
}
