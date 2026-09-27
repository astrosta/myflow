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


| 周   | 内容                                             |
| --- | ---------------------------------------------- |
| W1  | 对象模型 + `RunManager` 状态机 + `RunStore` 接口（内存实现）  |
| W2  | `StreamBridge`（内存实现，带 offset）+ worker + SSE 编帧 |
| W3  | 取消 / 断开 / 幂等 / `/wait` / `/join` + 边界测试        |




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

`start_run` **放在** `services.py` **而不是 router 里。** router 只做参数绑定和响应编码；
调度器和 IM 通道（M9）会直接调 `services.start_run`，它们没有 `Request` 对象。
写进 router 里，M9 就只能选择"伪造一个 Request"或者"复制一份逻辑"——两条路都错。

反过来，`RunManager` **和** `StreamBridge` **绝不能 import FastAPI**。M1 的嵌入式客户端、
M8 的子代理都要用 `RunManager` 的部分能力，让它依赖 Web 框架就把它们一起拖下水了。
M0 §4 的分层边界测试正是为这条服务的。

---



## 2. 对象模型：五个东西，职责不能混

这一节是整个 M3 的地基。**先把这张表钉死，再写代码。**


| 对象               | 回答什么问题             | 存在哪          | 丢了会怎样              |
| ---------------- | ------------------ | ------------ | ------------------ |
| **Run**          | 概念：一次"对话回合"        | ——           | ——                 |
| **RunRecord**    | 这一轮的状态、属主、取消信号     | 进程内可变对象      | 取消和并发控制失效          |
| **RunStore**     | 跨进程/跨重启的真相         | SQL（M4）      | 重启后 run 卡在 running |
| **StreamBridge** | 这一轮产出过哪些可重放的帧      | 内存队列 / Redis | SSE 重连拿到 gap       |
| **SSE**          | 当前这条 HTTP 连接在读哪条队列 | HTTP 响应      | ——                 |


**最重要的三条边界规则：**

1. **不要用"桥上有没有事件"判断 run 成功没有。** 那是 `RunRecord.status`。
2. **不要把** `RunManager` **当事件总线。** 它不 publish 任何东西。
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
这是 SDK 解码器的约定。另一个：`onCreated` 回调**只从** `Content-Location` **响应头抠** `run_id`，
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
    metadata: dict | None = None                # 进 run 记录；不许夹带凭据（§4.3 第 2 步）
    checkpoint_id: str | None = None            # 编辑 / 再生成：从这个 checkpoint 分叉（§4.4 第 10 步）
    checkpoint: dict | None = None              # 同上，完整坐标对象；优先于 checkpoint_id
    stream_mode: list[str] | None = None
    on_disconnect: Literal["cancel", "continue"] = "cancel"
    multitask_strategy: Literal["reject", "interrupt", "rollback"] = "reject"
```

`extra="forbid"` **不能省。** SDK 版本升级会带上新字段（比如 `stream_resumable`），
静默丢掉的话，客户端以为自己开启了某个特性，服务端其实没实现——这类 bug 表现为
"偶尔行为不对"，极难定位。明确 422 让不兼容立刻可见。

**HTTP 请求模型和内部 launch 用同一份。** 调度器、IM 通道调 `start_run` 时构造的也是这个对象。
两份模型会漂移。

---



## 4. `start_run`：唯一收敛点（W1）

所有入口——浏览器、IM、调度器、MCP 通知、子代理完成回调——都走这一个函数。
它们的差别只在三样东西：**谁在调（**`caller`**）**、**传进来的 context**、**要不要挂 SSE**。

> **绝不为新入口开第二条执行栈。** 这是 M3 最重要的一条纪律。



### 4.1 签名：不收 `Request`

```python
# app/gateway/services.py
async def start_run(
    body: RunCreateRequest,
    *,
    thread_id: str,
    caller: RunCaller,
    services: RunServices,
    idempotency_key: str | None = None,
    require_existing_thread: bool = False,
) -> RunLaunch:
    """准入一次 run 并挂上 worker。只等到 create_task，不等 agent 跑完。

    第 1–12 步不产生外部可见状态：任何一步抛错，系统和没调用过一样。
    第 13 步之后才有副作用，13 和 14 之间不许 await（§4.5）。
    """
```


| 参数                        | 类型                 | 谁给                                                         | 含义                                            |
| ------------------------- | ------------------ | ---------------------------------------------------------- | --------------------------------------------- |
| `body`                    | `RunCreateRequest` | 路由由 FastAPI 绑定；内部调用方自己构造                                   | 请求本身。**只读**，start_run 不改它的任何字段                |
| `thread_id`               | `str`              | URL 路径参数；内部调用方显式传                                          | checkpointer 的分区键。唯一可信来源，body 里的同名字段一律不看      |
| `caller`                  | `RunCaller`        | `caller_from_request(request)` / `RunCaller.internal(...)` | 身份与权限。**所有身份相关的决定只看它**，不看 body                |
| `services`                | `RunServices`      | `get_run_services(request)` / `app.state.run_services`     | 进程级单例的打包                                      |
| `idempotency_key`         | `str | None`       | `Idempotency-Key` 头；调度器传 occurrence 键                      | **原始值**。加 scope 在 start_run 内部做（§4.5），调用方不能绕过 |
| `require_existing_thread` | `bool`             | 默认 `False`；MCP 通知、子代理回调传 `True`                            | thread 不存在时 404，而不是顺手建一条（§4.6）                |


**不收** `Request` **是 §1 那条规则的落地。** DeerFlow 的 `start_run` 收 `Request`，结果调度器和
MCP 通知都得拿 `SimpleNamespace` 伪造一个出来，伪造对象上漏一个属性就是一个静默的身份 bug。
`start_run` 真正要从请求里拿的只有两样：认证结果和进程单例。把它们做成两个显式参数，
路由负责从 `Request` 里取，内部调用方直接构造。

#### `RunCaller`：认证的结果，不是请求的一部分

```python
# app/gateway/run_models.py
class CallerKind(StrEnum):
    HTTP = "http"            # 浏览器 / SDK / PAT，身份来自 AuthMiddleware（M9）
    INTERNAL = "internal"    # 调度器、IM 通道、MCP 通知、子代理回调（M9 起）


@dataclass(frozen=True, slots=True)
class RunCaller:
    kind: CallerKind
    user_id: str | None                      # 这一轮归属的存储 ID（M9 §2.2）；M3 无认证，恒为 None
    scopes: frozenset[str] | None = None     # None = 不按 scope 限制；只有 PAT 有具体集合（M9）
    user_role: str | None = None             # M9：admin / user，进 runtime.context 供 Principal 用

    @property
    def is_internal(self) -> bool:
        return self.kind is CallerKind.INTERNAL

    def allows(self, permission: str) -> bool:
        return self.scopes is None or permission in self.scopes

    @classmethod
    def internal(cls, *, owner_user_id: str | None) -> RunCaller:
        """进程内调用方（调度器、MCP 通知）。run 归属于 owner，不归属于"系统"。"""
        return cls(kind=CallerKind.INTERNAL, user_id=owner_user_id)
```

```python
# app/gateway/deps.py
def caller_from_request(request: Request) -> RunCaller:
    # M3 没有认证：永远是匿名 HTTP 调用方。
    # M9 在这里读 AuthMiddleware 写进 request.state 的结果：普通会话 → HTTP + user_id；
    # PAT → 再带 scopes；内部 token（IM 通道走内部 HTTP）→ INTERNAL + owner 头里的 user_id。
    return RunCaller(kind=CallerKind.HTTP, user_id=None)
```

`RunCaller` 和 M9 §2.10 的 `Principal` 不是一回事：`RunCaller` 是 Gateway 边界上的认证结果，
第 12 步把它写进 `runtime.context`；`Principal` 是图内从 `runtime.context` 重建的授权主体。
两者只通过 context 衔接，不互相引用——否则 harness 就得 import app 层的类型。

#### `RunServices` 与 `RunLaunch`

```python
# app/gateway/deps.py
@dataclass(frozen=True, slots=True)
class RunServices:
    """lifespan 建一次，挂在 app.state.run_services。"""
    run_manager: RunManager
    bridge: StreamBridge
    run_context: RunContext          # 进程级那一份：checkpointer、thread_store、event_store（§6.1）


def get_run_services(request: Request) -> RunServices:
    return request.app.state.run_services


# app/gateway/run_models.py
@dataclass(frozen=True, slots=True)
class RunLaunch:
    record: RunRecord
    reused: bool                     # 这次调用命中了幂等键、拿到的是已有的 run（§4.5）
