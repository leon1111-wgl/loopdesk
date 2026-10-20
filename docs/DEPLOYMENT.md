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
- **按时间判断，不在启动时把“运行中”的一律结束**：多 worker、多实例部署时，别的进程正在执行的运行也是“运行中”。每条运行用一条条件更新结束（`WHERE status = 'running'` 且仍超过时限），多个实例同时执行，每条运行也只处理一次、只留一条提示。
- **应用启动时也执行一次**，清理早已超过时限的遗留，例如宕机较久后重启，或者单实例部署没配定时任务（docker-compose 演示环境就没有）。导致这次重启的那次崩溃留下的运行，启动时还没到时限，要靠定时任务。

## 数据保留

```bash
cd backend && LOOPDESK_DATABASE_URL=... .venv/bin/python -m app.ops.retention --days 90 --dry-run   # 先预览
cd backend && LOOPDESK_DATABASE_URL=... .venv/bin/python -m app.ops.retention --days 90
```

建议用 Kubernetes CronJob 或系统定时任务每天执行一次。超过保留期的链路明细（span）会被删除；运行记录的输入输出、对话正文，以及超过保留期之前已关闭的工单的标题、描述和处理结果，会被替换为“已过保留期”的标记（未关闭的工单不动，还有人要处理）；token、成本、延迟、校验结论等指标，以及审批、退款、反馈这些审计和数据飞轮记录都会保留；仍在等待审批的运行不会被处理。

## 数据库迁移

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
| `EMBEDDING_PROVIDER` | `hashing` | `hashing`（离线）/ `openai_compat` |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | — / `512` | 向量模型；远程模型返回的维度必须等于 `EMBEDDING_DIM`，否则建索引时报错 |
| `EVIDENCE_MODE` | `annotate` | `annotate`：流式输出并标注；`strict`：缓冲输出，校验不通过就拦截 |
| `VERIFIER` | `lexical` | `lexical` / `llm` |
| `MIN_RELEVANCE` | `0.3` | 检索相关度阈值（IDF 加权词覆盖率） |
| `MIN_COSINE` | 未设置 | 向量这一路的门槛。接入语义向量模型后用 `python -m app.evals.gate_probe --calibrate` 标定再设；未设置时，语义模型的余弦不能单独放行（只看覆盖率），离线哈希向量沿用 `MIN_RELEVANCE` |
| `MAX_AGENT_STEPS` | `6` | 单次运行最多调用模型的次数 |
| `RATE_LIMIT_PER_MINUTE` | `30` | 每个用户每分钟最多发起的对话请求数 |
| `LOGIN_ATTEMPTS_PER_EMAIL` / `LOGIN_ATTEMPTS_PER_IP` | `10` / `30` | 登录限流：同一账号（邮箱忽略大小写和首尾空格）、同一客户端 IP 每分钟最多尝试次数，两者同时生效 |
| `RATE_LIMIT_BACKEND` | `auto` | 登录限流的计数放在哪里：`auto`（PostgreSQL 上存数据库，所有 worker 共享；SQLite 上存进程内存）/ `database` / `memory`。在 SQLite 上跑多个 worker 时设为 `database` |
| `PROMPT_VERSION` | `v1` | 默认的系统提示词版本（见 `agent/prompts.py` 中的 `PROMPT_VERSIONS`） |
| `PROMPT_CANARY_VERSION` / `PROMPT_CANARY_PERCENT` | — / `0` | 灰度：按会话哈希把指定比例的对话稳定地分给候选版本；比例改回 0 即回滚 |
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

MCP 只暴露只读工具（知识检索、订单查询、物流查询），并且沿用 `LOOPDESK_MCP_USER` 对应账号的权限。退款和工单这类写操作不对外暴露。
