# 部署与配置

## 本地运行

```bash
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend && pnpm install && pnpm dev     # http://localhost:5173
```

只想启动一个进程：先执行 `pnpm build`，FastAPI 发现 `frontend/dist` 后会同时提供控制台页面，访问 http://localhost:8000 即可。

重置演示数据：`python -m app.seed --reset`。

## Docker

```bash
cp .env.example .env    # 按需修改
docker compose up --build
```

`docker-compose.yml` 会启动 PostgreSQL 16 和应用镜像（多阶段构建：Node 构建前端，Python 运行后端并托管静态文件，以非 root 用户运行）。上线前请务必修改 `LOOPDESK_JWT_SECRET`，并设置 `LOOPDESK_SEED_DEMO=0`。生产配置（`LOOPDESK_SEED_DEMO=0`）下，如果 JWT 密钥仍是仓库里公开的默认值（代码默认值、`.env.example`、`docker-compose.yml` 中的占位符）或短于 32 字节，服务会直接拒绝启动；演示模式下只在启动日志里打印警告。

放在 Nginx 等反向代理后面时，用 `uvicorn app.main:app --proxy-headers --forwarded-allow-ips=<代理 IP>` 启动，让登录限流看到真实的客户端 IP；不要让应用直接信任任意来源的 `X-Forwarded-For`，否则攻击者可以伪造 IP 绕过限流。

## 健康检查与排障

- `/api/health`：存活检查，只确认进程在运行。
- `/api/ready`：就绪检查，确认数据库可连接、表结构已经迁移到最新版本、检索索引已在后台预热完成，否则返回 503。预热让重启后的第一位顾客不必等索引构建（5 万片段约 9 秒）；`LOOPDESK_PREWARM_INDEX=0` 可以关闭，关闭后第一次检索时构建。Dockerfile 中的 `HEALTHCHECK` 和 Kubernetes 的 readinessProbe 都应该指向它。
- 模型故障不影响就绪：模型挂了，系统照样能转人工、引用政策原文回答（见 ARCHITECTURE“模型故障”），所以 `/api/ready` 不检查模型。故障要靠告警发现：看板顶部会提示最近 15 分钟出错的运行数和原因（模型不可用、请求被拒、超时、熔断中、进程中断）；Prometheus 的 `loopdesk_run_errors_total` 统计所有出错的运行（不管顾客最后拿到的是降级回复还是人工），可以这样告警：

  ```yaml
  - alert: LoopDeskModelErrors
    expr: increase(loopdesk_run_errors_total[5m]) > 5
    for: 2m
    annotations:
      summary: "LoopDesk 运行持续出错，顾客正在收到降级回复或被转人工"
  ```

  “请求被拒”（4xx）多半是配置或参数问题（密钥、模型名、`stream_options`），重试和等待都没用，要直接修配置。
- 每个响应都带 `X-Request-ID`。用户报障时只要提供这个 ID，就可以在日志中找到对应的请求，在链路追踪中找到对应的 Agent 运行（trace 详情里会显示 request_id）。

## 审计记录的外部锚点

每条审计记录提交后，会写一行 `"msg": "audit anchor"` 的结构化日志（带租户、编号、签名）。请把应用日志（`LOOPDESK_LOG_FORMAT=json`）送到能写数据库的人改不了的日志平台，并定期对照：

```bash
cd backend && LOOPDESK_DATABASE_URL=... .venv/bin/python -m app.ops.audit_anchor_check exported-app.log   # 不一致时退出码为 1
```

`/api/audit/verify` 能发现被改写和从中间删掉的记录；连同计数器一起删掉最新几条，只有和日志里的锚点对照才能发现。

## 升级注意

- **迁移 0013（知识版本号）之后，不要让旧版本的实例继续写知识库**：旧版本改片段时不会更新租户的知识版本号，新版本的实例会一直使用旧的检索索引和答案缓存，直到下一次版本号变化。升级时先停掉旧实例（或者升级期间暂停知识库编辑和补丁发布），再启动新版本。
- **迁移 0022（退款对账索引）**：退款表加 `(tenant_id, status, created_at)` 索引，供退款对账页面和每个员工页面轮询的计数使用；只建索引，不改数据。退款很多的库在 PostgreSQL 上建索引会锁写，可以在低峰执行，或先手工 `CREATE INDEX CONCURRENTLY ix_refunds_tenant_status_created ON refunds (tenant_id, status, created_at)` 再升级（迁移会因索引已存在而失败，此时 `alembic stamp 0022`）。
- **迁移 0021（退款记录）**：退款表不再外键关联本地 `orders` 表，补上顾客（从订单复制）、幂等键、支付系统退款号、错误、发送次数、关联工单和更新时间。升级前的退款没有幂等键，不受“一单一笔有效退款”的唯一索引约束（它们的订单状态本来就挡住了第二笔）。升级后把 `python -m app.ops.refund_reconcile` 加进定时任务（见“接入你自己的订单和支付系统”）。退款记录里有本地 `orders` 表之外的订单时，迁移拒绝降级到 0020（显式检查，SQLite 上也一样）。
- **迁移 0015（审计签名）之后**，升级前已有的审计记录没有签名，完整性校验会报“未签名”。升级后运行一次 `python -m app.ops.audit_backfill` 补签；之后再出现的未签名记录说明有人直接写过审计表，不要补签。

## 渠道消息补偿

