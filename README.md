# MyFlow

本地 agent 工作台：后端是 FastAPI 网关（LangGraph Platform 兼容），前端是 Next.js 聊天 UI。浏览器只打同源 `/api/*`，开发时由 Next rewrite 转到网关。

| 模块 | 目录 | 端口 | 说明 |
|---|---|---|---|
| 网关 | [`backend/`](backend/README.md) | `127.0.0.1:8001` | threads / runs / SSE / 上传 |
| 前端 | [`frontend/`](frontend/README.md) | `127.0.0.1:3000` | 工作区聊天 / 知识库 |
| 评测 | [`evals/`](evals/README.md) | — | 数据集构建 / 黑盒评测 / Langfuse 实验 |

更细的启动约束、配置和排障写在各自目录的 README 里。知识库（MinerU + Qwen3-Embedding-8B +
Milvus）默认关闭，配置、安装可选依赖和 API 见 [`backend/README.md`](backend/README.md#知识库mineru--qwen3-embedding-8b--milvus)。

MCP 服务（每个用户自己添加的 MCP server）也默认关闭：在 `/workspace/mcp-servers` 添加，
配置是 `mcp_servers` 与 `tool_search`，server header 的 Fernet 密钥来自
`MYFLOW_MCP_SERVER_KEY`（**不要**复用 `AUTH_JWT_SECRET`）。模型只看到
`tool_search` / `tool_execute` 两个内建工具，目录落在 `mcp_tools` + Milvus collection
`myflow_mcp_tools`。旧的外部网关路径已删除：旧表 `contextforge_tools` 与旧 collection
`myflow_contextforge_tools` **故意丢弃**，新目录由每个 server 的 `tools/list` 重建。

日常操作优先用仓库根的 [`Makefile`](Makefile)，`make help` 会列出全部目标。下面命令默认都在
**仓库根**执行。没有 `make` 时，各节也保留等价的原始命令。

## 前置

- Python **3.12+**、[uv](https://docs.astral.sh/uv/)
- Node **20.9+**、[pnpm](https://pnpm.io/)
- Docker Engine / Docker Desktop + Compose v2（Docker 部署时需要）
- GNU Make（推荐；本机或 Git Bash 均可）
- 仓库根有 `config.yaml`（可从 `config.example.yaml` 复制）和 `.env`（可从 `.env.example` 复制）

`.env` 至少要能展开 `config.yaml` 里的 `$DEEPSEEK_API_KEY`。`MYFLOW_CONFIG_PATH` 建议设成仓库根的 `config.yaml`
（相对路径跟着进程 CWD）；`make dev-backend` 会自动设置，Docker 网关使用容器内的 `/app/config.yaml`。

`make init` 会从模板补齐 `.env`、`config.yaml`、`frontend/.env.local` 和 `evals/.env`，**已有文件不会被覆盖**。

## 快速操作

| 操作 | 命令 |
|---|---|
| 补齐本地配置 | `make init` |
| 安装本地依赖 | `make install`（全部 optional extras：`make install-all`） |
| 本机启动网关 | `make dev-backend` |
| 本机启动前端 | `make dev-frontend` |
| 构建并启动全部容器 | `make up`（别名 `make deploy`） |
| 查看状态 / 探活 | `make ps` / `make health` |
| 重建并重启全部容器 | `make restart` |
| 停止容器（保留数据卷） | `make down` |
| 跟踪日志 | `make logs-backend` / `make logs-frontend` / `make logs-deps` |
| 测试 | `make test` |
| 评测 | `make eval-*`（见下文「评估模块」） |

## 本机联调

两个终端，都在仓库根操作。首次先初始化配置、安装依赖，再起网关和前端：

```bash
# 首次
make init
make install

# 终端 1 — 网关
make dev-backend

# 终端 2 — 前端
make dev-frontend                 # http://127.0.0.1:3000
```

打开 http://127.0.0.1:3000/workspace 即可发消息。发送会经 `3000/api/...` 转到 `8001`。
需要知识库、OpenSandbox、OpenViking 等 optional extras 时，把 `make install` 换成 `make install-all`。

开发热重载或 Windows + Postgres 需要额外参数时，通过 `BACKEND_ARGS` 传给 uvicorn：

```bash
# 热重载
make dev-backend BACKEND_ARGS="--reload"

# Windows + Postgres checkpointer
make dev-backend BACKEND_ARGS="--loop backend.app.gateway.selector_loop:selector_loop_factory"
```

探活：

```bash
make health FRONTEND_PORT=3000
# 或单独 curl
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8001/docs
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3000/workspace
```

不用 `make` 时，等价命令是：

```bash
# 依赖
uv sync --all-packages
(cd frontend && pnpm install)
test -f frontend/.env.local || cp frontend/.env.example frontend/.env.local

# 网关（仓库根）
uv run --env-file .env --with uvicorn \
  uvicorn backend.app.gateway.app:app --host 127.0.0.1 --port 8001

# 前端
(cd frontend && pnpm dev)
```

`--workers` 保持 1；多 worker 的约束见 [`backend/README.md`](backend/README.md)。

## Docker 部署

仓库根需要已有 `config.yaml` 和 `.env`（含 `SANDBOX_HOST_ROOT`、`DATABASE_URL`、`LANGFUSE_BASE_URL` 等）。
`make up` 会依次启动依赖、构建并启动网关与前端；首次构建较慢。

```bash
make init           # 仅补齐缺失文件
# 编辑 .env / config.yaml，填入真实密钥、路径和服务地址
make up             # 依赖 + 网关 + 前端（也可用 make deploy）
make ps             # 查看状态
make health         # 探活
make logs-backend   # 跟踪网关日志
make down           # 停止；数据卷保留
```

`make up` 先后台启动依赖，再构建并启动网关与前端。首次启动时依赖的健康检查可能需要几十秒，
用 `make ps` 观察，就绪后再 `make health`。`make restart` 等价于 `make down` + `make up`。

网关容器加入依赖项目的 Docker 网络 `myflow-deps_default`，用服务名访问 Postgres / Redis 等
（见 `.env.example`）。依赖对外仍多绑 `127.0.0.1`，容器互访不走宿主机端口。

### 依赖服务（`docker/docker-compose.yml`）

Postgres、Redis、Milvus、OpenViking、Gitea、OpenSandbox、Langfuse。端口与账号对齐 `config.yaml` / `.env`；
Langfuse 对外 `0.0.0.0:3050`，其余依赖只绑 `127.0.0.1`。

需要手工拆分操作时，在仓库根或 `docker/` 下执行均可（后者须带 `--env-file`）：

```bash
# 仓库根
docker compose -f docker/docker-compose.yml --env-file .env up -d

# 或进入 docker/
cd docker && docker compose --env-file ../.env up -d
```

停止依赖：

```bash
docker compose -f docker/docker-compose.yml --env-file .env down
```

### 网关与前端

各自一份镜像与 compose，不包含上述依赖；连接串写在 `.env`（服务名）。在仓库根执行：

```bash
# 网关：加入 myflow-deps_default，对外 8001（须已 up 依赖）
docker compose -f docker/backend-compose.yml --env-file .env up -d --build

# 前端：构建并后台启动，对外 0.0.0.0:2026
# /api 默认转到宿主机 8001。网关在别的地址时：
#   GATEWAY_UPSTREAM=192.168.1.10:8001 docker compose -f docker/frontend-compose.yml up -d --build
docker compose -f docker/frontend-compose.yml --env-file .env up -d --build
```

只构建、不启动：

```bash
make build
# 或
docker build -f docker/backend.Dockerfile -t myflow-backend:local .
docker build -f docker/frontend.Dockerfile -t myflow-frontend:local .
```

探活：

```bash
make health
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8001/docs
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:2026/workspace
```

停止网关 / 前端（`make down` 会连同依赖一起停）：

```bash
docker compose -f docker/backend-compose.yml down
docker compose -f docker/frontend-compose.yml down
```

## 评估模块（`evals/`）

[`evals/`](evals/README.md) 是独立的黑盒评测工作区，也是根 uv workspace 的成员。它不导入
`backend.app` 或 `agentkit`，只通过 HTTP 与 Gateway、Langfuse 通信：

- **构建数据集**：以固定测试账户读取该账户自己的 MCP 工具、私有 Skills、知识库文档（可选失败/低分
  追踪），扩展成草稿，去重和结构质检后写入 Langfuse 数据集；`--llm-synthesis` 用评判模型生成中文 query。
- **运行实验**：按数据集条目启动 thread / run，读取运行事件账本与 thread checkpoint，用规则评测器
  评分；配置评判模型后叠加 LLM 指标（DeepEval 是可选 extra）。
- **输出结果**：把分数、门禁和实验写回 Langfuse；控制台摘要与退出码可直接用于 CI。

日常构建、跑分、读结果和排障看 [`evals/使用指南.md`](evals/使用指南.md)；命令契约、指标定义和
机密约束看 [`evals/README.md`](evals/README.md)。

### 安装与机密

```bash
make init               # 复制 evals/env.template -> evals/.env（仅当不存在）
make eval-install       # 规则指标所需；等价于 uv sync --project evals
make eval-install-llm   # 可选：安装 DeepEval（LLM 指标也可用内置 prompt 引擎）
```

编辑 `evals/.env`，至少配置：

| 变量 | 必需场景 | 说明 |
|---|---|---|
| `AGENT_API` | `build` / `run` | Gateway 基础 URL，如 `http://127.0.0.1:8001/api` |
| `AGENT_USERS` | 同上 | 固定测试账户 JSON：`{"标签":{"username":"...","password":"..."}}` |
| `USER_ID_SALT` | 同上 | 必须与 Agent 追踪配置 `user_id_salt` 完全一致 |
| `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | `run`、真实 `build` | 数据集读写、实验和追踪 |
| `EVAL_JUDGE_BASE_URL` / `EVAL_JUDGE_API_KEY` / `EVAL_JUDGE_MODEL` | `--llm-synthesis`、LLM 指标 | 任意 OpenAI 兼容评判模型；三者都配置才启用 |

真实环境变量优先于 `--env-file`，CI 机密不会被 checkout 里的文件覆盖。密码等机密不要提交。

被测 Gateway 还必须满足以下条件，分数才有设计上的含义：

- `auth.enabled=true`；
- 追踪 `user_id_mode=hashed`，盐值与 `USER_ID_SALT` 一致；
- `subagent_enabled=true`，且 `skill-finder`、`knowledge-researcher` 可用；
- `skills.usage.prompt_top_k=0`（私有 Skills 不得进入静态提示索引）；
- `run_events.backend=db` 或 `jsonl`，不能是 `memory`；
- 测试账户已完成首次改密（`needs_setup=false`）。

### 常用流程

```bash
make test-evals       # 评测包单测

# 可选：用 make 变量覆盖 EVAL_DATASET / EVAL_LIMIT / EVAL_ARGS
make eval-build-dry   # LLM 增强试构建，limit 5，不写 Langfuse
make eval-build       # 写入 agent-eval/regression/v1，默认 limit 100

make eval-run-dry     # 只读 Langfuse，打印将跑多少条目
make eval-run         # ci 模式的分层子集，默认并发 2
make eval-run EVAL_MODE=full   # 全量条目 + 全部 LLM 指标
```

探索集（可包含失败/低分追踪，**不要**用于回归门禁）：

```bash
make eval-build EVAL_DATASET=agent-eval/explore EVAL_ARGS="--include-traces"
```

`make eval-build` 固定带 `--llm-synthesis`，因此需要 `EVAL_JUDGE_*`。只用规则模板构建、
或需要 `--accounts-file` / `--strict` 等更多标志时，直接调用 CLI；完整标志见
[`evals/README.md`](evals/README.md#命令)。数据集是冻结输入，重建同名数据集不会删除旧条目，
新版本应换数据集名（如 `agent-eval/regression/v2`）。

### 结果与 CI

成功的运行会打印每个指标的均值、阈值、样本数和 `PASS` / `FAIL`，最后给出 Langfuse 实验链接。
进程退出码：`0` 完成且通过，`1` 实验失败，`2` 无法按配置运行（缺少机密、账户未登录、数据集为空等）。

[`.github/workflows/agent-eval.yml`](.github/workflows/agent-eval.yml) 在 PR 跑固定 30 条
`ci` 子集，夜间跑 `full` 全量；数据集由外部构建，工作流不执行 `build`。

## 测试

```bash
make test             # 后端 + 前端 + 评测
make test-backend
make test-frontend
make test-evals
```

等价原始命令：

```bash
cd backend && uv run pytest
cd frontend && pnpm test
uv run --project evals pytest evals/tests -v
```
