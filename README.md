# EchoMind

作者 / 维护者：**YJn**

EchoMind 是面向客服与运营场景的多 Agent 智能客服项目，基于 FastAPI、Anthropic SDK、Redis 和 ChromaDB，实现从意图识别、知识检索、Agent 路由到记忆写回的对话流程，并提供工具调用追踪、在线监控和 LLM-as-Judge 评测接口。

本仓库是基于已有 EchoMind 项目的二次开发与维护版本。上游代码及第三方组件的版权归各自权利人所有；维护者署名不代表全部代码的原创归属。

## 核心能力

| 能力 | 实现说明 |
| --- | --- |
| 意图识别 | 融合 LLM 判断、向量相似度与关键词规则，提取意图、紧急程度和实体；向量分支支持本地字符 n-gram 兜底 |
| 多 Agent 编排 | 按请求路由到 General、Technical、Billing、Escalation 等 Agent，并支持协作与回复整合 |
| RAG 知识检索 | 基于 ChromaDB 管理知识，提供检索、文本添加及文件上传接口 |
| 分层记忆 | 使用 Redis 与 ChromaDB 管理工作记忆、情景记忆和用户画像 |
| 动态 Skills | 按业务场景注入处理规范，支持运行时重新加载 |
| 工具与观测 | 提供工具缓存、熔断、调用追踪、在线指标和路由降权 |
| 评测 | 提供意图识别与回复质量评测，使用 LLM-as-Judge 辅助评分 |

## 对话流程

```text
POST /chat
  → 读取会话记忆与用户画像
  → 意图识别、紧急程度判断、实体提取
  → 按意图检索知识库
  → Agent 路由与协作
  → Skills 注入、工具调用、回复生成
  → 写回记忆并记录监控指标
```

评测通过独立的 `/eval/run` 接口触发。

## 快速开始

### 1. 准备环境

- Docker 与 Docker Compose（使用 `docker compose` 命令）
- 可用的 Anthropic API Key，或兼容 Anthropic Messages 协议的服务
- 模型服务及镜像、构建依赖下载所需的网络连接

### 2. 配置环境变量

在项目根目录执行：

```bash
cp .env.example .env
```

编辑 `.env`，替换占位值：

```env
ANTHROPIC_API_KEY=your_api_key
ANTHROPIC_MODEL=your_available_model
REDIS_PASSWORD=replace_with_a_strong_password
```

`ANTHROPIC_MODEL` 请填写所用服务实际支持的模型名称。使用兼容服务时，额外设置其 Anthropic 协议端点：

```env
ANTHROPIC_BASE_URL=https://your-provider.example/anthropic
```

示例地址和密钥不能直接使用。其余容器连接与运行参数参见 [.env.example](.env.example)。`.env` 仅保留在本地，不提交到仓库。

### 3. 启动服务

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f echomind
```

首次构建会安装 Python 依赖并下载 ChromaDB 使用的向量模型，耗时取决于网络与本机环境。

### 4. 验证与体验

检查健康状态：

```bash
curl http://localhost:8000/health
```

发送一次对话请求（会调用配置的模型服务）：

```bash
curl -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"登录时提示 401，应该如何处理？","user_id":"demo-user"}'
```

响应包含 `response`、`conv_id`、意图、路由及工具使用等字段。后续请求传回同一个 `conv_id` 和 `user_id`，可继续该会话。知识检索效果取决于已导入的知识内容，可通过 Swagger 添加演示知识。

- Swagger：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- Nginx 代理入口：<http://localhost>

停止服务：

```bash
docker compose down
```

此命令保留数据卷和本地持久化目录。

## API 概览

| 接口 | 用途 |
| --- | --- |
| `GET /health` | 健康检查 |
| `POST /chat` | 对话与 Agent 编排 |
| `POST /search` | 知识检索 |
| `POST /knowledge/add` | 添加文本知识 |
| `POST /knowledge/upload` | 上传知识文件 |
| `GET /knowledge/stats` | 知识库统计 |
| `GET /skills` | 查看 Skills |
| `POST /skills/reload` | 重新加载 Skills |
| `GET /monitor` | 在线监控 |
| `GET /metrics` | Prometheus 指标 |
| `GET /trace/tool/{request_id}` | 查询单次请求的工具追踪 |
| `GET /trace/tools` | 查看近期工具追踪 |
| `POST /eval/run` | 运行评测 |

请求参数及响应结构以运行后的 Swagger 文档为准。对话和评测会消耗所配置模型服务的额度。

## 代码导航

```text
api/main.py                    FastAPI 入口、组件初始化与接口
agents/agent_orchestrator.py    Agent 路由、协作与回复整合
agents/tools.py                 Agent 工具定义与执行
core/intent_recognizer.py      意图识别与实体提取
core/skill_loader.py           Skills 加载与匹配
memory/conversation_memory.py  分层记忆管理
mcp/tool_manager.py            检索工具、缓存、熔断与重排
mcp/knowledge_base.py          ChromaDB 知识库
monitor/performance_monitor.py 在线监控
evaluation/evaluator.py       端到端评测
skills/                       本地业务处理规范（不随仓库提交）
tests/                        单元测试
config/                       Nginx 与 Prometheus 配置
```

建议先读 `api/main.py` 中的 `/chat`，再看意图识别、Agent 编排和工具管理。运行时的 `data/` 与 `logs/` 由本地服务使用，不纳入版本控制。

业务 Skills 需自行在 `skills/` 中配置；本仓库不附带默认规则文件。未配置时不会加载这些规则。

## 本地测试

使用 Python 3.12（与 Docker 镜像一致）：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install pytest
python -m pytest -q
```

单元测试使用模拟模型客户端，覆盖部分 Agent 编排、Skills、工具与评测逻辑；它们不能代替 Docker 全栈启动和真实模型调用验证。

## 部署与使用边界

默认 Compose 用于本地开发和演示，发布端口包括 API `8000`、ChromaDB `8001`、Redis `6379`、Prometheus `9090` 和 Nginx `80`。

API 尚未提供统一访问认证，跨域配置允许任意来源，Redis 配置存在默认密码回退。公网部署前需补充认证、访问控制和 TLS，限制数据库、监控及管理接口的访问，并设置独立的强密码。Nginx 的限流仅作用于经代理进入的请求。

模型输出、意图判断和自动评分可能存在误差；涉及退款、权限变更等业务操作时，需要接入真实业务系统的校验与审批规则。

不要将真实密钥、用户会话、画像、订单或日志提交到仓库。曾进入 Git 历史的真实凭据应撤销或轮换。

## 来源与许可

本仓库尚未附带 LICENSE。正式开源发布前需确认上游来源、代码及素材授权，再确定许可证并补充必要的来源说明。
