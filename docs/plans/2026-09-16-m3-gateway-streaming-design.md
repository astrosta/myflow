# M3 Gateway 与流式详细设计

> 配套文档：[分阶段开发方案](2026-09-16-greenfield-agent-service-roadmap.md) ·
> [M0 地基](2026-09-16-m0-foundation-design.md) · [M1 最小 Agent](2026-09-16-m1-minimal-agent-design.md) ·
> [M2 工具与沙箱](2026-09-16-m2-tools-sandbox-design.md)
>
> 本文细化 M3（2–3 周）。沿用包名：框架层 `agentkit.*`，应用层 `app.*`。

---

## 1. 为什么 M3 是分水岭

M0–M2 做的东西，做错了重写代价是线性的：换个配置系统、换个沙箱实现，改动范围是可枚举的。

M3 不一样。它定义的是**并发模型**——谁能跑、谁在跑、跑到哪了、怎么停。
后面每一个功能（持久化、前端、IM 通道、定时任务、子代理、多实例）都要接进这套模型。
一旦这里留了缺口，后面的每个阶段都会各自打一个补丁，最后变成五套互相不一致的"开 run"逻辑，
取消要写五遍、计费要写五遍、并发限制要写五遍，且永远对不齐。

**M3 的成功标准不是"能流式输出"，而是"后面所有入口都能接进同一条执行栈"。**

### 三周排期

| 周 | 内容 |
|---|---|
| W1 | 对象模型 + `RunManager` 状态机 + `RunStore` 接口（内存实现） |
| W2 | `StreamBridge`（内存实现，带 offset）+ worker + SSE 编帧 |
| W3 | 取消 / 断开 / 幂等 / `/wait` / `/join` + 边界测试 |

### M3 新增的文件

**执行栈在 harness 层，HTTP 在 app 层**——这个划分是 M3 的一条硬边界：

```
agentkit/runtime/                     ← 框架层，不认识 FastAPI
├── runs/
│   ├── manager.py                 ★  # RunManager + RunRecord + 状态机
│   ├── worker.py                  ★  # run_agent
│   ├── schemas.py                    # RunStatus / ThreadOperationKind / CancelOutcome
│   └── store/
│       ├── base.py                ★  # RunStore ABC
│       └── memory.py                 # M3 的实现；M4 加 sql.py
├── stream_bridge/
│   ├── base.py                    ★  # StreamBridge ABC + StreamEvent/StreamGap/哨兵
│   ├── memory.py                     # M3 的实现；M9 加 redis.py
│   └── provider.py                   # 按 config 选后端，懒加载
├── stream_modes.py                   # 公开 mode 名 ↔ LangGraph 内部 mode 名的映射
└── context.py                        # RunContext（打包基础设施依赖）

app/gateway/                          ← 应用层，唯一认识 HTTP 的地方
├── app.py                            # FastAPI 装配 + lifespan
├── deps.py                           # 依赖注入：bridge / run_manager / config
├── run_models.py                  ★  # RunCreateRequest（HTTP 和内部 launch 共用）
├── services.py                    ★  # start_run / sse_consumer / format_sse
└── routers/
    ├── threads.py                    # /state、thread CRUD
    └── thread_runs.py                # /runs、/stream、/wait、/join、/cancel
```

**`start_run` 放在 `services.py` 而不是 router 里。** router 只做参数绑定和响应编码；
调度器和 IM 通道（M9）会直接调 `services.start_run`，它们没有 `Request` 对象。
写进 router 里，M9 就只能选择"伪造一个 Request"或者"复制一份逻辑"——两条路都错。

反过来，**`RunManager` 和 `StreamBridge` 绝不能 import FastAPI**。M1 的嵌入式客户端、
M8 的子代理都要用 `RunManager` 的部分能力，让它依赖 Web 框架就把它们一起拖下水了。
M0 §4 的分层边界测试正是为这条服务的。

---

## 2. 对象模型：五个东西，职责不能混

这一节是整个 M3 的地基。**先把这张表钉死，再写代码。**

| 对象 | 回答什么问题 | 存在哪 | 丢了会怎样 |
|---|---|---|---|
| **Run** | 概念：一次"对话回合" | —— | —— |
| **RunRecord** | 这一轮的状态、属主、取消信号 | 进程内可变对象 | 取消和并发控制失效 |
| **RunStore** | 跨进程/跨重启的真相 | SQL（M4） | 重启后 run 卡在 running |
| **StreamBridge** | 这一轮产出过哪些可重放的帧 | 内存队列 / Redis | SSE 重连拿到 gap |
| **SSE** | 当前这条 HTTP 连接在读哪条队列 | HTTP 响应 | —— |

**最重要的三条边界规则：**

1. **不要用"桥上有没有事件"判断 run 成功没有。** 那是 `RunRecord.status`。
2. **不要把 `RunManager` 当事件总线。** 它不 publish 任何东西。
3. **不要用 SSE 重放当历史。** 缓冲是有界的、会过期的。历史靠 checkpoint 和事件账本。

违反这三条的代码在单机单用户下全都能跑通，然后在第一次刷新页面/断线重连时暴露。

### 执行路径全景

```
HTTP / IM / 调度器 / 内部回调
        │
        ▼
   start_run()                     唯一收敛点：校验 → 准入 → 挂 task
        │  create_or_reject()  ──► RunManager ◄──► RunStore
        │  create_task(worker)                     跨进程真相
        │
        ├──► HTTP 立刻返回 StreamingResponse
        │       sse_consumer 订 StreamBridge，编 SSE 帧
        │
        └──► worker task: run_agent()
                try_start() → 组图 → astream → bridge.publish()
                finally: publish_end()
```

**HTTP handler 和 worker 是两条独立协程。** `await start_run(...)` 只等到"准入 + `create_task`"，
不等 agent 跑完。这是整个设计的核心分叉点。

---

## 3. HTTP 契约：直接对齐 LangGraph Platform

