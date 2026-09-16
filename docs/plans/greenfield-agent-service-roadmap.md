# 从零构建一个对标 DeerFlow 的 Agent 服务 —— 分阶段开发方案

> 目标形态：**完整对标 DeerFlow** —— LangGraph 驱动的超级 agent 服务，具备沙箱执行、持久记忆、
> 子代理委托、可扩展工具（内置 / MCP / 社区）、多用户与多实例、定时任务、IM 通道接入，
> 全部以 thread 为隔离单元。
>
> 本文按**功能模块的依赖顺序**排期，不是按"后端→前端"或"简单→复杂"排期。
> 每个阶段的"已知陷阱"一节来自 DeerFlow 现有实现中被明确记录下来的设计约束，
> 是这个项目已经付过学费的地方，建议直接采纳而不要重新踩一遍。

---

## 零、总体判断

### 难的不是 agent 循环

一个能用的 ReAct 循环用 LangGraph 写出来不到 200 行，一周内可以跑通。DeerFlow 这个体量的代码
几乎全部长在**循环外面**：

- 一次执行怎么被准入、怎么取消、怎么超时
- 事件怎么流回浏览器、断线重连怎么接上
- 多 Gateway 实例之间怎么不打架
- 上下文超了怎么压缩、工具输出太长怎么截断
- 子代理的 token 记在谁头上、并发子代理怎么不串号

因此排期的核心原则是：**改起来最贵的抽象要最早定型，哪怕第一版实现极其简陋。**

### 六个必须在 M0–M3 内定型的地基抽象

| 抽象 | 第一版可以多简陋 | 但必须先定死的是 |
|---|---|---|
| **Harness / App 分层** | 两个目录 | 依赖单向（app → harness），且用 CI 测试钉死 |
| **Thread / Run / Checkpoint 三元模型** | 内存字典 | 三个词各自回答什么问题，语义不能混 |
| **存储 Provider 抽象** | 只有 memory 实现 | 接口是 async 的，能平滑换 SQLite / Postgres |
| **StreamBridge 抽象** | 进程内 `asyncio.Queue` | 带 offset、能 replay、有显式 `end` 帧 |
| **配置系统** | 一个 YAML 文件 | 静态运维配置 vs 运行时可写配置必须分家 |
| **虚拟路径模型** | 本地目录 | `/mnt/user-data/...` 这一层间接寻址 |

这六个里任何一个等到有真实流量再补，代价都是重写半个后端。

反过来，MCP、技能、子代理、调度器、IM 通道、扩展插件这些看起来最有价值的功能，**全部可以晚做**，
因为它们是挂在上述抽象上的叶子节点。

### 三元概念模型（务必先统一口径）

```
User
 └── Thread                 一次会话（Web 对话 / IM 群会话 / 定时任务绑定的会话）
       ├── Checkpoint       图状态：messages、title、artifacts、todos…（跨 run 累积）
       ├── Files            该 thread 的 uploads / outputs / workspace
       └── Run × N          每一次"让 agent 跑一轮"
             ├── run 行      生命周期、token 统计、owner 租约
             ├── Stream      这次执行的 SSE（有界、会过期）
             └── run_events  这次执行的持久账本（可选）
```

- **Thread** 回答"这是哪段对话"。
- **Run** 回答"这轮从何时开始、是否还在跑、用了多少 token、能不能取消"。
- **Checkpoint** 回答"图现在认为世界是什么样"。
- **Message** 是 checkpoint / 事件流里的**内容**，不是 run。

同一 thread 历史上可以有很多 run；**同一时刻最多一个活跃操作**。

---

## 阶段总览

| 阶段 | 主题 | 单人工期 | 可并行 |
|---|---|---|---|
| M0 | 地基：仓库分层、配置系统、模型工厂 | 1 周 | — |
| M1 | 最小 agent：图、ThreadState、内存 checkpoint | 1–2 周 | — |
| M2 | 工具与沙箱：Sandbox 抽象、文件工具、路径安全 | 2 周 | — |
| M3 | **Gateway 与流式：run 生命周期、SSE、StreamBridge** | 2–3 周 | — |
| M4 | 持久化：SQLAlchemy + Alembic、领域分表 | 1–2 周 | 与 M5 |
| M5 | 前端：Next.js + useStream + nginx 统一入口 | 2 周 | 与 M4 |
| M6 | 中间件体系：压缩、标题、上传、错误处理、预算 | 2–3 周 | — |
| M7 | 能力扩展：MCP、技能系统、搜索抓取 | 2 周 | 与 M8 |
| M8 | 子代理委托与并发 | 2 周 | 与 M7 |
| M9 | 多用户、鉴权、多实例、调度器、IM 通道 | 3–4 周 | 内部可并行 |
| M10 | 记忆、护栏、扩展插件、可观测性、评测 | 3–4 周 | 内部可并行 |

