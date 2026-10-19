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

`docker-compose.yml` 会启动 PostgreSQL 16 和应用镜像（多阶段构建：Node 构建前端，Python 运行后端并托管静态文件，以非 root 用户运行）。上线前请务必修改 `LOOPDESK_JWT_SECRET`，并设置 `LOOPDESK_SEED_DEMO=0`。

## 环境变量

所有变量都以 `LOOPDESK_` 开头，完整示例见 [`.env.example`](../.env.example)。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./loopdesk.db` | 也支持 `postgresql+psycopg://...` |
| `JWT_SECRET` | 开发用默认值 | 生产环境必须修改，至少 32 字节 |
| `SEED_DEMO` / `SEED_DEMO_TRAFFIC` | `1` / `1` | 演示租户、账号和预置对话 |
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
| `REFUND_WINDOW_DAYS` / `REFUND_WINDOW_MAX_COUNT` / `REFUND_WINDOW_MAX_AMOUNT` | `30` / `3` / `200` | 累计退款风控：窗口天数内同一顾客的退款笔数或累计金额超过上限时，小额退款也转人工审核 |
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

收到消息后，服务会立即返回 200（飞书要求 3 秒内响应），然后在后台运行 Agent 并回复原消息。同一个 `event_id` 只处理一次。需要审批的退款处理完后，结果会主动推送到原群聊。

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