```
POST /api/threads/{id}/runs           准入后立即返回 JSON，agent 后台跑
POST /api/threads/{id}/runs/stream    同上 + HTTP 挂 SSE 直到 event: end
POST /api/threads/{id}/runs/wait      同上 + 阻塞到结束，返回 checkpoint 快照
GET  /api/threads/{id}/runs/{rid}/join  旁观已有 run 的流
POST /api/threads/{id}/runs/{rid}/cancel
GET  /api/threads/{id}/state          读 checkpoint
```

**这么做的唯一理由是省掉整个前端流式状态机。** 对齐之后，浏览器直接用官方
`@langchain/langgraph-sdk` 的 `useStream`，Python 用 `langgraph-sdk`。
自研 wire 格式意味着自研解码器、自研重连、自研消息合并——那是 M5 一整周的工作量，
而且每个 bug 都要自己调。

一个具体的对齐点：SSE 帧的字段顺序必须是 `event` → `data` → `id` → 空行，
这是 SDK 解码器的约定。另一个：`onCreated` 回调**只从 `Content-Location` 响应头抠 `run_id`**，
不从第一条 SSE 事件里读。所以那个头是必需的，不是可选的优化。

### 请求模型

```python
class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")   # SDK 多出的字段直接 422

    assistant_id: str
    input: dict | None = None
    command: Command | None = None              # 人机确认续跑
    config: dict | None = None                  # recursion_limit 等
    context: dict | None = None                 # 运行时开关
    stream_mode: list[str] | None = None
    on_disconnect: Literal["cancel", "continue"] = "cancel"
    multitask_strategy: Literal["reject", "interrupt", "rollback"] = "reject"
```

**`extra="forbid"` 不能省。** SDK 版本升级会带上新字段（比如 `stream_resumable`），
静默丢掉的话，客户端以为自己开启了某个特性，服务端其实没实现——这类 bug 表现为
"偶尔行为不对"，极难定位。明确 422 让不兼容立刻可见。

**HTTP 请求模型和内部 launch 用同一份。** 调度器、IM 通道调 `start_run` 时构造的也是这个对象。
两份模型会漂移。

---

## 4. `start_run`：唯一收敛点（W1）

所有入口——浏览器、IM、调度器、MCP 通知、子代理完成回调——都走这一个函数。
它们的差别只在**传进来的 context**和**要不要挂 SSE**。

> **绝不为新入口开第二条执行栈。** 这是 M3 最重要的一条纪律。

### 4.1 执行顺序本身就是设计

顺序不是随意的：**所有校验在前，所有副作用在后**。任何一步失败时，
前面的步骤都不能已经产生外部可见的状态。

```
 1. 取消能力门        multitask_strategy != reject 需要 runs:cancel 权限
 2. 校验 thread_id、拒绝 metadata 夹带密钥
 3. 规范化 stream_mode
 4. 模型白名单        context.model_name 必须能在 config 里解析到，否则 400
 5. Thread 属主检查    别人的 thread → 404（不是 403，防枚举）
 6. 解析 agent 工厂    resolve_agent_factory(assistant_id)
 7. 规范化 input       dict → LangChain message；剥掉调用方伪造的 server-owned 字段
 8. Trace id          覆盖客户端自带的值
 9. build_run_config  写入 configurable.thread_id（来自 URL，不信 body）；钳制 recursion_limit
10. checkpoint 选择    编辑/再生成会带 checkpoint_id
11. merge_run_context 只放行白名单键
12. 注入认证用户       user_id 来自服务端 auth，不来自 body
13. create_or_reject  ──► 得到 pending RunRecord
14. create_task(worker)
```

第 5 步用 404 而不是 403：403 等于告诉攻击者"这个 thread 存在但不属于你"，
可以用来枚举系统里有哪些会话。

第 9 步的 `thread_id` 来自 URL 而不是 body，因为它是 checkpointer 的分区键。
信 body 意味着调用方可以把自己的消息写进别人的会话。

### 4.2 第 13 步和第 14 步之间不能有任何 `await`

**这是全文最值得写进代码注释的一行。**

```python
record = await run_manager.create_or_reject(...)
# ⚠️ 这两行之间不能有任何 await。
# 窗口里到达的取消请求会卡在补 thread metadata 的 IO 上，
# 而 worker 还没挂上，SSE 订阅者永远等不到 publish_end。
record.task = asyncio.create_task(run_after_metadata(record))
```

诱惑很大：准入成功了，顺手把 thread metadata 更新一下再挂 worker，看起来很自然。
但 `create_or_reject` 已经在唯一索引上占了位——从这一刻起，这条 thread 对外就是"忙"的。
如果在挂 worker 之前 `await` 了任何 IO，这个窗口里到达的取消请求会找到一条
**没有 task 的 pending 记录**，取消不掉，也没人负责发 `publish_end`。前端永远转圈。

正确做法：把 metadata 更新放进 worker 协程的第一步（`run_after_metadata`），
让它在 worker 内部执行、可被 abort 打断。

### 4.3 `create_task` 失败必须把 pending 标成 error

```python
try:
    record.task = asyncio.create_task(run_after_metadata(record))
except BaseException:
    await run_manager.fail_start_if_pending(record.run_id)
    raise
```

不做这一步，唯一索引上会留一行永远 pending 的僵尸记录，**那条 thread 从此再也开不了新 run**，
只能手工改库。而这个分支几乎不可能在正常测试里覆盖到。

### 4.4 客户端传进来的 context 要过白名单

```python
_ALLOWED_CONTEXT_KEYS = {
    "model_name", "mode", "thinking_enabled", "reasoning_effort",
    "is_plan_mode", "subagent_enabled", "agent_name",
}
_INTERNAL_ONLY_KEYS = {"non_interactive"}   # 只有 internal 调用方能设
```

两个规则：

**`__` 前缀的私有通道键要丢掉。** LangGraph 用这个前缀传内部状态，
客户端能塞进去就等于能操纵图内部。

**不是所有键都该进 `configurable`。** `configurable` 会被写进 checkpoint 并持久化。
临时凭据（比如一次性的 GitHub token）只能进 `context`，进 `configurable` 就等于写进了数据库。

---

## 5. RunManager（W1）

它是**登记处**，不是执行器。图执行在 worker 里，SSE 在 StreamBridge 里。

### 5.1 双层存储