```bash
cd backend && LOOPDESK_DATABASE_URL=... .venv/bin/python -m app.ops.channel_retry   # 每分钟一次
```

飞书事件先写进数据库再确认。处理它的进程如果在确认之后崩溃，飞书不会重发，这个任务会重新认领并回答：没开始处理的事件等 30 秒，处理中的事件等两倍运行时限（至少 5 分钟）才认为原进程已失联；同一事件最多尝试 3 次，之后给顾客回复兜底话术。每个事件都用条件更新认领，多个实例同时运行也不会重复处理。

## 中断运行的收尾

```bash
cd backend && LOOPDESK_DATABASE_URL=... .venv/bin/python -m app.ops.stale_runs   # 每分钟一次，和 channel_retry 一起调度
```

进程在运行途中被杀掉（kill -9、OOM、容器被回收、不等请求结束的发布）时，进程里的异常处理一行也执行不到，运行记录会一直停在“运行中”：指标里一直算着它，链路不会结束，刚办完退款的顾客也收不到回答。这个任务把“运行中”超过时限的运行记为失败（`interrupted: the process ended before the run finished`，写上结束时间），像正常结束的运行一样导出 OpenTelemetry 链路；顾客还在等这次运行时（对话里最新的一条是顾客的提问，或者是这次运行留下的“已提交人工审核”），在对话里留一条系统提示：处理中断，涉及退款的以订单状态为准，可以重新发送或转人工。飞书会话不留（渠道补偿任务会重新回答那条消息），转人工中的对话、顾客已经重新提问的对话也不留。

- **时限 = 运行时限 + 5 秒 + 3 × 模型超时 + 60 秒**（`LOOPDESK_RUN_DEADLINE_SECONDS`、`LOOPDESK_LLM_TIMEOUT`），默认 45 + 5 + 180 + 60 = 290 秒。运行时限只约束 Agent 循环；循环前的追问改写有自己的 5 秒上限；开启 LLM 校验（`LOOPDESK_VERIFIER=llm`）时，循环后的校验是一次不受时限约束的模型调用，最多尝试 3 次；60 秒留给工具、提交和锁等待。时限短了会把还在运行的记录判为失败，长了只是收尾晚一些，所以取上界，没开 LLM 校验也算上它。定时任务要用和应用相同的这几个环境变量，时限由它们推算。`LOOPDESK_RUN_DEADLINE_SECONDS=0` 时，循环部分按“最大步数 × 两档模型 × 3 次尝试”估算。
- **审批恢复中的运行从审批决定的时间算起**：恢复时运行重新变成“运行中”，运行记录本身却可能是几天前创建的。等待审批的运行不会被处理。
- **审批恢复途中被杀掉时，审批决定在同一个事务里收尾**：这时审批单已经是“已通过”或“已驳回”，没人能再审。退款没执行（没有带这张审批单号的退款单）的，审批单退回待审批，清掉审批人、意见和决定时间，在原审批人名下记一条审计 `approval.reopen`（错误类型 `interrupted`），链路里记一个 `approval/reopened`；顾客那边仍显示“已提交人工审核”，不留中断提示，重新审批后照常回复，退款只会执行一次。退款已执行的，保留决定，把退款结果（退款单号和金额）作为回复写进对话（已提交、支付系统还没回音的退款也算已执行：可能已经退了，回复“已提交、正在核实”，由对账任务用原来的幂等键继续）；已驳回的，保留驳回，把审核意见写进对话。这两种不管对话后来有没有新消息都会写，飞书会话同时推送一次（失败只记日志，对话里已经有了）。退回待审批不会再发企业微信提醒：请让审批人留意审批队列，或者按输出里的 `reopened` 告警，例如 `{"failed": 1, "notified": 0, "answered": 0, "reopened": 1}`（`notified` 是留了中断提示的，`answered` 是写了退款结果或审核意见的）。
- **按时间判断，不在启动时把“运行中”的一律结束**：多 worker、多实例部署时，别的进程正在执行的运行也是“运行中”。每条运行用一条条件更新结束（`WHERE status = 'running'` 且仍超过时限），提示、回复和退回的审批单都和它在同一个事务里提交，多个实例同时执行，每条运行也只处理一次、只留一条提示、审批单只退回一次。收尾以时限为前提：进程如果只是被冻结（容器暂停、虚拟机挂起），过了时限才恢复，它之后的写入照样生效，所以时限宁长勿短。
- **应用启动时也执行一次**，清理早已超过时限的遗留，例如宕机较久后重启，或者单实例部署没配定时任务（docker-compose 演示环境就没有）。导致这次重启的那次崩溃留下的运行，启动时还没到时限，要靠定时任务。

## 数据保留

```bash
cd backend && LOOPDESK_DATABASE_URL=... .venv/bin/python -m app.ops.retention --days 90 --dry-run   # 先预览
cd backend && LOOPDESK_DATABASE_URL=... .venv/bin/python -m app.ops.retention --days 90
```

建议用 Kubernetes CronJob 或系统定时任务每天执行一次。超过保留期的链路明细（span）会被删除；运行记录的输入输出、对话正文，以及超过保留期之前已关闭的工单的标题、描述和处理结果，会被替换为“已过保留期”的标记（未关闭的工单不动，还有人要处理）；token、成本、延迟、校验结论等指标，以及审批、退款、反馈这些审计和数据飞轮记录都会保留；仍在等待审批的运行不会被处理。