单人全职约 **5–7 个月**到 M8，完整对标（含 M9/M10）约 **8–10 个月**。
三人小队在 M5 之后可并行，整体压到 **4–5 个月**。

---

## M0 — 地基（1 周）

> 详细设计：[M0 地基详细设计](m0-foundation-design.md)

### 目标
让"配置 → 模型实例"这条最短链路跑通，此外什么都不做。

### 模块清单
- monorepo 骨架：`backend/`（含 `packages/harness/` 与 `app/`）、`frontend/`、`docker/`、`scripts/`、`docs/`
- 配置系统：`config/app_config.py` + 分领域子配置
- 反射加载：`reflection/`（`resolve_class` / `resolve_variable`）
- 模型工厂：`models/factory.py`
- 工具链：ruff、pytest、pre-commit、Makefile

### 关键设计决策

**1. Harness / App 分层，第一天就写边界测试。**

- `packages/harness/`：可发布的 agent 框架包，导入前缀 `deerflow.*`。包含 agent 编排、工具、沙箱、
  模型、MCP、技能、配置——一切"构建并运行 agent"需要的东西。
- `app/`：不发布的应用层，导入前缀 `app.*`。包含 FastAPI Gateway 与 IM 通道。
- **依赖规则：app 可以 import harness，harness 永远不能 import app。**

写一个扫 import 语句的测试（DeerFlow 是 `tests/test_harness_boundary.py`）放进 CI。这条测试比任何
架构文档都管用——一旦 harness 里出现一句 `from app.gateway...`，框架就再也拆不出来了。

**2. 配置要分成两份文件。**

- `config.yaml`：运维态静态配置（模型、数据库、沙箱模式、各子系统开关）。生产环境只读挂载。
- `extensions_config.json`：运行时可被 API 改写的动态配置（MCP 服务器列表、技能启用状态）。

第一版不需要做锁，但**文件必须从一开始就分开**。混在一起的后果见 M7 陷阱。

**3. 模型工厂用反射而不是 if-else。**

配置里写 `use: langchain_openai:ChatOpenAI`，运行时动态 import。加一个 20 行的 `resolve_class()`，
以后接任何 LangChain 集成都零改动。模型配置项至少包含：`name` / `display_name` / `use` / `model` /
`api_key` / `max_tokens` / `supports_thinking` / `supports_vision`。

后两个布尔位现在就要有——M6 的视觉支持和思考模式都靠它们分流。

### 交付物
- 可 `uv sync` 安装的两个包
- `make check` / `make install` / `make lint` / `make format` / `make test`
- harness 边界测试通过

### 验收
`create_chat_model("gpt-4o").invoke("hi")` 能跑；改配置切换 provider 不用改代码。

---

## M1 — 最小 agent（1–2 周）

> 详细设计：[M1 最小 Agent 详细设计](m1-minimal-agent-design.md)

### 目标
一个能多轮对话、能调工具的 agent，通过嵌入式客户端和 CLI 调用，不碰 HTTP。

### 模块清单
- `agents/thread_state.py`：`ThreadState`
- `agents/lead_agent/agent.py`：agent 工厂 `make_lead_agent(config)`
- `agents/lead_agent/prompt.py`：系统提示词
- `runtime/checkpointer/provider.py`：checkpointer Provider 抽象
- `client.py`：嵌入式 Python 客户端
- `tui/` 或简单 CLI

### 关键设计决策

**1. ThreadState 的字段位现在就要预留。**

```python
class ThreadState(AgentState):
    messages: list[BaseMessage]      # 来自 AgentState

    sandbox: dict                    # 沙箱环境信息
    artifacts: list[str]             # 产出文件路径
    thread_data: dict                # {workspace, uploads, outputs}
    title: str | None                # 自动生成的会话标题
    todos: list[dict]                # 任务跟踪（plan mode）
    viewed_images: dict              # 视觉模型的图片元数据
```

字段先留空，但 schema 要预留。checkpoint 里存的就是这个结构，后期加字段要考虑旧 checkpoint 的
反序列化兼容。

**2. 系统提示词单独成模块。** 不要写进 agent 工厂函数里——DeerFlow 的 `prompt.py` 有 1100 多行，
它会长得比你想象的快。

**3. checkpointer 走自己的 Provider 接口。** 第一版内部用 LangGraph 的 `MemorySaver`，但业务代码
只能看到你的 Provider。M4 换 Postgres 时只改 Provider 实现。