| 层 | 存什么 | 生命周期 |
|---|---|---|
| 内存 `_runs` + `_runs_by_thread` | `task`、`abort_event`、`start_lock`、token 计数 | 进程内，结束后延迟 GC |
| `RunStore` | 可序列化字段：status、kwargs、token 汇总 | 跨重启、跨进程 |

三条规则：

**同 `run_id` 内存优先。** `get()` 先看 `_runs`，没有才从 store hydrate。
hydrate 出来的记录标 `store_only=True`，没有本地 `task`。
如果顺序反了，一个 live run 会被换成一份没有 `task` 的快照——取消就失效了。

**创建必须先写 store 再进内存。** 写 store 失败要回滚内存，避免"内存里有、库里没有"。

**`_runs` 和 `_runs_by_thread` 必须在同一次持锁、中间不 await 里一起改。**
否则按 thread 列 run 会漏掉刚创建的那条。

`get()` 是 async（要访问 store），所有调用都得 `await`。这个签名现在定下来，
M4 接 SQL 时不用改所有调用点。

#### `RunRecord` 的三组字段

```python
@dataclass
class RunRecord:
    # --- 第一组：可序列化，会进 RunStore ---
    run_id: str
    thread_id: str
    assistant_id: str | None
    status: RunStatus
    on_disconnect: DisconnectMode
    operation_kind: ThreadOperationKind = ThreadOperationKind.run
    multitask_strategy: str = "reject"
    metadata: dict = field(default_factory=dict)
    kwargs: dict = field(default_factory=dict)      # {"input": ..., "config": ...}，密钥已脱敏
    user_id: str | None = None
    model_name: str | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    idempotency_key: str | None = None

    # --- 第二组：进程内控制，绝不序列化 ---
    task: asyncio.Task | None = field(default=None, repr=False)
    abort_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    abort_action: str = "interrupt"
    start_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    finalizing: bool = False                        # 见 §5.2：不是状态机上的一档
    store_only: bool = False                        # 从 store hydrate 出来的，没有本地 task
    idempotency_reused: bool = False                # 见 §5.7：这个 caller 不许再挂 worker

    # --- 第三组：统计 ---
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    llm_call_count: int = 0
    lead_agent_tokens: int = 0                      # 归因维度，见下
    subagent_tokens: int = 0
    middleware_tokens: int = 0
    token_usage_by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    # --- M9 预留，M3 恒为默认值 ---
    owner_worker_id: str | None = None
    lease_expires_at: str | None = None
    ownership_lost: bool = False
    stop_reason: str | None = None
```

**`repr=False` 不是美观问题。** `task` 和 `abort_event` 会出现在任何异常回溯和日志里，
`asyncio.Task` 的 repr 包含整个协程栈。一条 error 日志会变成几十行噪音。

**`start_lock` 看起来多余，但要有。** 它序列化"一条已准入的 run 被交给了不止一个 worker 路径"
这种情况的启动。M3 里 `start_run` 保证只挂一个 task，所以它永远不会被争用；但 M9 的接管路径
和 M3 的幂等复用路径都可能触碰同一条记录。**一把从不被争用的锁的成本是零，而缺了它的
症状是同一个 run 跑两遍。**

**token 归因的三个维度现在就要定。** `lead_agent` / `subagent` / `middleware` 看起来 M3 用不上
（M8 才有子代理、M6 才有中间件调模型），但这是个**加起来必须等于总数**的分解。等到 M8 再
引入维度，历史数据就永远缺这一列，而报表要么少算要么对不上。同理
`token_usage_by_model` 是按模型名分桶——M6 的总结中间件会用另一个模型，不分桶就无法计价。

#### `RunStore` 接口：M3 要实现的十个方法

```python
class RunStore(abc.ABC):
    # --- 准入：唯一一个必须原子的方法 ---
    @abc.abstractmethod
    async def create_thread_operation_atomic(
        self, record: dict, *, kind: ThreadOperationKind
    ) -> bool:
        """在 (thread_id) WHERE status IN ('pending','running') 的部分唯一索引上抢位。
        返回 False 表示该 thread 已有活跃操作。不要抛 IntegrityError 给上层。"""

    # --- 状态迁移 ---
    @abc.abstractmethod
    async def start_run(self, run_id: str) -> bool:
        """pending -> running 的 CAS。返回 False 表示已不是 pending（多半被取消了）。"""

    @abc.abstractmethod
    async def update_status(self, run_id: str, status: RunStatus, *, error: str | None = None) -> None:
        """必须带 status IN ('pending','running') 守卫：终态行不接受后到的写入。"""

    @abc.abstractmethod
    async def update_run_completion(self, run_id: str, *, status: RunStatus, **totals) -> None:
        """终态 + token 汇总一次写完。拒绝替换一个**不同的**终态。"""

    # --- 读 ---
    @abc.abstractmethod
    async def get(self, run_id: str, *, user_id: str | None) -> dict | None: ...
    @abc.abstractmethod
    async def list_by_thread(self, thread_id: str, *, user_id: str | None, limit: int = 100) -> list[dict]: ...
    @abc.abstractmethod
    async def list_inflight(self) -> list[dict]:
        """启动时回收孤儿用：所有 pending/running 的行。"""

    # --- 删 ---
    @abc.abstractmethod
    async def delete_thread_operation(self, run_id: str, *, user_id: str | None) -> None:
        """释放非 run 的短命预留行（compact 等）。"""

    # --- 幂等 ---
    @abc.abstractmethod
    async def get_by_idempotency_key(self, scoped_key: str) -> dict | None: ...

    # --- M9 预留：M3 不实现，但签名现在定 ---
    # renew_lease / claim_for_takeover / request_cancel /
    # finalize_if_not_cancelled / list_inflight_with_expired_lease
```

**三个签名细节值得现在就定死：**

**一、准入方法返回 `bool`，不抛 `IntegrityError`。** 唯一索引冲突是**预期内的正常结果**
（用户手快点了两次发送），不是异常。让它冒到路由层意味着每个调用点都要 catch 一个
数据库特定的异常类型——而 SQLite 和 Postgres 抛的还不是同一个。