```

返回值只有两种形态，调用方按 `reused` 分支，**不要读** `record.idempotency_reused`（§4.5 说明原因）：


| `reused` | 保证                                          | 调用方该做的                                                 |
| -------- | ------------------------------------------- | ------------------------------------------------------ |
| `False`  | `record.task` 已挂上；状态可能已经是 running 甚至终态      | 挂 SSE / 返回 JSON / 订桥等 END                              |
| `True`   | 任意状态；`record.task` 可能是 `None`（`store_only`） | 绝不再 `create_task`；`/wait` 不把当前 checkpoint 当这次的结果（§8.3） |




#### 两类调用方长这样

```python
# HTTP：routers/thread_runs.py
@router.post("/{thread_id}/runs/stream")
async def stream_run(
    thread_id: str,
    body: RunCreateRequest,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)] = None,
) -> StreamingResponse:
    services = get_run_services(request)
    launch = await start_run(
        body, thread_id=thread_id, caller=caller_from_request(request),
        services=services, idempotency_key=idempotency_key,
    )
    return sse_response(services, launch, request)   # §8：编帧、Content-Location、断开语义


# 进程内：M9 调度器
async def launch_scheduled_run(app, *, thread_id, owner_user_id, prompt, occurrence_id) -> RunLaunch:
    body = RunCreateRequest(
        assistant_id="lead_agent",
        input={"messages": [{"role": "user", "content": prompt}]},
        context={"non_interactive": True},
        on_disconnect="continue",                    # 没有 SSE 连接，断开取消无意义
    )
    with ensure_trace_context():                     # 没有 HTTP 中间件，trace 在入口自己开
        return await start_run(
            body, thread_id=thread_id,
            caller=RunCaller.internal(owner_user_id=owner_user_id),
            services=app.state.run_services,
            idempotency_key=f"scheduled:{occurrence_id}",
        )
```

M9 §4.1 草图里的 `start_run(..., user_id=user_id, context=context)` 落到这里就是
`caller=RunCaller.internal(owner_user_id=...)`。草图 context 里的 `user_id`、`is_internal`
会在第 3 步被丢掉，第 12 步再按 `caller` 重写——身份永远不从 body 里读。

### 4.2 执行顺序本身就是设计

顺序不是随意的：**所有校验在前，所有副作用在后**。任何一步失败时，
前面的步骤都不能已经产生外部可见的状态。


| #   | 做什么                          | 函数                                                  | 性质      | 失败 → HTTP     |
| --- | ---------------------------- | --------------------------------------------------- | ------- | ------------- |
| 1   | 取消能力门                        | `_require_cancel_capability`                        | 纯函数     | 403           |
| 2   | thread_id 合同；metadata 不许夹带凭据 | `_validate_thread_id`、`_reject_metadata_secrets`    | 纯函数     | 422           |
| 3   | 规范化 stream_mode 和客户端开关       | `_normalize_stream_modes`、`collect_client_switches` | 纯函数     | 422           |
| 4   | 模型白名单                        | `_check_model_allowed`                              | 纯函数     | 400           |
| 5   | thread 属主                    | `_thread_access_allowed`                            | 只读 IO   | 404           |
| 6   | 解析 agent 工厂                  | `resolve_agent_factory`                             | 纯函数     | 404           |
| 7   | 规范化 input                    | `normalize_input`                                   | 纯函数     | 422           |
| 8   | 整理 metadata，盖 trace id       | `_build_run_metadata`                               | 纯函数     | ——            |
| 9   | 组 `RunnableConfig` 骨架        | `build_run_config`                                  | 纯函数     | 422           |
| 10  | checkpoint 选择                | `apply_checkpoint`                                  | 只读 IO   | 422 / 404     |
| 11  | 合并客户端开关                      | `merge_client_switches`                             | 纯函数     | ——            |
| 12  | 写服务端权威值                      | `inject_server_owned`                               | 纯函数     | ——            |
| 13  | 准入                           | `run_manager.create_or_reject`                      | **副作用** | 409           |
| 14  | 挂 worker                     | `asyncio.create_task`                               | **副作用** | 500（先补偿，§4.5） |


"只读 IO"的两步（5、10）只读 thread_store 和 checkpointer，不写任何东西。
`get_app_config()` 在入口取**一次**快照：第 4 步校验模型、第 9 步钳制 recursion_limit、
worker 里的 `assemble_agent` 用的是同一个对象（M1 §3.1 "app_config 在入口解析一次"）。
中途热重载不会造成"按配置 A 校验、按配置 B 组图"。

#### 错误模型：领域异常，不是 `HTTPException`

```python
# app/gateway/services.py
class StartRunError(Exception):
    status_code: ClassVar[int] = 400
    code: ClassVar[str] = "bad_request"

class RunPermissionDenied(StartRunError):   status_code, code = 403, "permission_denied"
class RunRequestInvalid(StartRunError):     status_code, code = 422, "invalid_run_request"
class ModelNotAllowed(StartRunError):       status_code, code = 400, "model_not_allowed"
class ThreadNotFound(StartRunError):        status_code, code = 404, "thread_not_found"
class AssistantNotFound(StartRunError):     status_code, code = 404, "assistant_not_found"
class CheckpointNotFound(StartRunError):    status_code, code = 404, "checkpoint_not_found"
class ThreadBusy(StartRunError):            status_code, code = 409, "thread_busy"
class IdempotencyKeyReused(StartRunError):  status_code, code = 409, "idempotency_key_reused"
```

`app.py` 注册一个异常处理器，把 `StartRunError` 编成 `{"detail": str(exc), "code": exc.code}`。
start_run 里**不出现** `HTTPException`。DeerFlow 的 MCP 通知路径要先 catch `HTTPException`，
再按 409/404 翻译回领域异常——内部调用方被迫理解 HTTP 状态码。领域异常让 M9 调度器直接写
`except ThreadBusy: 退回 queued`，其它 `StartRunError` 一律 `failed`（M9 §4.6）。

#### 主干

```python
async def start_run(body, *, thread_id, caller, services, idempotency_key=None, require_existing_thread=False):
    app_config = get_app_config()
    ctx = services.run_context

    # ── 1–4：纯函数，只看 body 和 caller ─────────────────────────────
    _require_cancel_capability(caller, body.multitask_strategy)                         # 1
    _validate_thread_id(thread_id)                                                        # 2
    _reject_metadata_secrets(body)                                                        # 2
    stream_modes = _normalize_stream_modes(body.stream_mode)                              # 3
    switches = collect_client_switches(body)                                              # 3（§4.7）
    model_name = _check_model_allowed(switches.get("model_name"), app_config)             # 4

    # ── 5：只读 IO ─────────────────────────────────────────────────
    if not await _thread_access_allowed(ctx.thread_store, thread_id, caller,
                                        require_existing=require_existing_thread):
        raise ThreadNotFound(f"Thread {thread_id} not found")                            # 5

    # ── 6–12：组装；除第 10 步外不做 IO ─────────────────────────────
    agent_factory = resolve_agent_factory(body.assistant_id)                              # 6
    graph_input = normalize_input(body.input, command=body.command, trusted=caller.is_internal)  # 7
    run_metadata = _build_run_metadata(body.metadata)                                     # 8
    config = build_run_config(thread_id, body.config, run_metadata, app_config=app_config)  # 9
    await apply_checkpoint(config, body, thread_id=thread_id, checkpointer=ctx.checkpointer)  # 10
    merge_client_switches(config, switches)                                               # 11
    run_id = new_run_id()
    inject_server_owned(config, caller, run_id=run_id,
                        trace_id=run_metadata[TRACE_METADATA_KEY], body_context=body.context)  # 12

    # ── 13–14：副作用（§4.5）───────────────────────────────────────
    scoped_key = scope_idempotency_key(caller, thread_id, idempotency_key)
    try:
        record = await services.run_manager.create_or_reject(                            # 13
            thread_id,
            body.assistant_id,
            run_id=run_id,
            on_disconnect=DisconnectMode(body.on_disconnect),
            multitask_strategy=body.multitask_strategy,
            metadata=run_metadata,
            kwargs=_persisted_kwargs(body),
            model_name=model_name,
            user_id=caller.user_id,
            idempotency_key=scoped_key,
        )
    except ConflictError as exc:
        raise ThreadBusy(str(exc)) from exc

    if record.run_id != run_id:                      # 命中幂等键：拿到的是已有的 run
        _ensure_same_admission(record, body)
        return RunLaunch(record=record, reused=True)

    # ⚠️ 从 create_or_reject 返回到 create_task 之间不许有 await（§4.5）
    run_ctx = ctx.for_run(
        app_config=app_config, agent_factory=agent_factory, run_config=config,
        graph_input=graph_input, stream_modes=tuple(stream_modes),
    )
    worker = run_after_metadata(
        record, run_ctx, bridge=services.bridge, run_manager=services.run_manager,
        owner_user_id=caller.user_id, require_existing_thread=require_existing_thread,
    )
    try:
        record.task = asyncio.create_task(worker, name=f"run:{record.run_id}")          # 14
    except BaseException as exc:
        worker.close()
        await _abandon_unattached_run(services, record, exc)
        raise
    return RunLaunch(record=record, reused=False)