**4. 包导入要保持轻量。** `agents` / `subagents` 的包根不要 eager import 重量级的图/执行器模块。
只需要类型、配置、注册表的内部模块应该直接 import 具体子模块。DeerFlow 为此把
`make_lead_agent` 写成一个把重量级 import 放在函数体内的薄包装。

### 交付物
CLI 里多轮对话（重启后丢失，正常）；一个假工具（如 `get_weather`）能被正确调用。

### 验收
`pytest` 里可以不起任何服务直接测 agent 行为。

---

## M2 — 工具与沙箱（2 周）

> 详细设计：[M2 工具与沙箱详细设计](m2-tools-sandbox-design.md)

### 目标
agent 能在受控环境里读写文件、执行命令。

### 模块清单
- `tools/tools.py`：`get_available_tools()` 三源汇聚
- `tools/builtins/`：`present_files`、`list_uploaded_files` 等
- `sandbox/sandbox.py`：`Sandbox` 抽象
- `sandbox/sandbox_provider.py`：`SandboxProvider` 抽象
- `sandbox/local/`：本地文件系统实现
- `sandbox/tools.py`：`bash` / `ls` / `read_file` / `write_file` / `str_replace`
- `sandbox/security.py`、`sandbox/path_patterns.py`：路径安全
- `sandbox/middleware.py`：沙箱生命周期中间件
- `config/paths.py`：路径解析

### 关键设计决策

**1. 工具三源统一汇聚。** 从第一天起就用一个 `get_available_tools()` 汇聚三类来源：内置工具
（代码写死）、配置工具（`config.yaml` 开关）、外部工具（M7 的 MCP）。三源的加载时机、缓存策略、
失败降级都不同，但对 agent 工厂只暴露一个入口。

**2. 沙箱两层抽象，按远程约束设计。**

```
SandboxProvider          Sandbox
  - acquire()              - execute_command()
  - acquire_async()        - read_file()
  - get()                  - write_file()
  - release()              - list_dir()
```

第一版只实现本地 provider，但**所有方法都必须是 async 的、都可能失败、都不能假设沙箱和 Gateway
在同一台机器上**。DeerFlow 的 Docker provider 和 K8s provisioner 模式都是后来长出来的，能长出来
全靠这层接口没有泄漏本地假设。

**3. 虚拟路径映射必须现在做。**

| 虚拟路径 | 物理路径 |
|---|---|
| `/mnt/user-data/workspace` | `.deer-flow/users/{uid}/threads/{tid}/user-data/workspace` |
| `/mnt/user-data/uploads` | `.../user-data/uploads` |
| `/mnt/user-data/outputs` | `.../user-data/outputs` |
| `/mnt/skills` | `skills/` |

agent 只认虚拟路径，沙箱层负责翻译。这层间接寻址让你以后可以把物理布局从 `threads/{tid}` 改成
`users/{uid}/threads/{tid}`（DeerFlow 加多用户时正是这么改的），而 prompt 和历史 checkpoint 完全不用动。

**4. 路径安全现在就写，不要留到"以后加"。** `../` 穿越、符号链接逃逸、绝对路径逃逸、
Windows 盘符、UNC 路径。

### 已知陷阱
- 文件写入要有并发锁（同一 thread 内两个工具同时写同一文件）
- `str_replace` 要求唯一匹配，匹配到多处必须报错而不是替换第一处
- 沙箱环境变量要有白名单策略，不要把 Gateway 的全部环境透给子进程

### 验收
模型在沙箱里写 Python 脚本并执行成功；构造 `../../etc/passwd` 类输入被拒绝且有明确错误信息。

---

## M3 — Gateway 与流式（2–3 周）⚠️ 全程最关键

> 详细设计：[M3 Gateway 与流式详细设计](m3-gateway-streaming-design.md)

这是整个项目的分水岭。做错了，后面每个阶段都要还债。

### 目标
把 M1 的 agent 包进 FastAPI，对外提供 thread / run 语义的 HTTP API 和 SSE 流。

### 模块清单
- `app/gateway/app.py`：FastAPI 应用
- `app/gateway/routers/`：`threads.py` / `thread_runs.py` / `runs.py` / `models.py` / `artifacts.py`
- `app/gateway/services.py`：`start_run()` —— **所有入口的唯一收敛点**
- `runtime/runs/manager.py`：`RunManager`（准入、取消、租约）
- `runtime/runs/worker.py`：`run_agent()` 后台执行协程
- `runtime/runs/schemas.py`：`RunStatus` / `RunRecord`
- `runtime/stream_bridge/`：`base.py` + `memory.py`
- `runtime/stream_modes.py`、`runtime/serialization.py`

### 关键设计决策

**1. 直接对齐 LangGraph Platform 的 API 形状。**