**二、所有读方法都带 `user_id` 参数。** M3 只有单用户，传 `None` 即可。但这个参数位现在留着，
M9 加多租户时改的是实现，不是几十个调用点。更关键的是**默认值不能是 `None`**——
`user_id: str | None` 但不给默认值，强迫每个调用点显式表态"我这里确实要全局查"。
给了默认值，M9 时会有一半调用点在无声地跨用户查询。

**三、`update_status` 和 `update_run_completion` 分开。** 前者是中途的状态变更，后者是终态
加汇总的一次性写入。合并成一个方法会让"终态守卫"这条规则难以表达——中途变更允许覆盖，
终态不允许。

### 5.2 状态机只有六个状态

```
            create_or_reject
                    │
                    ▼
                pending  ──try_start──►  running
                    │                       │
                    ▼                       ▼
          interrupted / error    success / error / timeout / interrupted
```

**`finalizing` 不是状态机上的一档。** 它是 `RunRecord` 上的一个布尔标志，
表示 worker 在 `finally` 里收尾（写标题、同步 thread 状态），
此时它**仍然占着"每 thread 一条活跃"的名额**。

把它做成一个状态会污染所有 `status IN (...)` 的查询，而它的唯一作用是让下一个 run
的 `wait_for_prior_finalizing` 知道要等。

另一个要点：**thread 元数据的 `running` 只在 `try_start` 成功之后才写。**
pending 阶段被取消的话，侧栏不该先闪一下 busy 再改回来。

### 5.3 唯一约束的粒度是"thread 操作"，不是"run"

```python
class ThreadOperationKind(StrEnum):
    RUN = "run"
    CHECKPOINT_WRITE = "checkpoint_write"   # compact、POST /state
    ARTIFACT_WRITE = "artifact_write"
    BRANCH = "branch"
    DELETE = "delete"
```

所有 kind 共享 store 上的同一个**部分唯一索引**：

```sql
CREATE UNIQUE INDEX ON runs (thread_id) WHERE status IN ('pending', 'running');
```

**M3 只用到 `run` 这一个 kind，但枚举和索引现在就要建对。**
理由：这些操作全都会写同一条 thread 的 checkpoint。以后加 compact 时如果另起一把锁，
compact 和对话就能并发写 checkpoint，结果是静默的状态损坏——最难查的那类 bug。

配套的两件事：

- 非 run 的操作用 `reserve_thread_operation(thread_id, kind=...)` 上下文管理器：
  进去插一条短命 pending 行，出来删掉。
- **列表 API 要过滤掉 `operation_kind != 'run'` 的行。** 用户不该在"对话历史"里
  看到一条 compact 记录。

### 5.4 准入：只用 `create_or_reject`

不要提供一个裸的 `create()`（只做 put）。并发下它会变成生的 `IntegrityError` 冒到路由层，
用户看到 500 而不是 409。

**锁顺序是这里的不变量：握住进程锁，做完本地检查 + store insert + 登记内存，再放锁。**
如果 store insert 在锁外，会出现"store 里已经插入了，本进程却返回 ConflictError 把调用方打回去"，
留下一行谁也不认领的幽灵 pending。

跨进程靠唯一索引，不靠这把进程锁——进程锁只是为了让本进程的三步保持原子。

### 5.5 `multitask_strategy`

决定"下一次准入怎么对待还活着的这一次"：

| 策略 | 对旧 run | 对新 run |
|---|---|---|
| `reject`（默认，Web 发送用） | 不动 | 不开，调用方 409 |
| `interrupt` | 标 `interrupted`，保留 checkpoint | 旧的让位后开始 |
| `rollback` | 同上，但状态滚回 run 前 | 同上 |

`interrupt`/`rollback` 的顺序是：**先原子插入新行，再去标记旧 run**。
反过来做，两者之间的窗口里这条 thread 是"空闲"的，第三个请求能挤进来。

还有一个边角要处理：标记旧 run 之后如果调用方自己被取消了，
要用 `shield` 把刚准入、还没人看见的新 run 收成终态，否则它会永远挂在 pending。

### 5.6 `try_start`：启动栅栏

worker 在**组装 agent 之前**调用，是 pending → running 的唯一入口。

```
abort 已置位 或 status != pending  →  返回 cancelled（不组图）
store.start_run() CAS             →  False 则当作已取消
再检查一次 abort                    →  才把内存标 running
```

**"再检查一次 abort"不是冗余。** store 的 CAS 是一次网络/磁盘往返，
这期间到达的取消请求会设置 `abort_event`。不复查的话，一个已经被取消的 run 会开始组图、
调模型、烧 token，然后才在第一轮 astream 里发现自己该停了。

`start_run` 里"准入和挂 task 之间不许 await"的纪律，配合这个栅栏，
保证了 pending 阶段的取消能在**模型调用之前**生效。

### 5.7 幂等键必须加 scope

```python
scoped_key = hash(f"{owner_id}:{thread_id}:{raw_key}")
```

store 上的幂等索引是**进程全局**的。直接用客户端传来的 `Idempotency-Key`，
两个不同用户用了同一个字符串（比如 `"1"`）就会串号——A 的重试拿到 B 的 run。

命中已有 key 时返回那条记录并设 `idempotency_reused=True`。
**调用方看到这个标志就不能再 `create_task`**，否则同一个 run 会跑两遍。

如果重试的 `input` 和第一次不一致，路由层返回 409——这说明客户端复用了 key 但改了内容，
是 bug 不是重试。

### 5.8 结束后延迟 GC

```python
await run_manager.cleanup(run_id, delay=300)
```

run 结束后不要立刻从内存注册表摘掉。晚到的 `/join` 请求（用户刚好在这时刷新页面）
需要拿到 live 引用才能读到完整的结束状态。5 分钟是个够用的经验值。

---

## 6. Worker（W2）

### 6.1 阶段

```
run_after_metadata(record)      尽量补 thread_meta（可被 abort 打断）
    └─► run_agent(bridge, run_manager, record, ctx)
         1. wait_for_prior_finalizing   上一个 run 还在收尾就等
         2. try_start                   失败则走取消收尾并 return
         3. thread_meta → running
         4. 捕获 rollback point          取消-rollback 的还原点
         5. publish("metadata", {...})  给 useStream
         6. 组装 agent（M1 的 assemble_agent）
         7. agent.astream(...)          chunk → serialize → publish
                                        每轮检查 abort_event
         8. 成功路径：标题生成等
         9. finally: ★ publish_end ★
```

