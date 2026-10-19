# LoopDesk · 证据优先的企业客服 Agent 平台

[![CI](https://github.com/leon1111-wgl/loopdesk/actions/workflows/ci.yml/badge.svg)](https://github.com/leon1111-wgl/loopdesk/actions/workflows/ci.yml)

**每一句回答都有出处，每一个高风险动作都要人复核，每一次人工纠错都会变成回归测试。**

LoopDesk 是一个面向企业客服场景的 Agent 平台：知识库问答（RAG）、订单 / 物流 / 退款 / 工单工具调用、人工审批、逐句证据校验、数据飞轮和回归评测，都放在同一个可观测的系统里。

作者：**Leon Wang** · 个人项目 · 仓库中的企业、订单与客户数据全部为虚构（SYNTHETIC）。

![对话测试台：证据校验标出编造内容](docs/assets/chat-verifier.png)

> 上图：管理员打开「演示模式」后，离线模型在回答末尾编造了一句“90 天无理由退货，2 小时到账”。校验器发现证据里没有 90 和 2 这两个数字，把这句话标红，并把整条回答判为“证据冲突”。

---

## 它解决什么问题

企业愿意把客服交给大模型，但上线后通常会卡在三件事上：

| 痛点 | 后果 | LoopDesk 的做法 |
|---|---|---|
| 幻觉：退款时效、费用、额度答错 | 直接引发客诉 | **证据绑定**：回答按句拆开，引用必须存在，数字必须与证据一致；严格模式下不达标的回答不会发出 |
| 越权与资损：Agent 自己把钱退出去 | 资金风险，无法追责 | **可复核动作**：工具按风险分级，超出租户额度的退款进入审批队列，并附上 Agent 的证据包；审批人不能是申请人本人 |
| 修了又犯：bad case 靠人记 | 质量不可控，改 Prompt 怕出问题 | **数据飞轮 + 回归门禁**：一次纠错同时生成知识补丁和评测用例；评测集进 CI，通过率低于阈值就阻止合并 |

## 功能一览

<table>
<tr>
<td width="50%"><img src="docs/assets/approvals.png" alt="审批队列"><br><b>审批队列</b>：金额、原因和 Agent 提交的证据包，通过后重新校验订单再执行</td>
<td width="50%"><img src="docs/assets/trace.png" alt="链路追踪"><br><b>链路追踪</b>：护栏、路由、模型、工具、审批、校验，每一步的输入输出和耗时</td>
</tr>
<tr>
<td><img src="docs/assets/evals.png" alt="评测中心"><br><b>评测中心</b>：按轨迹打分（工具、审批、检索命中、证据校验），并和上一次运行做对比</td>
<td><img src="docs/assets/retrieval.png" alt="检索调试器"><br><b>检索调试器</b>：BM25 名次、向量名次、RRF 分数、相关度阈值、注入样本屏蔽</td>
</tr>
<tr>
<td><img src="docs/assets/dashboard.png" alt="运营看板"><br><b>运营看板</b>：自助解决率、转人工率、证据覆盖率、缓存命中率、Prompt 版本对比，支持导出 CSV</td>
<td><img src="docs/assets/dashboard-dark.png" alt="深色模式"><br><b>深色模式</b>：跟随系统，也可以手动切换</td>
</tr>
<tr>
<td colspan="2"><img src="docs/assets/mobile-approvals.png" alt="手机端审批" width="260"> <img src="docs/assets/mobile-nav.png" alt="手机端导航" width="260"><br><b>手机端</b>：窄屏下用抽屉式导航，审批卡片改为单列，主管在手机上也能处理审批</td>
</tr>
</table>

**Agent 与 RAG**
- 自研 Agent 循环（不依赖框架）：流式输出、多步工具调用、最大步数保护、模型降级重试。
- 混合检索：jieba 分词 + BM25 与向量检索，用 RRF 融合；IDF 加权的“词覆盖率”作为绝对相关度，最关键的词缺失时 Agent 会拒答，而不是答非所问。
- 查询归一化：先把异体字和常见错别字统一（“到帐”改为“到账”），再用小型领域同义词表把口语短语扩展成知识库里的标准说法（“专票”扩展为“增值税专用发票”，“多掏钱”扩展为“额外支付 运费”），只追加、不替换。新增的 5 条口语评测用例在修复前全部检索失败，修复后全部通过。相关度门槛（词覆盖率）则改用**替换后**的标准说法计算：“能寄到国外吗”里的“寄到”“国外”不在知识库词表里，IDF 最高，留在分母里只会把分数拖到阈值以下，所以门槛按“配送 海外”打分；召回仍用追加后的完整查询。另一批 5 条口语用例（“能开发票吗”“退货运费谁出”等）由此从拒答或答错变为正确。
- 按标题切分文档并保留章节路径；入库时扫描提示注入，命中的片段不会进入模型上下文。
- 知识冲突检测：文档入库或补丁发布时，比对其他文档中同一主题、带单位的数字（天、元、积分……），发现“1-2 个工作日”和“1-3 个工作日”这类矛盾就记录下来并提醒管理员，而不是等顾客拿到错误答案。
- 多轮追问改写：识别“那运费谁出？”这类追问，结合上一问改写成完整问题再交给 Agent（真实模型下用快模型改写，离线时用规则改写）。对话中保存用户的原话，链路中记录改写结果。对应的多轮评测用例在修复前失败，修复后通过。
- 权限感知检索：内部文档只对员工可见。
- 知识版本感知的答案缓存：重复的 FAQ 直接复用已经通过证据校验的回答，命中时消耗 0 token。缓存键包含知识库版本签名、提示词和模型配置指纹以及可见范围，所以知识一更新缓存就自动失效，内部答案也不会经缓存泄漏给顾客。

**安全与治理**
- 多租户隔离：跨租户访问一律返回 404，避免通过 ID 探测数据。
- RBAC：管理员 / 客服坐席 / 顾客三种角色。
- 工具层鉴权：身份取自登录态，而不是模型生成的参数，所以 Prompt 注入也查不到别人的订单。
- 审批通过后，执行前再校验一次订单状态（防 TOCTOU）；同一订单不能重复提交待审批的退款申请。
- 售后时效由工具层判断，不靠模型自觉：签收 7 天内按规则自动处理；7 到 15 天只有质量问题可以退，工具看不到顾客上传的照片，所以一律转人工核实；超过 15 天直接拒绝，不进审批。期限按顾客提出申请的时间计算，审批排队不会让顾客因此超期。两个期限是租户配置（`PATCH /api/tenant`）。
- 转人工幂等：同一对话已经在人工队列中时，再次要求转人工不会新建工单，返回原来的排队位次；位次只计算排在自己前面的人，后来者不会把顾客往后挤。
- 累计退款风控（防“薅羊毛”）：同一顾客 30 天内已退款 3 笔或累计超过 200 元时，即使单笔低于自动审批额度也要人工审核。统计按顾客而不是按操作人，坐席代为申请也绕不过去。
- 手机号、身份证、银行卡、邮箱在进入模型、写入数据库和链路之前脱敏，覆盖人们实际的输入习惯（`138-1234-5678`、`+86 138 1234 5678`、每 4 位一组的卡号）；接口带限流。
- 浏览器加固：所有响应都带 `nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy`，以及只允许同源资源的 CSP（打包后的控制台已在该 CSP 下逐页验证，没有报错）。
- 数据保留：`python -m app.ops.retention --days 90` 删除超过保留期的链路明细，并抹除对话正文和运行记录中的文本；指标、审批和退款等审计记录保留；还在等待审批的运行不会被处理。
- 删除权：管理员可以调用 `DELETE /api/admin/users/{id}/personal-data` 抹除某个用户的个人数据，包括对话、运行记录和针对其消息的反馈，并将账号匿名化、使其无法登录；订单、退款、审批单作为财务和审计记录保留，但审批证据中引用的对话内容会被清除。

**质量闭环**
- 数据飞轮：纠错 → 失败归因（知识缺失 / 检索未命中 / 无依据 / 工具错误）→ 候选评测用例 + 知识补丁草稿 → 管理员发布 → 重建索引并激活用例。
- 回归评测在**隔离沙箱库**中运行：知识库和租户配置取自当前环境，订单数据取自固定夹具，所以演示中的真实退款不会把用例变红。
- Prompt 版本化：提示词按版本登记，评测 CLI 支持 `--compare v1,v2` 做离线 A/B（任何用例回归即失败）；线上按会话哈希稳定分流做灰度，看板按版本对比转人工率、证据覆盖率和 token。离线 A/B 中精简版 v2 的 token 减少 31%，并且没有回归；它对回答质量的影响需要用真实模型验证。

**集成**
- 模型：豆包（火山方舟）、混元、DeepSeek、通义、OpenAI 统一走 OpenAI 兼容协议；Claude 使用官方 SDK；没有 Key 时使用离线确定性模型。
- 渠道：飞书机器人事件回调（challenge 校验、事件去重、3 秒内先回 200，后台处理失败时记录日志并回复兜底话术）、企业微信群机器人审批提醒。
- MCP：只读工具通过 MCP 暴露，Claude Desktop、Cursor 等客户端可以直接接入。
- 运维：Prometheus `/metrics`、Docker、docker-compose（PostgreSQL）、GitHub Actions（SQLite + PostgreSQL 双库测试、评测门禁、镜像构建）。
- 数据库迁移：Alembic 管理表结构版本，启动时自动升级；对旧版本用 `create_all` 建的库会补齐缺失的表并标记为最新版本。CI 在 SQLite 和 PostgreSQL 上都会校验“模型定义与迁移脚本完全一致”。
- 可排障：每个请求都带 `X-Request-ID`（可以沿用网关传入的 ID），它会写进 JSON 结构化日志和 Agent 运行记录；发生 500 错误时只返回 request_id，不暴露内部信息；`/api/ready` 检查数据库和迁移版本，供 Kubernetes 和 Docker 的健康检查使用，`/api/health` 用作存活检查。
- 数据导出：对话记录、反馈、审批可按天数导出为 CSV（带 UTF-8 BOM，Excel 打开不乱码；对以 `=` `+` `-` `@` 开头的单元格做转义，防止 CSV 公式注入；按租户隔离，导出的文本是已脱敏的版本）。

## 架构

```mermaid
flowchart LR
    subgraph Channels
      W[Web 控制台<br/>React + TS]
      F[飞书机器人]
      M[MCP 客户端]
    end
    W -- SSE --> API
    F -- 事件回调 --> API
    M -- stdio --> MCP[MCP Server]
    API[FastAPI<br/>鉴权 / 多租户 / 限流] --> RT
    subgraph RT[Agent Runtime]
      G[输入护栏<br/>PII 脱敏 / 注入检测] --> R[模型路由<br/>快 / 强 / 降级]
      R --> L{LLM}
      L -- tool call --> P[策略检查<br/>预校验 / 审批阈值]
      P -- 需要人工 --> AQ[(审批队列)]
      P -- 放行 --> T[业务工具]
      T --> L
      L -- 最终回答 --> V[证据校验器]
    end
    T --> KB[(混合检索<br/>BM25 + 向量 + RRF)]
    T --> BIZ[(订单 / 工单 / 退款)]
    AQ -- 人工决定 --> RT
    RT --> TR[(Trace / Span)]
    V --> FB[反馈与纠错] --> FW[数据飞轮]
    FW --> KB
    FW --> EV[(评测用例)] --> CI[回归评测 / CI 门禁]
```

设计细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 快速开始

**环境要求**：Python 3.11+、Node 20+（推荐 pnpm）。不需要任何 API Key。

```bash
# 后端
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

```bash
# 前端（另开一个终端）
cd frontend
pnpm install
pnpm dev            # http://localhost:5173 ，/api 会代理到 8000
```

首次启动会自动创建两个演示租户、知识库、订单和几段演示对话。登录页列出了所有演示账号，点击即可进入。

**Docker**（PostgreSQL + 单镜像）：

```bash
docker compose up --build    # http://localhost:8000
```

**测试与评测**：

```bash
cd backend
pytest -q                                          # 全部测试
python -m app.evals.cli --fresh --gate 0.95        # 回归评测，低于 95% 退出码为 1
```

## 接入真实模型

在 `.env` 中设置（完整说明见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)）：

```bash
LOOPDESK_LLM_PROVIDER=openai_compat
LOOPDESK_LLM_PRESET=doubao              # doubao | hunyuan | deepseek | qwen | openai
LOOPDESK_LLM_API_KEY=...
LOOPDESK_LLM_MODEL_STRONG=<控制台里的模型或接入点 ID>
LOOPDESK_LLM_MODEL_FAST=<更便宜的模型>
LOOPDESK_VERIFIER=llm                   # 真实模型会改写措辞，用 LLM 判定句子是否有依据（数字校验始终开启）
```

## 评测结果

| 评测集 | 用例数 | 通过率 | 证据覆盖率 | p50 / p95 | 说明 |
|---|---|---|---|---|---|
| Leon优选 default | 37 | 100% | 100% | 3ms / 7ms | 覆盖 RAG、口语与错别字、多轮追问、拒答、RBAC、订单工具、审批、售后时效、累计风控、注入攻击 |
| Iris云 default | 3 | 100% | 100% | — | 含跨租户隔离用例 |

> **请如实理解这些数字**：它们来自**离线确定性模型和虚构数据**，只能证明“流程按设计工作”（工具调对了、审批触发了、注入被挡住了），**不代表真实模型的回答质量**。换成真实模型后请重新运行评测，方法见 [docs/EVALUATION.md](docs/EVALUATION.md)。

**答案缓存基准**（`python -m app.evals.cache_bench`）：16 个 FAQ 问题各问两遍，第二遍 100% 命中缓存，答案与第一遍一致，token 从 14,845 降到 0。离线模型几乎瞬间返回，所以这里不报延迟收益；接入真实模型后，省下的是真实的首字延迟和 token 账单。

**性能基线**（`python -m app.evals.load_bench`，Apple M1 Max、单进程、SQLite、离线模型）：开启 SQLite WAL 后，并发 1 时约 143 RPS（p95 9ms），并发 16 时 p99 从 1.37s 降到 0.75s。瓶颈分析和下一步计划见 [docs/PERFORMANCE.md](docs/PERFORMANCE.md)。测的是模型以外的平台开销，不代表接入真实模型后的线上表现。

## 目录结构

```
backend/
  app/
    agent/        Agent 循环、工具注册与业务工具、证据校验器、链路追踪
    rag/          解析与切分、Embedding、BM25 + 向量 + RRF 混合检索、入库
    llm/          OpenAI 兼容 / Claude / 离线模型适配器、模型路由
    guardrails/   PII 脱敏、提示注入检测
    flywheel/     反馈归因、知识补丁、评测用例生成
    evals/        沙箱评测运行器、CLI 门禁
    channels/     飞书、企业微信
    api/          REST + SSE 接口
    mcp_server.py MCP 服务
  evals/          评测集（JSONL）
  seed_data/      演示知识库（Markdown）
  tests/          pytest
frontend/src/     React 控制台（对话、审批、知识库、链路、飞轮、评测、看板）
docs/             架构、评测、部署、性能文档
```

## 技术栈

Python 3.12 · FastAPI · SQLAlchemy 2 · SQLite / PostgreSQL · jieba · httpx · PyJWT · pytest · React 19 · TypeScript · Vite · Docker · GitHub Actions · MCP

## 路线图

- [ ] 向量库切换到 pgvector / Milvus，支持十万级片段
- [ ] 异步化 Agent 循环（async httpx + async SQLAlchemy），并支持多副本部署（限流和事件去重改用 Redis）
- [ ] 评测集扩展到 200 条以上，接入真实模型做 A/B 对比
- [ ] 多 Agent：把售后和技术支持拆成两个专职 Agent，由路由 Agent 分派
- [ ] 飞书加密事件（AES）与企业微信应用消息回调

## 声明

本项目为个人学习与求职作品。仓库中的企业名称、订单、客户和对话全部为虚构数据，不代表任何真实企业的政策。