## 数据库迁移

**配置官方渠道**：上线前在知识库页面的“官方渠道”里填写自己的域名（子域名自动包含）和官方客服账号（微信号、QQ 号或群号），也可以 `PATCH /api/tenant` 传 `official_domains`、`official_contacts`（整组替换，记审计）。没有填写时，回答里的任何链接和客服账号都不会发给顾客（转人工），知识库里带着“填写、领取、核验”等要求的站外链接会被扣下复核。页面会列出知识库里出现过的其他域名，确认是自己或可信合作方（快递、海关、银行）的再加入。修改之后自动按新的官方渠道重扫这个租户的知识。**从旧版本升级**：迁移 0020 之后官方渠道是空的，回答里的任何链接和客服账号都不会发出，带着“填写、领取、核验”等要求的站外链接片段会在启动重扫时被扣下；升级后第一件事是在知识库页面设置官方渠道（页面列出知识里出现过的域名，一键加入）。

**知识库安全扫描规则更新后**：每个实例启动时按当前规则重扫一遍已入库的知识（`app/ops/rescan_knowledge.py`，只是正则，不调用模型；失败不影响启动），新命中的片段扣下等管理员复核，按新规则不再命中的放回检索，管理员放行过的片段只要没有新的命中原因就保持放行。也可以手动执行：

```bash
cd backend && LOOPDESK_DATABASE_URL=... .venv/bin/python -m app.ops.rescan_knowledge
```

表结构由 Alembic 管理（`backend/migrations`）。应用启动时会自动执行 `alembic upgrade head`（`LOOPDESK_AUTO_MIGRATE=1`，默认开启）。

- 修改模型后生成迁移：`cd backend && LOOPDESK_DATABASE_URL=... .venv/bin/alembic revision --autogenerate -m "说明"`。生成后先人工检查，再和代码一起提交。CI 中的 `tests/test_migrations.py` 会校验模型与迁移完全一致，漏写迁移会直接失败。
- 由旧版本用 `create_all` 创建的数据库（没有 `alembic_version` 表）：启动时会补齐缺失的表，并标记为最新版本。
- 多副本部署时，建议把迁移放进发布流程中的一次性任务（例如 Kubernetes 的 Job 或 initContainer），并对应用设置 `LOOPDESK_AUTO_MIGRATE=0`，避免多个副本同时执行迁移。

## 环境变量