第 6 步的 `RunContext` 用一个 dataclass 打包基础设施（checkpointer、thread_store、
event_store、回调），不要让 `run_agent` 的参数列表随阶段增长无限变长。
M4–M10 每个阶段都会往里加东西。

### 6.2 `finally` 里必须 `publish_end`

**这是 worker 对 SSE 层的硬义务，没有例外：成功、失败、取消、启动栅栏没组图，全都要发。**

漏掉的后果是前端永远转圈。而这个 bug 的恶劣之处在于**它只在异常路径上出现**——
正常流程的测试百分百通过，只有在模型报错、沙箱超时、用户取消这些分支上才暴露。

所以测试策略上要专门写一组"注入各种失败，断言 END 一定到达"的用例。

#### 骨架：`publish_end` 要在最内层的 finally

```python
async def run_agent(bridge, run_manager, record, ctx: RunContext) -> None:
    run_id = record.run_id
    try:
        await run_manager.wait_for_prior_finalizing(record.thread_id)

        if await run_manager.try_start(run_id) is RunStartOutcome.cancelled:
            return                                    # 不组图。END 由 finally 负责

        await ctx.thread_store.set_status(record.thread_id, "running")
        rollback_point = await _capture_checkpoint_head(ctx, record.thread_id)

        await bridge.publish(run_id, "metadata", {"run_id": run_id, "thread_id": record.thread_id})

        assembly = assemble_agent(ctx.run_config, app_config=ctx.app_config)

        async for mode, chunk in assembly.graph.astream(
            ctx.graph_input, config=ctx.run_config, stream_mode=ctx.stream_modes
        ):
            if record.abort_event.is_set():           # ★ 每一轮都查
                raise RunAborted(record.abort_action)
            for event, data in translate_chunk(mode, chunk):
                await bridge.publish(run_id, event, data)

        terminal, error = RunStatus.success, None

    except RunAborted as aborted:
        terminal, error = RunStatus.interrupted, None
        if aborted.action == "rollback":
            await _restore_checkpoint(ctx, record.thread_id, rollback_point)
    except asyncio.CancelledError:
        terminal, error = RunStatus.interrupted, None
        raise                                          # 不吞，但终态要写
    except Exception as exc:
        terminal, error = RunStatus.error, str(exc)
        logger.exception("Run %s failed", run_id)

    finally:
        record.finalizing = True
        try:
            # 收尾可能很多：写终态、标题生成、token 汇总、事件账本落盘（M4）。
            # 这里面任何一步都可能抛。
            await _finalize(run_manager, record, ctx, terminal, error)
        finally:
            # ★ 无论收尾成功与否，流必须结束。
            await bridge.publish_end(run_id)
            record.finalizing = False
```

**`publish_end` 放在嵌套的内层 `finally`，不是和收尾逻辑并列。** 这是全节最重要的一行结构。
收尾要做的事会随阶段增长——M4 加事件账本落盘、M6 加标题生成、M9 加租约释放——**其中任何
一个新增的 IO 抛异常，都会在"并列写法"下把 `publish_end` 跳过去**。而这类回归极难发现：
它只在那个新增步骤失败时触发，而那个步骤失败本身就是个罕见分支。

嵌套写法把这条不变量变成了结构性的，不依赖后来的人记得把 `publish_end` 放在最后。

**`CancelledError` 要写终态再 `raise`，不能吞。** 吞掉它会让上层的 `asyncio.gather`、
lifespan 的排空逻辑以为这个 task 正常结束了。但也不能只 `raise` 不写终态——那样 store 里
会留一行永远 running。

**`try_start` 返回 cancelled 时直接 `return`，不是 `raise`。** 这条路径是"这个 run 在组图之前
就被取消了"，是预期内的正常流程。`finally` 仍然会跑，`publish_end` 仍然会发——订阅者收到
一个只有 metadata 和 end 的流，这是对的。

#### chunk 到 SSE 事件的翻译

`translate_chunk` 是 M1 §6.2 那套流式契约在服务端的落地，**两边的事件名必须一致**：

| LangGraph 内部 mode | 发出的 SSE `event` | 说明 |
|---|---|---|
| `values` | `values` | 状态快照 |
| `messages` | `messages` | 前端 SDK 认这个名；公开 API 参数名叫 `messages-tuple` |
| `updates` | `updates` | 节点级增量 |
| `custom` | `custom` | `StreamWriter` 转发 |

**公开参数名和 wire 事件名不是一回事**，`stream_modes.py` 负责这层映射：请求里客户端写
`messages-tuple`，映射到 LangGraph 的 `messages`，而 SSE 帧上的 `event:` 是 `messages`。
三个名字听起来该统一，但前两个是 LangGraph Platform 已经定下的协议，改了就用不了官方 SDK。
**把映射关进一个文件，别让它散进 router。**

客户端传了不支持的 mode（`events`、或者内部名 `messages`）要 **422 拒绝，不要静默替换成
`values`**。静默替换的症状是前端收到完全不同结构的数据，而它以为自己订的是增量。

### 6.3 关机排空

进程退出时，lifespan 要先给 inflight task 设 abort、`task.cancel()`，
**在超时内等它们把最后一次 checkpoint 写完，再关 checkpointer 连接池**。

顺序反了的话，LangGraph 内部的 `aput` 会打到已关闭的连接池，
产生一个关不掉的异常循环，进程无法正常退出。

---

## 7. StreamBridge（W2）

### 7.1 为什么需要这层

**唯一的理由：创建 run 的那个 HTTP 连接，和消费 SSE 的那个连接，不一定是同一个。**

刷新页面、多标签页、断线重连、`/join` 旁观、IM 通道在进程内订阅——
这些场景下生产者和消费者完全解耦。没有这层，它们就只能耦合在同一个调用栈上。

（反过来，M1 的嵌入式 client **不用** StreamBridge：生产者和消费者在同一个调用栈，直接 `yield`。）

### 7.2 协议

