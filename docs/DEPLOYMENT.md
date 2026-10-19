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
- `/api/ready`：就绪检查，确认数据库可连接并且表结构已经迁移到最新版本，否则返回 503。Dockerfile 中的 `HEALTHCHECK` 和 Kubernetes 的 readinessProbe 都应该指向它。
- 每个响应都带 `X-Request-ID`。用户报障时只要提供这个 ID，就可以在日志中找到对应的请求，在链路追踪中找到对应的 Agent 运行（trace 详情里会显示 request_id）。

## 数据保留

```bash
cd backend && LOOPDESK_DATABASE_URL=... .venv/bin/python -m app.ops.retention --days 90 --dry-run   # 先预览
cd backend && LOOPDESK_DATABASE_URL=... .venv/bin/python -m app.ops.retention --days 90
```

建议用 Kubernetes CronJob 或系统定时任务每天执行一次。超过保留期的链路明细（span）会被删除；运行记录的输入输出、对话正文会被替换为“已过保留期”的标记；token、成本、延迟、校验结论等指标，以及审批、退款、反馈这些审计和数据飞轮记录都会保留；仍在等待审批的运行不会被处理。

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
| `SEED_DEMO` / `SEED_DEMO_TRAFFIC` | `1` / `1` | 演示租户、账号和预置对话 |
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
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | — / `512` | 向量模型 |
| `EVIDENCE_MODE` | `annotate` | `annotate`：流式输出并标注；`strict`：缓冲输出，校验不通过就拦截 |
| `VERIFIER` | `lexical` | `lexical` / `llm` |
| `MIN_RELEVANCE` | `0.3` | 检索相关度阈值 |
| `MAX_AGENT_STEPS` | `6` | 单次运行最多调用模型的次数 |
| `RATE_LIMIT_PER_MINUTE` | `30` | 每个用户每分钟最多发起的对话请求数 |
| `LOGIN_ATTEMPTS_PER_EMAIL` / `LOGIN_ATTEMPTS_PER_IP` | `10` / `30` | 登录限流：同一账号（邮箱忽略大小写和首尾空格）、同一客户端 IP 每分钟最多尝试次数，两者同时生效 |
| `PROMPT_VERSION` | `v1` | 默认的系统提示词版本（见 `agent/prompts.py` 中的 `PROMPT_VERSIONS`） |
| `PROMPT_CANARY_VERSION` / `PROMPT_CANARY_PERCENT` | — / `0` | 灰度：按会话哈希把指定比例的对话稳定地分给候选版本；比例改回 0 即回滚 |
| `ANSWER_CACHE_TTL_HOURS` | `24` | 答案缓存的有效期，设为 0 即关闭；知识或配置变化时缓存会自动失效 |
| `REFUND_WINDOW_DAYS` / `REFUND_WINDOW_MAX_COUNT` / `REFUND_WINDOW_MAX_AMOUNT` | `30` / `3` / `200` | 累计退款风控：窗口天数内同一顾客的退款笔数或累计金额超过上限时，小额退款也转人工审核 |
| `TRACE_WRITES` | `auto` | 链路写入方式：`auto` 在 PostgreSQL 上批量写（运行结束、暂停审批、出错时一次写入，同次实测吞吐 +39%–47%），在 SQLite 上每个 span 完成即提交；也可以显式设为 `per_span` 或 `buffered`。批量模式下，运行中的链路要等运行结束才能看到 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_VERIFICATION_TOKEN` | — | 飞书机器人 |
| `WECOM_WEBHOOK_KEY` | — | 企业微信群机器人（审批提醒） |

## 接入国产大模型

| 厂商 | `LLM_PRESET` | Base URL | 模型 ID 从哪里获取 |
|---|---|---|---|
| 字节 · 豆包（火山方舟） | `doubao` | `https://ark.cn-beijing.volces.com/api/v3` | 方舟控制台的模型 ID 或推理接入点 ID |
| 腾讯 · 混元 | `hunyuan` | `https://api.hunyuan.cloud.tencent.com/v1` | 混元控制台的模型列表 |
| DeepSeek | `deepseek` | `https://api.deepseek.com/v1` | 官方文档 |
| 阿里 · 通义 | `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 百炼控制台 |

模型 ID 经常更新，所以代码里没有写死，请从控制台复制最新值。快模型和强模型可以用同一家厂商的不同规格，也可以只配置一个（`MODEL_FAST` 默认等于 `MODEL_STRONG`）。

## 飞书机器人

1. 在飞书开放平台创建企业自建应用，开启机器人能力，并申请 `im:message` 相关权限。
2. 事件订阅的请求地址填写 `https://<你的域名>/api/channels/feishu/<租户 slug>/events`，订阅 `im.message.receive_v1`。
3. **关闭 Encrypt Key**（当前版本还不支持 AES 解密），把 Verification Token 填到 `LOOPDESK_FEISHU_VERIFICATION_TOKEN`。
4. 把 App ID 和 App Secret 填到环境变量中。

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