```
POST /api/threads/{id}/runs          准入后立即返回，agent 后台跑
POST /api/threads/{id}/runs/stream   同上，但 HTTP 挂着 SSE 直到 event: end
POST /api/threads/{id}/runs/wait     同上，阻塞到结束返回 checkpoint 快照
GET  /api/threads/{id}/state         读 checkpoint
```

这样前端可以直接用官方 `useStream` SDK，省掉整个前端流式状态机。DeerFlow 用 nginx 把
`/api/langgraph/*` 重写到原生路由来提供这个兼容面。

**2. Run 状态机只有六个状态。**

```
[*] --> pending      create_or_reject（HTTP 此时已返回）
pending --> running  try_start（CAS）
pending --> interrupted / error
running --> success / error / timeout / interrupted
```

`finalizing` **不是**状态机上的一档，只是 worker 在 `finally` 里仍占着"每 thread 一条活跃"的名额。

**3. 所有入口收敛到同一个 `start_run()`。**

```
浏览器 useStream.submit
IM ChannelManager
调度器 launch_scheduled_thread_run
MCP 通知 launch_mcp_task_notification_run
        │
        ▼
   start_run()            校验属主、模型白名单、normalize_input
        │
        ▼
   RunManager.create_or_reject   →  pending RunRecord
        │
        ▼
   create_task(run_agent)
```

不同入口的差别只在传入的 **context**（`thinking_enabled` / `is_plan_mode` / `non_interactive` /
`on_disconnect` / `multitask_strategy`）和**要不要挂 SSE**，**绝不为新入口开第二条执行栈**。
一旦有两套执行路径，取消、计费、并发限制就要写两遍，且永远对不齐。

**4. StreamBridge 从第一版就要带 offset。**

这层抽象存在的唯一理由是：创建 run 的那个 HTTP 连接，和消费 SSE 的那个连接，**不一定是同一个**
（刷新页面、多标签页、断线重连、`/join` 旁观）。第一版用进程内队列，但接口要支持
`Last-Event-ID` 续传，否则 M9 上 Redis 时要重写。

**5. 取消必须是协作式的。** `cancel()` 只设置 `abort_event`（和 `abort_action`），worker 在图的
每一轮和 `try_start` 前检查。**路由层绝对不要直接 `task.cancel()`**，否则 store 上的
`cancel_action` CAS 会对不上。

取消有两种 action：
- `interrupt`：保留已写进 checkpoint 的内容
- `rollback`：回滚到本 run 开始前的 checkpoint

**6. `multitask_strategy` 决定下一次准入怎么对待还活着的这一次：**

| 策略 | 对旧 run | 对新 run |
|---|---|---|
| `reject`（默认，Web 发送） | 不动 | 不开，调用方 409 |
| `interrupt` | 标 `interrupted`，留 checkpoint | 旧的让位后开始 |
| `rollback` | 同上，但状态滚回 run 前 | 同上 |

### 已知陷阱（全部是 DeerFlow 付过学费的）

1. **准入和 `create_task` 之间不能有任何 `await`。** 否则在这个窗口里到达的取消请求会卡在补
   metadata 的 IO 上，SSE 订阅者永远等不到 `end`。
2. **`create_task` 失败必须把 pending 行标成 `error`。** 否则唯一索引上留一行僵尸，那条 thread
   从此再也开不了新 run。
3. **`finally` 里一定要 `publish_end`。** 漏掉这一句前端就永远转圈，而且这个 bug 只在异常路径上
   出现，测试很难覆盖。
4. **每 thread 一个活跃操作的唯一约束，不只管对话 run。** 以后的 compact、手动改 state、分支、
   删除线程都要共享它，因为它们都会写 checkpoint。现在建表就把 `operation_kind` 字段留出来，
   并且列表 API 要把 `operation_kind != run` 的行过滤掉（用户不该在"对话历史"里看到 compact）。
5. **`/wait` 必须订阅 StreamBridge，不能 `await record.task`。** 因为 `on_disconnect` 语义只活在
   创建该 run 的 worker 内存里。
6. **run 结束后延迟 GC（约 5 分钟）再从进程内注册表摘掉**，让晚到的 `/join` 仍能拿到 live 引用。

### 验收
- `curl` 一个 `/runs/stream` 中途 Ctrl+C，run 在几秒内变成 `interrupted`，无协程泄漏
- 重连 `/join` 能拿到后续事件并正常收到 `end`
- 同一 thread 并发两次 `POST /runs`，第二次返回 409
- 带 `Idempotency-Key` 重复提交，复用同一个 run 而不是跑两遍

---

## M4 — 持久化（1–2 周，可与 M5 并行）

> 详细设计：[M4 持久化详细设计](m4-persistence-design.md)

### 目标
重启不丢数据；同一套代码支持 SQLite（开发）和 Postgres（生产）。