```python
class StreamBridge(abc.ABC):
    supports_cross_process: bool = False       # memory=False, redis=True

    async def publish(self, run_id, event, data) -> None: ...
    async def publish_end(self, run_id) -> None: ...
    def subscribe(self, run_id, *, last_event_id=None, heartbeat_interval=None
                  ) -> AsyncIterator[StreamEvent | StreamGap]: ...
    async def cleanup(self, run_id, *, delay=0) -> None: ...
```

四种控制对象，**不要和业务 SSE 事件名混淆**：

| 对象 | 含义 | 订阅方看到 |
|---|---|---|
| `StreamEvent` | 一条可重放事件（`id` / `event` / `data`） | `event:` + `data:` + `id:` |
| `HEARTBEAT_SENTINEL` | 空闲超过心跳间隔 | 注释行 `: heartbeat`，**无 id** |
| `END_SENTINEL` | producer 调了 `publish_end` | `event: end` |
| `StreamGap` | 游标已不在保留窗口内 | `event: gap`，**无 id**，然后停 |

`id` 单调递增，用作 SSE 的 `id:`，重连时客户端带 `Last-Event-ID` 回来。
**heartbeat 和 END 的 id 是空串，不能当续传游标。**

### 7.3 缓冲有界，落后的订阅者拿 gap 然后停

```yaml
stream_bridge:
  queue_maxsize: 256              # 每 run 保留的数据事件数
  heartbeat_interval_seconds: 15
```

超出上限时从头部删，同时 watermark（`start_offset`）前进。
落后于 watermark 的订阅者收到 `StreamGap` 后**订阅立即停止**。

**这是一个刻意的选择：宁可明确告诉客户端"你缺了一段"，也不要吐半截重放假装完整。**
部分重放会让 `useStream` 拼出损坏的消息列表——比如一条 AI 消息只有后半截 token，
看起来像模型输出了乱码。客户端收到 gap 的正确反应是 `reload_durable_state`
（拉 `/state` 和事件账本），而不是接着往下拼。

注意 **gap 不影响 run 本身**，它继续跑。gap 只是这条订阅的终结。

#### `StreamGap` 要带三个 id，不是一个光秃秃的标记

```python
@dataclass(frozen=True)
class StreamGap:
    requested_event_id: str | None            # 客户端请求的游标，或落后订阅者最后收到的那条
    earliest_available_event_id: str | None   # 当前保留窗口下界
    latest_available_event_id: str | None     # 当前保留窗口上界
```

三个都要，因为**两类消费者对 gap 的正确反应完全不同**：

| 消费者 | 收到 gap 后 | 用哪个字段 |
|---|---|---|
| `sse_consumer`（浏览器） | 发一帧 `event: gap`，`recovery: reload_durable_state`，然后结束订阅 | 三个都透传，供客户端诊断 |
| `wait_for_run_completion`（`/wait`） | **从保留窗口的尾部重新订阅**，继续等 END | `latest_available_event_id` |

`/wait` 不需要完整重放，它只想知道什么时候结束。如果 `StreamGap` 只是个光秃秃的标记，
`/wait` 就没有安全的续订点，只能把 gap 当成失败返回——而 run 其实还在正常跑。

缓冲为空时三个字段都是 `None`（比如 run 刚创建、一条事件都还没发）。消费者要能处理这种情况：
`/wait` 此时用 `None` 续订，等价于"从头订"，是对的。

### 7.4 内存实现的游标规则

| 情况 | 行为 |
|---|---|
| 无 cursor | 从当前 `start_offset`（最早还在的事件）开始 |
| cursor 低于 watermark | gap |
| cursor 在窗口内且对得上 | 从下一条开始 |
| 窗口内但 id 未知 | 从最早保留事件重放 |
| 活订阅者被裁到 watermark 后面 | 同样 gap 然后停 |

第二行值得说明：即使 cursor 的数字看起来合法，只要它低于 watermark 就必须 gap。
被裁掉的事件已经无法核对，不能假设它就是客户端想要的那一条。

### 7.5 M3 只做内存实现，但接口要能容纳 Redis

`supports_cross_process` 这个标志现在就要有。内存桥上，一个跑在别的进程里的 run
（`store_only`）是订不到的——路由必须 409，不能让客户端订一条空流死等。
M9 换 Redis 时这个标志变 True，路由逻辑一行不用改。

同样地，`redis` 包是可选依赖，**不要在 `stream_bridge/__init__.py` 里 eager import**。
工厂按 `type` 懒加载分支。

---

## 8. SSE 层（W2）

### 8.1 帧格式

```
event: metadata
data: {"run_id":"...","thread_id":"..."}
id: 1

event: messages
data: {...}
id: 2

: heartbeat

event: end
data: {}
```

字段顺序 `event` → `data` → `id` → 空行，是 SDK 解码器的约定。

响应头（创建端）：

```
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
Content-Location: /api/threads/{tid}/runs/{run_id}
```

`X-Accel-Buffering: no` 是给 nginx 看的。不加的话 nginx 会把整段响应缓冲起来批量下发，
用户体感就是"模型不流式了"。**nginx 侧还要确认 `gzip_types` 不含 `text/event-stream`**——
压缩同样会造成攒批。

跨源部署时 `Content-Location` 必须出现在 CORS 的 `Access-Control-Expose-Headers` 里，
否则 SDK 读不到 `run_id`。

#### 编帧函数

```python
def format_sse(event: str, data: Any, *, event_id: str | None = None) -> str:
    """字段顺序 event -> data -> id -> 空行，对齐 LangGraph Platform 的 wire 格式。"""
    frame = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n"
    if event_id:
        frame += f"id: {event_id}\n"
    return frame + "\n"
```

`default=str` 是必需的兜底：`data` 里会混进 `datetime`、`UUID`、LangChain 的消息对象。
一次序列化失败会让整条流在中间断掉，而且**异常发生在生成器里，HTTP 响应头早就发出去了**——
客户端看到的是一个突然截断的流，没有任何错误信息。宁可让某个字段变成它的 `str()` 形式。

#### 消费循环