```

`ctx.for_run(...)` 是 `dataclasses.replace` 的薄包装。§6.1 说 `RunContext` 打包基础设施，
§6.2 的骨架又读 `ctx.run_config` / `ctx.graph_input` / `ctx.stream_modes`——两者不矛盾：
lifespan 建的那份只有基础设施字段，start_run 派生出每 run 一份、补上本次的输入。
§6.2 骨架里的 `assemble_agent` 就是这里的 `ctx.agent_factory`，M3 只有这一个。

`create_or_reject` **比 §5.4 多收一个可选的** `run_id`（缺省时自己生成）。第 12 步要把
`run_id` 写进 `runtime.context`（M1 §3.3：M8 按它给委托计数），而准入在第 13 步。
预分配还顺带解决了幂等判断的一个竞态，见 §4.5。

### 4.3 第 1–5 步：能早拒就早拒



#### 1. 取消能力门

```python
def _require_cancel_capability(caller: RunCaller, strategy: str) -> None:
    if strategy != "reject" and not caller.allows("runs:cancel"):
        raise RunPermissionDenied(f"multitask_strategy={strategy!r} requires runs:cancel")
```

`interrupt` / `rollback` 会终止一条已经在跑的 run（§5.5），那是取消能力，不是创建能力。
一个只有 `runs:create` 的 PAT 不能借它去掐别人的 run。放在 start_run 而不是路由装饰器上，
因为调度器、IM 这些不经过装饰器的入口也要过这道门。M3 的 `scopes` 恒为 `None`，这一步是
空操作，但调用点现在就在。

#### 2. thread_id 合同与 metadata 凭据

```python
# agentkit/utils/ids.py（M0）
THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

def validate_thread_id(thread_id: str) -> str:
    if not isinstance(thread_id, str) or not THREAD_ID_PATTERN.fullmatch(thread_id):
        raise ValueError("thread_id must match ^[A-Za-z0-9_-]{1,64}$")
    return thread_id


# app/gateway/services.py
def _validate_thread_id(thread_id: str) -> None:
    try:
        validate_thread_id(thread_id)
    except ValueError as exc:
        raise RunRequestInvalid(str(exc)) from exc


_CREDENTIAL_METADATA_KEYS = frozenset({"auth_token"})

def _reject_metadata_secrets(body: RunCreateRequest) -> None:
    config_metadata = (body.config or {}).get("metadata")
    for where, bag in (("metadata", body.metadata), ("config.metadata", config_metadata)):
        if isinstance(bag, dict) and (hit := _CREDENTIAL_METADATA_KEYS & bag.keys()):
            raise RunRequestInvalid(f"{where}.{min(hit)} is not allowed; pass credentials in context.secrets")
```

这条正则就是 M9 §4.5 引用的"M3 的 thread id 合同"。它允许调用方自带不透明 id，
同时保证 id 能原样拼进宿主机路径（M2 的 `threads/{tid}`）——`/`、`..`、空串都进不来。
**只在会产生状态的入口校验**；纯读和清理接口仍接受历史遗留 id，否则老会话连删都删不掉。

metadata 会原样进 run 记录、被 run API 回显、还会被抄进 thread 元数据（§4.6）。
凭据放进来就是落库。**拒绝而不是剥掉**：静默剥掉的话，客户端以为 token 生效了，
实际上 run 没拿到；422 同时告诉它正确的位置（`context.secrets`，§4.7）。

#### 3. stream_mode 和客户端开关

```python
def _normalize_stream_modes(raw: list[str] | str | None) -> list[str]:
    try:
        return normalize_stream_modes(raw)       # agentkit/runtime/stream_modes.py：None → ["values"]；str → [str]
    except UnsupportedStreamModeError as exc:
        raise RunRequestInvalid(str(exc)) from exc
```

`normalize_stream_modes` 是 harness 层的函数，抛的是 harness 的 `UnsupportedStreamModeError`；
包成 `RunRequestInvalid` 之后才走统一的异常处理器。`RunCreateRequest` 的字段校验器调的是同一个函数，所以
HTTP 请求在绑定阶段就已经 422 了；start_run 再调一次是为了拿到规范化之后的列表，
顺带兜住内部调用方。公开名 → LangGraph 内部名的映射在 worker 里做（§6.2），这里保留公开名。

`collect_client_switches` 把 `body.context`、`body.config.context`、`body.config.configurable`
三个袋子过同一道白名单，类型不对或键未知就 422。它必须在第 4 步之前跑，因为模型名可能在
任何一个袋子里。只读、不写 config，写回在第 11 步。

#### 4. 模型白名单

```python
def _check_model_allowed(model_name: str | None, app_config: AppConfig) -> str | None:
    if model_name is None:
        return None
    if app_config.get_model_config(model_name) is None:
        raise ModelNotAllowed(f"Model {model_name!r} is not in the configured model allowlist")
    return model_name
```

类型检查（非字符串 → 422）已经在第 3 步做过，这里只查名字在不在配置里。返回值写进
`RunRecord.model_name`，token 计价按它分桶。

和 M1 §3.5 的"未知模型降级到默认"不冲突：那条兜的是不走网关的嵌入式调用和 CLI。
经过网关的 run，模型名已经对着同一份 `app_config` 快照校验过，worker 里不会触发降级。
客户端显式点名一个不存在的模型，应该拿到 400，而不是被悄悄换成别的模型。

#### 5. thread 属主

```python
async def _thread_access_allowed(thread_store, thread_id: str, caller: RunCaller, *, require_existing: bool) -> bool:
    if caller.user_id is None:                  # M3 无认证：只剩存在性
        return not require_existing or await thread_store.get(thread_id, user_id=None) is not None
    return await thread_store.check_access(thread_id, caller.user_id, require_existing=require_existing)
```

第 5 步用 404 而不是 403：403 等于告诉攻击者"这个 thread 存在但不属于你"，
可以用来枚举系统里有哪些会话。

`check_access` 的语义见 M4 §4.4：行不存在且 `require_existing=False` 时返回 `True`——
"不先建 thread、直接发消息"是 LangGraph 的正常用法，thread 行由 worker 的第 0 步补（§4.6）。
M9 的内部调用方不跳过这一步，而是按它代表的 owner 查：internal 身份不是万能钥匙，
一个替用户 A 投递的通道 run 打不到用户 B 的 thread 上。

这一步和第 13 步之间隔着几次 await，thread 可能在中间被删。删除本身是一个 thread 操作
（§5.3 的 `DELETE`），它占着活跃位的时候第 13 步会 409；删完之后才准入的 run 会重建 thread 行，
除非调用方传了 `require_existing_thread=True`——那条路径在 worker 第 0 步再严格校验一次。

### 4.4 第 6–10 步：把请求翻译成图的输入和 config



#### 6. agent 工厂

```python
def resolve_agent_factory(assistant_id: str) -> AgentFactory:
    if assistant_id != "lead_agent":
        raise AssistantNotFound(f"Assistant {assistant_id!r} not found")
    from agentkit.agents.lead_agent.agent import assemble_agent   # 重量级 import 放函数体内（M1 §3.2）
    return assemble_agent
```

未知 `assistant_id` 是 404，与 LangGraph Platform 一致。M9 的自定义 agent **不在这里加新工厂**：
它们是同一个 `assemble_agent` 配不同的 `agent_name`（M1 §3.3），映射加在这个函数里。

#### 7. input

```python
_CLIENT_INPUT_CHANNELS = frozenset({"messages"})
_CLIENT_MESSAGE_TYPES = frozenset({"human"})

# 客户端不能伪造的 additional_kwargs 键。M3 为空，但剥离函数和调用点现在就接好；
# M6 并入 PROVENANCE_KEYS | {ORIGINAL_USER_CONTENT_KEY, "hide_from_ui", ...}（M6 §4.1、§4.7）。
SERVER_OWNED_MESSAGE_KEYS: frozenset[str] = frozenset()


def normalize_input(raw: dict | None, *, command: Any, trusted: bool) -> dict:
    if command is not None:
        raise RunRequestInvalid("command is not supported yet")     # M6 换成 Command(resume=...)
    if raw is None:
        return {}
    if not trusted and (extra := raw.keys() - _CLIENT_INPUT_CHANNELS):
        raise RunRequestInvalid(f"input keys {sorted(extra)} are not writable by clients")
    messages = raw.get("messages")
    if messages is None:
        return dict(raw)
    if not isinstance(messages, list):
        raise RunRequestInvalid("input.messages must be a list")

    converted = []
    for index, item in enumerate(messages):
        try:
            [message] = convert_to_messages([item])
        except (ValueError, TypeError, NotImplementedError) as exc:
            raise RunRequestInvalid(f"Invalid message at input.messages[{index}]: {exc}") from exc
        if not trusted:
            if message.type not in _CLIENT_MESSAGE_TYPES:
                raise RunRequestInvalid(f"input.messages[{index}]: role {message.type!r} is not accepted from clients")
            message = _strip_server_owned_keys(message)
        converted.append(message)
    return {**raw, "messages": converted}