### 模块清单
- `persistence/models/`：SQLAlchemy 模型
- `persistence/migrations/`：Alembic
- `persistence/thread_meta/`、`persistence/run/`、`persistence/user/`
- `runtime/events/store/`：`base.py` / `memory.py` / `jsonl.py` / `db.py`
- `runtime/checkpointer/`：SQLite / Postgres 实现
- `config/database_config.py`、`config/checkpointer_config.py`

### 关键设计决策

**1. 按领域分表，别搞一张大 JSON 表。** DeerFlow 的划分可以直接借鉴：
`thread_meta` / `runs` / `run_events` / checkpoint（LangGraph 自带）/ `users`。
后续阶段还会加 `scheduled_tasks` / `scheduled_task_runs` / `mcp_tasks` /
`channel_connections` / `personal_access_tokens` / `projects` / `feedback` /
`managed_subagents` / `subagent_batches` / `webhook_delivery`。

**2. `run_events.seq` 是 thread 全局递增，不是每 run 从 0 开始。** 前端要按时间顺序渲染整条会话的
事件流，跨 run 排序必须靠单调序号。

**3. JSON 字段的查询语义要在 SQLite 和 Postgres 之间强制一致。**
"字段缺失"必须区别于"字段为 null"，bool 不能匹配 int，float 过滤要接受整数和实数两种 JSON 数字。
这种差异在开发环境（SQLite）测不出来，上了 Postgres 才炸。DeerFlow 为此写了统一的
`json_value_matches` 并用同一套测试跑两个后端。

**4. `runs` 表上的 `owner_worker_id` 和 `lease_expires_at` 两列现在就加。**
即使 M9 才用。加列容易，改所有并发路径难。

### 已知陷阱
- Alembic 的 forward revision 恢复要有预案（见 `docs/database-forward-revision-recovery.md`）
- 序列化的时间戳绑定到 SQL `DateTime` 字段前要做类型强制
- SQLite 需要按连接设置 PRAGMA（WAL、busy_timeout、foreign_keys）

### 验收
重启 Gateway 后历史会话完整；同一套测试在 SQLite 和 Postgres 上都通过。

---

## M5 — 前端（2 周，可与 M4 并行）

> 详细设计：[M5 前端详细设计](m5-frontend-design.md)

### 目标
可用的 Web 聊天界面。

### 模块清单
- Next.js App Router 项目骨架
- LangGraph 官方 `useStream` 接入
- 消息渲染、工具调用卡片、流式 token、思考过程折叠
- 线程侧栏、artifact 预览、文件上传入口
- nginx 配置：`/` → 前端，`/api/*` → Gateway

### 关键设计决策

因为 M3 已经对齐了 Platform 协议，这里工作量主要在 UI 而不是状态管理。

**nginx 上必须关掉 SSE 的压缩和缓冲。** 否则事件会被攒着批量下发，体感上就是"模型不流式了"。
HTML 和文本资源可以压缩，SSE、字体、图片、音视频不要压。

**刷新页面不应该开新 run**，而是对现有 thread 做 join，订阅当前 run 的流
（`useStream(reconnectOnMount)`）。

**统一入口的端口绑定要显式写 loopback。** `"${BIND_HOST:-127.0.0.1}:${PORT:-2026}:2026"`，
裸写 `"${PORT}:2026"` 会绑 `0.0.0.0`。建议写一个测试扫 compose 文件的所有 published port。

### 验收
浏览器完成一次带工具调用的多轮对话；刷新页面对话继续流式而不是重跑。

---

## M6 — 中间件体系（2–3 周）

> 详细设计：[M6 中间件体系详细设计](m6-middleware-design.md)

### 目标
把所有横切能力从 agent 主流程里剥出来，变成可插拔的中间件链。

### 模块清单（按建议落地顺序）

**第一梯队（必做，撞墙顺序）：**
| 中间件 | 解决什么 |
|---|---|
| `SummarizationMiddleware` | 上下文超限自动总结旧消息 |
| `ThreadDataMiddleware` | 初始化 workspace / uploads / outputs 路径 |
| `UploadsMiddleware` | 把上传文件清单注入上下文 |
| `TitleMiddleware` | 首轮后自动生成会话标题 |
| `ToolErrorHandlingMiddleware` | 工具异常不炸整轮 |
| `DanglingToolCallMiddleware` | 清理悬空 tool_call |