所有变量都以 `LOOPDESK_` 开头，完整示例见 [`.env.example`](../.env.example)。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./loopdesk.db` | 也支持 `postgresql+psycopg://...` |
| `JWT_SECRET` | 开发用默认值 | 生产环境必须修改，至少 32 字节；`SEED_DEMO=0` 时使用公开默认值或过短的密钥会拒绝启动 |
| `SEED_DEMO` / `SEED_DEMO_TRAFFIC` | `1` / 离线模型 `1`、真实模型 `0` | 演示租户、账号和预置对话。预置对话会调用十几次模型，所以接真实模型时默认关闭；显式打开也在后台执行，不会拖住启动 |
| `LOG_FORMAT` / `LOG_LEVEL` | `json` / `INFO` | 结构化日志：每行一个 JSON，包含 request_id、tenant_id、user_id，可直接接入 ELK 或 Loki；本地调试可设为 `text` |
| `AUTO_MIGRATE` | `1` | 启动时执行 Alembic 迁移；多副本部署时建议设为 0，改由发布任务执行 |
| `LLM_PROVIDER` | `mock` | `mock` / `openai_compat` / `anthropic` |
| `LLM_PRESET` | — | `doubao` / `hunyuan` / `deepseek` / `qwen` / `openai`，自动填写 Base URL |
| `LLM_BASE_URL` / `LLM_API_KEY` | — | 自定义 OpenAI 兼容端点 |
| `LLM_MODEL_STRONG` / `LLM_MODEL_FAST` | `mock-strong` / `mock-fast` | 路由使用的两档模型 |
| `LLM_STREAM_USAGE` | `1` | 流式请求时要求返回 usage；厂商不支持时设为 `0`，系统会改用估算值 |
| `ANTHROPIC_EFFORT` | `low` | Claude 的 effort 档位 |
| `PRICES` | 示例价格 | `{"model": [输入, 输出]}`，单位：元 / 百万 token |
| `OUTPUT_GUARD` | `rules` | `rules` / `llm`：`llm` 时，规则放过、带诈骗线索的模型回答再由快模型判断，回答不再流式输出；模型失败时以规则结果为准 |
| `EMBEDDING_PROVIDER` | `hashing` | `hashing`（离线）/ `openai_compat` |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | — / `512` | 向量模型；远程模型返回的维度必须等于 `EMBEDDING_DIM`，否则建索引时报错 |
| `EVIDENCE_MODE` | `annotate` | `annotate`：流式输出并标注；`strict`：缓冲输出，校验不通过就拦截 |
| `VERIFIER` | `lexical` | `lexical` / `llm`：`llm` 时由快模型判定措辞是否有依据（数字仍按规则核对）；带思考过程的模型（如 DeepSeek V4.1 Flash）会让 p95 从约 13 秒升到约 40 秒，门禁没有提高，见 EVALUATION |
| `MIN_RELEVANCE` | `0.3` | 检索相关度阈值（IDF 加权词覆盖率） |
| `MIN_COSINE` | 未设置 | 向量这一路的门槛。接入语义向量模型后用 `python -m app.evals.gate_probe --calibrate` 标定再设；未设置时，语义模型的余弦不能单独放行（只看覆盖率），离线哈希向量沿用 `MIN_RELEVANCE` |
| `MAX_AGENT_STEPS` | `6` | 单次运行最多调用模型的次数 |
| `RATE_LIMIT_PER_MINUTE` | `30` | 每个用户每分钟最多发起的对话请求数 |
| `LOGIN_ATTEMPTS_PER_EMAIL` / `LOGIN_ATTEMPTS_PER_IP` | `10` / `30` | 登录限流：同一账号（邮箱忽略大小写和首尾空格）、同一客户端 IP 每分钟最多尝试次数，两者同时生效 |
| `RATE_LIMIT_BACKEND` | `auto` | 登录限流的计数放在哪里：`auto`（PostgreSQL 上存数据库，所有 worker 共享；SQLite 上存进程内存）/ `database` / `memory`。在 SQLite 上跑多个 worker 时设为 `database` |
| `PROMPT_VERSION` | `1.02` | 默认的系统提示词版本（见 `agent/prompts.py` 中的 `PROMPT_VERSIONS`），写作 1.00、1.02、2.00；旧的写法 v1、v2 仍然有效 |
| `PROMPT_CANARY_VERSION` / `PROMPT_CANARY_PERCENT` | — / `0` | 灰度：按会话哈希把指定比例的对话稳定地分给候选版本（例如 `2.00`）；比例改回 0 即回滚 |
| `ANSWER_CACHE_TTL_HOURS` | `24` | 答案缓存的有效期，设为 0 即关闭；知识或配置变化时缓存会自动失效 |
| `REFUND_WINDOW_DAYS` / `REFUND_WINDOW_MAX_COUNT` / `REFUND_WINDOW_MAX_AMOUNT` | `30` / `3` / `200` | 累计退款风控：窗口天数内同一顾客的退款笔数或累计金额超过上限时，小额退款也转人工审核 |
| `RUN_DEADLINE_SECONDS` | `45` | 整次运行的时间预算（所有步骤、两档模型、所有重试加起来），也是模型挂起时顾客最长的等待时间。每一步开始前检查；模型调用的超时被压到剩余时间，过了期限不再重试。超时后转人工（建工单、告知排队位次），链路里记录 `run_deadline`。设为 0 关闭。中断运行的收尾时限也由它推算（见上文） |
| `LLM_BREAKER_FAILURES` | `3` | 每个模型的熔断阈值：连续失败这么多次就打开，`LLM_BREAKER_COOLDOWN_SECONDS`（默认 60）秒内不再调用这个模型，顾客直接得到降级回复（引用政策原文或转人工）；之后放一次试探调用，成功才关闭。中间有一次成功就重新计数。设为 0 关闭。熔断状态在每个进程内 |
| `OTLP_ENDPOINT` / `OTLP_SERVICE_NAME` | — / `loopdesk` | 设置后，每次运行结束时以 OpenTelemetry trace 导出到这个 OTLP/HTTP 地址（例如 `http://otel-collector:4318/v1/traces`），按 GenAI 语义约定命名；只导出元数据，不含问题、回答和工具参数 |
| `AUDIT_KEY` | 演示模式下由 `JWT_SECRET` 派生 | 审计记录的 HMAC 签名密钥。生产配置（`SEED_DEMO=0`）下必须单独设置、至少 32 字节、不能和 JWT 密钥相同，否则拒绝启动：从 JWT 密钥派生的话，轮换 JWT 密钥会让所有旧记录校验失败。请和数据库分开保管：能写数据库的人不应同时拿到它。更换密钥后旧记录会校验失败（暂不支持轮换） |
| `TRACE_WRITES` | `auto` | 链路写入方式：`auto` 在 PostgreSQL 上批量写（运行结束、暂停审批、出错时一次写入，同次实测吞吐 +39%–47%），在 SQLite 上每个 span 完成即提交；也可以显式设为 `per_span` 或 `buffered`。批量模式下，运行中的链路要等运行结束才能看到 |
| `FEISHU_APPS` | — | 飞书机器人，每个租户一个应用：`{"<租户 slug>": {"app_id", "app_secret", "verification_token"}}` |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_VERIFICATION_TOKEN` + `FEISHU_TENANT` | — | 只接一个租户时的旧写法，必须用 `FEISHU_TENANT` 指明属于哪个租户 |
| `WECOM_WEBHOOK_KEY` | — | 企业微信群机器人（审批提醒） |
| `COMMERCE_APIS` | — | 每个租户自己的订单和支付系统（HTTP）：`{"<租户 slug>": {"base_url", "token", "timeout", "max_attempts", "signing_secret"}}`，见“接入你自己的订单和支付系统”；没配置的租户用演示表 |

## 接入你自己的订单和支付系统

LoopDesk 不记订单和资金的账。Agent 的三个订单类工具（查订单、查物流、退款）通过两个网关访问外部系统（`backend/app/integrations/`）：

- `OrderGateway`：查订单、列出某位顾客的订单、查物流；
- `RefundGateway`：发起退款（带幂等键）、查询退款状态。

默认实现是演示用的本地表（`orders`）。在 `LOOPDESK_COMMERCE_APIS` 里给某个租户配置 HTTP 接口后，这个租户的订单和退款都走它自己的系统；没配置的租户照旧用演示表。评测沙箱、启动时的演示流量和缓存基准始终使用演示数据，不会访问你的系统。权限判断（顾客只能看自己的订单）、退款规则（单笔额度、售后时效、累计风控、人工审批）和退款记录都留在 LoopDesk，不管订单在哪里。

### 配置

```bash
LOOPDESK_COMMERCE_APIS='{"leon": {"base_url": "https://oms.example.com/loopdesk", "token": "<长随机串>", "timeout": 5, "max_attempts": 3, "signing_secret": "<可选>"}}'
```

| 字段 | 默认值 | 说明 |
|---|---|---|
| `base_url` | 必填 | 必须是 https（只有 localhost 可以用 http），不跟随重定向（重定向会把令牌带到别处） |
| `token` | 必填 | 每个请求带 `Authorization: Bearer <token>`；不出现在日志、链路和配置的打印里 |
| `timeout` | `5` | 一次尝试里每个阶段的超时（秒）：建立连接最多 3 秒，发送、等待响应各自不超过它 |
| `max_attempts` | `3` | 只对幂等请求生效：查询，以及带幂等键的退款（1–5）。`timeout × max_attempts` 不能超过 20 秒：顾客在一次最长 45 秒的运行里等这些调用，一次退款要调用两次 |
| `signing_secret` | 不签名 | 设置后每个请求带 `X-LoopDesk-Timestamp` 和 `X-LoopDesk-Signature: v1=<hex>`，签名是 HMAC-SHA256(密钥, 时间戳 `\n` 方法 `\n` 路径（含 base_url 里的路径和查询串）`\n` 幂等键（没有就是空串）`\n` 请求体)；每次重试重新签名。请求头里只有幂等键被签名，其他头靠 TLS 和令牌保护。对方应拒绝时间戳和当前时间相差超过 5 分钟的请求 |

每个租户各配各的，一个租户的令牌不会出现在另一个租户的请求里。字段写错、用了明文 http、缺少令牌、超时乘次数超过 20 秒时服务启动直接报错。

### 接口约定

所有请求都带 `X-LoopDesk-Tenant: <租户 slug>`，处理顾客消息时还带 `X-Request-Id`（和 LoopDesk 的访问日志、链路对得上）。订单号以大写发送和比对，时间用 ISO 8601（不带时区的按 UTC），**金额一律是以分为单位的整数**（和微信支付一致），不用浮点数的元。

| 请求 | 成功 | 其他 |
|---|---|---|
| `GET /orders/{order_id}` | 200，订单对象 | 404：没有这个订单 |
| `GET /orders?limit=20`，顾客放在请求头 `X-LoopDesk-Customer` | 200，`{"orders": [订单对象, …]}`，新的在前 | 顾客标识不放进 URL，以免进访问日志 |
| `GET /orders/{order_id}/shipment` | 200，`{"carrier", "tracking_no", "events": [{"time", "desc"}]}`，事件从早到晚 | 404：还没发货 |
| `POST /refunds`，请求头 `Idempotency-Key` | 200 / 201，`{"refund_id", "status"}` | 409 / 422，`{"code", "message"}`，见下文 |
| `GET /refunds/{refund_id}` | 200，`{"refund_id", "status"}` | |

订单对象：`{"order_id", "customer_ref", "status", "amount_fen", "items": [{"name", "qty", "price_fen"}], "paid_at", "delivered_at", "region"}`。`status` 用 `paid` / `shipped` / `delivered` / `refunding` / `refunded`，其他状态原样显示、不能在线退款；`delivered_at` 决定售后时效；`customer_ref` 是顾客的登录邮箱（LoopDesk 的账号就是邮箱；你们按手机号或会员号识别顾客的话，在实现这套接口的服务里做映射）。

退款请求体：`{"order_id", "amount_fen", "reason", "approval_id", "loopdesk_refund_id"}`，只取自退款记录里不会再变的字段（不含顾客的账号信息：两次发送之间顾客改了邮箱或被执行删除权，同一个键也不会带着不同的请求体）。`status` 取 `processing`（已受理、未完成）、`succeeded` 或 `failed`。

**拒绝只认这几个码**：`order_not_refundable`、`amount_exceeds_refundable`、`order_not_found`、`refund_declined`（风控等原因拒绝）。带着这些码的 409 / 422 表示没有付款：退款记录改为失败、订单释放，顾客收到对应的中文（`refund_declined` 和其他情况一律说“支付系统没有受理这笔退款，请转人工客服处理”）；`message` 只进链路，不给顾客看（里面可能有内部规则或商户号）。**其他任何 409 / 422**（例如同一个键的第一个请求还没处理完时的 `idempotency_key_in_use`、“稍后再试”）都不算拒绝：退款可能已经存在，记录保持“待确认”，之后用同一个键再发。

**幂等是硬性要求**：同一个 `Idempotency-Key` 再次到达时，必须返回第一次的结果，不能再退一次（第一次还在处理时可以返回 409 `idempotency_key_in_use`）；同一个键带着不同的请求体，应返回 422。键至少保留 7 天（LoopDesk 只在 24 小时内自动重发）。LoopDesk 在第一次发送之前就把键写进退款记录，之后每一次重发（网络重试、审批重试、对账任务）都用同一个键、同一个请求体。键是随机生成的，不是审批单号或退款记录的序号：数据库重建、或几套环境共用一个支付沙箱时，序号会重复，对方会把新的退款当成旧的重放。

### LoopDesk 这一侧保证什么

- **只重试幂等请求**：查询和带幂等键的退款，在超时、连接失败、408 / 425 / 429 / 5xx 时重试，间隔 0.2 秒、0.4 秒（或按 `Retry-After`，最多等 2 秒）；不带键的写请求只发一次。401 / 403 / 400 / 404 不重试，记 ERROR 日志（多半是配置问题）。过了这次运行的时限（45 秒）不再开始新的尝试，剩下的交给对账任务。
- **一次工具调用只读一次订单**：退款的预校验、审批策略和执行看到的是同一份订单（以前要读三次）；人工审批之后执行时重新读取。
- **先记账、再付款**：退款先写进 LoopDesk 的退款记录（`pending`，带幂等键）并提交，之后才请求支付系统。同一订单同时只能有一笔有效退款（数据库部分唯一索引），并发的第二个请求直接被拒绝；按顾客加锁、在锁内重新统计累计风控，和以前一样。
- **一个订单在线只退一次**：部分退款也算这一次；之后再申请，顾客会被告知“该订单已退款（退款单 #N），每个订单只能在线申请一次退款，如需再退请转人工客服”，还在确认中的说“正在处理”。
- **没有回音不等于失败**：超时、5xx 重试用尽、不在拒绝码里的 409 / 422 时，退款可能已经退了。记录保持 `pending`，订单继续被占着，顾客收到“已提交，支付系统暂未返回结果，请勿重复申请”，不会被告知“已受理”或“几个工作日到账”（证据校验也把这种情况下的到账承诺判为与工具结果矛盾）。
- **明确拒绝才释放**：带合约拒绝码的 409 / 422 时记录改为 `failed`（不计入累计风控），订单释放。
- **审批不会白花**：执行人工审批通过的退款时，如果订单系统不可用、退款还没提交，这次执行算失败：审批单退回待审批，审批人当场收到 503“已退回待处理，可以重新审批”，而不是审批单停在“已通过”、退款却没提交。退款已经提交、只是支付系统没回音的，决定保留，由对账任务继续。审批列表接口里，通过的退款审批带着退款记录的状态（`refund`：状态、尝试次数、关联工单）。
- **对方的回复不合约定，按故障处理**：字段缺失、类型不对、金额不是整数分、订单号对不上、列出了别的顾客的订单，都当作接口故障（链路里写明哪里不对），不会当成数据给顾客看。查订单遇到故障时顾客收到“订单系统暂时无法访问，请稍后再试或转人工客服”。带换行或非 ASCII 字符、没法放进请求头的值不发送，按故障处理。
- **链路里看得到每次外部调用**：记在对应工具 span 的 `integration` 字段里，包括系统、调用、状态码、尝试次数、耗时、拒绝码或错误原因；不记请求头（里面有令牌）。

### 对账任务

```bash
cd backend && LOOPDESK_DATABASE_URL=... .venv/bin/python -m app.ops.refund_reconcile   # 每 5 分钟一次
```

- `pending` 超过 2 分钟的退款，先用一条条件更新“租下”（同时运行的两个任务只有一个会发），再用原来的幂等键和请求体重新发送，直到支付系统给出结果：已经退过的返回原结果，没退过的退一次，拒绝的释放订单。**只在提交后 24 小时内自动重发**：过了对方保留幂等键的期限，重发就成了一个新请求，部分退款可能被再退一次。
- 配置了 HTTP 接口的租户，30 天内 `processing` 的退款用 `GET /refunds/{refund_id}` 查询，直到成功或失败。
- **需要人处理的变成工单**：一小时还确认不了的退款（例如支付系统一直返回 400、401、404），以及受理之后又失败的退款（顾客之前被告知会退回），各开一张紧急工单，出现在员工的工单工作台，挂在顾客提出退款的那段对话上（处理结果会发给顾客）。退款还没有结果时，工单被关掉会再开一张。
- **人工确认结果**：管理员在支付系统后台核实后，调用 `POST /api/refunds/{refund_id}/settle`，`{"outcome": "succeeded" 或 "failed", "external_id": 支付系统的退款号（可选）, "note": 核实情况}`。只能确认待确认或处理中的退款，而且只能确认一次（条件更新，并发的第二次返回 409）；记入操作审计（“退款确认”类）。`failed` 释放订单，顾客可以重新申请，所以**只有对账任务已经不再处理的退款才能记为失败**（提交超过 24 小时的待确认、没有支付系统退款号或超过 30 天的处理中）：之前对账任务还会用同一个键重发、或支付系统还可能处理完已受理的那笔，提前记失败会让顾客再申请时退两次，接口返回 409 并说明原因。`succeeded`（支付后台查到已退款）随时可以记。
- **控制台的“退款对账”页面**（`/refunds`，员工可见，`GET /api/refunds?status=open|pending|processing|succeeded|failed|all`，本租户最多 200 笔，新的在前）：默认的“未结清”列出支付系统还没有结果的退款（各租户的待确认和停在 `claiming` 的；接了 HTTP 支付系统的租户再加处理中），需要人工的排在最前面，不会被新的挤出 200 笔之外。每行有已等待时长、发送次数、最近错误、关联工单和“下一步”（自动重发中 / 对账任务在查询 / 需要人工确认 / 需要排查 / 演示订单表），按对账任务的规则在服务端算出（有测试拿对账任务的实际行为核对）。导航上的数字来自 `GET /api/refunds/summary`（两个计数：未结清、需要人工），不是列表。管理员在这里点“确认结果”，等同于调用上面的 settle 接口：列表和对话框都显示在支付后台查找用的幂等键（坐席也看得到，方便先去核实），核实说明必填，对账任务还在处理的退款只能记为已退款。停在 `claiming` 的退款不能确认（它没有提交给支付系统），先按对账任务的 ERROR 日志排查。演示订单表不动钱，退款一直是处理中，不算未结清，也不能在页面上确认。确认结果不会通知顾客。
- 某一笔处理出错（程序缺陷、数据库错误）只记 ERROR 日志并计数，不影响其他退款。
- 输出例如 `{"confirmed": 1, "refused": 0, "still_pending": 0, "escalated": 0, "succeeded": 2, "failed": 0, "processing": 5, "stuck": 0, "errored": 0}`。`stuck` 不为 0 说明有退款记录停在只该存在于事务内部的 `claiming` 状态（程序缺陷），`errored` 不为 0 说明某一笔处理时出错，日志里都有 ERROR。

### 目前的限制

- 顾客按登录邮箱识别。LoopDesk 里没有账号的顾客，坐席不能通过 Agent 为其退款（累计风控按 LoopDesk 账号统计），请在订单系统里处理。
- 累计风控只统计经 LoopDesk 发起的退款，商家后台直接退的不算在内。
- 一个订单在线只能退一次（和演示表的规则一致），部分退款之后再退要转人工。
- 对账任务确认结果后不会主动通知顾客；顾客再问时，查到的是订单系统里的状态。需要人处理的退款会变成工单，处理结果经工单发回对话。
- 签名不覆盖除幂等键以外的请求头，也没有防重放的随机数；依靠 TLS、令牌和时间戳窗口。

### 自测

`backend/tests/fake_commerce.py` 是按这份约定写的进程内假服务（经 `httpx.MockTransport` 调用，不走网络），`backend/tests/test_integrations.py` 覆盖查单、越权、物流、自动退款、审批退款、回复丢失后的重发、同一个键还在处理时的 409、无回音、拒绝、订单系统故障（包括执行审批时）、累计风控、对账、升级成工单和多实例对账。接入自己的系统时，可以照着它写契约测试。

## 接入国产大模型

| 厂商 | `LLM_PRESET` | Base URL | 模型 ID 从哪里获取 |
|---|---|---|---|
| 字节 · 豆包（火山方舟） | `doubao` | `https://ark.cn-beijing.volces.com/api/v3` | 方舟控制台的模型 ID 或推理接入点 ID |
| 腾讯 · 混元 | `hunyuan` | `https://api.hunyuan.cloud.tencent.com/v1` | 混元控制台的模型列表 |
| DeepSeek | `deepseek` | `https://api.deepseek.com/v1` | 官方文档 |
| 阿里 · 通义 | `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 百炼控制台 |

上线前先跑一次模型自检：

```bash
python -m app.ops.llm_check
```

它给快、强两档模型各发一个最小请求，成功显示耗时；失败时给出原因和处理建议：401/403 是密钥或权限，404 是模型名或地址，400/422 是参数不被支持（例如 `stream_options`，可设 `LOOPDESK_LLM_STREAM_USAGE=0`），5xx 和 429 是对方不可用或限流，超时是网络或地址。任何一档失败时退出码为 1，可以放进部署流水线。服务本身启动和就绪都不依赖模型（模型不可用时会转人工或引用政策原文），所以自检是单独的一步。

模型 ID 经常更新，所以代码里没有写死，请从控制台复制最新值。快模型和强模型可以用同一家厂商的不同规格，也可以只配置一个（`MODEL_FAST` 默认等于 `MODEL_STRONG`）。

## 语音通道

默认关闭：没有语音引擎时 `/api/voice/config` 返回 `enabled: false` 和原因，对话页不显示语音按钮。开启需要语音模型（CPU 可以跑，GPU 更快）：

```bash
cd backend
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu   # 没有 CUDA 的机器
pip install -r requirements-voice.txt                                            # FunASR、faster-whisper、Silero、Kokoro
```

| 变量 | 默认 | 说明 |
|---|---|---|
| `LOOPDESK_VOICE_ASR` | `off` | `paraformer`（流式 + SeACo 带热词整句重解码，推荐）、`sensevoice`（非流式，最快，不支持热词）、`whisper`（large-v3-turbo，热词放进提示） |
| `LOOPDESK_VOICE_TTS` | `off` | `cosyvoice`（经 sidecar，流式）、`kokoro`（小模型，句级）、`say`（macOS 自带，基线） |
| `LOOPDESK_VOICE_VAD` | `energy` | `energy`（无模型）或 `silero` |
| `LOOPDESK_VOICE_DEVICE` | `cpu` | FunASR 用的设备：`cpu` / `cuda` |
| `LOOPDESK_VOICE_END_SILENCE_MS` | `600` | 连续对话时，尾部静音多久算一句说完（取值见 EVALUATION 的端点检测扫描） |
| `LOOPDESK_VOICE_BARGE_IN_MS` | `240` | 播报中顾客说话持续多久才算打断 |
| `LOOPDESK_VOICE_READBACK_THRESHOLD` | `0.6` | 订单号、金额的置信度低于它先复述 |
| `LOOPDESK_VOICE_HOTWORDS` | `1` | 用租户知识库里的领域词和商品名做热词 |
| `LOOPDESK_VOICE_PUNC` | `0` | 给不出标点的引擎（Paraformer）的整句结果加标点（FunASR ct-punc，`iic/punc_ct-transformer_zh-cn-common-vad_realtime-vocab272727`，约 280 MB）；中间结果不加；模型加载失败时照常不带标点。效果和耗时见 EVALUATION 的语音评测 |
| `LOOPDESK_VOICE_COSYVOICE_URL` | `http://127.0.0.1:50000` | CosyVoice sidecar 地址 |
| `LOOPDESK_VOICE_TTS_VOICE` | — | 音色：Kokoro 的音色名（如 `zf_001`）、`say` 的声音名 |