```python
async def sse_consumer(bridge, record, request, run_mgr, *, apply_on_disconnect: bool = True):
    last_event_id = request.headers.get("Last-Event-ID")
    try:
        async for entry in bridge.subscribe(record.run_id, last_event_id=last_event_id):
            if await request.is_disconnected():        # ★ 每一项都查，包括心跳
                break

            if isinstance(entry, StreamGap):
                yield format_sse("gap", {
                    "code": "stream_replay_gap",
                    "run_id": record.run_id,
                    "requested_event_id": entry.requested_event_id,
                    "earliest_available_event_id": entry.earliest_available_event_id,
                    "latest_available_event_id": entry.latest_available_event_id,
                    "recovery": "reload_durable_state",
                })
                return                                  # gap 之后不再续传

            if entry is HEARTBEAT_SENTINEL:
                yield ": heartbeat\n\n"                 # 注释行，无 event 无 id
                continue

            if entry is END_SENTINEL:
                yield format_sse("end", None)
                return

            yield format_sse(entry.event, entry.data, event_id=entry.id or None)
    finally:
        if apply_on_disconnect and record.status in (RunStatus.pending, RunStatus.running):
            if record.on_disconnect == DisconnectMode.cancel:
                await run_mgr.cancel(record.run_id)
```

#### 心跳的真实用途不是保活

这一点很容易误解。心跳的常见理由是"防止中间代理掐掉空闲连接"——那确实是收益之一，
但**在这套设计里它有个更硬的作用：给消费循环一个醒来的机会去检查客户端是否断开。**

`request.is_disconnected()` 只能在循环体里调用。如果模型思考了 90 秒一个 token 都没吐，
没有心跳的话 `async for` 会在 `subscribe` 里一直挂着，这 90 秒内客户端早就关了页面，
服务端却毫不知情，继续烧 token。心跳把"最坏情况下多久能发现断开"从**无上限**压到了
`heartbeat_interval`。

这也是为什么 `/wait` 也要订阅心跳——它不渲染任何东西，纯粹是为了周期性检查断开。

**心跳帧是 SSE 注释行（`:` 开头），不是事件。** 它没有 `event:` 也没有 `id:`，SDK 的解码器
会直接忽略。给它一个 id 是个真实的 bug：客户端会把心跳的 id 记成 `Last-Event-ID`，
重连时带回来，而这个 id 在缓冲里根本不存在。

#### `finally` 里的断开处理有个前置条件

注意 `record.status in (pending, running)` 这个判断。没有它，一个**正常结束**的 run
也会在客户端断开时走进 `cancel()`——因为 `finally` 对正常 `return` 同样执行。
虽然 `cancel` 对终态记录是幂等的（返回 `not_cancellable`），但它会产生一条误导性的
"取消失败"日志，并且在 M9 多实例下会白白打一次 store。

### 8.2 创建端和观察端的断开语义不同

```python
sse_consumer(bridge, record, apply_on_disconnect: bool)
```

| 入口 | `apply_on_disconnect` | 理由 |
|---|---|---|
| `POST /runs/stream` | `True` | 用户关了页面，按 `on_disconnect` 策略取消 |
| `POST /runs/wait` | `True` | 同上，创建端语义 |
| `GET /runs/{id}/join` | **`False`** | 只读观察者断开不能取消别人的 run |

第三行是个安全边界：如果 join 也能触发取消，那么一个只有 `runs:read` 权限的令牌
只要连上再断开就等于执行了取消——权限模型被绕过。

### 8.3 `/wait` 必须订阅 StreamBridge，不能 `await record.task`

诱惑很明显：`/wait` 要等 run 结束，而 `record.task` 就是那个 task，直接 await 不就行了？

不行，两个原因：

**`on_disconnect` 语义只活在创建该 run 的 worker 内存里。** `await task` 拿不到客户端断开事件，
用户关掉连接后 run 还在烧钱。

**多进程下 `record.task` 可能是 `None`**（`store_only`）。订桥则在 Redis 实现下天然跨进程。

`/wait` 的订阅者只关心 END，但有个细节：**遇到 gap 时不要当成断开，
而是从最新可用事件 id 续订继续等 END**。它不需要完整重放，只需要知道什么时候结束。

还有一条硬规则：**客户端断开导致的返回，绝不能把半截 checkpoint 当成功响应返回。**

---

## 9. 取消（W3）

### 9.1 协作式，路由层绝不直接 `task.cancel()`

```python
async def cancel(self, run_id: str, action: Literal["interrupt", "rollback"]) -> CancelOutcome:
    record.abort_action = action
    record.abort_event.set()
    # 必要时再 task.cancel()，但必须走这条路径
```

worker 在图的每一轮和 `try_start` 之前检查 `abort_event`。

**路由层直接 `task.cancel()` 会让 store 上的 `cancel_action` CAS 对不上**——
状态写进去了但没人知道该 rollback 还是 interrupt，而且绕过了所有终态写入的守卫。

两种 action：

- `interrupt`：保留已写进 checkpoint 的内容
- `rollback`：回滚到本 run 开始前的 checkpoint（worker 第 4 步捕获的那个点）

### 9.2 `CancelOutcome` → HTTP 映射由路由查表，不要自己猜

| 结果 | 含义 | HTTP |
|---|---|---|
| `cancelled` | 本进程执行了取消 | 200 |
| `not_active_locally` | 记录不在本进程内存里 | 409 |
| `not_cancellable` | 已经是终态 | 4xx |
| `unknown` | 找不到或 store 失败 | 404 |
| `requested` | （M9）非 owner，已写 durable cancel | 202 |
| `taken_over` | （M9）租约过期，本进程 claim | 按接管处理 |

M3 只会产生前四种，但**枚举现在就定全**。后两种是多进程才有的结果，
等到 M9 再加枚举值意味着所有 `match` 分支都要回头补。

### 9.3 取消和完成会抢同一行

`request_cancel` 和 `finalize_if_not_cancelled` 都是对活跃行的 CAS：
**已经被接受的取消，不能被后到的 success 覆盖掉。**

`update_status` 要带 `status IN ('pending','running')` 守卫——
已经是终态的行，后到的写入进不去。

---

## 10. 为 M4 / M9 预留的位

这些**现在不实现，但接口/字段现在就留**。加字段容易，改所有并发路径难。

