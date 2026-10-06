# 后端服务（Gateway）

FastAPI 装配的网关，ASGI 应用入口是 `backend.app.gateway.app:app`。

本文里的每条命令都在本机实跑验证过（Python 3.12 / uv 0.12.5 / Windows + Git Bash）。
没有验证过的部分会明确标出来。

---

## 1. 最短路径

```bash
cd D:/python/myflow          # ★ 必须是仓库根，不能是 backend/
uv run --env-file .env --with uvicorn \
  uvicorn backend.app.gateway.app:app --host 127.0.0.1 --port 8001
```

起来之后：

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8001/docs   # → 200
```

> **注意**：现在 `GET /openapi.json` 返回的是 `"paths": {}`——**一个路由都没有**。
> `backend/app/gateway/app.py` 只创建了 `FastAPI(lifespan=lifespan)`，没有
> `include_router`，`routers/threads.py` 和 `routers/thread_runs.py` 目前还是
> 只有 docstring 的空壳。所以 `/docs` 能打开但列不出任何接口，这是**预期状态**，
> 不是启动失败。

---

## 2. 三个必须遵守的前提

这三条都不是风格问题，违反了就直接起不来。

### 2.1 必须从仓库根启动

`backend.app.gateway.app` 这个导入路径要求 `backend` 能作为顶层包被找到。但
**`backend/__init__.py` 不存在**，它靠的是 Python 3 的隐式命名空间包——也就是
"当前工作目录在 `sys.path` 上"。

所以从 `backend/` 目录启动会失败：

```
$ cd backend && uv run --with uvicorn uvicorn backend.app.gateway.app:app
ModuleNotFoundError: No module named 'backend'
```

`.venv` 里虽然有 `backend-0.1.0.dist-info`，但它的 RECORD 里没有任何包文件，
**装进去的只有元数据，不是可导入的代码**。别指望 `pip install -e` 那种效果。

> 想摆脱"必须在仓库根"这个约束的话，加一个 `backend/__init__.py`，
> 或者把 `app.py` 里的 `from backend.app.gateway.run_models import ...`
> 改成相对导入。这属于结构调整，本文不改。

### 2.2 `.env` 不会被自动加载

代码里**没有任何 dotenv 调用**（`backend/` 和 `agentkit/` 全树 grep 无命中）。
`.env` 只是一份给 `uv --env-file` 用的清单。

不带 `--env-file` 就直接启动，会在 lifespan 里炸：

```
FileNotFoundError: No config file found. Please set the MYFLOW_CONFIG_PATH
environment variable or provide a config file path.
ERROR:    Application startup failed. Exiting.      # 退出码 3
```

原因是 `.env` 里的 `MYFLOW_CONFIG_PATH=./config.yaml` 没进环境（见 §5.1 的查找顺序）。

### 2.3 依赖里没有 uvicorn

`backend/pyproject.toml` 的 `dependencies` 只有 `agentkit` 和 `fastapi`，
`uv.lock` 里搜不到 uvicorn。所以 `uv run uvicorn ...` 会报找不到命令。两个办法：

**A. 临时带上（不改仓库，本文默认用法）**

```bash
uv run --with uvicorn uvicorn backend.app.gateway.app:app ...
```

**B. 固化成开发依赖（推荐长期这么干）**

在 `backend/pyproject.toml` 里加：

```toml
[dependency-groups]
dev = [
    "pytest>=9.1.1",
    "uvicorn[standard]>=0.40",
]
```

然后：

```bash
uv sync
uv run --env-file .env uvicorn backend.app.gateway.app:app --port 8001
```

> 加 `[standard]` 才有 `--reload` 用的 watchfiles 和 uvloop。
> 只装裸 `uvicorn` 时 `--reload` 会退回 StatReload（实测可用，但不监听新文件）。

---

## 3. 常用启动方式

以下都假设当前目录是 `D:/python/myflow`。

### 本地开发（热重载）

```bash
uv run --env-file .env --with uvicorn \
  uvicorn backend.app.gateway.app:app --reload --host 127.0.0.1 --port 8001
```

实测 Output：

```
INFO:     Started reloader process [26548] using StatReload
INFO:     Application startup complete.
```

### 本机联调（给前端用）

前端的 `GATEWAY_INTERNAL_URL` 默认指向 `http://127.0.0.1:8001`，
nginx 的 `upstream myflow_gateway` 默认指向 `myflow-gateway:8001`。所以本机就用 8001：

```bash
uv run --env-file .env --with uvicorn \
  uvicorn backend.app.gateway.app:app --host 127.0.0.1 --port 8001
```

### 容器内

容器里要让别的容器访问，必须绑 `0.0.0.0`：

```bash
uv run --env-file /app/.env --with uvicorn \
  uvicorn backend.app.gateway.app:app --host 0.0.0.0 --port 8001
```