模型在第一次建立语音连接时加载（几秒到几十秒），之后全进程共享。

**CosyVoice 2 单独运行**：它固定了 torch 2.3、numpy 1.26、transformers 4.51，和后端放不进同一个环境。按 `.github/workflows/voice-eval.yml` 里 `synth-cosyvoice` 的步骤建一个独立环境，下载 `FunAudioLLM/CosyVoice2-0.5B`，然后：

```bash
python backend/sidecars/cosyvoice_server.py --cosyvoice /opt/CosyVoice --model /models/CosyVoice2-0.5B --port 50000
```

它一次只合成一句；并发要靠多开实例或 GPU（CosyVoice 自带 vLLM / TensorRT 路径）。

**反向代理**：语音走 WebSocket（`/api/voice/ws`），Nginx 要转发升级头，并把读超时放长（一次语音会话可能持续几分钟）：

```nginx
location /api/voice/ws {
    proxy_pass http://loopdesk;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 600s;
}
```

浏览器只在 HTTPS（或 localhost）下给麦克风权限。应用的安全头已允许本站使用麦克风（`Permissions-Policy: microphone=(self)`），摄像头和定位仍关闭。

**隐私**：转写在显示、存储和送模型之前先脱敏，原始转写和音频都不落库；如果要保存录音做数据迭代，先拿到顾客同意，并单独设定保留期限。

