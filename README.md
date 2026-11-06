# MyFlow

本地 agent 工作台：后端是 FastAPI 网关（LangGraph Platform 兼容），前端是 Next.js 聊天 UI。浏览器只打同源 `/api/*`，开发时由 Next rewrite 转到网关。

| 服务 | 目录 | 端口 | 说明 |
|---|---|---|---|
| 网关 | [`backend/`](backend/README.md) | `127.0.0.1:8001` | threads / runs / SSE / 上传 |
| 前端 | [`frontend/`](frontend/README.md) | `127.0.0.1:3000` | 工作区聊天 / 知识库 |

更细的启动约束、配置和排障写在各自目录的 README 里。知识库（MinerU + Qwen3-Embedding-8B +
Milvus）默认关闭，配置、安装可选依赖和 API 见 [`backend/README.md`](backend/README.md#知识库mineru--qwen3-embedding-8b--milvus)。

## 前置

- Python **3.12+**、[uv](https://docs.astral.sh/uv/)
- Node **20.9+**、[pnpm](https://pnpm.io/)
- 仓库根有 `config.yaml`（可从 `config.example.yaml` 复制）和 `.env`（可从 `.env.example` 复制）

`.env` 至少要能展开 `config.yaml` 里的 `$DEEPSEEK_API_KEY`。`MYFLOW_CONFIG_PATH` 建议设成仓库根的 `config.yaml`（相对路径跟着进程 CWD）。

## 本机联调

两个终端，都在**仓库根**操作。先起网关，再起前端。

```bash
# 终端 1 — 网关
uv run --env-file .env --with uvicorn \
  uvicorn backend.app.gateway.app:app --host 127.0.0.1 --port 8001

# 终端 2 — 前端
cd frontend
pnpm install
cp .env.example .env.local   # 首次
pnpm dev                     # http://127.0.0.1:3000
```

打开 http://127.0.0.1:3000/workspace 即可发消息。发送会经 `3000/api/...` 转到 `8001`。

探活：

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8001/docs
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3000/workspace
```

## Docker

仓库根需要已有 `config.yaml` 和 `.env`（含 `MYFLOW_HOST`、`SANDBOX_HOST_ROOT` 等）。先起依赖，再起网关 / 前端。

### 依赖服务（`docker/docker-compose.yml`）

Postgres、Redis、Milvus、OpenViking、Gitea、OpenSandbox、Langfuse。端口与账号对齐 `config.yaml` / `.env`；除 Langfuse（`${MYFLOW_HOST}:3050`）外，其余只绑 `127.0.0.1`。

在仓库根或 `docker/` 下执行均可（后者须带 `--env-file`）：

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

各自一份镜像与 compose，不包含上述依赖；地址仍写在 `config.yaml` / `.env`。在仓库根执行：

```bash
# 网关：构建并后台启动，对外 8001
docker compose -f docker/backend-compose.yml up -d --build

# 前端：构建并后台启动，对外 ${MYFLOW_HOST}:2026（服务器 IP）
# /api 默认转到宿主机 8001。网关在别的地址时：
#   GATEWAY_UPSTREAM=192.168.1.10:8001 docker compose -f docker/frontend-compose.yml up -d --build
docker compose -f docker/frontend-compose.yml --env-file .env up -d --build
```

只构建、不启动：

```bash
docker build -f docker/backend.Dockerfile -t myflow-backend:local .
docker build -f docker/frontend.Dockerfile -t myflow-frontend:local .
```

探活：

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8001/docs
curl -s -o /dev/null -w "%{http_code}\n" http://${MYFLOW_HOST}:2026/workspace
```

停止网关 / 前端：

```bash
docker compose -f docker/backend-compose.yml down
docker compose -f docker/frontend-compose.yml down
```

## 测试

```bash
cd backend && uv run pytest
cd frontend && pnpm test
```