> **`--host 0.0.0.0` 只在容器/受信网络里用。** 本机裸跑时绑 `0.0.0.0` 等于
> 整个局域网都能访问这个服务（M5 §7.3 同一条约束：发布出去的端口才是攻击面）。
> 所以本机一律 `127.0.0.1`。

### 编程式启动（嵌进别的进程 / 调试）

```python
import os
import uvicorn

os.environ.setdefault("MYFLOW_CONFIG_PATH", "config.yaml")   # 相对 CWD

if __name__ == "__main__":
    uvicorn.run(
        "backend.app.gateway.app:app",
        host="127.0.0.1",
        port=8001,
        reload=False,          # 传 import 字符串时 reload 才有效
    )
```

以脚本方式运行时 `sys.path[0]` 是脚本所在目录，**不是仓库根**——
放在仓库根跑，或者在脚本开头手动把仓库根插进 `sys.path`。

---

## 4. 参数怎么选

| 参数 | 建议 | 说明 |
|---|---|---|
| `--host` | 本机 `127.0.0.1`，容器 `0.0.0.0` | 见 §3 的安全约束 |
| `--port` | `8001` | 与前端 `GATEWAY_INTERNAL_URL` / nginx upstream 对齐 |
| `--reload` | 只在开发时开 | 生产禁用；它要求 import 字符串形式 |
| `--workers` | **保持默认的 1** | 见下 |
| `--log-level` | `info` | 只影响 uvicorn 自己的 logger，和 `config.yaml` 的 `log_level` 是两回事 |
| `--env-file` | `.env` | **uv 的参数，不是 uvicorn 的**，要放在 `uvicorn` 之前 |

### 为什么 `--workers` 要保持 1

M3 的 `StreamBridge` 目前只有内存实现（`supports_cross_process = False`）。
多个 worker 意味着**创建 run 的进程和订阅 SSE 的进程可能不是同一个**，
此时订阅会拿到空流死等。要上多 worker 得先换 Redis 实现。

另外 `GATEWAY_WORKERS` 这个环境变量必须和真实 worker 数一致：

```python
def gateway_worker_count() -> int:
    """How many gateway processes share this database. Unset means one."""
    raw = os.environ.get("GATEWAY_WORKERS", "1")
```

它影响两件事：run_events 的 `seq` 分配是否需要跨进程锁，
以及启动时 `recover_inflight_runs` 要不要把 inflight 行标成失败
（`>1` 时会拒绝回收，因为那些行可能还属于活着的同伴进程）。
**多 worker 却没设这个变量，会导致启动时错误地把别人正在跑的 run 标成 error。**

---

## 5. 配置

### 5.1 `config.yaml` 的查找顺序

`AppConfig.resolve_config_path()` 依次尝试：

1. 显式传入的 `config_path`（只有代码调用才有）
2. 环境变量 `MYFLOW_CONFIG_PATH`
3. `project_root() / "config.yaml"`
4. 都没有 → `FileNotFoundError`（就是 §2.2 那个报错）

其中：

```python
def project_root() -> Path:
    if env_root := os.getenv("MYFLOW_PROJECT_ROOT"):
        ...return root
    return Path.cwd().resolve()
```

**`project_root()` 默认就是当前工作目录。** 所以 `config.yaml` 和 `.myflow/`
这些相对路径全都跟着 CWD 走——又一次说明为什么要在仓库根启动。

### 5.2 `$ENV` 展开

配置里以 `$` 开头的字符串会从环境变量读取，**读不到直接报错，不会静默变成空串**：

```python
if config.startswith("$"):
    value = os.getenv(config[1:])
    if value is None:
        raise ValueError(f"Environment variable {config} is not set")
```

`config.yaml` 里的 `api_key: $DEEPSEEK_API_KEY` 就走这条路。所以
`DEEPSEEK_API_KEY` 必须在环境里——这正是要带 `--env-file .env` 的原因。

### 5.3 当前默认值（都是"什么都不持久化"）

| 配置段 | 默认 | 含义 |
|---|---|---|
| `database.backend` | `memory` | 不落地；改 `sqlite` 时目录默认 `.myflow/data` |
| `run_events.backend` | `memory` | 事件账本不持久化 |
| `checkpointer.type` | `memory` | checkpoint 不持久化 |

也就是说现在**重启即失忆**，这是 M1/M3 阶段的预期状态，M4 才会接上真正的存储。

### 5.4 启动时会做什么

`lifespan` 的顺序（`backend/app/gateway/app.py`）：

1. `get_app_config()` —— 读配置，失败就在这里退出
2. `init_engine_from_config(...)` —— 数据库引擎
3. 建 `thread_meta` / `run_store` / `event_store`
4. `make_checkpointer(...)`
5. `build_run_services(...)` → 挂到 `app.state.run_services`
6. `recover_inflight_runs(...)` —— 把上次进程留下的 inflight run 标成 error，有的话打 warning