**测试引擎**：`LOOPDESK_VOICE_ASR=scripted`（按 `LOOPDESK_VOICE_SCRIPTED_TEXT` 用 `|` 分隔的文字依次返回）和 `LOOPDESK_VOICE_TTS=tone`（播提示音）只在演示模式下可用，端到端测试用它们走完整条语音链路。

## 飞书机器人

1. 在飞书开放平台创建企业自建应用，开启机器人能力，并申请 `im:message` 相关权限。
2. 事件订阅的请求地址填写 `https://<你的域名>/api/channels/feishu/<租户 slug>/events`，订阅 `im.message.receive_v1`。
3. **关闭 Encrypt Key**（当前版本还不支持 AES 解密）。
4. 把这个租户的 App ID、App Secret 和 Verification Token 填进 `LOOPDESK_FEISHU_APPS`，例如 `{"leon": {"app_id": "cli_xxx", "app_secret": "...", "verification_token": "..."}}`。

**每个租户一个飞书应用**。事件只按该租户自己的 Verification Token 校验，schema 2.0 事件里的 `app_id` 也必须是该租户的应用，所以用 Leon优选 的 token 往 `/feishu/iris/events` 发事件会返回 403，不会在 Iris云文档 下建出用户和会话；回复和审批结果也用该租户自己的应用发出（access token 按应用缓存）。没有配置应用的租户，回调在非演示环境返回 503。两个租户填同一个 app_id、或者旧的单应用变量没有写 `LOOPDESK_FEISHU_TENANT` 时，服务启动直接报错，而不是悄悄让一个凭证对所有租户生效。请求体不是 JSON 对象时返回 400，字段格式不对的事件按“忽略”处理，不会返回 500 让飞书反复重发。