def _strip_server_owned_keys(message: BaseMessage) -> BaseMessage:
    kept = {k: v for k, v in message.additional_kwargs.items() if k not in SERVER_OWNED_MESSAGE_KEYS}
    if kept == message.additional_kwargs:
        return message
    return message.model_copy(update={"additional_kwargs": kept})
```

四条规则，各堵一个洞：

- **用** `convert_to_messages`**，不要手写转换。** 它保留 `additional_kwargs`（上传文件的元数据就在
这里）、`id`、`name`。DeerFlow 早期手写的版本只抄 `content`，附件信息被静默丢掉。
坏消息按下标报 422，客户端才知道重试哪一条。
- **外部调用方只能发** `human`**。** `system` 消息等于把不可信文本塞进 system 通道（M1 §4.3 明令禁止）；
`ai` / `tool` 消息等于伪造模型说过的话。
- **外部调用方只能写** `messages` **这一个通道。** `input` 是对 `ThreadState` 的部分写入，
`sandbox` / `thread_data` / `artifacts` 是服务端维护的——写进来就能伪造沙箱绑定或产出清单。
- **内部调用方原样放行。** IM 通道会在进图之前自己盖 `original_user_content`（M6 §4.2），
MCP 通知会带 `hide_from_ui`。它们是可信的，剥了反而错。

`command` 在 M3 一律 422：还没有中断点，resume 没有意义，而静默忽略违反 §3 的原则。
M6 接人机确认时把 `{"resume": v}` 翻成 `Command(resume=v)`，并规定它和 `input` 二选一。

#### 8. metadata 与 trace id

```python
TRACE_METADATA_KEY = "deerflow_trace_id"          # agentkit/trace_context.py；M4、M10 用同一个键
_RESERVED_METADATA_KEYS = CLIENT_IDENTITY_KEYS | {TRACE_METADATA_KEY}   # CLIENT_IDENTITY_KEYS 见 M1 §3.3


def _build_run_metadata(raw: dict | None) -> dict:
    metadata = {k: v for k, v in (raw or {}).items() if k not in _RESERVED_METADATA_KEYS}
    metadata[TRACE_METADATA_KEY] = ensure_trace_id()
    return metadata
```

trace id 的唯一来源是 ContextVar：HTTP 入口由 `TraceMiddleware` 绑定（值即响应头 `X-Trace-Id`），
进程内入口用 `ensure_trace_context()` 绑定（§4.1 的调度器例子）。客户端带来的同名键被覆盖，
不是被读取——否则落库的 run 会和响应头、日志对不上，而对上正是这个 id 存在的意义。

同一个值最终出现在三处：run 记录的 metadata（第 13 步落库）、`config["metadata"]`（第 9 步，
随 checkpoint 持久化）、`runtime.context["trace_id"]`（第 12 步，跨子代理和线程边界的载体）。
身份类键在这里一并剥掉（M9 §2.1）；M4 的 thread 元数据过滤复用同一个集合。

#### 9. `build_run_config`

```python
DEFAULT_RECURSION_LIMIT = 100
_PASSTHROUGH_CONFIG_KEYS = frozenset({"tags", "run_name"})
_HANDLED_CONFIG_KEYS = _PASSTHROUGH_CONFIG_KEYS | {"recursion_limit", "metadata", "configurable", "context"}


def build_run_config(thread_id: str, request_config: dict | None, run_metadata: dict, *, app_config: AppConfig) -> dict:
    request_config = request_config or {}
    if unknown := request_config.keys() - _HANDLED_CONFIG_KEYS:
        raise RunRequestInvalid(f"Unsupported config keys: {sorted(unknown)}")

    config = {key: request_config[key] for key in _PASSTHROUGH_CONFIG_KEYS if key in request_config}
    config["recursion_limit"] = _clamp_recursion_limit(request_config.get("recursion_limit"), app_config.max_recursion_limit)
    client_metadata = request_config.get("metadata") or {}
    config["metadata"] = {                                      # 新 dict：不能写穿到 body.config
        **{k: v for k, v in client_metadata.items() if k not in _RESERVED_METADATA_KEYS},
        **run_metadata,
    }
    config["configurable"] = {"thread_id": thread_id, "checkpoint_ns": ""}
    config["context"] = {"thread_id": thread_id}
    return config


def _clamp_recursion_limit(value: Any, ceiling: int) -> int:
    if value is None:
        return DEFAULT_RECURSION_LIMIT
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RunRequestInvalid("config.recursion_limit must be a positive integer")
    if value > ceiling:
        logger.warning("Clamped client recursion_limit %d to %d", value, ceiling)
    return min(value, ceiling)
```

第 9 步的 `thread_id` 来自 URL 而不是 body，因为它是 checkpointer 的分区键。
信 body 意味着调用方可以把自己的消息写进别人的会话。

`configurable` **/** `context` **两个袋子这里一个键都不抄。** 它们在第 3 步已经过了白名单，
第 11 步再写回。DeerFlow 在这里把 `body.config.configurable` 原样拷进来，然后一次次补洞——
`__` 前缀、身份键、internal 键都是从这条原样拷贝的路径漏进来的，每补一个都要在拷贝之后
再扫一遍。骨架从空开始搭，就不存在"拷完再删"。

其余规则：

- **顶层键白名单，未知键 422。** 最危险的是 `run_id`：LangChain 会拿它当根 run 的 id，
客户端能借此把自己的调用挂到别人的 trace 下。`callbacks` 从 JSON 里来不了，不必列。
- `recursion_limit` **必须钳制。** 每个 super-step 至少一次模型调用，不设上限等于允许一个请求
无限烧钱。`bool` 是 `int` 的子类，要显式排除。超过上限是钳制加告警而不是 422：网页端固定发
1000，运维调低上限时不该让所有请求突然失败。`max_recursion_limit` 本阶段加进 M0 的 `AppConfig`，
默认 1000。
- `metadata` **合并到新 dict。** 原地 `update` 会把服务端盖的 trace id 写穿回 `body.config`，
而 `body.config` 正是要落库回显的那份。
- `checkpoint_ns` **显式写** `""`（M1 §3.1）。



#### 10. checkpoint 选择

```python
async def apply_checkpoint(config: dict, body: RunCreateRequest, *, thread_id: str, checkpointer) -> None:
    checkpoint = body.checkpoint or {}
    checkpoint_id = checkpoint.get("checkpoint_id") or body.checkpoint_id
    if not checkpoint_id:
        return
    if checkpoint.get("thread_id") not in (None, thread_id):
        raise RunRequestInvalid("checkpoint.thread_id does not match the request thread_id")

    coords = {
        "thread_id": thread_id,
        "checkpoint_ns": str(checkpoint.get("checkpoint_ns") or ""),
        "checkpoint_id": str(checkpoint_id),
    }
    if (checkpoint_map := checkpoint.get("checkpoint_map")) is not None:
        coords["checkpoint_map"] = checkpoint_map
    if await checkpointer.aget_tuple({"configurable": coords}) is None:
        raise CheckpointNotFound(f"Checkpoint {checkpoint_id} not found")
    config["configurable"].update(coords)
```

编辑和再生成都是"从某个旧 checkpoint 分叉"。查询用 URL 的 `thread_id` 做分区键，别的 thread 的
checkpoint id 在这里自然查不到 → 404，跨 thread 分叉走不通。checkpointer 自己抛异常时原样
上抛成 500，**不要吞成 404**——那会让一次存储故障看起来像客户端传错了 id。
M4 的 delta 模式不能分叉，线性化改写在 worker 里做，这一步只负责校验和写坐标。

### 4.5 第 13–14 步：准入，然后立刻挂 task



#### 13 和 14 之间不能有任何 `await`

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

正确做法：把 metadata 更新放进 worker 协程的第一步（`run_after_metadata`，§4.6），
让它在 worker 内部执行、可被 abort 打断。

窗口里只允许同步代码：幂等复用的分支判断、`ctx.for_run(...)`、创建协程对象。
§11 的 `test_start_run_no_await.py` 用 AST 检查 `create_or_reject` 和 `create_task` 两个调用之间
没有 `Await` 节点，所以主干里这两个调用要直接写在 `start_run` 函数体里，不要包进辅助函数——
包进去之后 AST 锚点就对不上了，检查会静默失效。

#### 幂等：scope 在 start_run 里加，复用靠 run_id 判断

```python
def scope_idempotency_key(caller: RunCaller, thread_id: str, raw: str | None) -> str | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        raise RunRequestInvalid("Idempotency-Key must not be blank")
    material = "\0".join((caller.kind, caller.user_id or "", thread_id, raw))   # raw 是任意字符串，用 \0 分隔
    return "run:" + hashlib.sha256(material.encode()).hexdigest()