关闭时先 `run_manager.shutdown(timeout)`，再 `cancel_detached()`，最后关引擎。

---

## 6. 已知问题

### 6.1 `config.yaml` 的兜底查找是坏的（一行 bug）

`backend/packages/harness/agentkit/config/app_config.py:87`：

```python
elif file := existing_project_file(("config.yaml")):    # ← 少了一个逗号
```

`("config.yaml")` 是**字符串不是元组**。而 `existing_project_file` 是这么写的：

```python
def existing_project_file(names: tuple[str, ...]) -> Path | None:
    for name in names:          # 遍历字符串 → 得到 'c','o','n','f','i','g','.','y','a','m','l'
        file = root / name      # root / 'c'  →  D:\python\myflow\c
        if file.is_file():
            return file
    return None
```

它会去找名为 `c`、`o`、`n`…… 的文件，**永远返回 `None`**。实测：

```python
>>> existing_project_file("config.yaml")      # 现在的调用形态
None
>>> existing_project_file(("config.yaml",))   # 本来的意图
WindowsPath('D:/python/myflow/config.yaml')
```

**后果**：`config.yaml` 明明躺在仓库根，不设 `MYFLOW_CONFIG_PATH` 也找不到它，
于是 §2.2 那个 `FileNotFoundError`。加载顺序里第 3 条（项目根兜底）等于形同虚设。

**修复**：加一个逗号。

```diff
- elif file := existing_project_file(("config.yaml")):
+ elif file := existing_project_file(("config.yaml",)):
```

**在修掉之前**：必须靠 `MYFLOW_CONFIG_PATH`（也就是 `--env-file .env`，或自己
`export`），别指望它自己找到。

### 6.2 一个路由都没有

`app.py` 末尾只有 `app = FastAPI(lifespan=lifespan)`，没有 `include_router`。
`routers/threads.py`、`routers/thread_runs.py` 目前是纯 docstring 占位。
M3 定的那几个端点（`POST /threads/{id}/runs/stream` 等）**都还不存在**。

所以现在的冒烟测试只能用 `/docs` 和 `/openapi.json`（两者都由 FastAPI 自带），
不能拿业务端点探活。

### 6.3 `.env` 里的变量名疑似拼错

```
DEEPSEEKK_BASE_URL="https://api.deepseek.com"     # 两个 K
```

`config.yaml` 用的是 `base_url` 字面量，没引用这个变量，所以现在不影响运行。
但如果哪天想用 `$DEEPSEEKK_BASE_URL`，注意它拼错了。

---

## 7. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: No module named 'backend'` | CWD 不是仓库根 | `cd D:/python/myflow` 再启动 |
| `No config file found...` | `.env` 没注入（§6.1 的 bug 让兜底失效） | 加 `--env-file .env`，或 `export MYFLOW_CONFIG_PATH=/绝对路径/config.yaml` |
| `Environment variable $DEEPSEEK_API_KEY is not set` | 配置找到了但环境变量没传 | `--env-file .env`，或 `export DEEPSEEK_API_KEY=...` |
| `No models found in config` | `config.yaml` 里 `models` 为空 | 至少留一个 model |
| `agents.default_model ... is not one of models[].name` | 默认模型名和列表对不上 | 改 `agents.default_model` |
| `GATEWAY_WORKERS must be an integer` | 环境变量写成了非数字 | 设成整数或删掉 |
| `[Errno 10048] address already in use` | 端口被占 | 换 `--port`，或先杀掉旧进程 |
| `/docs` 打开是空的 | 正常，见 §6.2 | —— |
| 启动后没有任何日志 | `--log-level` 太高 | 用 `--log-level info` |

### 确认端口上的到底是不是你的进程

```bash
# Git Bash
netstat -ano | grep 8001
# 拿到 PID 再核对该进程的命令行
wmic process where "ProcessId=<PID>" get CommandLine
```

---

## 8. 本文的验证记录

跑过的命令和结果：

| 检查 | 结果 |
|---|---|
| 从仓库根 `uvicorn backend.app.gateway.app:app` 启动 | ✅ `Application startup complete.`，约 12s |
| `GET /docs` | ✅ 200 |
| `GET /openapi.json` | ✅ 200，`"paths": {}` |
| `--reload` | ✅ `Started reloader process ... using StatReload` |
| 从 `backend/` 启动 | ❌ `ModuleNotFoundError: No module named 'backend'` |
| 不带 `--env-file` 启动 | ❌ `FileNotFoundError: No config file found...`，退出码 3 |
| `existing_project_file("config.yaml")` | → `None`（§6.1 的 bug） |
| `existing_project_file(("config.yaml",))` | → `D:/python/myflow/config.yaml` |

**没验证的**：没有跑过多 worker（`--workers > 1`）的实际行为；
没有在 Linux 容器里跑过（本文命令在 Windows + Git Bash 下验证）。