**第二梯队（按需）：**
`TodoListMiddleware`（plan mode）、`ViewImageMiddleware`（视觉）、`ClarificationMiddleware`（澄清）、
`ToolOutputBudgetMiddleware`（工具输出预算）、`LoopDetectionMiddleware`（循环检测）、
`TokenBudgetMiddleware`（token 熔断）、`TokenUsageMiddleware`（用量统计）、
`SystemMessageCoalescingMiddleware`、`TerminalResponseMiddleware`、
`ModelLengthFinishReasonMiddleware`、`ReadBeforeWriteMiddleware`、
`InputSanitizationMiddleware`、`ToolResultSanitizationMiddleware`

（DeerFlow 现在有 47 个中间件文件，但它们是按痛点一个个长出来的，不需要一次做完。）

### 关键设计决策

**1. 中间件顺序即语义。** 在一个集中的 `build_middlewares()` 里组装并写注释说明为什么是这个顺序，
不要散落在各处 `append`。

**2. 上下文压缩要同时支持自动和手动。** 自动是中间件在超限时触发；手动是
`POST /api/threads/{id}/compact` 复用同一个中间件写一个新 checkpoint。手动 compact 必须走
M3 那条"每 thread 一个活跃操作"的预约路径。

**3. 视觉支持的图片载荷不要进 graph state。** 在 `wrap_model_call` 里注入，checkpoint 只保留轻量
元数据。否则每次 checkpoint 都要序列化几 MB 的 base64。还要在每次重建前把自己上一次注入的消息
扫出去，避免被中断的 run 在旧 checkpoint 里留下的载荷被反复重发。

### 已知陷阱

1. **悬空 tool_call 几乎必踩**：模型发了 tool_call 但执行被中断，下一轮请求因为缺对应的
   ToolMessage 被 provider 直接拒绝。
2. **工具输出预算**：单个工具返回 100KB 日志会瞬间吃掉上下文。截断要保头保尾并注明被截断。
3. **标题生成要用用户原话**，而不是被上传上下文包装过的那条消息；纯附件消息回退到默认标题。

### 验收
长对话自动压缩后能继续；故意让工具抛异常，对话不中断且模型能看到错误。

---

## M7 — 能力扩展：MCP 与技能（2 周，可与 M8 并行）

> 详细设计：[M7 MCP 与技能详细设计](m7-mcp-skills-design.md)

### 目标
不改代码就能给 agent 加能力。

### 模块清单
- `mcp/manager.py`、`mcp/client.py`、`mcp/cache.py`
- `skills/loader.py`、`skills/parser.py`、`skills/discovery.py`
- `community/`：web_search / web_fetch / scrape / image_search
- `app/gateway/routers/mcp.py`、`skills.py`
- `config/skills_config.py`、`config/tool_search_config.py`

### 关键设计决策

**1. MCP 用 `langchain-mcp-adapters` 的 `MultiServerMCPClient`**，支持 stdio / SSE / HTTP 三种
transport。配置放在运行时可写的 JSON 里，工具列表按文件 mtime 缓存并可通过
`POST /api/mcp/cache/reset` 强制失效。

**2. 技能系统本质是"按需加载的 prompt 片段 + 附带资源"。**

```
skills/
├── public/              # committed
│   └── pdf-processing/
│       └── SKILL.md
└── custom/              # gitignored
```

```markdown
---
name: PDF Processing
description: Handle PDF documents efficiently
license: MIT
allowed-tools: [read_file, write_file, bash]
---

# Skill Instructions
...
```

启动时只把 name + description 注入系统提示（省 token），模型判断需要时才加载全文。
这是投入产出比最高的模块——它让你不改一行代码就能给 agent 加领域能力。

**3. 技能文本一律按 UTF-8 处理**，读写都显式传 `encoding="utf-8"`，不要依赖平台 locale
（Windows 上会是 GBK）。

**4. 工具数量多了要做工具检索。** 当 MCP 工具上百个时，全量塞进 tool schema 会撑爆上下文。
做一个 `tool_search` 工具 + 延迟工具提升（deferred tool promotion）机制。

### 已知陷阱

**运行时可写配置文件的并发写。** 这是 M0 强调分文件的原因：
- 每次读-改-写必须同时持有进程内锁**和**跨进程文件咨询锁，进程内锁在多 worker 下会丢更新
- Docker 把单个文件挂载为独立 mount point 时，Linux 的 `rename()` 会返回 `EBUSY`，
  所以原子写要保留"临时文件 + rename"主路径，只在 `EBUSY` 时退化为就地覆写
- 那个退化路径是**非原子**的（写到一半崩溃会截断文件），只在别无选择时使用，其他 errno 必须抛出

### 验收
配好一个 MCP server 后重启即可用；新增一个技能目录后模型能自动发现并使用。

---

## M8 — 子代理委托（2 周，可与 M7 并行）

> 详细设计：[M8 子代理委托详细设计](m8-subagents-design.md)

### 目标
lead agent 能把子任务丢给独立上下文的子代理，支持后台异步和并发。