def _ensure_same_admission(record: RunRecord, body: RunCreateRequest) -> None:
    stored = record.kwargs or {}
    if stored.get("input") != body.input or record.assistant_id != body.assistant_id:
        raise IdempotencyKeyReused("Idempotency-Key already used with a different request")
```

**§5.7 的** `scoped_key` **在这里落地，不在路由里。** 幂等索引是进程全局的；scope 放在路由里，
调度器和 IM 就得各自记得加，漏一处就串号。scope 里多带一个 `caller.kind`，
HTTP 用户的原始 key 就不可能撞上同一 thread 上调度器的 occurrence 键。
长度上限（255）在路由的 `Header(max_length=...)` 上。

**判断"是不是复用"看** `record.run_id != run_id`**，不看** `record.idempotency_reused`**。**
那个标志挂在共享的记录上，而且一旦置位就不会复原。`create_or_reject` 的 interrupt / rollback
分支在登记新记录之后还有 await（§5.5：先插新行、再标旧 run）；同 key 的重试恰好在这个窗口到达，
就会找到这条记录并把标志翻成 `True`。原始请求随后读到 `True`，以为自己是重试，于是不挂 worker——
留下一条谁也不跑的 pending。预分配的 `run_id` 是这次调用私有的，重试改不了它。

只比对 `input` 和 `assistant_id`，它们回答"这一轮说了什么、对谁说"。开关不比：用户改了一下 UI
开关再重试，仍然是同一轮；复用的 run 保持第一次的开关。内容不一致是 409（§5.7：复用 key 却改了内容，
是 bug 不是重试）。

#### 落库的 kwargs：原始请求，脱敏

```python
def _persisted_kwargs(body: RunCreateRequest) -> dict:
    return {
        "input": body.input,                              # 原始值：幂等比对和 run API 回显都用它
        "config": redact_config_secrets(body.config),
        "context": redact_context_secrets(body.context),
    }
```

两个 `redact_*` 删掉 `secrets`、所有 `__` 前缀键和 trace 键。第 9–12 步组出来的 live config
**保留**密钥——run 要用；只有落库回显的这份不留（M4 §4.2、M7 §4）。
存原始 `input` 而不是第 7 步的结果：后者是消息对象，不是 JSON；而且幂等比对要拿它和下一次请求的
原始 body 比。

#### `create_task` 失败：worker 的两条硬义务落到 start_run 身上

```python
async def _abandon_unattached_run(services: RunServices, record: RunRecord, exc: BaseException) -> None:
    await services.run_manager.fail_start_if_pending(record.run_id, error=f"Failed to attach run worker: {exc!r}")
    await services.bridge.publish_end(record.run_id)
```

不做这一步，唯一索引上会留一行永远 pending 的僵尸记录，**那条 thread 从此再也开不了新 run**，
只能手工改库。而这个分支几乎不可能在正常测试里覆盖到。

worker 对外有两条硬义务：写终态（§5）、发 END（§6.2）。worker 没挂上，两条都落到这里。
`fail_start_if_pending` 只在记录仍是 pending 时生效：标 `error`、置 `abort_event`、写 store。
`publish_end` 是给已经拿到这个 `run_id` 的订阅者的——补偿那次 await 期间到达的幂等重试、`/join`。

- **先** `worker.close()`**。** 协程对象已经建出来却没被调度，不关会在 GC 时报
"coroutine was never awaited"，还会一直攥着它捕获的 config 和 input。
- **这里不用** `asyncio.shield`，和 §5.5 不同：shield 要新建一个 task，而刚刚失败的正是建 task。
- `except BaseException` **然后原样** `raise`**。** 不管是什么异常，僵尸行的后果都一样；
补偿做完，异常照常往上走，路由返回 500。



### 4.6 `run_after_metadata`：worker 的第 0 步

```python
THREAD_METADATA_SETUP_TIMEOUT_SECONDS = 5.0


async def run_after_metadata(
    record: RunRecord,
    run_ctx: RunContext,
    *,
    bridge: StreamBridge,
    run_manager: RunManager,
    owner_user_id: str | None,
    require_existing_thread: bool,
) -> None:
    """worker task 的入口：尽力补 thread 行，然后把控制权交给 run_agent（§6）。"""
    interrupted: asyncio.CancelledError | None = None
    try:
        failure = await _ensure_thread_metadata_bounded(
            record, run_ctx.thread_store, owner_user_id=owner_user_id, require_existing=require_existing_thread,
        )
    except asyncio.CancelledError as exc:            # 关机排空会 cancel 所有 inflight task（§6.3）
        interrupted, failure = exc, None
        record.abort_event.set()
    if failure is not None:
        if require_existing_thread:
            await run_manager.fail_start_if_pending(record.run_id, error=f"Thread verification failed: {failure}")
        else:
            logger.warning("Failed to ensure thread_meta for %s (non-fatal): %s", record.thread_id, failure)
    # 成功、超时、被 abort、严格校验失败，全部进 run_agent：try_start 栅栏是把
    # "pending 阶段的取消 / 失败"收成"不组图 + 终态 + END"的唯一路径（§5.6、§6.2）。
    await run_agent(bridge, run_manager, record, run_ctx)
    if interrupted is not None:
        raise interrupted