| 预留 | 为什么现在留 |
|---|---|
| `runs.owner_worker_id` / `lease_expires_at` 两列 | M9 多实例。加列容易，改并发路径难 |
| `operation_kind` 枚举 + 部分唯一索引 | 见 §5.3。后加等于放任并发写 checkpoint |
| `RunRecord.ownership_lost` 标志 | M9：丢租约后本进程禁止再写终态，见 [M9 §3.1](2026-09-16-m9-production-design.md) |
| `StreamBridge.supports_cross_process` | M9：路由据此决定 409 还是订阅 |
| `RunStore` 抽象接口 | M4 换 SQL 时只换实现 |
| `CancelOutcome` 的六个枚举值 | 见 §9.2 |
| 事件账本（`run_events`）的 publish 钩子位 | M4。SSE 和账本是两条并行的管子，不是同一条 |

最后一条值得强调：**SSE 和持久化事件账本不是同一条管子。**
SSE 是有界的、会过期的直播；账本是完整的、用于历史回放和审计的记录。
用 SSE 缓冲当历史，页面刷新后就什么都没有了。

---

## 11. 测试清单（W3）

| 文件 | 覆盖 |
|---|---|
| `test_run_manager.py` | 状态机全部合法/非法迁移；`create_or_reject` 并发只有一个赢；`try_start` 的双重 abort 检查 |
| `test_thread_operation_lock.py` | 同 thread 并发两次 `POST /runs` → 第二次 409；非 run 操作不出现在列表 API |
| `test_publish_end_always.py` | **注入各种失败路径**（模型异常、组图失败、启动栅栏拒绝、取消），断言 END 一定到达；**额外注入 `_finalize` 本身抛异常**，断言嵌套 finally 仍然发出 END |
| `test_run_store_contract.py` | 准入冲突返回 `False` 而不是抛 `IntegrityError`；`start_run` 对非 pending 行返回 `False`；`update_status` 不能改写终态行；`update_run_completion` 拒绝替换不同的终态 |
| `test_stream_bridge.py` | 有界裁剪；gap 后订阅停止；**`StreamGap` 三个 id 的取值正确**（含缓冲为空时全 `None`）；`Last-Event-ID` 续传；未知 id 的处理；心跳 |
| `test_sse_translate.py` | 公开 `messages-tuple` 映射到内部 `messages`；不支持的 mode → 422 而不是降级成 `values`；`format_sse` 遇到 `datetime`/`UUID` 不抛异常 |
| `test_cancel.py` | pending 取消不组图；running 取消在一轮内生效；取消不被后到的 success 覆盖 |
| `test_disconnect.py` | 创建端断开触发取消；**join 断开不触发**；`/wait` 断开不返回半截 checkpoint；**模型长时间无输出时，断开在一个心跳间隔内被发现**；正常结束后客户端断开**不**触发 `cancel()` |
| `test_idempotency.py` | 同 key 复用同一 run；`idempotency_reused` 时不挂第二个 worker；不同 input 同 key → 409 |
| `test_start_run_no_await.py` | **AST 断言**：`create_or_reject` 和 `create_task` 之间没有 `await` |
| `test_sse_format.py` | 帧字段顺序；`Content-Location`；heartbeat 无 id |

倒数第二个用 AST 静态检查有点不寻常，但这条规则是"看起来完全无害的一行代码会造成严重后果"
的典型，code review 拦不住它。同类的还有 M2 那个 blocking-IO gate。

---

## 12. 验收清单

- [ ] `curl` 一个 `/runs/stream` 中途 Ctrl+C，run 在几秒内变成 `interrupted`，无协程泄漏
- [ ] 断线后 `/join` 带 `Last-Event-ID` 能拿到后续事件并正常收到 `end`
- [ ] 把 `queue_maxsize` 调到 5，慢速订阅者收到 `event: gap` 后订阅停止，**run 继续跑完**
- [ ] 同一 thread 并发两次 `POST /runs`，第二次 409
- [ ] 带 `Idempotency-Key` 重复提交，复用同一个 run 而不是跑两遍
- [ ] 模型调用抛异常时，SSE 仍然收到 `event: end`，run 状态是 `error`
- [ ] `GET /join` 的客户端断开后，run **不**被取消
- [ ] 让收尾逻辑（写终态那一步）抛异常，SSE 仍然收到 `event: end`
- [ ] 模型思考 60 秒不吐 token，期间关掉浏览器，服务端在一个心跳间隔内取消该 run
- [ ] `/wait` 在遇到 gap 后不返回失败，而是续订并等到真正的终态
- [ ] `stream_mode: ["events"]` 返回 422，而不是静默变成 `values`
- [ ] 浏览器用官方 `useStream` 直连，不写任何自定义解码器就能正常渲染流式输出

最后一条是对"对齐 Platform 协议"这个决策的实际检验。

---

## 13. 刻意不做的事

| 不做 | 理由 |
|---|---|
| Redis StreamBridge | M9。但 `supports_cross_process` 和工厂分支现在就有 |
| 租约 / 心跳 / 孤儿回收 | M9。单进程下重启时把所有 inflight 当孤儿收掉就够了 |
| SQL RunStore | M4。M3 用内存实现，但走同一个抽象接口 |
| 持久化事件账本 | M4。M3 先留 publish 钩子位 |
| 子代理的 `custom` 事件 | M8：`task_started` / `task_running` / `task_*` 终态，见 [M8 §3 / §14](2026-09-16-m8-subagents-design.md) |
| `stream_subgraphs` | 子图的 `values`/`messages` 漏进主线程会污染对话，默认关掉 |
| 人机确认（`Command(resume=...)`）| M6。但 `RunCreateRequest.command` 字段现在就留 |

---

## 14. 一句话总结

M3 的产出物看起来是"一套 HTTP API + SSE"，实际产出物是**一条所有入口共享的执行栈**。

检验标准：等到 M9 要接入飞书通道时，需要写的代码应该只有"把飞书消息翻译成
`RunCreateRequest`，然后调 `start_run`"——**不需要碰 RunManager、worker、StreamBridge 任何一行。**
如果那时候发现要改它们，说明 M3 没做对。
