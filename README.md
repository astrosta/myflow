# MyFlow

本地 agent 工作台：后端是 FastAPI 网关（LangGraph Platform 兼容），前端是 Next.js 聊天 UI。浏览器只打同源 `/api/*`，开发时由 Next rewrite 转到网关。

| 服务 | 目录 | 端口 | 说明 |
|---|---|---|---|
| 网关 | [`backend/`](backend/README.md) | `127.0.0.1:8001` | threads / runs / SSE / 上传 |
| 前端 | [`frontend/`](frontend/README.md) | `127.0.0.1:3000` | 工作区聊天 |

更细的启动约束、配置和排障写在各自目录的 README 里。

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

## 测试

```bash
cd backend && uv run pytest
cd frontend && pnpm test
```