收到消息后，服务会立即返回 200（飞书要求 3 秒内响应），然后在后台运行 Agent 并回复原消息。同一个 `event_id` 只处理一次。需要审批的退款处理完后，结果会主动推送到原群聊。后台任务里的异常不会再被静默吞掉：会以 ERROR 级别记录到 `app.channels.feishu` 日志（含 message_id 和堆栈），并给用户回复“智能客服暂时繁忙”的兜底话术。

## 企业微信审批提醒

在群里添加“群机器人”，把 Webhook 地址中 `key=` 后面的值填到 `LOOPDESK_WECOM_WEBHOOK_KEY`。每产生一张审批单，群里会收到一条 Markdown 提醒。未配置时只在 trace 中记录（dry run）。

## MCP

```bash
cd backend
LOOPDESK_MCP_USER=agent@leon.demo .venv/bin/python -m app.mcp_server
```

Claude Desktop 配置示例：

```json
{
  "mcpServers": {
    "loopdesk": {
      "command": "/path/to/loopdesk/backend/.venv/bin/python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/path/to/loopdesk/backend",
      "env": { "LOOPDESK_MCP_USER": "agent@leon.demo" }
    }
  }
}
```

MCP 只暴露只读工具（知识检索、订单查询、按顾客列出最近的订单、物流查询），并且沿用 `LOOPDESK_MCP_USER` 对应账号的权限（员工按邮箱查顾客的订单，顾客身份只能看自己的）；订单来自这个账号所在租户配置的订单系统。退款和工单这类写操作不对外暴露。