### 模块清单
- `subagents/registry.py`：agent 注册表
- `subagents/executor.py`：后台执行引擎
- `subagents/builtins/`：general-purpose、bash 等内置子代理
- `tools/builtins/task_tool.py`、`batch_task_tool.py`、`background_tasks_tool.py`
- `agents/middlewares/subagent_limit_middleware.py`
- `persistence/managed_subagents/`、`persistence/subagent_batches/`

### 关键设计决策

**1. 子代理需要两个 ID，不能合并。** 这是 DeerFlow 明确记录的设计陷阱：

| ID | 用途 |
|---|---|
| provider 的 `tool_call_id` | 对外相关性键：ToolMessage、`task_*` SSE 事件、持久化生命周期事件、前端卡片、扩展契约的 `scope_id` |
| 服务端生成的 `execution_id` | 注册表主键：轮询、取消、超时处理、清理 |

**原因：provider 的 `tool_call_id` 在不同父 run 之间不保证全局唯一**，拿它当注册表所有权键，
并发时会串号。调度闭包要持有自己的结果对象，不要再通过可变注册表解析所有权。

**2. 子代理的 token 归到父 run 上**，且通过消息状态传递（`ToolMessage.additional_kwargs`），
不要用进程级全局缓存。

**3. 子代理**不**占用"每 thread 一条活跃 run"的名额。** 它是 lead 图内部的一次委托，
进度是 root namespace 上的 `custom` SSE 事件，不是新的 `/runs`。

**4. 如果要流式透传子图事件，必须保留 namespace。** 子图帧的 SSE 事件名要是 `values|<ns>`
（LangGraph Platform 风格）而不是裸 `values`。因为被委托的子代理继承了父 checkpoint namespace，
把它的 `values` 快照发成裸 `values` 会让 SDK 客户端用子代理的局部状态**覆盖整个会话视图**。
所有只关心根帧的消费者（文件工具分块合批、子代理事件持久化、LLM 错误降级检测）都要忽略带
namespace 的帧。

### 验收
一个需要 3 个并发子任务的请求能正确完成；中途取消父 run，所有子代理被清理。

---

## M9 — 生产化：多用户、多实例、调度、通道（3–4 周，内部可并行）

> 详细设计：[M9 生产化详细设计](m9-production-design.md)

这一段是"能演示"到"能上线"的距离。四条线相对独立。

### 9A. 多用户与鉴权
- `authz/`：`principal.py` / `rbac.py` / `enforcement.py` / `provider.py` / `tool_filter.py` / `sandbox_authz.py`
- `app/gateway/auth/`：登录、会话、PAT
- `persistence/user/`、`persistence/personal_access_tokens/`
- 文件存储改为按用户分桶：`users/{uid}/threads/{tid}/...`
- IM 通道要把 owner 显式通过 `user_id=` 参数穿透，不能依赖请求上下文

### 9B. 多 Gateway 实例
- run 的 owner 租约 + 心跳 + 孤儿回收
- StreamBridge 换 Redis 实现
- checkpoint cache 换 Redis
- **丢租约（`ownership_lost`）时，收尾阶段的持久化全部跳过**——这个进程不准再写终态；
  由 peer 的孤儿回收标 `error`（`stop_reason=orphan_recovered`）并补发 SSE `end`
- 终态写入带 `status IN ('pending','running')` 守卫，先到的赢

### 9C. 定时任务调度器
- `scheduler/schedules.py`、`app/scheduler/`
- `persistence/scheduled_tasks/`、`persistence/scheduled_task_runs/`
- 前端 `/workspace/scheduled-tasks` 页面

**核心约束：调度器只决定"何时"跑，执行必须复用 M3 的 run 路径。**

occurrence 状态机（**不是** run 状态）：`queued`（持久，重启存活）→ `launching`（短租约声明，
唯一可以调用正常启动路径的状态）→ `running`（已绑定 Gateway run）。

- 唯一约束：每个 task 最多一个非终态 occurrence
- 每个 occurrence 提供稳定的幂等键，恢复后的重试复用同一条 run
- 等待中的行**不**占用 `max_concurrent_runs` 配额，由原子队列声明来强制预算
- 忙时排队而不是跳过（不要重新引入 skip-on-overlap）
- 调度运行是非交互的：`context.non_interactive=true` 时从工具集里去掉 `ask_clarification`，
  **且这个键只对内部认证调用者生效**，客户端传入的一律丢弃

### 9D. IM 通道
- `app/channels/`：Feishu / Slack / Telegram / Discord / DingTalk
- `persistence/channel_connections/`、`persistence/webhook_delivery/`
- 同样收敛到 `start_run()`，差别只在投递策略（等最终回复 / 流式改卡片 / fire-and-forget）