async def _ensure_thread_metadata_bounded(record, thread_store, *, owner_user_id, require_existing) -> BaseException | None:
    """最多等 5 秒，abort 一到就放手。返回 None 表示就绪或已被 abort；否则返回失败原因。"""
    setup = asyncio.create_task(
        _ensure_thread_metadata(thread_store, record, owner_user_id=owner_user_id, require_existing=require_existing)
    )
    aborted = asyncio.create_task(record.abort_event.wait())
    try:
        done, _ = await asyncio.wait(
            {setup, aborted}, timeout=THREAD_METADATA_SETUP_TIMEOUT_SECONDS, return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        aborted.cancel()
        if not setup.done():
            setup.cancel()                           # 超时 / abort / 自己被 cancel：不再等它
            setup.add_done_callback(_log_detached_setup_result)
    if setup in done:
        return None if setup.cancelled() else setup.exception()
    if aborted in done:
        return None
    return TimeoutError(f"thread metadata setup exceeded {THREAD_METADATA_SETUP_TIMEOUT_SECONDS}s")


async def _ensure_thread_metadata(thread_store, record, *, owner_user_id, require_existing) -> None:
    # M4 起 get / create 都带 user_id=owner_user_id or AUTO；M9 的通道路径必须显式传 owner（M9 §5.3）
    if await thread_store.get(record.thread_id) is not None:
        return
    if require_existing:
        raise LookupError(f"Thread {record.thread_id} was deleted during run admission")
    await thread_store.create(
        record.thread_id,
        assistant_id=record.assistant_id,
        metadata={k: v for k, v in record.metadata.items() if k not in _RESERVED_METADATA_KEYS},
    )
```

- **超时只影响元数据，不影响 run。** 慢的 thread_store 不该把"模型开始输出"往后推。
非严格模式下失败和超时只打 warning，run 照跑——缺一行 thread 元数据影响的是侧栏，不是这一轮对话。
建 thread 行时剥掉 trace id：一个 thread 跨很多 run，run 级的 id 不属于它。
- **abort 一到就放手。** 这正是 §4.5 把元数据挪进 worker 的原因：pending 阶段的取消不会被卡在
一次慢 IO 后面。thread 元数据的 `running` 状态也不在这里写，要等 `try_start` 成功（§5.2）。
- **失败、超时、abort 全都进** `run_agent`**，不在这里自己发 END。** 终态和 END 只有一条路径
（`try_start` 栅栏加 `run_agent` 的 `finally`）；这里另写一份，就是第二条执行栈的雏形。
- `CancelledError` **先记下，走完** `run_agent` **再抛。** 直接让它冒出去的话 `run_agent` 根本没进，
既没终态也没 END。先置 abort 再进 `run_agent`：`try_start` 返回 cancelled，收尾发 END，
然后原样再抛，lifespan 看到的仍然是一个被取消的 task。
- **严格模式失败先** `fail_start_if_pending`**。** 记录变成 error，`try_start` 看到非 pending 就不组图。
这条路径给 MCP 通知和子代理回调用：聊天已经被删了，就不该被一条迟到的通知复活。

**pending 阶段只能靠** `abort_event` **取消，不能** `task.cancel()`**。** task 建出来之后、第一次被调度之前
被 cancel，`CancelledError` 会在协程函数体的第一行之前抛出——上面的 `try` 还没进，任何
`except` / `finally` 都兜不住，这条 run 既没终态也没 END。§9.1 的 `cancel()` 只对 running 的 run
补 `task.cancel()`；§5.5 的 interrupt / rollback 处理被顶替的旧 run 时也要守这条（DeerFlow 这个分支
不区分 pending 和 running 都会 `task.cancel()`，照抄时要补上判断）。关机排空是唯一例外：进程要退出了，
残留的 pending 行由下次启动时的孤儿回收收掉（§13）。

### 4.7 客户端开关的白名单（第 3、11、12 步）

键名在工厂模块定义一次（M1 §3.3），Gateway 只 import，不手写第二份。这一节只决定每类键
**从哪个袋子收、写到哪**：


| 类别            | 键                                                                                                 | 谁能传        | 从哪收                        | 写到                                       |
| ------------- | ------------------------------------------------------------------------------------------------- | ---------- | -------------------------- | ---------------------------------------- |
| 装配键           | `model_name` `thinking_enabled` `reasoning_effort` `is_plan_mode` `subagent_enabled` `agent_name` | 所有调用方      | 三个袋子                       | `configurable` + `context`               |
| 请求级密钥         | `secrets`                                                                                         | 所有调用方      | `context`、`config.context` | 只进 `context`                             |
| 内部开关          | `non_interactive`                                                                                 | 仅 internal | 仅顶层 `body.context`         | `configurable` + `context`，值相同           |
| 内部身份          | `channel_user_id`                                                                                 | 仅 internal | 仅顶层 `body.context`         | 只进 `context`                             |
| 服务端拥有         | `user_id` `user_role` `authz_attributes` `run_id` `trace_id` `is_internal`                        | 无人         | 丢弃，第 12 步写                 | 只进 `context`                             |
| 身份别名          | `owner_id` `auth_user` `auth_user_id`                                                             | 无人         | 丢弃                         | ——                                       |
| checkpoint 坐标 | `thread_id` `checkpoint_ns` `checkpoint_id` `checkpoint_map`                                      | 无人         | 丢弃，第 9、10 步写               | `configurable`（`thread_id` 也进 `context`） |
| 私有通道          | `__*`                                                                                             | 无人         | 丢弃                         | ——                                       |
| 其它            | 任何别的键                                                                                             | ——         | **422**                    | ——                                       |


三个袋子是 `body.context`、`body.config.context`、`body.config.configurable`。LangGraph SDK 的老用法
把开关放在 `configurable`，新用法放在 `context`；三处都收、用同一条规则，就不会出现"放错袋子
所以没生效"。冲突时公开位置说了算：`context` > `config.context` > `config.configurable`。

#### 第 3 步：收集

```python
# app/gateway/services.py
from agentkit.agents.lead_agent.runtime_keys import (    # M1 §3.3 的常量，放在轻量子模块（M1 §3.2）
    CHECKPOINT_COORDINATE_KEYS, CLIENT_ASSEMBLY_KEY_TYPES, CLIENT_ASSEMBLY_KEYS,
    CLIENT_IDENTITY_KEYS, CONTEXT_ONLY_KEYS, SERVER_OWNED_KEYS,
)

REQUEST_SECRETS_KEY = "secrets"                          # M7 §4
# user_role / authz_attributes：M9 的 Principal 从 runtime.context 读它们，客户端不能预置
_DROPPED_KEYS = (
    SERVER_OWNED_KEYS | CLIENT_IDENTITY_KEYS | CHECKPOINT_COORDINATE_KEYS | CONTEXT_ONLY_KEYS
    | {"user_role", "authz_attributes"}
)


def collect_client_switches(body: RunCreateRequest) -> dict[str, Any]:
    config = body.config or {}
    bags = (                                             # 低优先级先读，高优先级覆盖
        ("config.configurable", config.get("configurable")),
        ("config.context", config.get("context")),
        ("context", body.context),
    )
    switches: dict[str, Any] = {}
    for where, bag in bags:
        if bag is None:
            continue
        if not isinstance(bag, dict):
            raise RunRequestInvalid(f"{where} must be an object")
        for key, value in bag.items():
            if key.startswith("__") or key in _DROPPED_KEYS:
                continue
            if key in CLIENT_ASSEMBLY_KEYS:
                if not isinstance(value, CLIENT_ASSEMBLY_KEY_TYPES[key]):
                    raise RunRequestInvalid(f"{where}.{key} has the wrong type")
                switches[key] = value
            elif key == REQUEST_SECRETS_KEY:
                if where == "config.configurable":
                    raise RunRequestInvalid("secrets must go in context: configurable is persisted into checkpoints")
                switches[key] = _validate_secrets(where, value)      # 必须是 dict[str, str]
            else:
                raise RunRequestInvalid(f"{where}.{key} is not a supported run option")
    return switches
```

`CLIENT_ASSEMBLY_KEY_TYPES` 是 M1 §3.3 那张类型表的代码形态，和 `CLIENT_ASSEMBLY_KEYS` 放在一起：
`bool` 键只收 `bool`（`1` 和 `"false"` 都是 422，工厂不做矫正），`str | None` 键收字符串或 `null`。
M1 §3.3 里的 `_SERVER_OWNED_KEYS` / `_CLIENT_IDENTITY_KEYS` / `_CONTEXT_ONLY_KEYS` 带下划线；
Gateway 要跨包 import 它们，所以落地时去掉下划线，和其余常量一起放进 `runtime_keys.py`——
`agent.py` 是重量级模块，Gateway 只为几个常量 import 它，会把整张工具图拉起来。

**丢弃和 422 的分界。** 丢弃的都是服务端自己会写的键——先删、后写（M9 §2.1："不是校验后使用，是删掉"）。
这些键报 422 反而会误伤：M9 §4.1 草图里调度器的 context 就带着 `user_id` 和 `is_internal`。
除此之外的未知键一律 422，理由和 §3 的 `extra="forbid"` 相同：静默丢掉，客户端会以为某个特性已经打开。

`mode` **不在白名单里。** 早先的草稿收过它，但它没有读者（M1 §3.3），现在客户端传它会得到 422。
前端如果需要"模式"这个概念，在前端把它展开成上表的装配键。

#### 第 11 步：写回

```python
def merge_client_switches(config: dict, switches: dict[str, Any]) -> None:
    for key, value in switches.items():
        if key in CLIENT_ASSEMBLY_KEYS:
            config["configurable"][key] = value          # 工厂读这里（M1 §3.1）
        config["context"][key] = value                   # 工具和中间件读 runtime.context
```

装配键两边都写：工厂读 `configurable`，而 LangGraph 1.1 起 `ToolRuntime.context` 不再回退到
`configurable`，只写一边，工具和中间件读到的就是默认值，而且不报错。

#### 第 12 步：服务端权威值

```python
def inject_server_owned(config: dict, caller: RunCaller, *, run_id: str, trace_id: str, body_context: dict | None) -> None:
    context = config["context"]
    context.update(
        user_id=caller.user_id,
        user_role=caller.user_role,
        is_internal=caller.is_internal,                  # False 也写：Principal 用 `is True` 判断（M9 §2.10）
        run_id=run_id,
        trace_id=trace_id,
    )
    if not caller.is_internal or not body_context:
        return
    if body_context.get("non_interactive") is True:
        config["configurable"]["non_interactive"] = True # 工厂据此去掉 ask_clarification（M1 §3.1）
        context["non_interactive"] = True                # M6 据此不注册 ClarificationMiddleware
    if isinstance(channel_user_id := body_context.get("channel_user_id"), str):
        context["channel_user_id"] = channel_user_id     # 只进 context（M9 §5.3）
```

**顺序就是 M1 §3.3 要求的那个顺序**：第 3 步规范化 → 第 9、10 步写坐标 → 第 11 步写客户端开关
→ 第 12 步最后写服务端值。第 11 步只添加白名单里的键、从不删除，第 12 步最后写、覆盖一切，
所以不会出现"刚写入的服务端值被自己的剥离删掉"。

两条老规则仍然成立：

`__` **前缀的私有通道键要丢掉。** LangGraph 用这个前缀传内部状态，
客户端能塞进去就等于能操纵图内部。

**不是所有键都该进** `configurable`**。** `configurable` 会被写进 checkpoint 并持久化。
临时凭据（M3 里就是 `secrets`）只能进 `context`，进 `configurable` 就等于写进了数据库——
所以 `secrets` 出现在 `config.configurable` 里是 422，而不是被挪个位置。`user_id` 和
`channel_user_id` 同理：前者进 checkpoint 会在 thread 转移属主后留下旧身份，后者会把群里
某个人的平台 ID 永久写进对话历史。

---



## 5. RunManager（W1）

它是**登记处**，不是执行器。图执行在 worker 里，SSE 在 StreamBridge 里。

### 5.1 双层存储


| 层                              | 存什么                                        | 生命周期         |
| ------------------------------ | ------------------------------------------ | ------------ |
| 内存 `_runs` + `_runs_by_thread` | `task`、`abort_event`、`start_lock`、token 计数 | 进程内，结束后延迟 GC |
| `RunStore`                     | 可序列化字段：status、kwargs、token 汇总              | 跨重启、跨进程      |


三条规则：

**同** `run_id` **内存优先。** `get()` 先看 `_runs`，没有才从 store hydrate。
hydrate 出来的记录标 `store_only=True`，没有本地 `task`。
如果顺序反了，一个 live run 会被换成一份没有 `task` 的快照——取消就失效了。

**创建必须先写 store 再进内存。** 写 store 失败要回滚内存，避免"内存里有、库里没有"。

`_runs` **和** `_runs_by_thread` **必须在同一次持锁、中间不 await 里一起改。**
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

`repr=False` **不是美观问题。** `task` 和 `abort_event` 会出现在任何异常回溯和日志里，
`asyncio.Task` 的 repr 包含整个协程栈。一条 error 日志会变成几十行噪音。

`start_lock` **看起来多余，但要有。** 它序列化"一条已准入的 run 被交给了不止一个 worker 路径"
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

**一、准入方法返回** `bool`**，不抛** `IntegrityError`**。** 唯一索引冲突是**预期内的正常结果**
（用户手快点了两次发送），不是异常。让它冒到路由层意味着每个调用点都要 catch 一个
数据库特定的异常类型——而 SQLite 和 Postgres 抛的还不是同一个。

**二、所有读方法都带** `user_id` **参数。** M3 只有单用户，传 `None` 即可。但这个参数位现在留着，
M9 加多租户时改的是实现，不是几十个调用点。更关键的是**默认值不能是** `None`——
`user_id: str | None` 但不给默认值，强迫每个调用点显式表态"我这里确实要全局查"。
给了默认值，M9 时会有一半调用点在无声地跨用户查询。

**三、**`update_status` **和** `update_run_completion` **分开。** 前者是中途的状态变更，后者是终态
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

`finalizing` **不是状态机上的一档。** 它是 `RunRecord` 上的一个布尔标志，
表示 worker 在 `finally` 里收尾（写标题、同步 thread 状态），
此时它**仍然占着"每 thread 一条活跃"的名额**。

把它做成一个状态会污染所有 `status IN (...)` 的查询，而它的唯一作用是让下一个 run
的 `wait_for_prior_finalizing` 知道要等。

另一个要点：**thread 元数据的** `running` **只在** `try_start` **成功之后才写。**
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

**M3 只用到** `run` **这一个 kind，但枚举和索引现在就要建对。**
理由：这些操作全都会写同一条 thread 的 checkpoint。以后加 compact 时如果另起一把锁，
compact 和对话就能并发写 checkpoint，结果是静默的状态损坏——最难查的那类 bug。

配套的两件事：

- 非 run 的操作用 `reserve_thread_operation(thread_id, kind=...)` 上下文管理器：
进去插一条短命 pending 行，出来删掉。
- **列表 API 要过滤掉** `operation_kind != 'run'` **的行。** 用户不该在"对话历史"里
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


| 策略                   | 对旧 run                        | 对新 run     |
| -------------------- | ----------------------------- | ---------- |
| `reject`（默认，Web 发送用） | 不动                            | 不开，调用方 409 |
| `interrupt`          | 标 `interrupted`，保留 checkpoint | 旧的让位后开始    |
| `rollback`           | 同上，但状态滚回 run 前                | 同上         |


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
**调用方看到这个标志就不能再** `create_task`，否则同一个 run 会跑两遍。

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

`publish_end` **放在嵌套的内层** `finally`**，不是和收尾逻辑并列。** 这是全节最重要的一行结构。
收尾要做的事会随阶段增长——M4 加事件账本落盘、M6 加标题生成、M9 加租约释放——**其中任何
一个新增的 IO 抛异常，都会在"并列写法"下把** `publish_end` **跳过去**。而这类回归极难发现：
它只在那个新增步骤失败时触发，而那个步骤失败本身就是个罕见分支。

嵌套写法把这条不变量变成了结构性的，不依赖后来的人记得把 `publish_end` 放在最后。

`CancelledError` **要写终态再** `raise`**，不能吞。** 吞掉它会让上层的 `asyncio.gather`、
lifespan 的排空逻辑以为这个 task 正常结束了。但也不能只 `raise` 不写终态——那样 store 里
会留一行永远 running。

`try_start` **返回 cancelled 时直接** `return`**，不是** `raise`**。** 这条路径是"这个 run 在组图之前
就被取消了"，是预期内的正常流程。`finally` 仍然会跑，`publish_end` 仍然会发——订阅者收到
一个只有 metadata 和 end 的流，这是对的。

#### chunk 到 SSE 事件的翻译

`translate_chunk` 是 M1 §6.2 那套流式契约在服务端的落地，**两边的事件名必须一致**：


| LangGraph 内部 mode | 发出的 SSE `event` | 说明                                       |
| ----------------- | --------------- | ---------------------------------------- |
| `values`          | `values`        | 状态快照                                     |
| `messages`        | `messages`      | 前端 SDK 认这个名；公开 API 参数名叫 `messages-tuple` |
| `updates`         | `updates`       | 节点级增量                                    |
| `custom`          | `custom`        | `StreamWriter` 转发                        |


**公开参数名和 wire 事件名不是一回事**，`stream_modes.py` 负责这层映射：请求里客户端写
`messages-tuple`，映射到 LangGraph 的 `messages`，而 SSE 帧上的 `event:` 是 `messages`。
三个名字听起来该统一，但前两个是 LangGraph Platform 已经定下的协议，改了就用不了官方 SDK。
**把映射关进一个文件，别让它散进 router。**

客户端传了不支持的 mode（`events`、或者内部名 `messages`）要 **422 拒绝，不要静默替换成**
`values`。静默替换的症状是前端收到完全不同结构的数据，而它以为自己订的是增量。

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


| 对象                   | 含义                               | 订阅方看到                      |
| -------------------- | -------------------------------- | -------------------------- |
| `StreamEvent`        | 一条可重放事件（`id` / `event` / `data`） | `event:` + `data:` + `id:` |
| `HEARTBEAT_SENTINEL` | 空闲超过心跳间隔                         | 注释行 `: heartbeat`，**无 id** |
| `END_SENTINEL`       | producer 调了 `publish_end`        | `event: end`               |
| `StreamGap`          | 游标已不在保留窗口内                       | `event: gap`，**无 id**，然后停  |


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


| 消费者                                | 收到 gap 后                                                 | 用哪个字段                       |
| ---------------------------------- | -------------------------------------------------------- | --------------------------- |
| `sse_consumer`（浏览器）                | 发一帧 `event: gap`，`recovery: reload_durable_state`，然后结束订阅 | 三个都透传，供客户端诊断                |
| `wait_for_run_completion`（`/wait`） | **从保留窗口的尾部重新订阅**，继续等 END                                 | `latest_available_event_id` |


`/wait` 不需要完整重放，它只想知道什么时候结束。如果 `StreamGap` 只是个光秃秃的标记，
`/wait` 就没有安全的续订点，只能把 gap 当成失败返回——而 run 其实还在正常跑。

缓冲为空时三个字段都是 `None`（比如 run 刚创建、一条事件都还没发）。消费者要能处理这种情况：
`/wait` 此时用 `None` 续订，等价于"从头订"，是对的。

### 7.4 内存实现的游标规则


| 情况                   | 行为                            |
| -------------------- | ----------------------------- |
| 无 cursor             | 从当前 `start_offset`（最早还在的事件）开始 |
| cursor 低于 watermark  | gap                           |
| cursor 在窗口内且对得上      | 从下一条开始                        |
| 窗口内但 id 未知           | 从最早保留事件重放                     |
| 活订阅者被裁到 watermark 后面 | 同样 gap 然后停                    |


第二行值得说明：即使 cursor 的数字看起来合法，只要它低于 watermark 就必须 gap。
被裁掉的事件已经无法核对，不能假设它就是客户端想要的那一条。

### 7.5 M3 只做内存实现，但接口要能容纳 Redis

`supports_cross_process` 这个标志现在就要有。内存桥上，一个跑在别的进程里的 run
（`store_only`）是订不到的——路由必须 409，不能让客户端订一条空流死等。
M9 换 Redis 时这个标志变 True，路由逻辑一行不用改。

同样地，`redis` 包是可选依赖，**不要在** `stream_bridge/__init__.py` **里 eager import**。
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
用户体感就是"模型不流式了"。**nginx 侧还要确认** `gzip_types` **不含** `text/event-stream`——
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

**心跳帧是 SSE 注释行（**`:` **开头），不是事件。** 它没有 `event:` 也没有 `id:`，SDK 的解码器
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


| 入口                    | `apply_on_disconnect` | 理由                            |
| --------------------- | --------------------- | ----------------------------- |
| `POST /runs/stream`   | `True`                | 用户关了页面，按 `on_disconnect` 策略取消 |
| `POST /runs/wait`     | `True`                | 同上，创建端语义                      |
| `GET /runs/{id}/join` | `False`               | 只读观察者断开不能取消别人的 run            |


第三行是个安全边界：如果 join 也能触发取消，那么一个只有 `runs:read` 权限的令牌
只要连上再断开就等于执行了取消——权限模型被绕过。

### 8.3 `/wait` 必须订阅 StreamBridge，不能 `await record.task`

诱惑很明显：`/wait` 要等 run 结束，而 `record.task` 就是那个 task，直接 await 不就行了？

不行，两个原因：

`on_disconnect` **语义只活在创建该 run 的 worker 内存里。** `await task` 拿不到客户端断开事件，
用户关掉连接后 run 还在烧钱。

**多进程下** `record.task` **可能是** `None`（`store_only`）。订桥则在 Redis 实现下天然跨进程。

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

**路由层直接** `task.cancel()` **会让 store 上的** `cancel_action` **CAS 对不上**——
状态写进去了但没人知道该 rollback 还是 interrupt，而且绕过了所有终态写入的守卫。

两种 action：

- `interrupt`：保留已写进 checkpoint 的内容
- `rollback`：回滚到本 run 开始前的 checkpoint（worker 第 4 步捕获的那个点）



### 9.2 `CancelOutcome` → HTTP 映射由路由查表，不要自己猜


| 结果                   | 含义                            | HTTP  |
| -------------------- | ----------------------------- | ----- |
| `cancelled`          | 本进程执行了取消                      | 200   |
| `not_active_locally` | 记录不在本进程内存里                    | 409   |
| `not_cancellable`    | 已经是终态                         | 4xx   |
| `unknown`            | 找不到或 store 失败                 | 404   |
| `requested`          | （M9）非 owner，已写 durable cancel | 202   |
| `taken_over`         | （M9）租约过期，本进程 claim            | 按接管处理 |


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


| 预留                                             | 为什么现在留                                                           |
| ---------------------------------------------- | ---------------------------------------------------------------- |
| `runs.owner_worker_id` / `lease_expires_at` 两列 | M9 多实例。加列容易，改并发路径难                                               |
| `operation_kind` 枚举 + 部分唯一索引                   | 见 §5.3。后加等于放任并发写 checkpoint                                      |
| `RunRecord.ownership_lost` 标志                  | M9：丢租约后本进程禁止再写终态，见 [M9 §3.1](2026-09-16-m9-production-design.md) |
| `StreamBridge.supports_cross_process`          | M9：路由据此决定 409 还是订阅                                               |
| `RunStore` 抽象接口                                | M4 换 SQL 时只换实现                                                   |
| `CancelOutcome` 的六个枚举值                         | 见 §9.2                                                           |
| 事件账本（`run_events`）的 publish 钩子位                | M4。SSE 和账本是两条并行的管子，不是同一条                                         |


最后一条值得强调：**SSE 和持久化事件账本不是同一条管子。**
SSE 是有界的、会过期的直播；账本是完整的、用于历史回放和审计的记录。
用 SSE 缓冲当历史，页面刷新后就什么都没有了。

---



## 11. 测试清单（W3）


| 文件                                 | 覆盖                                                                                                                                                                                                       |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_run_manager.py`              | 状态机全部合法/非法迁移；`create_or_reject` 并发只有一个赢；`try_start` 的双重 abort 检查                                                                                                                                         |
| `test_thread_operation_lock.py`    | 同 thread 并发两次 `POST /runs` → 第二次 409；非 run 操作不出现在列表 API                                                                                                                                                  |
| `test_publish_end_always.py`       | **注入各种失败路径**（模型异常、组图失败、启动栅栏拒绝、取消），断言 END 一定到达；**额外注入** `_finalize` **本身抛异常**，断言嵌套 finally 仍然发出 END                                                                                                       |
| `test_run_store_contract.py`       | 准入冲突返回 `False` 而不是抛 `IntegrityError`；`start_run` 对非 pending 行返回 `False`；`update_status` 不能改写终态行；`update_run_completion` 拒绝替换不同的终态                                                                        |
| `test_stream_bridge.py`            | 有界裁剪；gap 后订阅停止；`StreamGap` **三个 id 的取值正确**（含缓冲为空时全 `None`）；`Last-Event-ID` 续传；未知 id 的处理；心跳                                                                                                               |
| `test_sse_translate.py`            | 公开 `messages-tuple` 映射到内部 `messages`；不支持的 mode → 422 而不是降级成 `values`；`format_sse` 遇到 `datetime`/`UUID` 不抛异常                                                                                              |
| `test_cancel.py`                   | pending 取消不组图；running 取消在一轮内生效；取消不被后到的 success 覆盖                                                                                                                                                        |
| `test_disconnect.py`               | 创建端断开触发取消；**join 断开不触发**；`/wait` 断开不返回半截 checkpoint；**模型长时间无输出时，断开在一个心跳间隔内被发现**；正常结束后客户端断开**不**触发 `cancel()`                                                                                             |
| `test_idempotency.py`              | 同 key 复用同一 run；`idempotency_reused` 时不挂第二个 worker；不同 input 同 key → 409；**重叠重试把共享记录的** `idempotency_reused` **翻成 True 时，原始请求仍是** `reused=False` **并挂上 worker**；HTTP 和 internal 用同一个原始 key 不串号             |
| `test_start_run_no_await.py`       | **AST 断言**：`create_or_reject` 和 `create_task` 之间没有 `await`                                                                                                                                               |
| `test_start_run_validation.py`     | 第 1–12 步逐步注入失败：run store、bridge、thread_store 都没有新写入；每步的状态码和 §4.2 的表一致；`StartRunError` 编成 `{detail, code}`；外部调用方发 `system` 消息、写 `sandbox` 通道、带 `command` 都是 422；`recursion_limit` 超上限被钳制、`true` 被拒        |
| `test_run_context_whitelist.py`    | 三个袋子同一套规则、`context` 优先；未知键 422，`__` / 身份 / 坐标 / `user_role` 静默丢；`secrets` 不进 `configurable`、不进落库的 kwargs；`non_interactive` / `channel_user_id` 只认 internal 的顶层 `context`；`is_internal=False` 也写进 context |
| `test_start_run_attach_failure.py` | `create_task` 抛异常：记录变 `error`、END 到达、协程被 close、同一 thread 能再开新 run                                                                                                                                        |
| `test_run_after_metadata.py`       | thread_store 挂住 → 5 秒后照常开跑；pending 阶段 abort 不等元数据；严格模式下 thread 已删 → `error` + END、不组图；元数据阶段被 cancel → 仍然收到 END，task 仍以 cancelled 结束                                                                      |
| `test_sse_format.py`               | 帧字段顺序；`Content-Location`；heartbeat 无 id                                                                                                                                                                  |


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


| 不做                          | 理由                                                                                                  |
| --------------------------- | --------------------------------------------------------------------------------------------------- |
| Redis StreamBridge          | M9。但 `supports_cross_process` 和工厂分支现在就有                                                             |
| 租约 / 心跳 / 孤儿回收              | M9。单进程下重启时把所有 inflight 当孤儿收掉就够了                                                                     |
| SQL RunStore                | M4。M3 用内存实现，但走同一个抽象接口                                                                               |
| 持久化事件账本                     | M4。M3 先留 publish 钩子位                                                                                |
| 子代理的 `custom` 事件            | M8：`task_started` / `task_running` / `task_*` 终态，见 [M8 §3 / §14](2026-09-16-m8-subagents-design.md) |
| `stream_subgraphs`          | 子图的 `values`/`messages` 漏进主线程会污染对话，默认关掉                                                             |
| 人机确认（`Command(resume=...)`） | M6。但 `RunCreateRequest.command` 字段现在就留                                                              |


---



## 14. 一句话总结

M3 的产出物看起来是"一套 HTTP API + SSE"，实际产出物是**一条所有入口共享的执行栈**。

检验标准：等到 M9 要接入飞书通道时，需要写的代码应该只有"把飞书消息翻译成
`RunCreateRequest`，然后调 `start_run`"——**不需要碰 RunManager、worker、StreamBridge 任何一行。**
如果那时候发现要改它们，说明 M3 没做对。