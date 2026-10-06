# 后端服务（Gateway）

FastAPI 网关。ASGI 入口：`backend.app.gateway.app:app`。

## 最短路径

必须在**仓库根**启动，不能 `cd backend`。

```bash
cd /path/to/myflow
uv run --env-file .env --with uvicorn \
  uvicorn backend.app.gateway.app:app --host 127.0.0.1 --port 8001
```

- `--env-file` 是 **uv** 的参数，必须写在 `uvicorn` 之前。代码不会自己读 `.env`。
- 依赖里没有 uvicorn，所以用 `--with uvicorn`。要热重载再加 `--reload`。
- 本机绑 `127.0.0.1`。容器里才绑 `0.0.0.0`。

起来之后：`http://127.0.0.1:8001/docs`。

开发热重载：

```bash
uv run --env-file .env --with uvicorn \
  uvicorn backend.app.gateway.app:app --reload --host 127.0.0.1 --port 8001
```

`--workers` **保持 1**。当前 `StreamBridge` 是进程内内存实现，多 worker 时创建 run 和订阅 SSE 可能不在同一进程。环境变量 `GATEWAY_WORKERS` 必须和真实进程数一致，否则启动回收会把别人的 inflight run 标成 error。

## 配置

`AppConfig.resolve_config_path()` 顺序：

1. 代码显式传入的路径
2. `MYFLOW_CONFIG_PATH`
3. 项目根下的 `config.yaml`（`MYFLOW_PROJECT_ROOT` 或当前工作目录）

`config.yaml` 里以 `$` 开头的值从环境变量展开，缺了会直接报错（例如 `api_key: $DEEPSEEK_API_KEY`）。

`lifespan` 会建数据库引擎、thread_meta / run_store / event_store、checkpointer，并把 `RunServices` 挂到 `app.state`。默认 `database` / `run_events` / `checkpointer` 都可以是 memory：重启即失忆。

沙箱段见 `config.yaml` 的 `sandbox`。本机 Local provider 默认不开宿主 bash；可信环境可设 `allow_host_bash: true`。

## 主要路由

都挂在 `/api`：

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/api/threads` | 建会话 |
| `GET` | `/api/threads/{id}` | 读会话 |
| `POST` | `/api/threads/search` | 列会话 |
| `POST` | `/api/threads/{id}/runs/stream` | 创建 run 并 SSE |
| `POST` | `/api/threads/{id}/runs/wait` | 创建 run 并阻塞到结束 |
| `GET` | `/api/threads/{id}/runs/{run_id}/join` | 旁观已有 run（断开不取消） |
| `POST` | `/api/threads/{id}/runs/{run_id}/cancel` | 取消 |
| `POST` | `/api/threads/{id}/uploads` | 上传 |
| `GET` | `/api/threads/{id}/artifacts/...` | 产物 |

浏览器应走前端同源 `/api`，不要直接跨源打 8001（开发时 Next rewrite，生产时 nginx）。

## 测试

```bash
cd backend
uv run pytest
```

## 排障

| 现象 | 处理 |
|---|---|
| `No module named 'backend'` | 在仓库根启动 |
| `No config file found` | `--env-file .env`，或设置 `MYFLOW_CONFIG_PATH` |
| `$DEEPSEEK_API_KEY is not set` | `.env` 里补上，并用 `--env-file` |
| `address already in use` | 换 `--port` 或杀掉占用 8001 的进程 |
| SSE 只有一帧 `event: end` | 订阅早于 worker 第一帧 `publish`；见 `MemoryStreamBridge.subscribe` 在 buffer 为 None 时直接 END |