### 9E. MCP 长时任务（可选）
- `mcp_tasks` 表 + 租约轮询的独立运行时
- Agent 只负责 submit，数据库是真相源，`ThreadState` 只收到有界的当前线程投影
- 远端完成后 Gateway 开一次**通知 run** 把结果说回对话

### 验收
两个 Gateway 实例并行，kill 掉持有某个 run 的那个，另一个在租约过期后接管并给客户端补 `end`。

---

## M10 — 记忆、护栏、扩展、可观测性（3–4 周，内部可并行）

> 详细设计：[M10 记忆、护栏、扩展、可观测性详细设计](m10-memory-guardrails-extensions-design.md)

### 10A. 持久记忆
- `agents/memory/manager.py`、`tools.py`、`summarization_hook.py`
- 跨 thread 的事实抽取、存储、检索、容量淘汰
- 淘汰策略要可评测（DeerFlow 用 LongMemEval 做离线基准）

### 10B. 护栏与审计
- `guardrails/`：provider 抽象 + 内置实现 + 中间件
- `agents/middlewares/`：`sandbox_audit_middleware`、`tool_receipt_middleware`、
  `receipt_verification`、`safety_finish_reason_middleware`
- `workspace_changes/`：工作区变更记录、diff、扫描

### 10C. 扩展插件系统
- `extensions/`：加载器、注册表、放置策略、隔离
- 可贡献：中间件、任务生命周期、系统模型观察者、Gateway 服务、FastAPI 路由
- 插件列表放在 `config.yaml` 的顶层 `plugins:`（**故意**不放在 API 可写的 JSON 里——
  这个列表会导致代码被 import，属于运维控制面）
- 每次变更都需要重启；构建钩子和扩展代码都以 Gateway 权限执行，只接受可信来源

### 10D. 可观测性与评测
- `tracing/`：Langfuse / OpenTelemetry
- `scripts/benchmark/`：可复现的离线基准
  - 数据集按不可变 revision + SHA-256 钉死，不允许静默下载
  - 不提交上游数据集文本、凭据、完整 provider 请求
  - 固定时钟、确定性排序、记录所有 revision

---

## 模块 → 阶段 对照表

| DeerFlow 模块 | 阶段 |
|---|---|
| `config/` | M0（核心）+ 各阶段增量 |
| `reflection/`、`models/` | M0 |
| `agents/thread_state.py`、`agents/lead_agent/` | M1 |
| `runtime/checkpointer/` | M1（内存）→ M4（DB） |
| `tools/`、`sandbox/` | M2 |
| `runtime/runs/`、`runtime/stream_bridge/`、`app/gateway/` | M3 |
| `runtime/events/`、`persistence/` | M4 |
| `frontend/`、`docker/nginx` | M5 |
| `agents/middlewares/` | M6 + 持续 |
| `mcp/`、`skills/`、`community/` | M7 |
| `subagents/` | M8 |
| `authz/`、`scheduler/`、`app/channels/`、`app/mcp_tasks/` | M9 |
| `agents/memory/`、`guardrails/`、`extensions/`、`tracing/`、`workspace_changes/` | M10 |

---

## 最容易走错的三个岔路

**1. 先做功能再补架构。** 最典型的是先用一个 `POST /chat` 同步返回结果跑通 demo，然后发现要流式、
要取消、要历史、要并发，最后整个 HTTP 层重写。M3 那一段看起来慢，但它是唯一不能省的。

**2. 把 Gateway 和 agent 框架写在一起。** 看起来省事，但你会失去所有的单元测试能力——测一个
中间件要起 FastAPI。harness / app 的分层不是洁癖，是可测性。

**3. 过早引入 Kubernetes / 微服务。** DeerFlow 到现在也是单体 Gateway 内嵌 agent 运行时，
只有沙箱是可选外置的。分布式的复杂度要等到有真实并发压力再付。

---

## 工程纪律（建议从 M0 起执行）

- **TDD 强制**：每个特性和 bugfix 都带测试。并发相关的代码尤其要有确定性的竞态测试。
- **文档同步**：架构变更同步更新 `AGENTS.md`，用户可见变更同步更新 `README.md`。
- **提交前格式化**：CI 强制 `ruff format --check`。
- **测试分层**：默认离线套件（无网络、无凭据）/ 显式 live 套件 / 阻塞 IO 严格套件。
  异步服务里混入同步阻塞调用是最难查的性能问题，值得单独一条 CI gate。
- **版本号多处锁步**：后端 `pyproject.toml`、前端 `package.json`、Helm Chart 的
  `version` + `appVersion` 必须一致，打 tag 时 CI 校验，不一致就阻断发布。
