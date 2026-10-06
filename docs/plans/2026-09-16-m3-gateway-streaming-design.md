# M3 Gateway 与流式详细设计

> 配套文档：[分阶段开发方案](2026-09-16-greenfield-agent-service-roadmap.md) ·
> [M0 地基](2026-09-16-m0-foundation-design.md) · [M1 最小 Agent](2026-09-16-m1-minimal-agent-design.md) ·
> [M2 工具与沙箱](2026-09-16-m2-tools-sandbox-design.md) · [路由层详细设计](2026-09-16-router-layer-design.md)
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

> 这些路由的**横向约定**——错误体形状、分页游标、幂等头、能力门、注册顺序——见
> [路由层详细设计](2026-09-16-router-layer-design.md)。本文只管执行栈与 SSE 编帧。

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


| 参数                        | 类型                 | 谁给                                                         | 含义                                       |
| ------------------------- | ------------------ | ---------------------------------------------------------- | ---------------------------------------- |
| `body`                    | `RunCreateRequest` | 路由由 FastAPI 绑定；内部调用方自己构造                                   | 请求本身。**只读**，start_run 不改它的任何字段           |
| `thread_id`               | `str`              | URL 路径参数；内部调用方显式传                                          | checkpointer 的分区键。唯一可信来源，body 里的同名字段一律不看 |
| `caller`                  | `RunCaller`        | `caller_from_request(request)` / `RunCaller.internal(...)` | 身份与权限。**所有身份相关的决定只看它**，不看 body           |
| `services`                | `RunServices`      | `get_run_services(request)` / `app.state.run_services`     | 进程级单例的打包                                 |
| `idempotency_key`         | `str               | None`                                                      | `Idempotency-Key` 头；调度器传 occurrence 键    |
| `require_existing_thread` | `bool`             | 默认 `False`；MCP 通知、子代理回调传 `True`                            | thread 不存在时 404，而不是顺手建一条（§4.6）           |


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
§6.1 的 `_call_agent_factory` 调的就是这里的 `ctx.agent_factory`（M3 默认 `assemble_agent`），
再用 `unwrap_agent_graph` 取出裸图。

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
那个标志挂在共享的记录上，而且一旦置位就不会复原。只要 `create_or_reject` 在登记新记录之后
还有 await，同 key 的重试就可能在那个窗口里找到这条记录、把标志翻成 `True`；原始请求随后读到
`True`，以为自己是重试，于是不挂 worker——留下一条谁也不跑的 pending。M3 的准入在登记之后没有
await（§5.4 第 5 步），但这是一条靠纪律维持的性质，M9 很可能打破它（§5.5）。预分配的 `run_id`
是这次调用私有的，重试改不了它，所以不依赖那条纪律。

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
RunManager 只回答三件事：这条 thread 现在能不能开一个新操作、某个 run 走到了状态机的哪一格、
取消信号怎么送到 worker 手里。

#### 公开方法


| 方法                                                             | 谁调                      | 做什么                                            | 见                     |
| -------------------------------------------------------------- | ----------------------- | ---------------------------------------------- | --------------------- |
| `create_or_reject(...) -> RunRecord`                           | start_run               | 准入一个 run，得到 pending 记录                         | §5.4                  |
| `reserve_thread_operation(thread_id, *, kind, user_id)`        | compact、`POST /state` 等 | 非 run 操作的短命占位（async context manager）           | §5.3                  |
| `try_start(run_id) -> RunStartOutcome`                         | worker                  | pending → running 的唯一入口                        | §5.6                  |
| `fail_start_if_pending(run_id, *, error) -> bool`              | start_run、worker 第 0 步  | 挂不上 task / 严格校验失败时把 pending 收成 error           | §5.6                  |
| `wait_for_prior_finalizing(thread_id, run_id, *, abort_event)` | worker                  | 等同 thread 更早的 run 收完尾                          | §5.6                  |
| `update_run_progress(run_id, totals)`                          | worker                  | 运行中的 token 快照                                  | §5.6                  |
| `complete(run_id, *, status, error, totals) -> bool`           | worker 收尾               | 终态 + 汇总一次写完                                    | §5.6                  |
| `cancel(run_id, *, action) -> CancelOutcome`                   | cancel 路由、SSE 断开        | 协作式取消                                          | §5.9、§9               |
| `get(run_id, *, user_id) -> RunRecord                          | None`                   | 路由                                             | 内存优先，再从 store hydrate |
| `list_by_thread(thread_id, *, user_id, limit)`                 | 路由                      | 只列 run，不列预留                                    | §5.1                  |
| `has_inflight(thread_id) -> bool`                              | compact 等               | 本进程有没有活跃 run（含收尾中）                             | §5.3                  |
| `has_later_run(thread_id, run_id) -> bool`                     | worker 收尾               | 同 thread 是否已有更新的 run（旧 run 不得把 thread 写回 idle） | §6.2                  |
| `schedule_cleanup(run_id)`                                     | worker 收尾               | 延迟摘除内存记录                                       | §5.8                  |
| `shutdown(*, timeout)`                                         | lifespan                | 置 `_closing`、排空 worker                         | §5.9、§6.3             |


它不 publish 任何东西（§2 第二条），也不认识 thread_store、checkpointer、StreamBridge——
thread 状态投影和 END 帧都是 worker 的事。

#### 构造与内部状态

```python
# agentkit/runtime/runs/manager.py
RUN_RECORD_RETENTION_SECONDS = 300.0


class RunManager:
    def __init__(self, store: RunStore, *, retention_seconds: float = RUN_RECORD_RETENTION_SECONDS) -> None:
        self._store = store
        self._retention_seconds = retention_seconds
        self._runs: dict[str, RunRecord] = {}
        self._runs_by_thread: dict[str, dict[str, None]] = {}   # 有序集合：保持准入顺序，§5.6 靠它判断"更早"
        self._lock = asyncio.Lock()
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._closing = False                       # shutdown 置位；之后 _admit 拒新准入（§6.3）
```

`store` **不是可选参数。** M3 传 `MemoryRunStore`。DeerFlow 允许 `store=None`，结果每个方法都长出
一条 `if self._store is None` 分支——那条分支 M4 之后没人测，却一直留在代码里。

**一把进程锁，两条持锁纪律：**

1. 只有准入（§5.4）在持锁期间 await store：本地检查、store 插入、登记内存必须是一个原子单元。
2. 其余方法一律"锁内改内存，锁外写 store"。持锁 await 会把所有 thread 的准入排到一次慢 IO 后面。

M9 多实例下准入如果成了瓶颈，换成按 `thread_id` 分片的锁，上面两条不变。

### 5.1 双层存储


| 层                              | 存什么                                        | 生命周期         |
| ------------------------------ | ------------------------------------------ | ------------ |
| 内存 `_runs` + `_runs_by_thread` | `task`、`abort_event`、`start_lock`、token 计数 | 进程内，结束后延迟 GC |
| `RunStore`                     | 可序列化字段：status、kwargs、token 汇总              | 跨重启、跨进程      |


三条规则：

**同** `run_id` **内存优先。** `get()` 先看 `_runs`，没有才从 store hydrate。
hydrate 出来的记录标 `store_only=True`，没有本地 `task`。
如果顺序反了，一个 live run 会被换成一份没有 `task` 的快照——取消就失效了。

**内存命中也要过属主。** store 的 `get` 带 `user_id` 过滤，内存这一层不过滤的话，M9 里任何人
拿到别人的 `run_id` 就能读到 live 记录，进而取消它。DeerFlow 的 `get` 在内存命中时直接返回。

```python
async def get(self, run_id: str, *, user_id: str | None) -> RunRecord | None:
    async with self._lock:
        record = self._runs.get(run_id)
    if record is None:
        row = await self._store.get(run_id, user_id=user_id)
        async with self._lock:
            record = self._runs.get(run_id)          # store 往返期间可能刚登记了同一个 run
        if record is None:
            return _record_from_row(row) if row is not None else None      # store_only=True
    return record if user_id is None or record.user_id == user_id else None
```

await 之后**再查一次内存**不是多余的：store 往返期间同一个 run 可能刚被准入登记，
这时返回 store 里那份快照，就是"live run 被换成没有 task 的快照"。

**创建必须先写 store 再进内存。** 顺序反过来的话，store 失败时要回滚内存，而回滚本身可能被
取消打断，留下"内存里有、库里没有"。先 store 后内存，失败时内存里什么都没有，不需要回滚。

`_runs` **和** `_runs_by_thread` **必须在同一次持锁、中间不 await 里一起改。**
否则按 thread 列 run 会漏掉刚创建的那条。所以两者只经这两个函数修改：

```python
def _index_run_locked(self, record: RunRecord) -> None:
    self._runs[record.run_id] = record
    self._runs_by_thread.setdefault(record.thread_id, {})[record.run_id] = None


def _unindex_run_locked(self, record: RunRecord) -> None:
    self._runs.pop(record.run_id, None)
    bucket = self._runs_by_thread.get(record.thread_id)
    if bucket is not None:
        bucket.pop(record.run_id, None)
        if not bucket:
            del self._runs_by_thread[record.thread_id]


def _thread_records_locked(self, thread_id: str) -> list[RunRecord]:
    return [self._runs[run_id] for run_id in self._runs_by_thread.get(thread_id, ())]
```

两步写进同一个同步函数，"中间不 await"就由结构保证，不靠记性。`_thread_records_locked`
直接下标取、不做防御：两个结构只经上面两个函数修改，不一致就是 bug，应该在测试里炸出来，
而不是静默漏掉一条 run。

```python
async def list_by_thread(self, thread_id: str, *, user_id: str | None, limit: int = 100) -> list[RunRecord]:
    rows = await self._store.list_by_thread(thread_id, user_id=user_id, limit=limit)   # 只含 run，已过属主
    async with self._lock:
        live = {record.run_id: record for record in self._thread_records_locked(thread_id)}
    return [live.get(row["run_id"]) or _record_from_row(row) for row in rows]
```

分页和属主过滤都以 store 为准，内存只负责把 live 对象换进去，这样从列表里拿到的记录也能取消。
创建是先写 store 的，所以任何 live run 都已经在 store 里，列表不会漏掉刚准入的那条。

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
# agentkit/runtime/runs/store/base.py
ACTIVE_STATUSES = frozenset({"pending", "running"})


class RunStore(abc.ABC):
    # --- 准入：唯一一个必须原子的方法 ---
    @abc.abstractmethod
    async def create_thread_operation_atomic(
        self, row: dict, *, kind: ThreadOperationKind, supersede_active: bool = False
    ) -> bool:
        """在 (thread_id) WHERE status IN ('pending','running') 的部分唯一索引上抢位。

        supersede_active=True（interrupt / rollback）：先把该 thread 上活跃的 run 行标成 interrupted
        再插入，两步在同一个原子操作里；活跃行里有非 run 的预留时不顶替，返回 False。
        返回 False 表示没抢到位（thread 忙，或幂等键已被占）。不要抛 IntegrityError 给上层。"""

    # --- 状态迁移：全部是对活跃行的 CAS ---
    @abc.abstractmethod
    async def start_run(self, run_id: str) -> bool:
        """pending -> running。返回 False 表示已不是 pending（多半被取消了）。"""

    @abc.abstractmethod
    async def update_status(
        self, run_id: str, status: RunStatus, *, error: str | None = None, stop_reason: str | None = None
    ) -> bool:
        """WHERE status IN ('pending','running')。返回 False 表示行已是终态或不存在。"""

    @abc.abstractmethod
    async def update_run_progress(self, run_id: str, **totals: Any) -> None:
        """WHERE status = 'running'。CAS 落空就静默返回：run 已终态，进度数字不再有意义。"""

    @abc.abstractmethod
    async def update_run_completion(
        self, run_id: str, *, status: RunStatus, error: str | None = None, **totals: Any
    ) -> bool:
        """终态 + token 汇总一次写完。WHERE status IN ('pending','running') OR status = :status：
        同一个终态可以写两遍（崩溃重试），换成另一个终态被拒，返回 False。"""

    # --- 读：user_id 没有默认值 ---
    @abc.abstractmethod
    async def get(self, run_id: str, *, user_id: str | None) -> dict | None: ...

    @abc.abstractmethod
    async def list_by_thread(self, thread_id: str, *, user_id: str | None, limit: int = 100) -> list[dict]:
        """只返回 operation_kind='run' 的行，created_at DESC。"""

    @abc.abstractmethod
    async def list_inflight(self) -> list[dict]:
        """启动时回收孤儿用：所有 pending/running 的行，不按 user_id 过滤。"""

    @abc.abstractmethod
    async def get_by_idempotency_key(self, scoped_key: str) -> dict | None: ...

    # --- 删 ---
    @abc.abstractmethod
    async def delete_thread_operation(self, run_id: str, *, user_id: str | None) -> None:
        """释放非 run 的短命预留行。对 operation_kind='run' 的行调用要抛 ValueError。"""

    # --- M9 预留：M3 不实现，但签名现在定 ---
    # renew_lease / claim_for_takeover / request_cancel /
    # finalize_if_not_cancelled / list_inflight_with_expired_lease
```

**三个签名细节值得现在就定死：**

**一、准入方法返回** `bool`**，不抛** `IntegrityError`**。** 唯一索引冲突是**预期内的正常结果**
（用户手快点了两次发送），不是异常。让它冒到路由层意味着每个调用点都要 catch 一个
数据库特定的异常类型——而 SQLite 和 Postgres 抛的还不是同一个。

`False` 有两种含义：thread 忙，或者幂等键已被占。返回值不区分它们，调用方紧接着查一次
`get_by_idempotency_key` 就能分清（§5.4）——两个 worker 同时拿同一个键准入时，输的那个
正是靠这一查拿到赢家的 run。

**二、所有读方法都带** `user_id` **参数。** M3 只有单用户，传 `None` 即可。但这个参数位现在留着，
M9 加多租户时改的是实现，不是几十个调用点。更关键的是**默认值不能是** `None`——
`user_id: str | None` 但不给默认值，强迫每个调用点显式表态"我这里确实要全局查"。
给了默认值，M9 时会有一半调用点在无声地跨用户查询。

**三、**`update_status` **和** `update_run_completion` **分开。** 前者是中途的状态变更，后者是终态
加汇总的一次性写入。合并成一个方法会让"终态守卫"这条规则难以表达——中途变更允许覆盖，
终态不允许。

三个 CAS 方法（`start_run`、`update_status`、`update_run_completion`）都返回 `bool`。
`False` 是正常结果——取消和完成撞车（§9.3）、崩溃后重复收尾——调用方据此决定要不要改内存、
要不要告警。返回 `None` 的话，这些竞态只能靠再读一遍 store 去猜。

#### 行和记录的互转

```python
def _record_to_row(record: RunRecord) -> dict:
    """只取第一组和第三组字段；StrEnum 存成普通字符串。"""


def _record_from_row(row: dict) -> RunRecord:
    """第二组（task、abort_event、start_lock…）全部是新建的空值，并标 store_only=True。"""
    return RunRecord(
        run_id=row["run_id"],
        thread_id=row["thread_id"],
        assistant_id=row.get("assistant_id"),
        status=RunStatus(row.get("status") or RunStatus.pending),
        on_disconnect=DisconnectMode(row.get("on_disconnect") or DisconnectMode.cancel),
        operation_kind=ThreadOperationKind(row.get("operation_kind") or ThreadOperationKind.run),
        metadata=row.get("metadata") or {},
        kwargs=row.get("kwargs") or {},
        # ……其余第一组、第三组字段按名抄，缺省 None / 0 / {}
        store_only=True,
    )
```

缺列时的缺省值（`pending` / `cancel` / `run`）是给 M4 迁移前的旧行准备的：加列之前写入的行
读回来不能炸。hydrate 出来的记录**不进** `_runs`：`_runs` 只放本进程拥有或刚刚拥有过的记录，
放进去就没人负责 cleanup，还会在准入时被当成本地活跃 run。

#### 内存实现：原子性来自"没有 await"

```python
# agentkit/runtime/runs/store/memory.py
class MemoryRunStore(RunStore):
    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self._run_id_by_idempotency_key: dict[str, str] = {}

    async def create_thread_operation_atomic(self, row, *, kind, supersede_active=False) -> bool:
        # 函数体内没有 await，事件循环不会在中间切走——这就是全部的原子性。
        # 以后谁要在这里加 await，必须先加一把锁。
        key = row.get("idempotency_key")
        if key is not None and key in self._run_id_by_idempotency_key:
            return False
        active = [r for r in self._rows.values() if r["thread_id"] == row["thread_id"] and r["status"] in ACTIVE_STATUSES]
        if active:
            if not supersede_active or any(r["operation_kind"] != ThreadOperationKind.run for r in active):
                return False
            for r in active:
                r["status"], r["updated_at"] = RunStatus.interrupted.value, row["created_at"]
        self._rows[row["run_id"]] = {**row, "operation_kind": kind.value}
        if key is not None:
            self._run_id_by_idempotency_key[key] = row["run_id"]
        return True

    async def get(self, run_id, *, user_id):
        row = self._rows.get(run_id)
        if row is None or (user_id is not None and row.get("user_id") != user_id):
            return None
        return copy.deepcopy(row)        # 读方法一律返回副本
```

内存实现要和 M4 的 SQL 实现跑**同一套契约测试**（§11 的 `test_run_store_contract.py`）。
读方法返回副本是契约的一部分：调用方改了拿到的 dict，不能改到存储里——SQL 实现天然如此，
内存实现不复制的话，测试在内存上过、到 M4 才发现有代码依赖了"改返回值就改了库"。

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


| 从         | 到                               | 谁触发                              | 方法                            |
| --------- | ------------------------------- | -------------------------------- | ----------------------------- |
| ——        | `pending`                       | 准入                               | `create_or_reject`            |
| `pending` | `running`                       | worker 启动栅栏                      | `try_start`                   |
| `pending` | `interrupted`                   | 启动前被取消；被 interrupt / rollback 顶替 | `cancel` / `create_or_reject` |
| `pending` | `error`                         | task 挂不上；严格 thread 校验失败          | `fail_start_if_pending`       |
| `running` | `success` / `error` / `timeout` | worker 收尾                        | `complete`                    |
| `running` | `interrupted`                   | 取消；被顶替                           | `cancel` / `create_or_reject` |
| 终态        | 任何                              | ——                               | 一律拒绝                          |


`timeout` 由 worker 的整体超时产生（`asyncio.timeout` 包住整轮执行，时长来自配置）。
M3 可以先不配超时，但状态和迁移现在就要在。

```python
_ACTIVE = frozenset({RunStatus.pending, RunStatus.running})
_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.pending: frozenset({RunStatus.running, RunStatus.interrupted, RunStatus.error}),
    RunStatus.running: frozenset({RunStatus.success, RunStatus.error, RunStatus.timeout, RunStatus.interrupted}),
}


class InvalidRunTransition(RuntimeError):
    """活跃状态下的非法迁移：调用顺序错了，是 bug。"""


def _transition_locked(record: RunRecord, target: RunStatus, *, error: str | None = None) -> bool:
    """改内存状态。返回 False 表示记录已是终态——竞态里的输家，正常结果。"""
    if record.status not in _ACTIVE:
        return False
    if target not in _ALLOWED_TRANSITIONS[record.status]:
        raise InvalidRunTransition(f"{record.run_id}: {record.status} -> {target}")
    record.status = target
    if error is not None:
        record.error = error
    record.updated_at = now_iso()
    return True
```

**"不允许"分两种，处理方式相反。** 从终态出发的迁移是竞态的输家（取消之后才到的完成、
重复收尾），返回 `False`，调用方照常往下走。从活跃状态出发、但目标不在表里的迁移
（比如 pending 直接到 success，说明 worker 跳过了 `try_start`）是调用顺序错了，要抛。
混成一种的话，要么竞态把进程炸掉，要么 bug 被静默吞掉。

`record.status =`  **在整个包里只出现在这个函数里。** §11 的 `test_run_manager.py` 用 grep 钉住：
状态迁移的规则只有写在一处，才能用一张表测全。

`finalizing` **不是状态机上的一档。** 它是 `RunRecord` 上的一个布尔标志，
表示 worker 在 `finally` 里收尾（写标题、同步 thread 状态），
此时它**仍然占着"每 thread 一条活跃"的名额**。

把它做成一个状态会污染所有 `status IN (...)` 的查询，而它的唯一作用是让下一个 run
的 `wait_for_prior_finalizing` 知道要等。

它**只在本进程内存里占位**。取消会立刻把 store 行写成 `interrupted`（§5.9），部分唯一索引从那一刻起
就不再算它；worker 这时可能还在 `finally` 里回滚 checkpoint。M3 单进程靠准入的本地检查兜住
（§5.4 第 2 步把 `finalizing` 的记录也算作占位）；跨进程的收尾期占位是 M9 的事。

另一个要点：**thread 元数据的** `running` **只在** `try_start` **成功之后才写。**
pending 阶段被取消的话，侧栏不该先闪一下 busy 再改回来。

### 5.3 唯一约束的粒度是"thread 操作"，不是"run"

```python
class ThreadOperationKind(StrEnum):
    run = "run"
    checkpoint_write = "checkpoint_write"   # compact、POST /state
    artifact_write = "artifact_write"
    branch = "branch"
    delete = "delete"
```

成员名用小写，和 `RunStatus.pending`、`DisconnectMode.cancel` 一致；`RunRecord` 的缺省值写的就是
`ThreadOperationKind.run`。

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

```python
@asynccontextmanager
async def reserve_thread_operation(
    self, thread_id: str, *, kind: ThreadOperationKind, user_id: str | None
) -> AsyncIterator[RunRecord]:
    if kind is ThreadOperationKind.run:
        raise ValueError("normal runs must be admitted with create_or_reject()")
    record = await self._admit(
        thread_id, None, kind=kind, multitask_strategy="reject", user_id=user_id,
        on_disconnect=DisconnectMode.continue_,          # 预留没有 SSE 连接，这个字段只是为了填满必填项
    )
    try:
        yield record
    finally:
        try:
            await self._store.delete_thread_operation(record.run_id, user_id=record.user_id)
        except Exception:
            logger.warning("Failed to release thread operation %s", record.run_id, exc_info=True)
        finally:
            async with self._lock:
                self._unindex_run_locked(record)


async def has_inflight(self, thread_id: str) -> bool:
    async with self._lock:
        return any(
            r.operation_kind is ThreadOperationKind.run and (r.status in _ACTIVE or r.finalizing)
            for r in self._thread_records_locked(thread_id)
        )
```

- **预留只走** `reject`**，也不能被顶替。** 对一条持有预留的 thread 发 interrupt / rollback，
得到的是 409（§5.4 第 2 步），而不是把 compact 掐在半路——写到一半的 checkpoint 就是损坏的状态。
- **释放用准入时记下的** `record.user_id`，不读当时的请求上下文。`finally` 可能跑在别的
context 里（客户端断开、lifespan 收尾），那时读到的身份不可靠。
- **store 删行失败，内存照样摘。** 代价是那条 thread 在下次启动回收（M4 `recover_inflight_runs`）
之前一直是忙的。宁可忙，也不要留一条内存里有、却没人会释放的记录。M4 给这次删除加有限重试。
- `has_inflight` 只数 `run`，并把收尾中的也算上：compact 这类调用方关心的是
"有没有人还在写这条 thread 的 checkpoint"。



### 5.4 准入：只用 `create_or_reject`

不要提供一个裸的 `create()`（只做 put）。并发下它会变成生的 `IntegrityError` 冒到路由层，
用户看到 500 而不是 409。

**锁顺序是这里的不变量：握住进程锁，做完本地检查 + store insert + 登记内存，再放锁。**
如果 store insert 在锁外，会出现"store 里已经插入了，本进程却返回 ConflictError 把调用方打回去"，
留下一行谁也不认领的幽灵 pending。

跨进程靠唯一索引，不靠这把进程锁——进程锁只是为了让本进程的三步保持原子。

```python
_STRATEGIES = frozenset({"reject", "interrupt", "rollback"})


async def create_or_reject(
    self,
    thread_id: str,
    assistant_id: str | None,
    *,
    run_id: str | None = None,                 # start_run 预分配（§4.5）；缺省时这里生成
    on_disconnect: DisconnectMode = DisconnectMode.cancel,
    multitask_strategy: str = "reject",
    metadata: dict | None = None,
    kwargs: dict | None = None,                # 脱敏后的原始请求（§4.5）
    model_name: str | None = None,
    user_id: str | None = None,
    idempotency_key: str | None = None,        # 已加 scope（§4.5）
) -> RunRecord:
    return await self._admit(
        thread_id, assistant_id, kind=ThreadOperationKind.run, run_id=run_id,
        on_disconnect=on_disconnect, multitask_strategy=multitask_strategy,
        metadata=metadata or {}, kwargs=kwargs or {}, model_name=model_name,
        user_id=user_id, idempotency_key=idempotency_key,
    )


async def _admit(self, thread_id, assistant_id, *, kind, run_id=None, multitask_strategy="reject",
                 idempotency_key=None, user_id=None, **fields) -> RunRecord:
    if multitask_strategy not in _STRATEGIES:
        raise UnsupportedStrategyError(multitask_strategy)
    now = now_iso()
    record = RunRecord(
        run_id=run_id or new_run_id(), thread_id=thread_id, assistant_id=assistant_id,
        status=RunStatus.pending, operation_kind=kind, multitask_strategy=multitask_strategy,
        user_id=user_id, idempotency_key=idempotency_key, created_at=now, updated_at=now, **fields,
    )
    supersede = multitask_strategy != "reject"

    async with self._lock:
        # 1. 幂等：先内存，再 store。命中就直接返回，不看 thread 忙不忙，也不看 _closing
        if idempotency_key is not None:
            if (reused := await self._find_idempotent_locked(idempotency_key, thread_id, user_id)) is not None:
                return reused

        if self._closing:
            raise ConflictError("Gateway is shutting down")

        # 2. 本进程占位：活跃的、还在收尾的，都算
        occupants = [r for r in self._thread_records_locked(thread_id) if r.status in _ACTIVE or r.finalizing]
        if occupants and not supersede:
            raise ConflictError(f"Thread {thread_id} already has an active operation")
        if any(r.operation_kind is not ThreadOperationKind.run for r in occupants):
            raise ConflictError(f"Thread {thread_id} has an active non-run operation")

        # 3. store 抢位：跨进程的真相
        admitted = await self._store.create_thread_operation_atomic(
            _record_to_row(record), kind=kind, supersede_active=supersede,
        )
        if not admitted:
            if idempotency_key is not None:
                if (reused := await self._find_idempotent_locked(idempotency_key, thread_id, user_id)) is not None:
                    return reused              # 另一个 worker 拿同一个键先到了一步
            raise ConflictError(f"Thread {thread_id} already has an active operation")

        # 4. 登记内存：必须在 store 成功之后（§5.1）
        self._index_run_locked(record)

        # 5. 顶替旧 run：只改内存，不 await（§5.5）
        for old in occupants:
            _abort_locked(old, action=multitask_strategy)
    return record


async def _find_idempotent_locked(self, key: str, thread_id: str, user_id: str | None) -> RunRecord | None:
    existing = next((r for r in self._runs.values() if r.idempotency_key == key), None)
    if existing is None and (row := await self._store.get_by_idempotency_key(key)) is not None:
        existing = _record_from_row(row)
    if existing is None:
        return None
    if existing.thread_id != thread_id or existing.user_id != user_id:
        raise RuntimeError("idempotency key resolved to a different thread or user")   # scope 保证不会发生
    existing.idempotency_reused = True
    return existing
```

五步的顺序各有理由：

- **第 1 步在第 2 步之前。** 重试一个还在跑的 run，应该拿回那个 run，而不是 409。
- **第 2 步把** `finalizing` **也算作占位**，并且预留不可顶替。所以 `reject` 碰到收尾中的旧 run 也是 409——
刚点完停止马上发送，窗口通常只有几十毫秒；前端"停止后立刻发送"的路径应该用 `interrupt`。
- **第 3 步返回** `False` **后再查一次幂等键**，这就是 §5.1"三个签名细节"第一条说的分辨方式。
- **第 4 步在 store 成功之后**，失败时内存里什么都没有。
- **第 5 步之后直接返回。** 登记之后没有 await，是 §5.5 那个边角在 M3 里不存在的原因。

内存里最多只有保留期（§5.8）内的记录，幂等查找用线性扫描就够了；另建索引的话，
它就成了第三个必须和 `_runs` 同步修改的结构。`RuntimeError` 那一行在正确的 scope（§4.5）下
走不到——键里已经编进了 owner 和 thread——走到了就是 scope 写错了，应该大声失败而不是把别人的 run 返回出去。

### 5.5 `multitask_strategy`

决定"下一次准入怎么对待还活着的这一次"：


| 策略                   | 对旧 run                        | 对新 run     |
| -------------------- | ----------------------------- | ---------- |
| `reject`（默认，Web 发送用） | 不动                            | 不开，调用方 409 |
| `interrupt`          | 标 `interrupted`，保留 checkpoint | 旧的让位后开始    |
| `rollback`           | 同上，但状态滚回 run 前                | 同上         |


`interrupt`/`rollback` 的顺序是：**先原子插入新行，再去标记旧 run**。
反过来做，两者之间的窗口里这条 thread 是"空闲"的，第三个请求能挤进来。

M3 把两步压进 store 的同一次原子调用（`supersede_active=True`），内存侧的标记在锁内同步完成：

```python
def _abort_locked(record: RunRecord, *, action: str) -> None:
    """顶替（§5.4 第 5 步）和取消（§5.9）共用的内存侧动作。store 行由调用方负责：
    顶替时已在准入第 3 步的同一个原子操作里标好，取消时在锁外补写。"""
    was_running = record.status is RunStatus.running
    if not _transition_locked(record, RunStatus.interrupted):
        return                                   # 已是终态、只是还在收尾：不用再管
    record.abort_action = action                 # "interrupt" / "rollback"，worker 在 finally 里照做
    record.abort_event.set()
    record.finalizing = record.task is not None and not record.task.done()
    if was_running and record.finalizing:
        record.task.cancel()                     # pending 只置 abort，不 cancel（§4.6）
```

旧 run 的 worker 在下一轮 astream 看到 abort（running 的还会收到 `CancelledError`），在 `finally` 里
按 `abort_action` 回滚或保留 checkpoint、写终态、发 END，最后清掉 `finalizing`。新 run 的 worker
在 `try_start` 之前 `wait_for_prior_finalizing`（§5.6）——所以 rollback 之后的新一轮，
一定是从回滚完的 checkpoint 开始的。

还有一个边角：登记之后如果还有 await，调用方在那个 await 上被取消，刚准入、还没人看见的新 run
就会永远挂在 pending。M3 的做法是让这个窗口不存在：准入第 5 步之后直接返回。这是一条靠纪律维持的
性质——M9 如果要在这里加"通知远端 owner"之类的 await，就必须同时加上补偿：

```python
try:
    await self._notify_remote_owners(superseded)       # M9 才有的 await
except asyncio.CancelledError:
    cleanup = asyncio.create_task(self._close_unseen_admission(record))
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)              # 调用方反复被取消，也要等补偿跑完
        except asyncio.CancelledError:
            pass
    raise
```

`_close_unseen_admission` 把新行用 `update_status(interrupted)` 收掉，再从内存摘除。同一套补偿
也适用于准入第 3 步：SQL 实现的插入可能在调用方被取消时已经提交。M3 的内存 store 在那一步
没有让出点，不会发生；M4 要把插入 shield 住，插入成功就走这段补偿。

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

```python
class RunStartOutcome(StrEnum):
    started = "started"
    cancelled = "cancelled"


class RunStartupError(RuntimeError):
    """store 没法确认启动结果。worker 按 error 收尾。"""


async def try_start(self, run_id: str) -> RunStartOutcome:
    async with self._lock:
        record = self._runs.get(run_id)
    if record is None:
        raise RunStartupError(f"Cannot start unknown run {run_id}")
    async with record.start_lock:
        async with self._lock:                                   # ① 不做 IO 就能拒掉的先拒
            if record.abort_event.is_set() or record.status is not RunStatus.pending:
                return RunStartOutcome.cancelled
        try:
            started = await self._store.start_run(run_id)        # ② CAS，在管理器锁外
        except Exception as exc:
            raise RunStartupError(f"Failed to start run {run_id}") from exc
        async with self._lock:                                   # ③ CAS 期间到达的取消
            if started and not record.abort_event.is_set():
                _transition_locked(record, RunStatus.running)
                return RunStartOutcome.started
            if not started:                                      # store 说它已经不是 pending
                _transition_locked(record, RunStatus.interrupted)
                record.abort_event.set()
                return RunStartOutcome.cancelled
        # CAS 把行改成了 running，但取消先到：补写 interrupted，兜住 cancel 那次写 store 失败的情况
        await self._store.update_status(run_id, RunStatus.interrupted)
        return RunStartOutcome.cancelled
```

**store 抛异常不能当成 cancelled。** CAS 可能已经提交、行已经是 running；当成取消直接返回，
就留下一行没人收尾的 running。抛 `RunStartupError`，worker 走 error 收尾（§6.2），`complete`
的 CAS 不管 store 行是 pending 还是 running 都能把它收掉。

#### 启动失败、等前一个收尾

```python
async def fail_start_if_pending(self, run_id: str, *, error: str) -> bool:
    async with self._lock:
        record = self._runs.get(run_id)
        if record is None or record.status is not RunStatus.pending:
            return False                         # 已经启动的交给 worker 自己的收尾路径
        _transition_locked(record, RunStatus.error, error=error)
        record.abort_event.set()                 # 随后的 try_start 走 cancelled
    await self._store.update_status(run_id, RunStatus.error, error=error)
    return True


async def wait_for_prior_finalizing(
    self, thread_id: str, run_id: str, *, abort_event: asyncio.Event | None = None, poll_interval: float = 0.01
) -> None:
    while True:
        async with self._lock:
            prior: list[RunRecord] = []
            for record in self._thread_records_locked(thread_id):
                if record.run_id == run_id:
                    break
                prior.append(record)
            else:
                return                           # 自己已不在登记表里，没什么可等
            if not any(r.finalizing for r in prior):
                return
        if abort_event is not None and abort_event.is_set():
            return                               # 自己被取消了就别等，交给 try_start 去拒
        await asyncio.sleep(poll_interval)
```

`fail_start_if_pending` 置 abort 那一行是 §4.6 能成立的前提：worker 第 0 步严格校验失败后照常进
`run_agent`，靠的就是 `try_start` 看到这一位直接返回 cancelled、不组图。

"更早"按 `_runs_by_thread` 的插入顺序判断，这就是它用有序 dict 而不是 `set` 的原因。只等排在
自己前面的：排在后面的 run 在收尾，与这一轮无关。轮询间隔 10 ms——收尾窗口很短，而换成
`Condition` 需要每一次写 `finalizing` 都经过通知，§6.2 的骨架是直接赋值的。
§6.2 骨架调用这个方法时要传 `run_id` 和 `abort_event=record.abort_event`。

#### 运行中的快照与收尾

```python
@dataclass(frozen=True, slots=True)
class RunTotals:
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    llm_call_count: int = 0
    lead_agent_tokens: int = 0
    subagent_tokens: int = 0
    middleware_tokens: int = 0
    token_usage_by_model: Mapping[str, Mapping[str, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        attributed = self.lead_agent_tokens + self.subagent_tokens + self.middleware_tokens
        total = self.total_input_tokens + self.total_output_tokens
        if attributed != total:
            logger.error("Token attribution %d does not add up to total %d", attributed, total)


async def update_run_progress(self, run_id: str, totals: RunTotals) -> None:
    async with self._lock:
        record = self._runs.get(run_id)
        if record is None or record.status is not RunStatus.running:
            return
        _apply_totals(record, totals)
    await self._store.update_run_progress(run_id, **asdict(totals))


async def complete(
    self, run_id: str, *, status: RunStatus, error: str | None = None, totals: RunTotals | None = None
) -> bool:
    """worker 收尾的唯一出口。返回 False 表示终态已经被别人定了（多半是取消）。"""
    if status in _ACTIVE:
        raise ValueError(f"complete() needs a terminal status, got {status}")
    async with self._lock:
        record = self._runs[run_id]
        won = _transition_locked(record, status, error=error)
        if totals is not None:
            _apply_totals(record, totals)
        final_status, final_error = record.status, record.error      # 输了就是赢家的终态
    written = await self._store.update_run_completion(
        run_id, status=final_status, error=final_error, **(asdict(totals) if totals else {}),
    )
    return won and written
```

- **分解对不上只打 error，不抛。** "三个维度之和等于总数"是记账的不变量（§5.1），但收尾路径上的
一次记账 bug 不该让终态写不进去。测试里把这条日志当失败。
- **输给取消也要把 token 写进去。** 被取消的 run 同样花了钱。用赢家的终态去写，store 的
"同一个终态可以写两遍"正好放行，而"换成另一个终态"被拒——这就是 §9.3 说的"已经被接受的取消，
不能被后到的 success 覆盖"。
- **终态和汇总一次写完。** 分两次写，中间崩溃会留下"终态有了、token 全是 0"的行，账单少算。
- `complete` 不碰 `finalizing`、END、cleanup：那是 worker 嵌套 `finally` 的事（§6.2）。
§6.2 骨架里的 `_finalize(...)` 就是调这个方法，之后再 `schedule_cleanup`（§5.8）。



### 5.7 幂等键必须加 scope

```python
# §4.5 的 scope_idempotency_key：在 start_run 里做，所有入口共用
scoped_key = "run:" + sha256("\0".join((caller.kind, caller.user_id or "", thread_id, raw_key)))
```

```sql
CREATE UNIQUE INDEX ON runs (idempotency_key) WHERE idempotency_key IS NOT NULL;
```

store 上的幂等索引是**进程全局**的。直接用客户端传来的 `Idempotency-Key`，
两个不同用户用了同一个字符串（比如 `"1"`）就会串号——A 的重试拿到 B 的 run。

命中已有 key 时返回那条记录并设 `idempotency_reused=True`（§5.4 第 1 步，以及第 3 步失败后的复查）。
**调用方看到这个标志就不能再** `create_task`，否则同一个 run 会跑两遍。start_run 判断"是不是复用"
比对的是预分配的 `run_id`，不读这个标志（§4.5）：标志挂在共享记录上、置位后不复原，
只适合"这条记录被复用过"这类观察。

如果重试的 `input` 和第一次不一致，start_run 抛 `IdempotencyKeyReused`，路由编成 409（§4.5）——
这说明客户端复用了 key 但改了内容，是 bug 不是重试。

键跟着 run 行走：行还在，键就一直有效；M4 的保留策略清掉 runs 行时，键随之失效。
索引只管非空键，没带 `Idempotency-Key` 的请求不受影响。

### 5.8 结束后延迟 GC

```python
def schedule_cleanup(self, run_id: str) -> None:
    task = asyncio.create_task(
        self._cleanup_after(run_id, self._retention_seconds),
        context=contextvars.Context(),           # 空 context：不攥住请求的 user / trace
    )
    self._cleanup_tasks.add(task)                # 事件循环只弱引用 task，这里留强引用
    task.add_done_callback(self._cleanup_tasks.discard)


async def _cleanup_after(self, run_id: str, delay: float) -> None:
    await asyncio.sleep(delay)
    async with self._lock:
        record = self._runs.get(run_id)
        if record is None:
            return
        if record.status in _ACTIVE or record.finalizing:
            logger.warning("Refusing to clean up active run %s", run_id)
            return
        self._unindex_run_locked(record)
```

run 结束后不要立刻从内存注册表摘掉。晚到的 `/join` 请求（用户刚好在这时刷新页面）
需要拿到 live 引用才能读到完整的结束状态。5 分钟（`retention_seconds`）是个够用的经验值。

- **调度，不 await。** 早先的写法是在 worker 里 `await run_manager.cleanup(run_id, delay=300)`，
那会让 worker task 在 run 结束后再活 5 分钟：关机排空（§6.3）要等它，`asyncio.gather` 以为 run 还在跑。
- **空 context。** `create_task` 默认复制当前 context，延迟任务会把请求的 user、trace 这些 ContextVar
攥住 5 分钟，连同它们引用的对象。
- **只摘终态且不在收尾的。** 活记录被摘掉，准入的本地占位检查（§5.4 第 2 步）就看不见它，thread 会被当成空闲。
- **store 里的行不删。** run 历史本来就该留着（`list_by_thread`）。内存 store 的行随进程重启消失；
M4 的持久行归保留策略管。
- `shutdown` 时取消所有还没到点的 cleanup：进程要退出了，内存本来就要整个丢掉。



### 5.9 `cancel()` 与关机

§9 讲取消的协议和 HTTP 映射，这里只讲 RunManager 里改了什么。

```python
class CancelOutcome(StrEnum):
    cancelled = "cancelled"
    not_active_locally = "not_active_locally"
    not_cancellable = "not_cancellable"
    unknown = "unknown"
    requested = "requested"          # M9
    taken_over = "taken_over"        # M9


async def cancel(self, run_id: str, *, action: Literal["interrupt", "rollback"] = "interrupt") -> CancelOutcome:
    async with self._lock:
        record = self._runs.get(run_id)
        if record is not None:
            if record.status is RunStatus.interrupted and record.abort_event.is_set():
                return CancelOutcome.cancelled                   # 重复取消：幂等
            if record.status not in _ACTIVE:
                return CancelOutcome.not_cancellable
            _abort_locked(record, action=action)                 # 和顶替旧 run 是同一件事（§5.5）
    if record is None:
        row = await self._store.get(run_id, user_id=None)        # 属主已由路由用 get(..., user_id=) 查过
        if row is None:
            return CancelOutcome.unknown
        return CancelOutcome.not_active_locally if row["status"] in ACTIVE_STATUSES else CancelOutcome.not_cancellable
    await self._store.update_status(run_id, RunStatus.interrupted)
    return CancelOutcome.cancelled
```

**取消立刻写** `interrupted`**，不等 worker。** worker 可能卡在一次不理会取消的工具调用里；
等它自己写的话，用户点了停止，界面上却一直是 running。`_abort_locked` 同时置上 `finalizing`，
所以在 worker 真正退出之前，这条 thread 在本进程里仍然是占着的（§5.2）。

**胜负在锁内决出，store 只负责落盘。** 取消和完成各自先在管理器锁内做内存迁移：先到的赢，
后到的 `_transition_locked` 返回 `False`。锁外的 store 写入带的永远是内存胜者的终态
（`complete` 输了就写赢家的状态，§5.6），所以两次写入谁先落盘都不影响结果。
这是 §9.3"取消不被后到的 success 覆盖"在单进程里的实现；跨进程由 M9 的 CAS 接手。

M3 只会产生前四种结果，和 §9.2 的表对应：本进程内存里有、还活着 → `cancelled`；
内存里有、已终态 → `not_cancellable`；内存里没有、store 里还活着（M3 单进程下只可能是重启前留下的孤儿）
→ `not_active_locally`；哪里都没有 → `unknown`。

```python
async def shutdown(self, *, timeout: float) -> None:
    """§6.3 的第一步：拒新准入，再让 run 自己停，超时再强制。之后 lifespan 才能关 checkpointer。"""
    async with self._lock:
        self._closing = True
        live = [r for r in self._runs.values() if r.status in _ACTIVE and r.task is not None and not r.task.done()]
        for record in live:
            record.abort_action = "interrupt"
            record.abort_event.set()
            if record.status is RunStatus.running:
                record.task.cancel()                             # pending 只置 abort（§4.6）
    if live:
        _, stuck = await asyncio.wait([r.task for r in live], timeout=timeout)
        for task in stuck:
            task.cancel()                                        # 强制：§4.6 说的唯一例外
        if stuck:
            await asyncio.wait(stuck, timeout=1.0)
    for task in list(self._cleanup_tasks):
        task.cancel()
```

关机**不替 worker 写终态**：只置 abort，由 worker 的 `finally` 走正常收尾（写 `interrupted`、发 END）。
超时后还没退出的 task 被强制取消，它们留下的活跃行由下次启动时的孤儿回收收掉（§13、M4 `recover_inflight_runs`）。

---



## 6. Worker（W2）

#### worker 的公开符号

worker 是一组函数，不是一个类。**唯一的入口是** `run_after_metadata`——`start_run` 第 14 步用
`create_task` 挂上的就是它；其余符号要么是它调用的阶段函数，要么是给测试和后续阶段用的钩子。


| 符号                                                                                                  | 谁调                            | 做什么                                              | 见         |
| --------------------------------------------------------------------------------------------------- | ----------------------------- | ------------------------------------------------ | --------- |
| `run_after_metadata(record, run_ctx, *, bridge, run_manager, owner_user_id, require_existing_thread)` | `start_run` 第 14 步（`create_task`） | 第 0 步：尽力补 thread 行，再进 `run_agent`               | §4.6      |
| `run_agent(bridge, run_manager, record, ctx)`                                                        | `run_after_metadata`；测试      | 启动栅栏 → 组图 → `astream` → 收尾 → END                 | §6.2      |
| `_stream_graph(bridge, record, ctx, graph, usage)`                                                   | `run_agent` 第 7 步             | 解包 → 序列化 → publish；每帧查 abort                     | §6.2      |
| `_finalize(bridge, run_manager, record, ctx, *, terminal, error, error_type, rollback, usage)`       | `run_agent` 内层 `finally`     | 回滚 → 终态 + token → 投影                             | §6.2      |
| `_call_agent_factory(factory, run_config, *, app_config, checkpointer)`                              | `run_agent` 第 6 步             | 按工厂签名传参，不盲传                                      | §6.1      |
| `unwrap_agent_graph(result)`                                                                         | `run_agent` 第 6 步             | `AgentAssembly` 取 `.graph`；裸图恒等                 | §6.1      |
| `_install_runtime(run_config, record)`                                                               | `run_agent` 第 6 步之前           | 把官方 `Runtime` 挂进 `configurable["__pregel_runtime"]` | §6.2      |
| `capture_rollback_point(ctx, thread_id)`                                                             | `run_agent` 第 4 步             | 只读 head，得到还原点                                    | §6.4      |
| `restore_rollback_point(ctx, point)`                                                                 | `_finalize` ①                  | 在运行前的 head 上写一个子 checkpoint                      | §6.4      |
| `UsageCollector(run_manager, run_id)`                                                                | `_stream_graph`、`_finalize`    | 挂在调用根上的记账回调                                      | §6.5      |
| `spawn_detached(coro)` / `cancel_detached()`                                                        | worker 收尾 / lifespan          | 不属于任何请求的后台任务                                     | §6.2      |
| `serialize(chunk, *, mode)` / `to_jsonable(obj)`                                                   | `_stream_graph`；M1 嵌入式客户端共用   | wire 形状转换                                        | §6.2      |
| `normalize_stream_modes(raw)` / `to_langgraph_stream_modes(raw)`                                   | `start_run` 第 3 步 / `_stream_graph` | 公开名 ↔ LangGraph 内部名                              | §6.2      |
| `unpack_stream_item(item)` / `sse_event_name(mode, namespace)`                                     | `_stream_graph`                 | 解包一项、定事件名                                        | §6.2      |
| `RunAborted` / `RollbackPoint` / `RollbackUnavailable`                                            | worker 内部                     | 见各节                                              | §6.2、§6.4 |


三点边界：

- **这一层不认识 HTTP。** worker 模块没有 `fastapi` 的 import（`test_run_agent.py` 用 AST 断言，§11）；
  失败用领域异常或终态表达，不抛 `HTTPException`。状态码、响应头、`StreamingResponse` 全在 app 层。
- **入口只有一个。** 生产路径必须经过 `run_after_metadata`：它保证"准入之后无论如何都会走到
  `try_start` 和 END"。测试可以直接调 `run_agent`（§11 的用例就是这么写的），但那是测试的便利，
  不是第二条路径。
- **加符号前先问它属于哪一类。** 入口、阶段函数，还是钩子？三者都不是的话，八成应该塞进
  `RunContext`（§6.1 的字段表），而不是往参数列表上再加一个。

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


| #   | 阶段                                           | 失败 / 取消时                                          | 终态                               |
| --- | -------------------------------------------- | ------------------------------------------------- | -------------------------------- |
| 0   | `run_after_metadata` 补 thread 行（§4.6）        | 只告警；严格模式 → `fail_start_if_pending`                | ——                               |
| 1   | `wait_for_prior_finalizing`                  | 自己被 abort 就不再等                                    | ——                               |
| 2   | `try_start`                                  | cancelled → 直接 `return`                           | `interrupted`（或第 0 步写下的 `error`） |
| 3   | thread 状态 → running                          | 抛 → 走 except                                      | `error`                          |
| 4   | 捕获回滚点（§6.4）                                  | 捕获失败只告警，run 照跑，之后的 rollback 退化成 interrupt         | ——                               |
| 5   | publish `metadata`                           | 抛 → 走 except                                      | `error`                          |
| 6   | `_call_agent_factory` + `unwrap_agent_graph` | 抛 → 走 except（多半是模型配置坏了）                           | `error`                          |
| 7   | astream：解包 → 序列化 → publish                   | abort → `interrupted`；超时 → `timeout`；异常 → `error` | 见左                               |
| 8   | `_finalize`：回滚、终态、投影                         | 每一步单独兜底，互不连累                                      | ——                               |
| 9   | END                                          | 内层 `finally`，不会被跳过                                | ——                               |


#### 每步碰了哪些状态

把 §6.1 的阶段表竖过来看：**每一步允许碰哪几样状态，是这一节的硬约束。**


| #   | 阶段                          | `RunStore`                          | `thread_store`                    | `StreamBridge`          | checkpointer          |
| --- | --------------------------- | --------------------------------- | ------------------------------- | ----------------------- | --------------------- |
| 0   | `run_after_metadata`        | ——                                | `get` / `create`（尽力，5 秒上限）    | ——                      | ——                    |
| 1   | `wait_for_prior_finalizing` | ——                                | ——                              | ——                      | ——                    |
| 2   | `try_start`                 | `start_run`（CAS）                | ——                              | ——                      | ——                    |
| 3   | thread 状态 → running         | ——                                | `set_status("running")`         | ——                      | ——                    |
| 4   | 捕获回滚点                       | ——                                | ——                              | ——                      | 只读 `aget_tuple`       |
| 5   | `metadata` 帧                | ——                                | ——                              | `publish`               | ——                    |
| 6   | 组图                          | ——                                | ——                              | ——                      | 编译时挂 saver，不写         |
| 7   | `astream` → publish         | `update_run_progress`（5 秒节流）     | ——                              | 每条事件                    | 每个 super-step 读写      |
| 8 ① | `_finalize`：回滚 / 账本（M4）     | ——                                | ——                              | ——                      | `rollback` 才写         |
| 8 ② | `_finalize`：终态 + token      | `update_run_completion`          | ——                              | ——                      | ——                    |
| 8 ③ | `_finalize`：投影              | ——                                | `set_status(_thread_status(final))` | `error` 帧（仅 error / timeout） | ——                    |
| 9   | END（内层 `finally`）           | `schedule_cleanup`（只摘内存，延迟 5 分钟） | ——                              | `publish_end`           | ——                    |


- **竖着读：checkpointer 是唯一跨 run 的状态。** 前四步一个字节都不写它，所以 pending 阶段的取消
  （第 0–2 步）不可能留下半个 checkpoint；而第 7 步一开始写它，回滚点就必须已经在手（第 4 步）。
  这两件事的先后关系，就是 §6.4 那条"捕获时机"的全部理由。
- **`RunStore` 只被写三次**：启动栅栏的 CAS、5 秒节流的进度快照、终态 + token 一次写完。
  worker 不写第四样东西；收尾之后没人再碰 store（§5.8 的 `schedule_cleanup` 只摘内存）。
- **`thread_store` 的两次写分居两端**：`running` 在栅栏之后、终态在 ③。中间不管发生什么，
  `running` 都不会被 worker 自己抹掉——它只由 ③ 收口，而且只在 `has_later_run` 为假时收。
- **第 2 步之前失败，一条事件都不会发。** 订阅者只会收到 END（§6.2）：`metadata` 帧排在
  启动栅栏之后，没启动的 run 不假装自己开始过。

#### `run_after_metadata` 的参数与三条出口

代码在 §4.6，这里只钉参数和出口——它是 `start_run` 与 worker 之间唯一的接口面。


| 参数                        | 类型             | 谁给                     | 含义                                          |
| ------------------------- | -------------- | ---------------------- | ------------------------------------------- |
| `record`                  | `RunRecord`    | `start_run` 第 13 步的返回值 | 已准入的 pending 记录；worker 读它，不改 `status`（§5.2） |
| `run_ctx`                 | `RunContext`   | `ctx.for_run(...)`     | 每 run 快照；第 0 步只用得到 `thread_store`           |
| `bridge`                  | `StreamBridge` | `services.bridge`      | 原样交给 `run_agent`                           |
| `run_manager`             | `RunManager`   | `services.run_manager` | 严格模式失败时调 `fail_start_if_pending`          |
| `owner_user_id`           | `str \| None`  | `caller.user_id`       | M4 起建 thread 行时带属主；M3 恒为 `None`            |
| `require_existing_thread` | `bool`         | `start_run` 的入参原样传   | `True` 时 thread 不存在就失败，不顺手建               |


| 出口      | 触发条件                                   | 谁写终态                                                          | END |
| ------- | -------------------------------------- | ------------------------------------------------------------- | --- |
| 继续      | 元数据就绪、5 秒超时、非严格模式失败、abort 到达           | `run_agent`（`try_start` 决定组不组图）                             | 发   |
| 严格校验失败  | `require_existing_thread` 且 thread 已删 | `fail_start_if_pending` 写 `error`，`try_start` 见非 pending 直接拒绝 | 发   |
| 关机排空被取消 | 元数据阶段吃到 `CancelledError`              | 先置 abort → 走完 `run_agent` 的收尾 → 原样再抛                           | 发   |


- **三条出口都进** `run_agent`，没有第四条。第 0 步自己发 END、自己写终态，就是第二条执行栈的雏形。
- 返回类型是 `None`：终态在 store 里、事件在桥上（本节开头的符号表）。调用方要结果就去读那两个地方。
- `run_after_metadata` **不是扩展点**（§6.7）：M4 之后要加的东西都进 `run_agent`。

`run_agent` 只收四个参数：桥、RunManager、记录、`RunContext`。M4–M10 每个阶段都会往里加东西，
加在 `RunContext` 上，不加在参数列表上。

```python
# agentkit/runtime/context.py
_PER_RUN_FIELDS = frozenset({"app_config", "agent_factory", "run_config", "graph_input", "stream_modes"})


@dataclass(frozen=True, slots=True)
class RunContext:
    # --- 进程级：lifespan 建一次 ---
    checkpointer: BaseCheckpointSaver           # M1 §5 的 provider；M3 是内存 saver，M4 换 SQL
    thread_store: ThreadMetaStore
    event_store: RunEventStore | None = None    # M4

    # --- 每 run：start_run 用 for_run() 填（§4.2），进程级那份上恒为缺省值 ---
    app_config: AppConfig | None = None         # 入口取的快照
    agent_factory: AgentFactory | None = None
    run_config: dict | None = None
    graph_input: Any = None
    stream_modes: tuple[str, ...] = ()          # 公开名；映射成 LangGraph 名在 §6.2

    def for_run(self, **per_run: Any) -> RunContext:
        if missing := _PER_RUN_FIELDS - per_run.keys():
            raise TypeError(f"for_run() missing per-run fields: {sorted(missing)}")
        if extra := per_run.keys() - _PER_RUN_FIELDS:
            raise TypeError(f"for_run() cannot override infrastructure: {sorted(extra)}")
        return dataclasses.replace(self, **per_run)
```

- `frozen`：进程级那份被所有 run 共享，worker 改一个字段就改到了别人的 run。
- `for_run` **两头都卡**：少给一个每 run 字段，worker 要到第 6 步才会因为 `None` 报一个莫名其妙的错；
多给一个基础设施字段，一个 run 就能悄悄换掉自己的 checkpointer。两种都在构造时拒绝。
- **checkpointer 要传给工厂。** M1 §3.1 写的是"M1/M3 阶段不传 checkpointer"，但 M3 的 `/state`、
回滚点（§6.4）和经 HTTP 的多轮对话都依赖它。这里传进程级的内存 saver，M4 换成 SQL saver 时
这一行不用动。

#### `RunContext` 的字段分工

`RunContext` 是 worker 唯一的依赖注入面。它把字段分成两层：**每 run 的字段只由 `for_run()` 填**，
进程级的字段在 lifespan 建一次、所有 run 共享。


| 字段              | 层级  | 谁填                      | worker 用它做什么                            | M3 取值                  |
| --------------- | --- | ----------------------- | ---------------------------------------- | ---------------------- |
| `checkpointer`  | 进程级 | lifespan                | 第 4 步捕获回滚点、第 6 步传给工厂、`_finalize` ① 还原  | 内存 saver；M4 换 SQL      |
| `thread_store`  | 进程级 | lifespan                | 第 0 步补 thread 行、③ 写 thread 状态            | 内存实现                   |
| `event_store`   | 进程级 | lifespan                | 第 7 步的账本钩子位（M4）                          | `None`                 |
| `app_config`    | 每 run | 入口的快照（§4.2）             | 第 7 步读 `runs.timeout_seconds`；第 6 步交工厂   | 同一个对象，worker 不重读配置      |
| `agent_factory` | 每 run | `resolve_agent_factory` | 第 6 步组图                                 | M3 默认 `assemble_agent`  |
| `run_config`    | 每 run | 第 9–12 步                | 第 6 步 `_install_runtime`、第 7 步 `astream` | 白名单过滤 + 服务端权威值         |
| `graph_input`   | 每 run | 第 7 步（`normalize_input`） | 第 7 步 `astream` 的位置参数                  | `{"messages": [...]}`  |
| `stream_modes`  | 每 run | 第 3 步（公开名）              | 第 7 步映射成内部名                              | `("values",)`          |


- **worker 只读** `ctx`。`frozen=True` 加 `slots=True` 让它连"顺手改一个字段"都做不到；
  要写回的东西都在 `record` 上，或者经 `run_manager` 落 store。
- **`for_run` 两头都卡键**（代码见上）：少给一个每 run 字段，worker 要到第 6 步才会因为 `None`
  报一个莫名其妙的错；多给一个基础设施字段，一个 run 就能悄悄换掉自己的 checkpointer。
- **`store_only` 的记录永远不进 worker**：它没有 `task`、没有 `abort_event`（§5.1），
  `try_start` 在 `_runs` 里也找不到它（§5.6 抛 `RunStartupError`）。worker 拿到的 `record` 一定是 live 对象。

两个运行参数本阶段加进 `AppConfig`，和 §4.4 的 `max_recursion_limit` 一起：

```python
# agentkit/config/runs_config.py
class RunsConfig(BaseModel):
    timeout_seconds: float | None = Field(default=None, gt=0)      # 整轮执行的上限；None = 不限
    shutdown_timeout_seconds: float = Field(default=10.0, gt=0)    # §6.3 排空时等 worker 的时间
```

`timeout_seconds` 从每 run 的 `app_config` 快照里读，改 YAML 对下一个 run 生效；
`shutdown_timeout_seconds` 在关机那一刻读当前配置。两者都不是 STARTUP_ONLY。

#### `run_agent` 的四个参数

```python
# agentkit/runtime/runs/worker.py
AgentFactory = Callable[..., Any]     # 返回 AgentAssembly 或裸图；解包见下


async def run_agent(
    bridge: StreamBridge,
    run_manager: RunManager,
    record: RunRecord,
    ctx: RunContext,
) -> None:
    """一条 run 从启动栅栏到 END 的全过程。不返回值：终态写在 store 里，事件写在桥上。"""
```


| 参数            | 类型             | 谁持有             | 生命周期        | worker 对它做什么                                                                                                          |
| ------------- | -------------- | --------------- | ----------- | --------------------------------------------------------------------------------------------------------------------- |
| `bridge`      | `StreamBridge` | 进程（lifespan）    | 进程级         | `publish` / `publish_end`；不 `create`、不按订阅者计数取消                                                                        |
| `run_manager` | `RunManager`   | 进程              | 进程级         | `wait_for_prior_finalizing` / `try_start` / `complete` / `update_run_progress` / `has_later_run` / `schedule_cleanup` |
| `record`      | `RunRecord`    | 本 run 的 live 对象 | 准入到 cleanup | 读 `run_id` / `thread_id` / `abort_event` / `abort_action` / `status`；**不**改 `status`（只经 manager）                      |
| `ctx`         | `RunContext`   | `for_run()` 的快照 | 本 run       | 读五个每 run 字段 + 进程级 `checkpointer` / `thread_store`                                                                     |


`ctx.run_config` 是 §4.2 第 11 步写好的那份，worker 不再过白名单、不再碰 `secrets`：

```python
# start_run 交给 worker 的 run_config（密钥已剥离）
{
    "configurable": {"thread_id": str, "user_id": str | None, ...},   # 无 secrets
    "context": {...},                                                # 服务端键已注入（M1 §3.3）
    "recursion_limit": int,                                          # 已按 max_recursion_limit 钳制
    "callbacks": list | None,                                        # 可选；M1 的追踪回调挂在这里
}
```

`ctx.graph_input` 是第 9 步规范化后的图输入：`{"messages": [...]}`，或 M6 才启用的 `Command`。
`ctx.stream_modes` 是公开名的元组（`("values", "messages-tuple")`），映射成 LangGraph 名在 §6.2。

#### 组图：工厂签名与解包

M1 §3.1 的工厂有三层。worker 调的是第 2 层，拿到的可能是 `AgentAssembly`，也可能是第三方测试工厂返回的裸图：

```python
# agentkit/agents/lead_agent/agent.py（M1 已定；这里只写 worker 怎么调）
def assemble_agent(
    config: dict,
    *,
    app_config: AppConfig | None = None,
    checkpointer: Any | None = None,
) -> AgentAssembly: ...


def unwrap_agent_graph(result: Any) -> Any:
    """对 AgentAssembly 取 .graph；对裸图是恒等。按类型判断，不 duck-type .graph。"""
    return result.graph if isinstance(result, AgentAssembly) else result


def _call_agent_factory(factory: AgentFactory, run_config: dict, *, app_config, checkpointer) -> Any:
    """第三方 / 测试工厂可能不收 app_config、checkpointer；按签名传，不盲传。"""
    params = inspect.signature(factory).parameters
    kwargs: dict[str, Any] = {}
    if "app_config" in params:
        kwargs["app_config"] = app_config
    if "checkpointer" in params:
        kwargs["checkpointer"] = checkpointer
    return factory(run_config, **kwargs)
```

- **checkpointer 一定要传（工厂收的话）。** M1 §3.1 写过"M1/M3 阶段不传"，但那是当时还没有 `/state` 和回滚。
M3 的多轮对话、`GET /state`、§6.4 的回滚点都靠这份进程级 saver。不传，每次 `astream` 都是无状态的，
thread_id 只剩文件隔离（M2）的意义。
- **解包按** `isinstance(AgentAssembly)`**，不按** `hasattr(result, "graph")`**。** 一张裸图碰巧带 `.graph`
属性就会被拆错。
- **工厂抛异常就是第 6 步失败**，走 `error` 终态。常见原因是模型配置坏了、白名单里的模型在装配时才发现不可用。
不要在 worker 里再调一次 `create_agent` 绕过工厂——那会漏掉中间件和提示词。



#### worker 不准做的事

这些和 §4 / §5 的纪律对称，散进实现里就会长出第二条执行栈：


| 不准                                                        | 为什么                                                   |
| --------------------------------------------------------- | ----------------------------------------------------- |
| `from fastapi import ...` / 抛 `HTTPException`             | worker 在 harness 层；HTTP 映射是路由的事                       |
| `await record.task`                                       | 那就是自己。写成这样会自死锁                                        |
| 调 `create_or_reject` / `cancel` / `fail_start_if_pending` | 准入和取消是 manager / start_run 的事；worker 只认 `abort_event` |
| `record.status = ...`                                     | 终态只经 `_transition_locked`（§5.2）                       |
| 吞 `CancelledError`                                        | lifespan 的 `asyncio.wait` 会以为 task 正常结束               |
| `await` 桥清理或 `schedule_cleanup` 的延迟                       | worker 会多活 60 秒 / 5 分钟，关机排空被拖死                        |
| `graph.astream(..., subgraphs=True)`                      | 子图 `values` 漏进主线程会污染会话视图（§13）；M8 才开，且事件名必须带 namespace |
| 在模型实例上再挂一份 `UsageCollector`                               | 重复计数 + 重复 span（M1 §3.6）                               |
| 自己 `get_checkpointer()` / `get_app_config()`              | 用 `ctx` 里那份快照；入口解析一次（M1 §3.1）                         |




### 6.2 `finally` 里必须 `publish_end`

**这是 worker 对 SSE 层的硬义务，没有例外：成功、失败、取消、启动栅栏没组图，全都要发。**

漏掉的后果是前端永远转圈。而这个 bug 的恶劣之处在于**它只在异常路径上出现**——
正常流程的测试百分百通过，只有在模型报错、沙箱超时、用户取消这些分支上才暴露。

所以测试策略上要专门写一组"注入各种失败，断言 END 一定到达"的用例。

#### 骨架：`publish_end` 要在最内层的 finally

```python
class RunAborted(Exception):
    """worker 在检查点上发现 abort_event 已置位。"""

    def __init__(self, action: str) -> None:
        super().__init__(action)
        self.action = action


STREAM_RETENTION_SECONDS = 60.0      # END 之后桥缓冲再留多久，给晚到的 /join 续传（§7）


async def run_agent(bridge: StreamBridge, run_manager: RunManager, record: RunRecord, ctx: RunContext) -> None:
    run_id, thread_id = record.run_id, record.thread_id
    terminal, error, error_type = RunStatus.interrupted, None, None   # try_start 拒绝、提前 return 时的终态
    rollback: RollbackPoint | None = None
    usage = UsageCollector(run_manager, run_id)                        # §6.5
    deadline: asyncio.Timeout | None = None
    timeout_s: float | None = None
    try:
        await run_manager.wait_for_prior_finalizing(thread_id, run_id, abort_event=record.abort_event)

        if await run_manager.try_start(run_id) is RunStartOutcome.cancelled:
            return                                    # 不组图。END 由 finally 负责

        await ctx.thread_store.set_status(thread_id, "running")        # §5.2：启动成功之后才写
        rollback = await capture_rollback_point(ctx, thread_id)          # §6.4；捕获失败返回 None
        await bridge.publish(run_id, "metadata", {"run_id": run_id, "thread_id": thread_id})

        _install_runtime(ctx.run_config, record)                         # ToolRuntime.context 靠它
        assembly = _call_agent_factory(
            ctx.agent_factory, ctx.run_config,
            app_config=ctx.app_config, checkpointer=ctx.checkpointer,
        )
        graph = unwrap_agent_graph(assembly)
        timeout_s = ctx.app_config.runs.timeout_seconds if ctx.app_config is not None else None
        deadline = asyncio.timeout(timeout_s)                            # 从这里开始计时；None = 不限
        async with deadline:
            await _stream_graph(bridge, record, ctx, graph, usage)        # ★ 每一帧都查 abort
        terminal, error = RunStatus.success, None

    except RunAborted:
        terminal, error = RunStatus.interrupted, None
    except asyncio.CancelledError:
        terminal, error = RunStatus.interrupted, None
        raise                                          # 不吞，但终态要写
    except TimeoutError as exc:
        if deadline is not None and deadline.expired():
            terminal, error_type = RunStatus.timeout, "TimeoutError"
            error = f"Run exceeded {timeout_s}s"
        else:                                          # 图里别处抛的内置 TimeoutError，不是整轮超时
            terminal, error, error_type = RunStatus.error, _public_error(exc), type(exc).__name__
    except Exception as exc:
        terminal, error, error_type = RunStatus.error, _public_error(exc), type(exc).__name__
        logger.exception("Run %s failed", run_id)

    finally:
        record.finalizing = True
        try:
            # 收尾可能很多：回滚、写终态、token 汇总、事件账本落盘（M4）、标题投影（M6）。
            # 每一步都可能抛，_finalize 自己逐步兜底（见下）。
            await _finalize(
                bridge, run_manager, record, ctx,
                terminal=terminal, error=error, error_type=error_type, rollback=rollback, usage=usage,
            )
        finally:
            # ★ 无论收尾成功与否，流必须结束。
            await bridge.publish_end(run_id)
            record.finalizing = False
            run_manager.schedule_cleanup(run_id)                         # §5.8：5 分钟后摘内存记录
            spawn_detached(bridge.cleanup(run_id, delay=STREAM_RETENTION_SECONDS))


_PUBLIC_ERROR_MAX_CHARS = 2000


def _public_error(exc: BaseException) -> str:
    """会进 runs.error、error 帧，并被 API 回显：只要类型名和截断后的消息，不带 traceback。"""
    message = str(exc).strip() or type(exc).__name__
    return f"{type(exc).__name__}: {message[:_PUBLIC_ERROR_MAX_CHARS]}"
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
一个只有 end 的流，这是对的——`metadata` 帧在第 5 步，排在启动栅栏之后，没启动的 run 不假装自己开始过。

**回滚不在 except 里做，挪到** `_finalize`**。** 早先的骨架只在 `except RunAborted` 分支里回滚。可是
§5.9 的 `cancel()` 对 running 的 run 会 `task.cancel()`，worker 收到的多半是 `CancelledError` 而不是
`RunAborted`——rollback 就这样被跳过了，而且只在"用户点了回滚、模型正在一次长调用里"这种组合下出现。
收尾时按 `record.abort_event` 和 `record.abort_action` 判断，两条路径都覆盖。

**超时要看** `deadline.expired()`**。** HTTP 客户端、沙箱调用抛出的内置 `TimeoutError` 同样会落进这个
except；只有 deadline 自己到期才是 `timeout` 终态，其余照普通 `error` 处理。deadline 在组完 agent
之后才建：`asyncio.timeout()` 在构造时就开始计时，放在函数开头的话，等前一个 run 收尾的时间也算进去了。

**每个 except 分支只算终态，不做 IO。** 写 store、发帧都集中在 `_finalize`。在 except 里做 IO，
那次 IO 自己抛的异常会盖掉原来的异常，而且同一段逻辑要在四个分支里各写一遍。

**两个 cleanup 都是调度出去的，不 await。** 桥的缓冲留 60 秒（重放窗口，给晚到的 `/join` 续传），
RunRecord 留 5 分钟（状态窗口，§5.8），长短不同、用途不同。await 其中任何一个，worker task
都要多活那么久。

**收尾期间再来的** `CancelledError` **不要** `uncancel()`**。** DeerFlow 后来给 finalize IO 加了
`task.uncancel()`，因为回滚和账本落盘可能要几秒，关机强制阶段会把这段 IO 打断。M3 的收尾短
（回滚 + `complete` + 投影），`_best_effort` 只接 `Exception`，强制取消会穿过收尾、落到内层
`finally` 发 END。`uncancel()` 会让 lifespan 的 `asyncio.wait` 以为 task 正常结束，而它其实
还在写 checkpoint——关机顺序就乱了。M4 账本变长之后再单独做可中断的收尾，不要提前引入。

**第 3 步** `thread_store.set_status("running")` **失败就让 run 失败。** 此时 `try_start` 已经把
RunStore 写成 running；except 走 `complete(error)`，两边重新对齐。M4 的 SQL thread_store
若把这一步改成 best-effort，要在同一条注释里写清"RunStore 已是 running、侧栏可能还是 idle"。

#### 异常 → 终态 → 线上字段

四个 `except` 分支只算终态、不做 IO（理由见上）。它们各自产出什么，收敛在这张表里：


| 触发                                    | `terminal`   | store 的 `error`                    | error 帧的 `error` | 备注                                |
| ------------------------------------- | -------------- | ---------------------------------- | ----------------- | --------------------------------- |
| 正常跑完                                  | `success`    | ——                                 | 不发帧               | ——                                |
| `RunStartupError`（store 无法确认启动，§5.6） | `error`      | `RunStartupError: Failed to start run …` | `RunStartupError` | 落进泛型 `except Exception`，没有单独分支     |
| `RunAborted("interrupt")`             | `interrupted` | ——                                 | 不发帧               | 每帧检查抛出（§6.2）                      |
| `RunAborted("rollback")`              | `interrupted` | ——                                 | 不发帧               | `_finalize` ① 按 `abort_action` 还原 |
| `asyncio.CancelledError`              | `interrupted` | ——                                 | 不发帧               | 写完终态再原样 `raise`                  |
| 整轮超时（`deadline.expired()`）          | `timeout`    | `Run exceeded {timeout_s}s`        | `TimeoutError`    | 只有 deadline 自己到期才算                |
| 图内 `TimeoutError`                     | `error`      | `TimeoutError: …`                  | `TimeoutError`    | 客户端 / 沙箱的超时，不是整轮超时                |
| 其它 `Exception`                        | `error`      | `Type: message`（截断 2000 字符）       | 类型名               | 同时 `logger.exception`             |


- **右两列只在内存胜者还是自己时才写出去。** 取消先到时，`complete` 写的是赢家的终态，
  error 帧也不发（`_finalize` ②③）。这就是"取消不被后到的 success 覆盖"在 worker 侧的表现（§9.3）。
- `error` 进 store 的格式由 `_public_error` 固定：类型名 + 截断后的消息，**不带 traceback**。
  它会经 `GET /runs/{id}` 回显给客户端，也会进 error 帧（对齐 Platform 的 `error` + `message`）。
- **`RunStartupError` 走 error 而不是 cancelled**：store 抛异常时 CAS 可能已经提交，当成取消
  会留下一行没人收尾的 running（§5.6）。

#### abort 的五个检查点

取消是协作式的（§9.1），worker 侧一共只有这五个地方会看 `abort_event`：


| 位置                              | 谁置位                                      | 怎么生效                  | 覆盖哪段时间        |
| ------------------------------- | ---------------------------------------- | --------------------- | ------------- |
| `wait_for_prior_finalizing` 的轮询 | `cancel()` / 顶替（§5.5）                   | 传进去的 `abort_event` 一置位就返回 | 等前一个 run 收尾    |
| `try_start` 的 ①③               | `cancel()` / `fail_start_if_pending`   | 启动栅栏，两次检查             | 准入到组图之前       |
| `_stream_graph` 的每一帧           | `cancel()`                             | 抛 `RunAborted(action)` | 有事件到达的时间段     |
| `task.cancel()`                 | `cancel()` 对 running 的 run / shutdown 的强制阶段 | 打断当前 `await`          | 模型或工具的一次长调用   |
| `_finalize` ①                   | `cancel(action=...)`                   | 决定要不要还原 checkpoint    | 收尾            |


- **前四个合起来才覆盖"准入 → 最后一帧"。** 只看栅栏，跑到一半的取消不生效；只看每帧，
  模型 90 秒不吐 token 的那段时间不生效；只看 `task.cancel()`，pending 阶段的取消会撞上 §4.6
  说的"`CancelledError` 在协程函数体第一行之前抛出"。
- **第五个不打断任何东西**，它只决定收尾要不要回滚。`interrupt` 和 `rollback` 的全部区别落在这一格。

#### Runtime 注入

LangGraph CLI / Server 调图时会自己建 `Runtime`。worker 走的是 `graph.astream(config=...)`，
不传官方的 `context=` 参数，所以要手动挂：

```python
def _install_runtime(run_config: dict, record: RunRecord) -> None:
    """让中间件和工具经 ToolRuntime.context 读到 thread_id / run_id。"""
    from langgraph.runtime import Runtime

    context = dict(run_config.get("context") or {})
    context.setdefault("thread_id", record.thread_id)
    context.setdefault("run_id", record.run_id)
    run_config["context"] = context
    runtime = Runtime(context=context, store=None)          # M4 才挂 store
    run_config.setdefault("configurable", {})["__pregel_runtime"] = runtime
```

`thread_id` / `run_id` 在 §4.2 第 11 步已经写进 context；这里 `setdefault` 是防御测试工厂
直接构造 `RunContext`、绕过 `start_run` 的路径。`__pregel_runtime` 是 LangGraph 的内部键，
双下划线前缀表示运行时通道，用户 context 白名单进不来（M1 §3.3）。

```python
# agentkit/runtime/tasks.py
_DETACHED: set[asyncio.Task] = set()


def spawn_detached(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """不属于任何请求的后台任务：空 context、留强引用（理由同 §5.8）。"""
    task = asyncio.create_task(coro, context=contextvars.Context())
    _DETACHED.add(task)
    task.add_done_callback(_DETACHED.discard)
    return task


def cancel_detached() -> None:
    for task in list(_DETACHED):
        task.cancel()
```



#### `_finalize`：收尾按"终态可见"切成三段

```python
async def _finalize(
    bridge: StreamBridge,
    run_manager: RunManager,
    record: RunRecord,
    ctx: RunContext,
    *,
    terminal: RunStatus,
    error: str | None,
    error_type: str | None,
    rollback: RollbackPoint | None,
    usage: UsageCollector,
) -> None:
    run_id, thread_id = record.run_id, record.thread_id

    # ① 终态之前：必须在"终态可见"之前落盘的写入
    if record.abort_event.is_set() and record.abort_action == "rollback":
        await _best_effort("rollback", restore_rollback_point(ctx, rollback))      # §6.4
    # M4：事件账本 flush 放在这里

    # ② 终态 + token，一次写完。输给取消时写的是赢家的终态（§5.6）
    await _best_effort("complete", run_manager.complete(run_id, status=terminal, error=error, totals=usage.totals()))
    final = record.status

    # ③ 依赖终态的投影
    if final in (RunStatus.error, RunStatus.timeout):
        await _best_effort("error frame", bridge.publish(run_id, "error", {
            "error": error_type or "RunError",
            "message": record.error or final.value,
        }))
    if not await run_manager.has_later_run(thread_id, run_id):
        await _best_effort("thread status", ctx.thread_store.set_status(thread_id, _thread_status(final)))
    # M6：从 checkpoint 读 title，投影到 thread 的 display_name；M9：释放租约


async def _best_effort(step: str, awaitable: Awaitable[Any]) -> None:
    try:
        await awaitable
    except Exception:
        logger.exception("Finalize step %r failed", step)


def _thread_status(status: RunStatus) -> str:
    return "error" if status in (RunStatus.error, RunStatus.timeout) else "idle"


# RunManager 上的只读辅助（§5 公开方法表里的一行）
async def has_later_run(self, thread_id: str, run_id: str) -> bool:
    async with self._lock:
        run_ids = [r.run_id for r in self._thread_records_locked(thread_id) if r.operation_kind is ThreadOperationKind.run]
    return run_id in run_ids and run_ids[-1] != run_id
```

- **为什么按"终态可见"切。** 终态一写，store 行就离开部分唯一索引（§5.3），别的进程可以准入了。
所以要改 checkpoint 的写入（回滚、M4 的账本）必须在 ② 之前做完；只依赖终态的投影放在 ② 之后。
- **每一步单独兜底。** 回滚失败不能让终态写不进去，终态写失败不能让 thread 卡在 running。
`_best_effort` 只接 `Exception`：收尾期间再来一次取消（关机的强制阶段）照样往上抛，
内层 `finally` 仍然会发 END。
- `error` **帧看的是** `record.status`**（胜者），不是** `terminal`**。** 取消先到的话，worker 自己撞上的异常
就不该再报给前端。字段对齐 LangGraph Platform（`error` + `message`），`useStream` 的 `onError`
拿到的就是它。它必须排在 END 之前：END 之后订阅就结束了。
- **thread 状态只由同 thread 最后一个 run 写。** interrupt / rollback 顶替时，旧 run 的收尾可能晚于
新 run 启动；它把 thread 写回 idle，侧栏就显示空闲，而新 run 正在跑。
- **用户取消投影成** `idle`**，不是** `interrupted`**。** LangGraph Platform 里 thread 的 `interrupted`
表示"有一个人机确认在等回复"（M6），不是"被用户停了"。


| `_finalize` 关键字        | 来源                                     | 收尾怎么用                                                       |
| ---------------------- | -------------------------------------- | ----------------------------------------------------------- |
| `terminal`             | 各 except 分支算出的意图终态                     | 交给 `complete`；取消先到时 manager 会忽略它，改写赢家的状态                    |
| `error` / `error_type` | `_public_error` / `type(exc).__name__` | `error` 进 store；`error_type` 进 error 帧的 `error` 字段。取消获胜时帧不发 |
| `rollback`             | 第 4 步的 `RollbackPoint                  | None`                                                       |
| `usage`                | 本 run 的 `UsageCollector`               | `totals()` 和终态一次写入；即使图没跑过（`try_start` 拒绝）也交一份全 0            |




#### `_finalize` 三段的前置与后置

三段不是随手切的，是按**"终态可见"这条线**切的：


| 段            | 前置条件  | 写什么                                              | 后置保证                                      | 失败时                   |
| ------------ | ----- | ------------------------------------------------ | ----------------------------------------- | --------------------- |
| ① 终态可见之前     | 无     | checkpointer（回滚）、M4 的事件账本 flush                   | "必须在终态之前落盘"的写入都已尝试过                       | `_best_effort` 吞，继续 ② |
| ② 终态 + token | ① 已跑完 | `RunStore`：`update_run_completion`              | store 行离开部分唯一索引（§5.3）；`record.status` 从此是胜者 | 吞；内存胜者不变，③ 仍按它投影      |
| ③ 依赖终态的投影     | ② 已跑完 | `StreamBridge`（error 帧）、`thread_store`（thread 状态） | 前端看到的终态与 store 一致                          | 吞，三段互不连累            |


**顺序不能换。** ① 必须在 ② 之前：终态一写，store 行就离开部分唯一索引，别的进程（M9）或下一条
run 就能准入了，而回滚是"改 checkpoint"——排在新 run 后面执行就会覆盖新 run 的写入。③ 必须在 ②
之后：它读的是 `record.status`（胜者的终态），先做就没有这个值可读。

`_best_effort` **只接** `Exception`**。** `CancelledError` 是 `BaseException`，关机强制阶段的那一次
取消会穿过整个收尾、落到内层 `finally`，END 因此仍然发得出去。连它一起吞掉，进程就会卡在一次
写 checkpoint 的 `await` 上，而 lifespan 以为排空已经做完了。

#### thread 状态的投影


| `record.status`（胜者）        | `thread_store` 写成 | 谁读它                                        |
| --------------------------- | ----------------- | ------------------------------------------ |
| `success`                 | `idle`          | 侧栏                                         |
| `interrupted`（用户取消 / 被顶替） | `idle`          | 侧栏。**用户取消不是** `interrupted`：那个值留给 M6 的人机确认 |
| `error` / `timeout`      | `error`         | 侧栏的错误态                                     |


- **只在** `has_later_run(...)` **为假时写。** interrupt / rollback 顶替时，旧 run 的收尾可能晚于
  新 run 的启动；它把 thread 写回 `idle`，侧栏就显示空闲，而新 run 正在跑。
- `_thread_status` **是个纯函数**：③ 和 M4 的侧栏接口共用同一份映射，不各写一遍。
- M6 起 thread 的 `interrupted` 由"等待人机确认"写入，与 worker 无关——worker 永远不写这个值。

#### chunk 到 SSE 事件的翻译

服务端这一侧**不做** M1 §6.2 客户端那套去重和 usage 合并——`useStream` 自己按 id 合并消息，
token 由 §6.5 的回调统计。worker 只做三件事：解包、序列化、命名。事件名对齐 LangGraph Platform 的 wire：


| LangGraph 内部 mode                 | 发出的 SSE `event` | 说明                                       |
| --------------------------------- | --------------- | ---------------------------------------- |
| `values`                          | `values`        | 状态快照                                     |
| `messages`                        | `messages`      | 前端 SDK 认这个名；公开 API 参数名叫 `messages-tuple` |
| `updates`                         | `updates`       | 节点级增量                                    |
| `custom`                          | `custom`        | `StreamWriter` 转发                        |
| `debug` / `tasks` / `checkpoints` | 同名透传            | M3 不解释载荷；客户端订了就原样发出                      |


**公开参数名和 wire 事件名不是一回事**，`stream_modes.py` 负责这层映射：请求里客户端写
`messages-tuple`，映射到 LangGraph 的 `messages`，而 SSE 帧上的 `event:` 是 `messages`。
三个名字听起来该统一，但前两个是 LangGraph Platform 已经定下的协议，改了就用不了官方 SDK。
**把映射关进一个文件，别让它散进 router。**

```python
# agentkit/runtime/stream_modes.py
RunStreamMode = Literal[
    "values", "messages-tuple", "updates", "debug", "tasks", "checkpoints", "custom",
]
SUPPORTED_RUN_STREAM_MODES: frozenset[str] = frozenset(get_args(RunStreamMode.__value__))


class UnsupportedStreamModeError(ValueError):
    def __init__(self, modes: list[str]) -> None:
        self.modes = tuple(dict.fromkeys(modes))          # 去重、保序，给 422 正文用
        super().__init__(f"Unsupported stream mode(s): {', '.join(self.modes)}")


def normalize_stream_modes(raw: list[str] | str | None) -> list[str]:
    """入口校验：None / [] → ["values"]；str → [str]；未知或非 str → UnsupportedStreamModeError。"""
    if raw is None:
        modes = ["values"]
    elif isinstance(raw, str):
        modes = [raw]
    else:
        modes = raw or ["values"]
    unsupported = [
        mode if isinstance(mode, str) else type(mode).__name__
        for mode in modes
        if not isinstance(mode, str) or mode not in SUPPORTED_RUN_STREAM_MODES
    ]
    if unsupported:
        raise UnsupportedStreamModeError(unsupported)
    return modes


def to_langgraph_stream_modes(raw: list[str] | str | None) -> list[str]:
    """公开名 → astream 的内部名。恒返回 list；只把 messages-tuple 换成 messages，其余原样。"""
    mapped = ["messages" if mode == "messages-tuple" else mode for mode in normalize_stream_modes(raw)]
    return list(dict.fromkeys(mapped))                    # 去重保序：values + messages-tuple + values → [values, messages]
```

客户端传了不支持的 mode（`events`、或者内部名 `messages`）要 **422 拒绝，不要静默替换成**
`values`。静默替换的症状是前端收到完全不同结构的数据，而它以为自己订的是增量。
`normalize_stream_modes` 在 §4.2 第 3 步已经跑过一遍；worker 再跑是为了把公开名映成内部名，
并且保证 `astream` 拿到的永远是 list。

#### `_stream_graph` 的参数与 `astream` 关键字

```python
async def _stream_graph(
    bridge: StreamBridge,
    record: RunRecord,
    ctx: RunContext,
    graph: Any,
    usage: UsageCollector,
) -> None:
    lg_modes = to_langgraph_stream_modes(ctx.stream_modes)       # messages-tuple → messages，去重；恒为 list
    callbacks = [*(ctx.run_config.get("callbacks") or ()), usage]
    config = {**ctx.run_config, "callbacks": callbacks}           # 回调只挂调用根（M1 §3.6）
    configurable = dict(config.get("configurable") or {})
    configurable.setdefault("thread_id", record.thread_id)
    config["configurable"] = configurable

    stream = graph.astream(
        ctx.graph_input,
        config=config,
        stream_mode=lg_modes,
        subgraphs=False,                                          # §13：M3 关掉；M8 才开
    )
    try:
        async for item in stream:
            _raise_if_aborted(record)                             # 先查再发：已取消的 run 不再往桥上写
            mode, chunk, namespace = unpack_stream_item(item)
            await bridge.publish(
                record.run_id,
                sse_event_name(mode, namespace),
                serialize(chunk, mode=mode),
            )
            await usage.maybe_report()                            # §6.5：节流的进度快照
    finally:
        await _close_quietly(stream)


def _raise_if_aborted(record: RunRecord) -> None:
    if record.abort_event.is_set():
        raise RunAborted(record.abort_action)
```


| `astream` 关键字                          | M3 取值                                       | 为什么这样                                                                    |
| -------------------------------------- | ------------------------------------------- | ------------------------------------------------------------------------ |
| 位置参数 `input`                           | `ctx.graph_input`                           | §4.2 第 9 步规范化后的消息列表或（M6）`Command`                                        |
| `config`                               | `run_config` + 调用根 callbacks + `thread_id`  | `recursion_limit`、checkpoint 坐标、Runtime 都在这份 config 里                    |
| `stream_mode`                          | `to_langgraph_stream_modes(...)` 的 **list** | 永远 list，见下                                                               |
| `subgraphs`                            | `False`                                     | 子图 `values`/`messages` 漏进主线程会覆盖会话视图                                      |
| `interrupt_before` / `interrupt_after` | **不传**                                      | `RunCreateRequest` 没有这两个字段（`extra="forbid"`）；M6 人机确认走 `Command`，不靠节点中断列表 |


worker **不**在这里改 `interrupt_before_nodes` 这类图上的可变属性。工厂每次调用返回一张新图
（或缓存键命中的同一张，M1 §6.4），往图对象上写会漏到下一个共用这张图的 run。

```python
def unpack_stream_item(item: Any) -> tuple[str, Any, tuple[str, ...]]:
    if isinstance(item, tuple) and len(item) == 3:           # subgraphs=True：(namespace, mode, chunk)
        namespace, mode, chunk = item
        return str(mode), chunk, tuple(namespace)
    if isinstance(item, tuple) and len(item) == 2:           # stream_mode 是 list：恒为 (mode, chunk)
        mode, chunk = item
        return str(mode), chunk, ()
    return "values", item, ()                                # 防御：不该走到。单 mode 字符串才会产裸 chunk


def sse_event_name(mode: str, namespace: tuple[str, ...]) -> str:
    return "|".join((mode, *namespace)) if namespace else mode       # 子图帧：values|<ns>（M8）


async def _close_quietly(stream: AsyncIterator[Any]) -> None:
    try:
        await stream.aclose()
    except Exception:
        logger.warning("Failed to close graph stream", exc_info=True)
```

- `stream_mode` **永远传 list。** LangGraph 对 list 一律产出 `(mode, chunk)`，哪怕只有一个 mode；
传字符串才产出裸 chunk——而单个 `messages` mode 的裸 chunk 本身又是 `(message, metadata)` 二元组，
和 `(mode, chunk)` 长得一样，分不出来。`to_langgraph_stream_modes` 保证返回 list，
这里就不需要 M1 客户端那种"单模式退化"的防御。
- **先查 abort 再 publish。** 这个检查点只在有帧到达时才有；模型一次长调用期间没有帧，
这段时间里的取消靠 §5.9 对 running run 的 `task.cancel()` 打断。两者合起来才覆盖所有时刻。
- **不管怎么离开循环都要** `aclose()`**。** 异常或 abort 跳出 `async for` 时，异步生成器不会立刻关闭，
要等 GC，而 GC 可能发生在别的 context 里；在那之前，图里进行中的模型请求、checkpointer 写入都挂着。
关闭失败只告警：这时已经有一个异常在路上（abort、错误或取消），关闭的异常不能盖掉它；
正常跑完的生成器已经耗尽，`aclose()` 是空操作。
- **子图帧的事件名带 namespace。** M3 不订阅子图，namespace 恒为空。M8 打开之后，子代理的 `values`
快照要是用裸 `values` 发出去，SDK 会拿子代理的局部状态覆盖整个会话视图（roadmap M8 第 4 条）。



#### 序列化

```python
# agentkit/runtime/serialization.py（M1 的嵌入式客户端用同一个）
def serialize(chunk: Any, *, mode: str) -> Any:
    if mode == "messages":                               # (message_chunk, metadata) → [message, metadata]
        message, metadata = chunk
        return [to_jsonable(message), to_jsonable(metadata) if isinstance(metadata, dict) else {}]
    if mode == "values" and isinstance(chunk, dict):
        return {k: to_jsonable(v) for k, v in chunk.items() if not k.startswith("__pregel_")}
    return to_jsonable(chunk)


def to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Mapping):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in obj]
    if hasattr(obj, "model_dump"):                       # LangChain 消息、pydantic 模型
        return to_jsonable(obj.model_dump())
    return str(obj)
```

- `messages` **帧的 data 是** `[message, metadata]` **两元素数组**，这是 LangGraph Platform 的 wire 形状，SDK 按它解。
- `values` **只剥** `__pregel_`***。** 那是运行时内部对象，既序列化不了也不该外泄。`__interrupt__` 要留着：
SDK 从 values 帧里识别中断，M6 的人机确认靠它。
- `to_jsonable` **最后兜底** `str()`，和 §8 `format_sse` 的 `default=str` 是同一个态度：宁可某个字段
变成字符串，也不要一次序列化失败把整条流从中间截断。放在这里而不是只靠 `format_sse`，
是因为 M4 的事件账本拿的也是这份结果。
- `messages` **的 chunk 必须是二元组。** 不是就按普通对象走 `to_jsonable`，不要假设解包。
LangGraph 偶发把一条裸 message 交出来时，硬拆会让整轮 `error`，前面已经发出的 delta 全废。
- **publish 本身抛异常要往上走。** 桥满了会裁旧帧（§7），不会因为慢订阅者反压 worker；
桥自己坏了（实现 bug）才抛，这时 run 应当 `error`，不是假装还在流。

#### 解包、命名、序列化的参数

这四个函数是 worker 里唯一"碰数据"的地方，签名和失败策略现在定死：


| 函数                               | 参数                    | 返回                                             | 失败策略                                        |
| -------------------------------- | --------------------- | ---------------------------------------------- | ------------------------------------------- |
| `unpack_stream_item(item)`       | `astream` 产出的一项     | `(mode, chunk, namespace)`，namespace 恒为 tuple | 不抛；认不出就当成 `("values", item, ())`           |
| `sse_event_name(mode, namespace)` | 内部 mode 名、namespace   | `mode`；有 ns 时 `mode\|ns`                    | 不抛                                          |
| `serialize(chunk, *, mode)`      | 内部 mode 名             | JSON 可编码的 wire 形状                              | 不抛；最后兜 `str()`                             |
| `to_jsonable(obj)`               | 任意对象                  | JSON 值                                         | 不抛；`Mapping` / 序列 / `model_dump()` / `str()` 依次兜底 |
| `normalize_stream_modes(raw)`    | `list \| str \| None` | 公开名的 `list[str]`                              | 未知或非 str → `UnsupportedStreamModeError`   |
| `to_langgraph_stream_modes(raw)` | 同上                    | 内部名的 `list[str]`，去重保序                         | 同上（内部先跑 `normalize_stream_modes`）           |


- 前四个**一个都不会抛**：流已经在跑，任何一个抛都会把整轮变成 `error`，而它们处理的只是"这一帧
  长什么样"。后两个只在上桥之前跑，那里抛是正确的（422，而不是静默降级成 `values`）。
- `mode` 传的是**内部名**（`messages`），不是公开名（`messages-tuple`）：`serialize` 和
  `sse_event_name` 都按 LangGraph 交出来的 mode 判断，公开名在这一层已经不存在了。

`metadata` 帧（第 5 步）不走 `_stream_graph`：`useStream` 的 `onCreated` 从
`Content-Location` 拿 `run_id`，但 SDK 仍要求流里出现一次 `event: metadata`，载荷至少含
`run_id` 和 `thread_id`。它排在启动栅栏之后、第一帧图输出之前；`try_start` 拒绝的 run 不发
这帧，订阅者只收到 END。

### 6.3 关机排空

进程退出时，lifespan 要先给 inflight task 设 abort、`task.cancel()`，
**在超时内等它们把最后一次 checkpoint 写完，再关 checkpointer 连接池**。

顺序反了的话，LangGraph 内部的 `aput` 会打到已关闭的连接池，
产生一个关不掉的异常循环，进程无法正常退出。

```python
# app/gateway/app.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.run_services = services = build_run_services(get_app_config())
    try:
        yield
    finally:
        timeout = get_app_config().runs.shutdown_timeout_seconds
        await services.run_manager.shutdown(timeout=timeout)          # ① 拒新准入、置 abort、等 worker 收尾（§5.9）
        cancel_detached()                                             # ② 还没到点的桥缓冲清理，随进程一起丢
        # M4：关事件账本；M9 §3：停心跳、抽干孤儿回收、停扩展 / 通道 / 调度器
        await close_checkpointer(services.run_context.checkpointer)    # ③ 最后才关 checkpointer 连接池
```

- **① 等的是 worker 自己的** `finally`**。** 回滚的 checkpoint 写入、终态、END 都在那里完成；
只有超时之后才强制取消（§5.9）。被强制取消的 run 留下的活跃行，由下次启动时的孤儿回收收掉
（M4 `recover_inflight_runs`）。
- **排空期间拒绝新准入。** `shutdown` 第一件事是置 `_closing`（§5.9），之后 `_admit` 在幂等查找
之后、占位检查之前抛 `ConflictError`。幂等命中仍返回已有记录：关机那几秒里的重试不该再开新
run，但同一把 key 的查询还应该拿到原来的那条。否则调度器和 IM 通道在排空窗口里还能塞进新
run，而它们注定会被强制取消。409 正好落进 M9 §4.6 的"线程忙 → 退回 queued"，调度
occurrence 会在别的实例或重启之后重跑。
- `shutdown_timeout_seconds` **要小于部署平台的优雅退出期限。** K8s 的 `terminationGracePeriodSeconds`
默认 30 秒；排空等得比它久，③ 还没执行进程就被 SIGKILL 了，异常循环照样出现。默认 10 秒留足余量。



### 6.4 回滚点：捕获与还原

```python
@dataclass(frozen=True, slots=True)
class RollbackPoint:
    thread_id: str
    checkpoint_config: dict | None                       # 运行前 head 的坐标；None = 运行前是空 thread
    messages: tuple[Any, ...] = ()                       # 运行前的完整消息列表
    pending_writes: tuple[tuple[str, str, Any], ...] = ()


class RollbackUnavailable(RuntimeError):
    """运行前的状态没有捕获到，回滚无从做起。"""


async def capture_rollback_point(ctx: RunContext, thread_id: str) -> RollbackPoint | None:
    """None 表示捕获失败：之后的 rollback 请求退化成 interrupt（fail-closed）。"""
    try:
        head = await ctx.checkpointer.aget_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})
    except Exception:
        logger.warning("Rollback point capture failed for %s; rollback disabled for this run", thread_id, exc_info=True)
        return None
    if head is None:
        return RollbackPoint(thread_id=thread_id, checkpoint_config=None)
    checkpoint = head.checkpoint if isinstance(head.checkpoint, Mapping) else {}
    channels = checkpoint.get("channel_values")
    if not isinstance(channels, Mapping):
        logger.warning("Rollback point for %s has no channel_values; rollback disabled", thread_id)
        return None
    return RollbackPoint(
        thread_id=thread_id,
        checkpoint_config=head.config,
        messages=tuple(channels.get("messages") or ()),
        pending_writes=tuple(head.pending_writes or ()),
    )


async def restore_rollback_point(ctx: RunContext, point: RollbackPoint | None) -> None:
    if point is None:
        raise RollbackUnavailable("pre-run state was not captured")
    if point.checkpoint_config is None:
        await ctx.checkpointer.adelete_thread(point.thread_id)          # 运行前是空 thread：回到空
        return
    mutation = build_state_mutation_graph(
        "rollback_restore",
        checkpointer=ctx.checkpointer,
        state_schema=get_thread_state_schema(),                         # M1 ThreadState；M6 中间件加通道时跟
    )
    restored = await mutation.aupdate_state(
        point.checkpoint_config,
        {"messages": Overwrite(list(point.messages))},
        as_node="rollback_restore",
    )
    for task_id, writes in _group_writes_by_task(point.pending_writes):
        await ctx.checkpointer.aput_writes(restored, writes, task_id=task_id)


def build_state_mutation_graph(
    node_name: str,
    *,
    checkpointer: BaseCheckpointSaver,
    state_schema: type | dict,
) -> Any:
    """只有一个空节点的图，专门用来 aupdate_state。和 agent 图共享通道表，不共享节点。"""
    from langgraph.graph import StateGraph

    builder = StateGraph(state_schema)

    async def _noop(state: Any) -> dict:
        return {}

    builder.add_node(node_name, _noop)
    builder.set_entry_point(node_name)
    builder.set_finish_point(node_name)
    return builder.compile(checkpointer=checkpointer)


def _group_writes_by_task(
    pending_writes: tuple[tuple[str, str, Any], ...],
) -> list[tuple[str, list[tuple[str, Any]]]]:
    """LangGraph pending_writes 是 (task_id, channel, value)；aput_writes 要按 task 一组。"""
    grouped: dict[str, list[tuple[str, Any]]] = {}
    for task_id, channel, value in pending_writes:
        grouped.setdefault(task_id, []).append((channel, value))
    return list(grouped.items())
```

`RollbackPoint` / 两个函数的参数：


| 符号                           | 参数                                          | 含义                                                                                     |
| ---------------------------- | ------------------------------------------- | -------------------------------------------------------------------------------------- |
| `capture_rollback_point`     | `ctx`, `thread_id`                          | 读 `checkpoint_ns=""` 的 head。不收 `checkpoint_id`：回滚的是运行前的 head，不是客户端指定的分叉点               |
| `restore_rollback_point`     | `ctx`, `point`                              | `point is None` 抛 `RollbackUnavailable`（`_finalize` 的 `_best_effort` 吃掉，退化成 interrupt） |
| `build_state_mutation_graph` | `node_name`, `checkpointer`, `state_schema` | `node_name` 同时是唯一节点和 `as_node`；schema 必须是 thread 实际用的那份                                |
| `_group_writes_by_task`      | `pending_writes`                            | 保序：同一 `task_id` 的写入按捕获时的顺序重放                                                           |


捕获读的 config 固定为 `{"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}`。
`checkpoint_ns` 空字符串是根图；写成别的会读到子图的 head，回滚就把主对话还原成子代理的局部状态。
`messages` 通道缺失时记 `()`，不要当成捕获失败——空 thread 的第一条 run 就没有消息。
`channel_values` 本身缺失才算捕获失败（返回 `None`）。

#### `RollbackPoint` 的字段

四个字段各自对应还原时的一个动作，缺一个就还原不完整：


| 字段                  | 从哪来                                              | 运行前是空 thread 时 | 谁用                                    |
| ------------------- | ------------------------------------------------ | -------------- | ------------------------------------- |
| `thread_id`         | 入参                                               | 必有             | `adelete_thread` / `aupdate_state` 的分区键 |
| `checkpoint_config` | `head.config`                                    | `None`       | `None` → 走 `adelete_thread`，回到空 thread |
| `messages`          | `head.checkpoint["channel_values"]["messages"]`  | `()`         | 还原时 `Overwrite(list(...))`             |
| `pending_writes`    | `head.pending_writes`                            | `()`         | 按 `task_id` 分组后用 `aput_writes` 挂回     |

- **捕获时机：**`try_start` **之后、组图之前。** 第 1 步的 `wait_for_prior_finalizing` 保证了被顶替的上一个
run 已经写完它的回滚；更早捕获，拿到的可能是它回滚之前的状态。
- **分叉，不删除。** 在运行前的 checkpoint 上写一个子 checkpoint，它成为新的 head；被取消的那一轮留在
历史里，可以审计。只有"运行前是空 thread"才 `adelete_thread`。
- `Overwrite` **必须有。** `messages` 通道的 reducer 是 `add_messages`，普通更新会把旧列表合并进当前列表，
而不是替换它。
- **用只有一个空节点的状态图来写，不用 agent 图。** 用 agent 图 `aupdate_state` 时 `as_node` 选得不对，
新 head 会带着待执行的任务，下一个 run 一启动就先把它跑了。这个图和 agent 图共享通道表，
schema 要用 thread 实际的 state schema：M6 的中间件会往里加通道，用基础 `ThreadState` 会把它们静默丢掉。
- **pending writes 重新挂上。** 运行前 checkpoint 上还没被消费的写入（比如上一轮留下的中断）要跟着还原。
- **失败都是 fail-closed。** 捕获失败就不回滚、只当 interrupt；还原失败只记日志，终态仍是 `interrupted`。
用户看到的是"停了，但没回滚"，这比写一半状态、假装回滚成功诚实。
- M4 的 delta 模式里，原始 checkpoint 不带物化好的消息列表，`channel_values` 读不到完整状态。
那时捕获要改走 `CheckpointStateAccessor`，还原改成在当前 head 上整体替换——DeerFlow 在这里吃过亏：
delta 模式下分叉，会把被放弃那一支的写入重放回来。



#### 四种失败模式的用户可见结果

回滚是一个"尽力而为"的收尾动作，四种情况都不改变终态：


| 情况                                  | 捕获                     | 还原                        | run 终态        | 用户看到                 |
| ----------------------------------- | ---------------------- | ------------------------- | ------------- | -------------------- |
| 正常回滚                                | 成功                     | 成功                        | `interrupted` | 对话回到本轮开始之前           |
| 运行前是空 thread                         | 成功（`checkpoint_config=None`） | `adelete_thread`          | `interrupted` | 空对话                  |
| 捕获失败（读 head 抛 / 没有 `channel_values`） | `None`                 | 抛 `RollbackUnavailable`，被吞 | `interrupted` | 停在原地，日志里一条 warning    |
| 还原失败（写 checkpoint 抛）                 | 成功                     | 抛，被吞                      | `interrupted` | 可能停在半路，日志里有 exception |


**没有一种情况会把 run 变成** `error`**。** 回滚是取消的附带效果，取消本身已经成功了；把它升级成
`error`，用户看到的是"停止失败"，而其实只是没回滚干净。宁可显示"停了，但没回滚"。

### 6.5 token 统计

```python
PROGRESS_REPORT_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0                       # M3 记进桶，不从 input 里扣；M9 账单再拆

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class UsageCollector(AsyncCallbackHandler):
    """挂在图的调用根（M1 §3.6）。M3 全部记在 lead；M6 按 middleware: 标签分流，M8 从 ToolMessage 补子代理。"""

    run_inline = True                                    # 见下：并行工具的回调按顺序在事件循环上跑

    def __init__(self, run_manager: RunManager, run_id: str) -> None:
        self._run_manager, self._run_id = run_manager, run_id
        self._by_model: dict[str, dict[str, int]] = {}
        self._calls = 0
        self._counted_llm_run_ids: set[str] = set()      # LangChain 的 llm run_id，不是 Gateway 的 run_id
        self._last_report = time.monotonic()

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID | None = None,
        tags: list[str] | None = None,                   # M3 忽略；M6 认 middleware:* / M8 认 subagent:
        **kwargs: Any,
    ) -> None:
        usage = _extract_usage(response)
        if usage.total <= 0:
            return                                       # 等同一次 llm run 的 usage 回放，见下
        rid = str(run_id) if run_id is not None else None
        if rid is not None and rid in self._counted_llm_run_ids:
            return
        if rid is not None:
            self._counted_llm_run_ids.add(rid)
        self._calls += 1
        model = _model_name(response) or "unknown"
        bucket = self._by_model.setdefault(model, {"input": 0, "output": 0, "cache_read": 0})
        bucket["input"] += usage.input_tokens
        bucket["output"] += usage.output_tokens
        bucket["cache_read"] += usage.cache_read_tokens

    def totals(self) -> RunTotals:
        input_tokens = sum(bucket["input"] for bucket in self._by_model.values())
        output_tokens = sum(bucket["output"] for bucket in self._by_model.values())
        return RunTotals(
            total_input_tokens=input_tokens,
            total_output_tokens=output_tokens,
            llm_call_count=self._calls,
            lead_agent_tokens=input_tokens + output_tokens,
            token_usage_by_model={model: dict(bucket) for model, bucket in self._by_model.items()},
        )

    async def maybe_report(self) -> None:
        now = time.monotonic()
        if now - self._last_report >= PROGRESS_REPORT_INTERVAL_SECONDS:
            self._last_report = now
            await self._run_manager.update_run_progress(self._run_id, self.totals())


def _extract_usage(response: LLMResult) -> _Usage:
    """优先 message.usage_metadata；没有再看 llm_output['token_usage']。缺字段当 0。"""
    input_tokens = output_tokens = cache_read = 0
    for generation in itertools.chain.from_iterable(response.generations or ()):
        message = getattr(generation, "message", None)
        meta = dict(getattr(message, "usage_metadata", None) or {})
        if not meta:
            llm_output = getattr(response, "llm_output", None) or {}
            meta = dict(llm_output.get("token_usage") or llm_output.get("usage") or {})
            if "prompt_tokens" in meta and "input_tokens" not in meta:
                meta["input_tokens"] = meta["prompt_tokens"]
            if "completion_tokens" in meta and "output_tokens" not in meta:
                meta["output_tokens"] = meta["completion_tokens"]
        input_tokens += int(meta.get("input_tokens") or 0)
        output_tokens += int(meta.get("output_tokens") or 0)
        details = meta.get("input_token_details") or {}
        if isinstance(details, Mapping):
            cache_read += int(details.get("cache_read") or 0)
    return _Usage(input_tokens, output_tokens, cache_read)


def _model_name(response: LLMResult) -> str | None:
    for generation in itertools.chain.from_iterable(response.generations or ()):
        message = getattr(generation, "message", None)
        meta = getattr(message, "response_metadata", None) or {}
        if isinstance(meta, Mapping) and (name := meta.get("model_name") or meta.get("model")):
            return str(name)
    return None
```


| 方法 / 字段          | 签名要点                               | 不变量                                                                        |
| ---------------- | ---------------------------------- | -------------------------------------------------------------------------- |
| `run_inline`     | 类属性 `True`                         | 回调在触发它的 task 上同步跑完，并行工具不会把 `_calls += 1` 交错成漏计或双计                          |
| `on_llm_end`     | `response`, `run_id=`, `tags=`     | `tags` M3 不读；`run_id` 是 LangChain 的一次 LLM 调用 id，用来去重                       |
| `_extract_usage` | 只读 `usage_metadata` / `llm_output` | 不读流里的消息；客户端没订 `messages-tuple` 时流里根本没有 token                               |
| `totals()`       | 无参，可重入                             | 各桶 `input`/`output` 之和 = `RunTotals` 的两个总数；M3 的 `lead_agent_tokens` = 两者之和 |
| `maybe_report()` | 无参                                 | 5 秒节流；失败由 `update_run_progress` 自己吞（§5.6 对非 running 静默）                    |


- **按** `on_llm_end` **记，不按流里的消息记。** 客户端没订 `messages-tuple` 时，流里根本没有 token 信息；
M6 中间件自己的模型调用（摘要、标题）也不经过流。
- **挂在调用根，只挂一份。** 模型实例上再挂一份就会重复计数，还会产生重复的 span（M1 §3.6）。
- **它依赖 `on_llm_end` 在 `create_agent` 下真的触发。** 项目里的 agent 由 `langchain.agents.create_agent`
  创建，chat 模型触发的是 `on_chat_model_start` + `on_llm_end`（`on_llm_start` 只对文本补全模型触发），
  回调挂在根 config 上由 ContextVar 继承下去。中间件若用自己的 `config={"callbacks": [...]}`
  覆盖继承链，这里同样收不到 usage——这条前提由 M4 §11 的 `test_llm_callbacks_fire.py` 锁住。
- **同一 LangChain** `run_id` **只计一次。** 流式提供商会先打一次 usage 为空的 `on_llm_end`，再回放一次
带齐 usage 的。先到的空包直接 return，等回放；回放还是空（提供商始终不给 usage）就漏计这一次——
宁可漏，也不要先加 0 再加实数把 `_calls` 加成 2，或者空包先占了 `run_id` 把后面的实数挡掉。
所以**空包既不加** `_calls`**，也不进** `_counted_llm_run_ids`。
- `run_inline = True` **不能省。** `AsyncCallbackHandler` 默认把 `on_llm_end` 调度成独立 task。
并行工具一开，两个回调会同时跑 `self._calls += 1`，事件循环上的交错读写不是原子的。
`run_inline` 让回调在 LangChain 触发它的那个点上跑完再继续，记账顺序等于回调触发顺序。
- **取不到模型名记进** `"unknown"` **桶**，不丢数：总数必须等于各桶之和。
- `cache_read` **记进桶，不从** `input` **里扣。** `usage_metadata.input_tokens` 已经含缓存命中；
再减一次账单就少了。M9 要拆价时读桶里的 `cache_read`，不改 M3 的总数不变量。
- **进度快照 5 秒节流。** 每次模型调用都写一次 store，到 M4 的 SQL 上会被放大；最终值由 `complete`
一次写定（§5.6），节流丢掉的中间值不影响账单。快照只在有帧到达时才可能触发，模型长时间不出帧的
那一段不会上报，终态会补上。
- M3 全部记在 lead 名下，三个维度之和恒等于总数。M6 按 `middleware:*` 标签分到 `middleware_tokens`，
M8 从子代理的 `ToolMessage.additional_kwargs` 补 `subagent_tokens`，不变量由 `RunTotals` 守（§5.6）。

#### 计数规则与不变量

每条规则都对应一个具体的错误模式，不是风格偏好：


| 规则                                        | 代码位置                                 | 守的不变量                     |
| ----------------------------------------- | ------------------------------------ | ------------------------- |
| 同一 LangChain `run_id` 只计一次              | `_counted_llm_run_ids`             | 提供商的 usage 回放不双计          |
| 空 usage 包既不进 `_counted_llm_run_ids`、也不加 `_calls` | `on_llm_end` 开头的 `usage.total <= 0` | 先到的空包不挡后面的实数              |
| 取不到模型名 → `"unknown"` 桶                   | `_model_name(response) or "unknown"` | 各桶之和 = 总数，不丢数            |
| `cache_read` 只记桶、不从 `input` 里扣           | `_Usage.cache_read_tokens`         | 总数与账单口径一致                 |
| 三个归因维度之和 = 总数                             | `RunTotals.__post_init__`（§5.6）      | 对不上只 `logger.error`，不抛   |
| 5 秒节流上报                                   | `maybe_report()`                   | 中间值可以丢，终态由 `complete` 一次写定 |

---



### 6.6 可测性：三个注入点

worker 的测试**不 mock** `RunManager`：它是纯内存状态机，真跑一次比配 mock 便宜，而且
`test_run_store_contract.py`（§11）已经把 store 的契约覆盖掉了。真正要换掉的是另外三个：


| 注入点                | 换成什么        | 假对象的最小契约                                                        | 对应用例                              |
| ------------------ | ----------- | --------------------------------------------------------------- | --------------------------------- |
| `ctx.agent_factory` | 返回假图的工厂     | 收 `run_config`；`app_config` / `checkpointer` 按签名可选；返回值能被 `unwrap_agent_graph` 处理 | `test_run_agent.py`、`test_stream_graph.py` |
| `bridge`           | 记录发布序列的假桥   | `publish` / `publish_end` / `subscribe` / `cleanup`            | `test_publish_end_always.py`     |
| `ctx.checkpointer` | M1 的内存 saver | `aget_tuple` / `aupdate_state` / `aput_writes` / `adelete_thread` | `test_rollback_point.py`         |


```python
class ScriptedGraph:
    """按脚本吐 chunk 的假图：抛异常、卡住、忽略取消都靠它注入。"""

    def __init__(self, script: Sequence[Any | BaseException], *, block: asyncio.Event | None = None) -> None:
        self._script, self._block = script, block

    def astream(self, input: Any, *, config: dict, stream_mode: list[str], subgraphs: bool) -> AsyncIterator[Any]:
        async def _gen() -> AsyncIterator[Any]:
            for item in self._script:
                if isinstance(item, BaseException):
                    raise item                       # 注入模型异常、沙箱超时
                if self._block is not None:
                    await self._block.wait()         # 注入"模型长时间没有输出"
                yield item
        return _gen()


class ListBridge:
    """记录 (run_id, event, data) 序列的假桥；断言 END 一定到达就看它的最后一项。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, Any]] = []

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        self.events.append((run_id, event, data))

    async def publish_end(self, run_id: str) -> None:
        self.events.append((run_id, "end", None))

    async def subscribe(self, run_id: str, **kwargs: Any) -> AsyncIterator[Any]:
        raise NotImplementedError("worker 不订阅；订阅是 SSE 层的事（§8）")

    async def cleanup(self, run_id: str, *, delay: float = 0.0) -> None:
        return None
```


- **假工厂要能被签名检查通过。** `_call_agent_factory` 按 `inspect.signature` 决定传不传
  `app_config` / `checkpointer`：测试工厂收 `(**kwargs)` 也在覆盖范围内，正是为了守住这条。
- **断言"END 一定到达"看的是最后一项，不是"有没有出现过 end"。** 顺序也是契约：
  END 之后不允许再有任何事件（§6.2）。`events[-1] == (run_id, "end", None)` 同时守住两件事。
- **AST 断言检查的是模块，不是测试。** `test_run_agent.py` 断言 `run_agent` 恰 4 个参数、
  模块不 import FastAPI（§11）；测试文件自己可以 import 任何东西。

### 6.7 后面每一阶段往 worker 里加东西的位置

这张表是给 M4–M10 的作者看的：**每一件事都有唯一的位置**，加在别处就是第二条执行栈。


| 要加的东西             | 加在哪                                                          | 为什么不是别处                                    |
| ----------------- | ------------------------------------------------------------ | ------------------------------------------ |
| M4 事件账本 publish   | `_stream_graph` 的 `publish` 旁边（同一份 `serialize` 结果）；flush 放 `_finalize` ① | 账本和 SSE 是两条管子（§10），但共用一次序列化；flush 必须在终态可见前 |
| M4 的 SQL checkpointer / thread_store | `RunContext` 的字段换实现                                      | worker 只认协议，不认识驱动                           |
| M4 的进度落库          | `update_run_progress` 的短事务                                  | `astream` 循环里不许持有 session（M4 §3.5）         |
| M6 标题生成           | `_finalize` ③；首轮被打断的 `fallback_title_from_state` 也在 ③      | 只依赖终态；提前写，被取消的 run 就没有标题（M6 §4.6）          |
| M6 人机确认           | `ctx.graph_input` 变成 `Command`；worker 一行不改                  | 中断是图语义，不是 worker 语义；thread 的 `interrupted` 只由它产生 |
| M6 中间件的模型调用       | `UsageCollector.on_llm_end` 按 `tags` 分流到 `middleware_tokens` | 挂在调用根，只有一处记账（§6.5）                         |
| M8 子图事件           | `_stream_graph` 的 `subgraphs=True` + `sse_event_name` 的 namespace | 解包和命名已经预留了 namespace（§6.2）                  |
| M8 子代理清理          | 内层 `finally` 里、`publish_end` 之前加 `cancel_all_for_run(run_id)` | 和 END 在同一个嵌套结构里，漏不掉（M8 §17）                |
| M9 租约续期           | **不进 worker**：RunManager 的心跳循环（M9 §3.2）                       | 心跳是进程级的，和"这条 run 跑到哪"无关                     |
| M9 丢租约后跳过持久化      | 每个写入点问 `should_persist_durable_state(record)`（M9 §3.1）       | 别写成十个 `if not record.ownership_lost`        |
| M10 扩展的 fail-open 判据 | 图内部（worker 不改），但 `except CancelledError` 必须只认运行期真取消            | 扩展用取消实现内部超时时，不能把成功的 run 标成 `interrupted`（M10 §4.5） |


- **加代码前先问：它属于 ①、②、③ 里的哪一段？** 答不出来，说明它要么不属于收尾，
  要么需要先改这一节——两种情况都不该直接往 `finally` 里塞。
- **收尾之外的钩子只有三处**：第 4 步（读 checkpoint）、第 6 步（组图）、第 7 步（每帧）。
  想加第四处时，先确认它不能挂在图或中间件上（M6 的中间件正是为这类需求准备的）。

---





## 7. StreamBridge（W2）

#### bridge 的公开符号

桥是一个小而完整的对象：**一个 ABC + 四个方法 + 三个控制对象**。它不引用 worker、不认识
RunManager，也不知道 HTTP 存在。


| 符号                                                   | 谁调                                              | 做什么                            | 见    |
| ---------------------------------------------------- | ----------------------------------------------- | ------------------------------ | ---- |
| `StreamBridge`（ABC）                                | `MemoryStreamBridge` / M9 的 `RedisStreamBridge` | 定义四个方法与 `supports_cross_process` | §7.2 |
| `StreamEvent`                                       | `sse_consumer`（§8.1）                           | 一条可重放事件（`id` / `event` / `data`） | §7.2 |
| `HEARTBEAT_SENTINEL` / `END_SENTINEL`             | 两类消费者（§8.1、§8.3）                                | 控制对象，不是业务事件                    | §7.2 |
| `StreamGap`                                         | `sse_consumer`、`wait_for_run_completion`       | 游标已不在保留窗口内                     | §7.3 |
| `bridge.publish` / `bridge.publish_end`            | worker（§6.2 第 7 步、内层 `finally`）                 | 生产事件、宣告结束                      | §7.2 |
| `bridge.subscribe`                                  | `sse_consumer`（§8.1）、`/wait`（§8.3）、M9 的 IM 通道   | 消费事件（重放 + 实时）                  | §7.4 |
| `bridge.cleanup`                                    | worker 收尾（detached，延迟 60 秒，§6.2）、M9 的孤儿回收        | 过期回收                           | §7.3 |
| `MemoryStreamBridge`                                | lifespan（`build_run_services`，§4.1）             | M3 的实现；M9 换 Redis             | §7.5 |
| `create_stream_bridge(config)`                       | lifespan                                        | 按 `backend` 懒加载选后端            | §7.5 |


三条纪律：

- **不认识 HTTP，也不认识 run 的状态。** 桥不 import `fastapi`，也不 import `RunManager`：
  它只知道"这个 `run_id` 上有没有事件、结束没结束"，不知道这一轮是成功还是失败，也不知道属主是谁。
  判存在性、判属主、判终态分别在路由、RunManager 和 store 里。
- **不持久化。** 缓冲是有界的直播窗口，历史由 M4 的事件账本负责（§10）。拿桥的缓冲当历史，
  页面刷新之后就什么都没有了。
- **不反压 producer。** 订阅者慢，桥裁自己的缓冲、让慢订阅者拿 gap；绝不让 worker 等订阅者
  （§7.3）。这条决定了 `publish` 不能是"等所有人收下再返回"。

### 7.1 为什么需要这层

**唯一的理由：创建 run 的那个 HTTP 连接，和消费 SSE 的那个连接，不一定是同一个。**

刷新页面、多标签页、断线重连、`/join` 旁观、IM 通道在进程内订阅——
这些场景下生产者和消费者完全解耦。没有这层，它们就只能耦合在同一个调用栈上。

（反过来，M1 的嵌入式 client **不用** StreamBridge：生产者和消费者在同一个调用栈，直接 `yield`。）

#### 四类消费者，差异只有三个旋钮

桥的 API 之所以是"四个方法 + 三个参数"，是因为下游只有这四类需求，而它们的差别只有三处：
**从哪开始（游标）、要不要心跳、缺了一段怎么办**。


| 消费者                              | 要什么                  | 怎么用桥                                                       |
| -------------------------------- | -------------------- | ---------------------------------------------------------- |
| 创建端 SSE（`POST /runs/stream`）    | 从第一条事件开始、实时、断线能续     | `subscribe(run_id)`；断开按 §8.2 处理                           |
| `/join`（刷新页面、多标签页、旁观）           | 带游标续传；缺了就诚实报缺        | `subscribe(run_id, last_event_id=...)`；gap 后终止订阅，客户端 reload |
| `/wait`                          | 只关心什么时候结束，不渲染任何东西    | `subscribe(...)`；gap 后从 `latest_available_event_id` 续订（§8.3） |
| IM 通道 / 子代理（M9、M8）               | 进程内直读，不需要重放、不需要心跳保活  | 同一个 `subscribe`，`heartbeat_interval=0`                  |


**不为任何一类开专用方法。** "IM 通道要一个 `drain()`"、"调度器要一个 `peek()`"这类需求
最后都会变成第二套语义；三个旋钮表达不了的时候，先确认它是不是真的属于桥。

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

#### 四个方法的参数定义


| 方法                                                                                              | 谁调                                   | 参数                                                                          | 返回 / 抛                                                    |
| ----------------------------------------------------------------------------------------------- | ------------------------------------ | --------------------------------------------------------------------------- | --------------------------------------------------------- |
| `async publish(run_id: str, event: str, data: Any) -> None`                                       | worker（§6.2 第 7 步）                    | `run_id` 分区键；`event` 已经过 `sse_event_name` 定名；`data` 已经过 `serialize` | `None`。**不因订阅者慢而抛**；只有实现 bug 才抛（§7.3）                     |
| `async publish_end(run_id: str) -> None`                                                          | worker 的嵌套 `finally`（§6.2）          | 同上                                                                        | `None`；**幂等**，重复调用无害                                     |
| `subscribe(run_id, *, last_event_id=None, heartbeat_interval=None) -> AsyncIterator[...]`            | `sse_consumer`、`/wait`、M9 的 IM 通道   | 见下                                                                          | 异步迭代器。**不抛**；缓冲按需创建（订阅早于首帧是常态），只有**墓碑**才立即产出一个 END（§7.4）                    |
| `async cleanup(run_id: str, *, delay: float = 0.0) -> None`                                       | worker 收尾（detached，`delay=60`）      | `delay` 秒之后删掉这条 run 的缓冲                                                    | `None`；重复调用无害                                            |


`subscribe` 的三个参数：


| 参数                   | 类型              | 缺省     | 谁给                       | 含义                                                      |
| -------------------- | --------------- | ------ | ------------------------ | ------------------------------------------------------- |
| `run_id`             | `str`         | ——     | 路由从 `RunRecord` 拿        | 分区键。桥**不判存在性**：路由必须先查一次 RunManager（`None` → 404），见 §7.4 |
| `last_event_id`      | `str \| None` | `None` | `Last-Event-ID` 头原样传     | 续传游标；`None` 与 `""` 都当"从头订"（SDK 会把空 id 原样带回来，§7.2）    |
| `heartbeat_interval` | `float \| None` | `None` | 路由 / 调用方                 | `None` = 用配置值；`<= 0` = 关掉心跳（嵌入式、IM 通道直读）               |


#### `StreamEvent` 的三个字段


| 字段      | 类型    | 谁写                                        | 规则                                                                        |
| ------- | ----- | ----------------------------------------- | ------------------------------------------------------------------------- |
| `id`    | `str` | **桥**（`publish` 时分配）                     | 每个 run 内单调递增，从 `"1"` 开始。**消费方只做相等比较，不得解析成整数**（M9 的 Redis 实现是 `<ms>-<seq>`） |
| `event` | `str` | worker（§6.2 的 `sse_event_name`）           | 直接变成 SSE 的 `event:`。`end` / `gap` 是控制对象的名字，业务事件不得占用                       |
| `data`  | `Any` | worker（`serialize` 的结果）                  | 桥**不再序列化**；编帧时才 `json.dumps`（§8.1）                                        |


- **只有 `publish()` 产生的事件有 id。** 心跳、END、gap 都没有（§7.2）：给了 id，客户端会把它记成
  `Last-Event-ID`，重连时带回来一个缓冲里根本不存在的游标，然后收到一个本可避免的 gap。
- **`metadata` 帧也是一个普通数据事件**（worker 第 5 步 publish 的），所以它拿到 `id=1`——
  §8.1 的示例帧就是这么来的。桥不区分帧的业务含义。
- **`id` 是字符串，不是数字。** 内存实现恰好连续可解析，Redis 实现不是；把 `id` 当整数比较的
  代码在 M9 会静默算错。

#### 事件对象的共享与不可变


| 问题                | 答案                                     | 理由                                                                  |
| ----------------- | -------------------------------------- | ------------------------------------------------------------------- |
| 一条事件被几个订阅者看到      | **全部**：fanout 时共享同一个对象，**不深拷贝**        | 深拷贝会把每帧的成本乘以订阅者数；而 `data` 已经过 `serialize`，本来就是 JSON 值             |
| 事件能不能被改           | 不能。`StreamEvent` 是 `frozen=True` 的 dataclass | 一个订阅者改了 `data`，其他订阅者看到的就是被改过的内容——这种 bug 只在"两个订阅者同时看一条 run"时出现      |
| `data` 里能不能放可变对象 | 不能：它只能是 `serialize()` 的结果（JSON 值）     | 同一份 dict 被 N 个订阅者共享；消费者要改，先自己 copy 一份                                |
| 桥要不要做防御性拷贝        | 不要                                     | 真要防就防在 `serialize`——它是唯一入口，而且在那里拷贝只发生一次（不是每个订阅者一次）                  |


这条契约在 Redis 实现下自动成立（跨进程只能传字节），所以**内存实现也必须守**：
否则本地测试全绿，多实例一上就出现"另一个订阅者看到的数据被别人改过"。

#### 发布顺序的三条保证


| 保证                    | 内容                                          | 靠什么                                             |
| --------------------- | ------------------------------------------- | ----------------------------------------------- |
| 同一 producer 的 FIFO   | 先 `publish` 的事件先被看到                       | producer 是**一个** task；`publish` 的同步段不被打断       |
| `id` 与到达顺序一致       | `id` 单调递增，且订阅者看到的顺序就是 id 顺序               | 分配 id、append、fanout 在同一个同步段里完成                  |
| END 是每个订阅者看到的最后一项     | END 之后不再有任何事件（producer 再 `publish` 会被丢弃并告警） | `buffer.ended` + `publish` 的早退（§7.3）            |


反例（说明这三条不是白写的）：如果 `id` 在某个 `await` 之后才分配，
两个并发 producer 就可能拿到同一个 id 或逆序 id；一旦 `id` 与到达顺序不一致，
客户端的 `Last-Event-ID` 续传就会**重放已经看过的事件或跳过没看过的事件**，
而这两种症状都不会报错。

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

#### 内存实现的数据结构与发布路径

两级缓冲，职责不同——**它们回答的是两个不同的问题**：


| 缓冲                          | 容量                                   | 回答的问题         | 满了怎么办            |
| --------------------------- | ------------------------------------ | ------------- | ---------------- |
| `_RunBuffer.events`（每 run 一个） | `queue_maxsize`（默认 256）             | "能重放多久"       | 从头部裁，watermark 前进 |
| `_Subscriber.queue`（每订阅者一个） | `SUBSCRIBER_QUEUE_MAXSIZE`（常量 64）   | "这个消费者跟得上吗"   | 标记 `lagged`，下次唤醒发 gap 然后停 |


```python
# agentkit/stream_bridge/memory.py
SUBSCRIBER_QUEUE_MAXSIZE = 64          # 投递队列：不可重放，满了就是"这个订阅者落后了"
TOMBSTONE_LIMIT = 4096                 # 只保留最近这么多条墓碑（约 1 MB 上限，见 §7.4）


@dataclass(slots=True)
class _Subscriber:
    queue: asyncio.Queue[Any] = field(default_factory=lambda: asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAXSIZE))
    cursor: str | None = None          # 已经交付给它的最后一条事件 id（gap 的 requested_event_id）
    lagged: bool = False               # 投递队列溢出：下次唤醒时发 gap 然后停
    pending_end: bool = False          # END 因队列满没能入队：下次唤醒时直接结束（见"END 不能被吞掉"）


@dataclass(slots=True)
class _RunBuffer:
    events: deque[StreamEvent] = field(default_factory=deque)   # 保留窗口，左端最老
    next_id: int = 1                                            # 下一条数据事件的 id
    ended: bool = False                                         # producer 调过 publish_end
    reclaimed: bool = False                                     # 已回收（墓碑）：之后订阅它的人立即拿 END
    subscribers: set[_Subscriber] = field(default_factory=set)

    @property
    def start_offset(self) -> int:     # watermark：窗口内最老事件的 id；空窗口时等于"下一条"
        return int(self.events[0].id) if self.events else self.next_id

    @property
    def latest_offset(self) -> int:    # 上界：已经分配出去的最后一个 id
        return self.next_id - 1
```


```python
class MemoryStreamBridge(StreamBridge):
    supports_cross_process = False

    def __init__(self, *, queue_maxsize: int = 256, heartbeat_interval: float = 15.0) -> None:
        self._queue_maxsize = queue_maxsize
        self._heartbeat_interval = heartbeat_interval
        self._buffers: dict[str, _RunBuffer] = {}
        self._tombstones: deque[str] = deque()      # 有界：墓碑被挤出去之后，那个 id 又变回"未知"

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        # 函数体内没有 await，事件循环不会在中间切走——这就是全部的原子性（同 §5.1 的 MemoryRunStore）。
        # 以后谁要在这里加 await（比如 M4 顺手写账本），必须先加一把锁。
        buffer = self._buffers.setdefault(run_id, _RunBuffer())
        if buffer.ended:
            logger.warning("Dropping %r published after end for run %s", event, run_id)
            return
        item = StreamEvent(id=str(buffer.next_id), event=event, data=data)
        buffer.next_id += 1
        buffer.events.append(item)
        while len(buffer.events) > self._queue_maxsize:
            buffer.events.popleft()                    # 裁剪最老的：watermark 跟着前进
        self._fanout(buffer, item)

    async def publish_end(self, run_id: str) -> None:
        buffer = self._buffers.setdefault(run_id, _RunBuffer())   # 一条事件都没发过的 run 也留缓冲
        if buffer.ended:
            return                                      # 幂等：worker 只该调一次，重复不致命
        buffer.ended = True
        self._fanout(buffer, END_SENTINEL)

    def _fanout(self, buffer: _RunBuffer, item: Any) -> None:
        for subscriber in list(buffer.subscribers):     # 快照：finally 里的注销可能发生在这一轮迭代中
            try:
                subscriber.queue.put_nowait(item)
            except asyncio.QueueFull:
                if item is END_SENTINEL:
                    subscriber.pending_end = True       # ★ END 不能被"队列满"吞掉（见下）
                else:
                    subscriber.lagged = True            # 普通事件溢出：它下一次唤醒时拿 gap

    async def cleanup(self, run_id: str, *, delay: float = 0.0) -> None:
        """延迟回收。**只丢事件载荷，留下一个墓碑**——"这条 run 已经结束"这个事实必须保留，
        否则之后订阅它的人会退回"未知 run"分支（§7.4 的三种状态）。"""
        if delay > 0:
            await asyncio.sleep(delay)
        buffer = self._buffers.get(run_id)
        if buffer is None:
            return
        buffer.events.clear()                       # 载荷是大头，先扔
        buffer.ended = True                         # 从没调过 publish_end 的 run（强制取消）也在这里收口
        buffer.reclaimed = True
        for subscriber in list(buffer.subscribers):
            try:
                subscriber.queue.put_nowait(END_SENTINEL)
            except asyncio.QueueFull:
                subscriber.pending_end = True       # 还挂着的订阅者不能被吊着（§7.3）
        self._remember_tombstone(run_id)

    def _remember_tombstone(self, run_id: str) -> None:
        self._tombstones.append(run_id)
        while len(self._tombstones) > TOMBSTONE_LIMIT:
            self._buffers.pop(self._tombstones.popleft(), None)   # 淘汰最老的墓碑
```


- **`publish` 的原子性来自"没有 await"**，和 `MemoryRunStore`（§5.1）是同一个手法：
  分配 id、append、裁剪、投递四步要么全做完，要么一步没做。加一把锁反而会让人以为
  这里存在需要保护的 await 窗口。
- **`setdefault` 不是防御性编程。** `publish_end` 对一个从没发过事件的 run 也要建缓冲：
  否则"订阅发生在 END 之后"和"订阅一个不存在的 run"会得到同一个结果——而前者应当稳定地
  给出一个 END（§7.4）。
- **END 之后还来的 publish 丢弃并告警，不抛也不复活。** 收尾之后仍可能有 detached 任务想写帧
  （M4 的账本、M8 的清理）。抛异常会把一个已经成功的 run 变成 `error`，而"多一帧没人要的事件"
  远没有这么严重。
- **裁剪只发生在 `publish` 里**，所以 `start_offset` 永远等于"当前窗口里最老的那条"，
  不需要再维护一个独立的 watermark 变量去跟它同步（那正是经典的 off-by-one 来源）。
- **`_fanout` 用 `put_nowait`，永不 await。** 这是"不反压 producer"的落点：慢订阅者的代价由它
  自己承担（gap + 断开），不转嫁给 worker。

#### 为什么是每订阅者队列，不是 `asyncio.Condition`

这是读到这里必然要问的问题：既然 `_RunBuffer` 已经存了保留窗口，为什么还要给每个订阅者一个队列？
直接 `notify_all()` 让大家自己去窗口里取，不是更省吗？

**先纠一个误解：** `_Subscriber` 不是"每个订阅者一份数据副本"。事件对象是**共享**的
（§7.2 的不可变契约），fanout 只是把同一个对象 `put_nowait` 进 N 个队列。
`_Subscriber` 真实持有的东西只有：

```python
@dataclass(slots=True)
class _Subscriber:
    queue: asyncio.Queue[Any]   # 64 个投递位（指针），不是 64 份事件
    cursor: str | None          # 我交付到哪一条了（gap 要带它）
    lagged: bool                # 我落后了（队列溢出）
    pending_end: bool           # 流结束了，但我没挤进队列
```

所以它换来的不是"数据"，是**投递语义**。下面逐条对比。

##### `asyncio.Condition` 的三个硬伤


| 硬伤                                    | 具体表现                                                                                 | 后果                                                                                    |
| ------------------------------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| **`notify()` 必须先持锁，而持锁是 `await`**    | `async with cond:` → `cond.notify_all()`；`Condition.notify()` 要求调用者已持有锁                 | `publish` 从"零 await 的同步段"变成异步操作——§7.3 的原子性论证**直接失效**：分配 id、append、裁剪、投递之间出现了让出点        |
| **`Condition.wait()` 没有 timeout**    | 心跳只能写成 `wait_for(cond.wait(), timeout)`，而 `wait_for` 取消 `wait()` 时这次 `notify` 是**边沿触发**的，会丢 | 丢一次通知就得靠"每次醒来重扫缓冲"兜底；而"重扫"已经等价于自己实现一个队列                                                |
| **不能在持锁期间 `yield`**                 | 生成器 `yield` 把控制权交回调用方（SSE 编帧、socket 写），慢客户端会**持着锁**待上几秒                              | 慢订阅者拖垮所有订阅者——正好违反"订阅者互不影响"这条不变量（§7.3）                                                  |


第三条尤其关键：队列方案的 `await queue.get()` 天然是"拿到即释放"，而 Condition 方案必须写成
"锁内取一批 → 出锁 → 逐个 yield"，中间的边界（取完之后窗口又被裁了怎么办、yield 期间又来了新事件怎么办）
全都要自己推一遍。

##### 三条替代路线的对比


| 方案                               | publish 里有没有 await       | 心跳                                    | 每订阅者状态      | 慢订阅者                                                     |
| -------------------------------- | ------------------------ | ------------------------------------- | ----------- | -------------------------------------------------------- |
| **每订阅者队列（本设计）**                  | **没有**（纯 `put_nowait`） | `wait_for(queue.get())`              | 队列 + cursor | 写满自己的 64 格 → `lagged` → gap，**只影响自己**                    |
| `Condition` + 共享窗口             | **有**（要拿锁）               | 会丢通知                                  | 只有 cursor   | 持锁 yield → 拖垮所有人                                         |
| 共享窗口 + 每订阅者 `Event`（电平触发）       | 没有（`Event.set()` 是同步的）  | `wait_for(event.wait())` 不会丢（电平触发）  | 只有 cursor   | 不受队列限制，但每帧要**唤醒所有人**并各自重扫缓冲 → token 流下的热点                |

##### `_Subscriber` 换来的三件 `Condition` 给不了的事

1. **投递隔离与有界积压**：慢订阅者写满自己的队列就被标记，`publish` 既不阻塞也不反压 producer。
   Condition 方案里，"慢"的代价由所有人在锁上分摊。
2. **"注册即快照"的无缝衔接**：注册、快照（`_resolve_replay`）、游标解析在**同一个同步段**里完成，
   重放列表与投递队列之间不需要再加锁、也不需要再扫一次缓冲（§7.4）。
3. **END 的 FIFO 语义**：`END_SENTINEL` 是队列里的一个元素，天然排在"已经投递的事件"之后。
   换成共享的 `ended` 标志，每个订阅者都要自己保证"先把窗口里剩下的发完，再发 END"。

**另外，它与 M9 的 Redis 实现同构**：两者都表达成"每消费者一份游标 + 一个有界投递位"
（consumer group / per-consumer cursor）。用 `asyncio.Condition` 则会把一个**进程内原语**
焊进接口语义里——Redis 那边根本没有对应物。

##### 什么时候该换，换成什么

如果将来订阅者数量变得很大（一屏几十个旁观者），或者帧很小很密（逐 token 推送），
O(订阅者数) 的唤醒与队列就不再划算。那时正确的方向**不是 Condition，而是把 `subscribe`
改成拉模型**：订阅者按 `seq` 从窗口拉，窗口是唯一缓冲，每人只维护自己的 `cursor`。

这条路已经铺好了一半：`_resolve_replay` 与 `Last-Event-ID` 本来就是"带游标消费"的形状，
`/wait` 也已经是"gap 之后从窗口尾续订"的消费者。M3 的规模（每条 run 个位数订阅者）下，
队列换来的隔离与顺序确定性更值；把这个门槛记在这里，等数据说话再换。

#### 发布路径的五个步骤


| # | 动作                                              | 谁保证     | 异常 / 边界                              |
| - | ----------------------------------------------- | ------- | ------------------------------------ |
| 1 | 取 / 建 `_RunBuffer`（`setdefault`）              | 同步段     | ——                                   |
| 2 | `ended` 检查：已结束就丢弃 + 告警                         | 同步段     | 不抛：收尾之后仍有 detached 任务想写帧是正常现象（§6.2）   |
| 3 | 分配 `id`、append、裁剪到 `queue_maxsize`            | 同步段     | ——                                   |
| 4 | fanout：对每个订阅者 `put_nowait`                     | 同步段     | 队列满 → 标记 `lagged` / `pending_end`，**不抛、不阻塞** |
| 5 | 返回 `None`                                      | ——      | 只有桥自己的实现 bug 才抛（M3 §6.2：publish 抛 → run 变 `error`） |


**这五步必须在同一个同步段里跑完**：任何一步之后插入一个 `await`，
都会出现"事件已经在窗口里、但某个订阅者既没在重放列表也没在队列里"的窗口——
客户端永远等不到那一条，而且不报任何错。这是 `publish` 里"没有 await"那条纪律的全部含义。

#### END 不能被"队列满"吞掉

```python
# agentkit/stream_bridge/memory.py（_fanout 的分支）
except asyncio.QueueFull:
    if item is END_SENTINEL:
        subscriber.pending_end = True       # 流确实结束了，只是没挤进队列
    else:
        subscriber.lagged = True            # 普通事件溢出：下一次唤醒时发 gap
```

- **不补这一行也能工作**：慢订阅者会先拿 gap，`/wait` 从 `latest_available_event_id` 续订、
  浏览器 `reload_durable_state`，最终都能发现"已经结束"。但每个慢订阅者都要白跑一轮
  续订/重拉，而 `/wait` 的续订窗口里如果 `cleanup` 到了，它看到的会是一个陌生 run 的立即 END——
  结果是对的，路径却长得多。
- **判定顺序在消费端**：`subscribe` 的循环里 `pending_end` 排在 `lagged` **前面**。
  反过来的话，一个既溢出又已经结束的订阅者会拿到 gap，把"结束"这个确定信息降级成"你缺了一段"。

#### 发布者不止一个时（M8 / M9）

M3 只有**一个** producer：worker 里跑 `_stream_graph` 的那个 task。上面三条保证都建立在这一点上。
将来如果 M8 的子代理、M9 的别的路径**直接**调 `publish`：

- 跨 producer 只有"**进入同步段的顺序**"，没有因果关系保证——先调用不代表因果上更早；
- 所以那些事件必须自带身份（`task_id` / `caller` 放进 `data` 或事件名），
  消费端按身份重建因果，**不要按到达顺序推断**（M8 §17 的子代理事件就是这个形状）；
- 而且**不许为了"顺手写账本"在 `publish` 里加 `await`**：那会立刻打破第 2、3 步之间的原子性。
  真要加，先加锁再改（§5.1 的 `MemoryRunStore` 是同一条纪律）。

#### 内存上界：`queue_maxsize` 到底在限制什么


| 组成          | 上界                              | 默认值下的量级                        |
| ----------- | ------------------------------- | ------------------------------ |
| 每 run 保留窗口  | `queue_maxsize` × 平均帧大小        | 256 × ~2 KB ≈ **0.5 MB**       |
| 每订阅者投递队列    | `SUBSCRIBER_QUEUE_MAXSIZE` × 帧大小 | 64 × ~2 KB ≈ **128 KB**        |
| 墓碑（已回收 run 的 id） | `TOMBSTONE_LIMIT` × ~200 B     | 4096 × 200 B ≈ **0.8 MB**（全局，不随 run 数增长） |
| 活跃 run 数    | 进程内同时 SSE 的会话数                  | M3 无硬上限；M9 由并发门限制              |
| **合计**      | 上面三项相乘                          | 20 run × 3 订阅者 ≈ **18 MB**     |


- `queue_maxsize` 同时决定**能重放多久**和**单 run 内存**，所以它不是一个"越大越好"的参数：
  按最坏情况估（一帧 8 KB、50 条并发 run、各 3 个订阅者）→ 256 帧就是 ~100 MB。
  到那个量级要么调小它，要么让客户端消费得更快，要么把重放窗口挪到 Redis（M9 用 TTL + `MAXLEN` 做同样的取舍）。
- **估算的意义在于回答"为什么是 256"**：它不是拍出来的，而是"一次刷新能补上的事件数"
  与"单 run 内存预算"之间的折中。改这个值之前，先把这张表重算一遍。

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

#### 游标解析：把 `Last-Event-ID` 翻译成"重放哪几条"或"发 gap"

```python
def _earliest(buffer: _RunBuffer) -> str | None:
    return buffer.events[0].id if buffer.events else None


def _latest(buffer: _RunBuffer) -> str | None:
    return buffer.events[-1].id if buffer.events else None


@dataclass(frozen=True, slots=True)
class _Replay:
    items: tuple[StreamEvent, ...]        # 要重放的事件，保序
    gap: StreamGap | None                 # 非 None 时 items 必为空


def _resolve_replay(buffer: _RunBuffer, last_event_id: str | None) -> _Replay:
    """纯函数，不 await。窗口的读写都在同一个同步段里，所以它看到的永远是自洽的一份。"""
    events = tuple(buffer.events)
    earliest, latest = _earliest(buffer), _latest(buffer)

    def _gap() -> _Replay:
        return _Replay(items=(), gap=StreamGap(
            requested_event_id=last_event_id,
            earliest_available_event_id=earliest,
            latest_available_event_id=latest,
        ))

    if not events:                                  # 还没发过事件：等价于"从头订"，不是 gap（§7.3）
        return _Replay(items=(), gap=None)
    if last_event_id in (None, ""):                 # 全新订阅：重放整个保留窗口
        return _Replay(items=events, gap=None)
    try:
        cursor = int(last_event_id)
    except ValueError:
        return _gap()                               # 认不出的游标：无法核对，宁可让客户端 reload
    if cursor < int(earliest) or cursor > int(latest):
        return _gap()                               # 低于 watermark（被裁了）或高于上界（不是这条 run 的）
    return _Replay(items=tuple(e for e in events if int(e.id) > cursor), gap=None)
```


| `Last-Event-ID`      | 行为                          | 为什么                                              |
| --------------------- | --------------------------- | ------------------------------------------------ |
| 缺省 / `""`           | 重放整个保留窗口；缓冲为空则只等新事件         | 新订阅。空串是 SDK 把心跳/END 的空 id 原样带回来的结果（§7.2）         |
| 在窗口内，不等于最新 id         | 从它的下一条开始重放                  | 正常续传                                             |
| 等于最新 id              | 不重放，只等新事件                   | `/wait` 收到 gap 后就是从这里续订的（§8.3）                   |
| 低于 `start_offset`    | gap                         | 被裁掉的事件已无法核对，不能假设它就是客户端要的那条（§7.4）                 |
| 认不出 / 高于最新 id         | gap                         | 不是这条 run 的游标（重启、别的实例、伪造）；让客户端重拉状态比静默续上诚实           |
| 缓冲为空                  | 不 gap，只等新事件                 | 此时 `StreamGap` 的三个 id 都是 `None`（§7.3）           |


#### `subscribe`：注册、重放、心跳、END

```python
    def subscribe(
        self, run_id: str, *, last_event_id: str | None = None, heartbeat_interval: float | None = None
    ) -> AsyncIterator[StreamEvent | StreamGap | _Sentinel]:
        async def _stream() -> AsyncIterator[Any]:
            # ★ 缓冲按需创建，**不能**把"没有缓冲"当成"未知 run / 已结束"：
            #   start_run 返回 → 路由建 SSE → worker 才走到第 5 步的 metadata 帧，
            #   这段时间里缓冲是空的、但 run 明明活着。用 .get() 判未知，客户端会在
            #   第一帧到来之前就收到 end。真正的"已结束"由墓碑（reclaimed）表达。
            buffer = self._buffers.setdefault(run_id, _RunBuffer())
            if buffer.reclaimed:                    # 墓碑：这条 run 结束且事件已被回收
                yield END_SENTINEL
                return

            timeout = self._heartbeat_interval if heartbeat_interval is None else heartbeat_interval
            subscriber = _Subscriber()
            # ★ 注册与快照必须在同一个同步段里：中间只要有一个 await，
            #   那期间 publish 的事件就既不在重放列表里、也不在投递队列里，而且不会报任何错。
            buffer.subscribers.add(subscriber)
            replay, ended = _resolve_replay(buffer, last_event_id), buffer.ended
            try:
                if replay.gap is not None:
                    yield replay.gap                # gap 是这条订阅的终点，调用方不得在它之后续订
                    return
                for item in replay.items:
                    subscriber.cursor = item.id
                    yield item
                if ended:
                    yield END_SENTINEL              # 订阅发生在 END 之后：先给窗口，再立刻给 END
                    return
                while True:
                    if subscriber.pending_end:      # ★ 先看 END：队列满了，但流确实已经结束
                        yield END_SENTINEL
                        return
                    if subscriber.lagged:
                        yield StreamGap(
                            requested_event_id=subscriber.cursor,
                            earliest_available_event_id=_earliest(buffer),
                            latest_available_event_id=_latest(buffer),
                        )
                        return
                    try:
                        item = await asyncio.wait_for(
                            subscriber.queue.get(), timeout=timeout if timeout > 0 else None
                        )
                    except TimeoutError:
                        yield HEARTBEAT_SENTINEL        # 给消费循环一次检查"客户端还在不在"的机会（§8.1）
                        continue
                    if item is END_SENTINEL:
                        yield END_SENTINEL
                        return
                    subscriber.cursor = item.id
                    yield item
            finally:
                buffer.subscribers.discard(subscriber)   # 正常结束、gap、异常、aclose 四条路径都走这里

        return _stream()
```


- **`subscribe()` 本身不注册任何东西。** 它返回异步生成器，注册发生在第一次 `__anext__`。
  这样路由已经返回了 `StreamingResponse`、客户端却立刻断开时，不会留下一个没人消费的订阅者。
- **注销在 `finally` 里，不在 `return` 前。** gap、END、`CancelledError`、`aclose()` 都是离开
  生成器的方式；只在正常路径注销，慢消费者的队列就会一直留在 `subscribers` 里，
  每次 `_fanout` 都白投一次。
- **心跳是 `wait_for` 的超时，不是后台任务。** 每个订阅者起一个心跳 task 太贵；
  `wait_for` 只在空闲到点时才触发，有事件时它连一次额外调度都不做。`heartbeat_interval <= 0`
  时传 `timeout=None`，就是纯粹的 `queue.get()`（IM 通道直读用得上）。
- **缓冲按需创建；"没有缓冲"不等于"已结束"。** 详见下面那张三态表——**存在性和属主由路由在
  订阅之前判**（`RunManager.get` 拿到 `None` → 404），桥不查库、也不查内存登记表。
  M9 的跨进程实现要更小心：durable 的终态**不等于**流已结束（落库可能早于最后几帧），
  只有 `stop_reason=orphan_recovered` 才允许合成 END（M9 §3.6）。
- **gap 之后这一条订阅就结束了。** 续订是调用方的事，而且必须换一个起点：
  `/wait` 用 `latest_available_event_id`（§8.3），浏览器用 `reload_durable_state`（§8.1）。

#### 订阅早于首帧：缓冲必须在订阅时就存在

这是一个真实的竞态，而且是**常态**而不是边角：

```
start_run()  ──► create_or_reject（准入）──► asyncio.create_task(worker) ──► HTTP 立刻返回
                                                    │
                                                    ① wait_for_prior_finalizing（可能等上一个 run 收尾）
                                                    ② try_start（一次 store CAS）
                                                    ③ thread_store.set_status("running")
                                                    ④ capture_rollback_point（读一次 checkpoint）
                                                    ⑤ publish("metadata")   ← ★ 第一帧在这里才出现
路由侧：拿到 launch ──► 建 StreamingResponse ──► sse_consumer ──► bridge.subscribe()   ← 通常早于 ⑤
```

**如果 `subscribe` 用 `self._buffers.get(run_id)` 判"未知 run"并立刻 `yield END_SENTINEL`，
客户端会在第一帧到来之前就收到 `end`**：流看起来"瞬间结束"，而 run 其实正常跑着，
之后所有帧都发进一个已经没人订阅的缓冲。症状是"偶发：刷新一下就正常了"——
因为刷新后 run 已经跑起来、缓冲存在了。

修法是**把缓冲的创建从"第一次 publish"提前到"第一次 subscribe"**，并明确区分三种状态：


| 订阅时桥里的状态                      | 含义                             | `subscribe` 的行为                    |
| ----------------------------- | ------------------------------ | ----------------------------------- |
| 有缓冲、未 `ended`                | run 已准入；**可能一条事件都还没发过**        | 重放窗口（空则不发）→ 进入实时等待（配心跳）             |
| 有缓冲、`ended`、未 `reclaimed`  | 流已结束，事件还在保留期内                  | 重放窗口 → 立刻 END                       |
| 有缓冲、`reclaimed`（墓碑）         | 已结束且事件已被 `cleanup` 回收         | **立刻 END**                          |
| 没有缓冲                          | 从没见过这个 `run_id`（路由本该已经 404）   | **新建空缓冲并等待**（不是 END！）               |


**第 4 行带来的新义务：存在性判定完全归调用方。** 桥不再用"有没有缓冲"猜 run 是否存在——
它猜不准（准入后、首帧前正是"没有缓冲"）。所以：

- `GET .../join` 必须先 `RunManager.get` → `None` 就 404（§8.2，已经有了）；
- `POST .../runs/stream` / `wait` 的 run 是刚准入的，天然存在；
- M9 的 IM 通道 / 子代理路径同理：**先查再订**，不许直接把用户给的 `run_id` 丢给 `subscribe`。

**墓碑是有界的**（`TOMBSTONE_LIMIT = 4096`）：`cleanup` 只清事件载荷、留下
`reclaimed=True` 这一个事实，超过上限后按 FIFO 淘汰最老的墓碑。
被淘汰的 `run_id` 会退回第 4 行（新建缓冲并等待）——这安全，因为那些 run 早就结束了，
调用方要么已经从 `/state` 拿到终态，要么本来就该 404。

> **为什么不让路由传一个 `assume_exists=True` 之类的开关？** 那会把"这条 run 现在是什么状态"
> 的判断推到每个调用点，而它们手里的信息（`RunRecord`）本来就够用——真要用，也该是
> "先查 RunManager"，而不是"给桥一个提示"。

#### 订阅者的五个阶段


| 阶段    | 谁在动                                                              | 状态                                            | 怎么进入下一阶段                  |
| ----- | ---------------------------------------------------------------- | --------------------------------------------- | ------------------------- |
| ① 注册  | `_stream()` 的第一行：注册 + 快照 + 解析游标，**同一个同步段**                    | `subscribers` 里多一个 `_Subscriber`            | 立即进入 ②                    |
| ② 重放  | 生成器同步 `yield` 窗口内的事件                                            | `cursor` 随每条推进                              | 重放完 → ③；解析出 gap → ⑤      |
| ③ 实时  | `wait_for(queue.get())`，队列由 `_fanout` 投递                        | `cursor` 在**每次 yield 之前**推进                 | END / `pending_end` / `lagged` / 断开 / `aclose` |
| ④ 终结  | 生成器 `yield` 最后一帧（END 或 gap）                                     | 不再接收新事件                                       | 生成器 `return`             |
| ⑤ 注销  | `finally`：`buffer.subscribers.discard(subscriber)`             | 从 fanout 的名单里消失                               | ——                        |


- **① 和 ② 之间不能有 `await`**（已在上文说明）：那正是"事件凭空消失"的窗口。
- **③ 里 `cursor` 必须在 `yield` 之前推进。** 客户端可能在 `yield` 挂起的任意时刻断开，
  而这张订阅的最后一个动作可能是发 gap——gap 要带上"我最后收到了哪条"。
- **⑤ 与 cleanup 的竞态是无害的**：`cleanup` 可能先把整条缓冲摘掉，此时 `discard` 作用在一个
  已经没人引用的 set 上；反过来，一个已经注销的订阅者再被 fanout 一次，也只是往没人读的队列里
  `put_nowait` 一下。

#### 端到端时序：一个 producer、一个缓冲、三个订阅者

```
producer(worker task)      桥 _RunBuffer                     A 创建端 SSE      B /join(慢)        C /wait
      │
      │ publish("values", ①) ─► id=1, append, fanout ───────► queue[1]         queue[1]         queue[1]
      │ publish("values", ②) ─► id=2, append, fanout ───────► queue[2]         (满 → lagged)     queue[2]
      │ publish_end()        ─► ended=True, fanout(END) ────► queue[END]       pending_end=true  queue[END]
      │
      │                        A: ① → ② → END → ⑤ 注销
      │                        B: 队列里还剩 ① → 消费到① → 看到 lagged → yield gap(requested=1, earliest=2) → 注销
      │                        C: ① → ② → END → ⑤ 注销（/wait 不看内容，只等 END）
```

注意三个订阅者**各自**的队列内容不同（B 因为慢被标记），但**看到的事件顺序一致**：
① 之后才是 ②，END 永远在最后。**慢，只会让它少看到一段（gap），不会让它看到乱序。**

#### 同一个 run 上的三个订阅者


| 订阅者                | 游标                          | 看到什么      | 慢下来会怎样                    | 怎么退出                     |
| ------------------ | --------------------------- | --------- | ------------------------- | ------------------------ |
| 创建端 SSE（§8.1）      | 无（从窗口最早一条）                  | 全量        | 队列满 → `lagged` → gap → 客户端 reload | END / gap / 客户端断开 / 响应被取消 |
| `/join`（§8.2）     | `Last-Event-ID`           | 从游标续传     | 同上                        | 同上（**断开不取消 run**）         |
| `/wait`（§8.3）     | 无（只要 END）                   | 同样收事件，但不渲染 | 同上；gap **不是终点**，从窗口尾续订     | 只在 END 退出                |


- **三条订阅互不影响**：各有各的队列，B 慢不会拖慢 A，也不会反压 producer（§7.3）。
- **订阅者数量没有硬上限**：`/join` 可以被很多人打开。这就是 fanout 必须"每条只 `put_nowait` 一次、
  绝不 `await`"的原因——它必须是 O(订阅者数) 的纯内存操作。
- `/wait` 也吃一套队列和缓冲，看起来浪费，但换来的是**同一个 `subscribe` 语义**（M3 §8.3
  那两条"不能 `await record.task`"的理由）。

### 7.5 M3 只做内存实现，但接口要能容纳 Redis

`supports_cross_process` 这个标志现在就要有。内存桥上，一个跑在别的进程里的 run
（`store_only`）是订不到的——路由必须 409，不能让客户端订一条空流死等。
M9 换 Redis 时这个标志变 True，路由逻辑一行不用改。

同样地，`redis` 包是可选依赖，**不要在** `stream_bridge/__init__.py` **里 eager import**。
工厂按 `backend` 懒加载分支（配置模型见下）。

#### 配置与工厂

```python
# agentkit/config/stream_config.py
class StreamBridgeConfig(BaseModel):
    backend: Literal["memory", "redis"] = "memory"
    queue_maxsize: int = Field(default=256, gt=0)                    # 每 run 保留的数据事件数
    heartbeat_interval_seconds: float = Field(default=15.0, ge=0)    # 0 = 关掉心跳（只给嵌入式用）


# agentkit/stream_bridge/provider.py
def create_stream_bridge(config: StreamBridgeConfig) -> StreamBridge:
    """按 backend 选实现。这个函数是 redis 依赖的唯一 import 点。"""
    if config.backend == "redis":                                    # M9
        from agentkit.stream_bridge.redis import RedisStreamBridge    # 懒加载：没装 redis 也不影响 M3
        return RedisStreamBridge(config)
    return MemoryStreamBridge(
        queue_maxsize=config.queue_maxsize,
        heartbeat_interval=config.heartbeat_interval_seconds,
    )
```


- **键名是 `backend`，不是 `type`。** M9 §3.6 的 `STARTUP_ONLY_FIELDS` 已经登记成
  `stream_bridge.backend`，两处不能各叫一个名字。M9 的 `stream_ttl_seconds` 是 Redis 专用字段，
  M3 不定义。
- **桥是进程级单例**，在 lifespan 里建一次，挂在 `RunServices.bridge`（§4.1）。`start_run` 和
  worker 拿到的都是这一个对象；测试可以自己 `MemoryStreamBridge(queue_maxsize=2)`。
- `backend` **登记为** `STARTUP_ONLY`（M0 §5.6）：换后端要重建单例，热重载改不了。
- **`redis` 是可选依赖**，所以工厂里是函数内 import，不是模块顶层 import；包被 import 时
  绝不能去连 Redis（§7.5）。

#### Redis 实现必须保持的语义（M9 的验收单）

M9 §3.6 说"M3 §7 的接口一字不改"。下面这张表就是那句话的可检验形式：


| 语义                     | 内存实现（M3）                        | Redis 实现（M9）必须一致                                        |
| ---------------------- | ------------------------------- | -------------------------------------------------------- |
| 四种控制对象                 | 两个哨兵 + `StreamGap`              | 同样四种；END 是一条特殊 entry，**不是**"暂时没有新条目"                      |
| `id` 格式              | `"1"`, `"2"`, …              | `<ms>-<seq>`；消费方不解析、只比较和原样回传（§7.2）                       |
| gap 判定                 | `cursor < start_offset` 或 `> latest` | 窗口下界用 `XRANGE ... COUNT 1` 读；低于它或高于上界同样是 gap，不做部分重放       |
| 心跳                     | `wait_for` 超时                 | **本地计时**。绝不能拿 `XREAD BLOCK` 的超时冒充心跳或 END                 |
| 不存在 / 未开始 / 已回收的 run    | 按需建缓冲；墓碑立即 END                 | 三态语义必须一致：key 不存在时 **等待**（`XREAD BLOCK` 天然如此），已回收（TTL 到期）时才允许立即 END |
| `cleanup(delay)`       | 延迟删缓冲                           | 延迟删 retained key；TTL 只是泄漏安全网，不是 run 超时（M9 §3.6）            |
| `supports_cross_process` | `False`                       | `True`；路由据此决定"409 还是订阅"（§7.5）                           |


两条协议测试（`test_redis_stream_bridge.py`，M9 §11）与内存实现**跑同一套用例**：
续传、gap、END 一定到达、心跳无 id。谁改坏了语义，两条一起红。

### 7.6 五个竞态和它们的答案

桥是并发的交汇点：worker 在发、N 个消费者在收、cleanup 在删。可能性不多，全部列出来：


| 竞态                     | 会发生什么                        | 答案                                                                       |
| ---------------------- | ---------------------------- | ------------------------------------------------------------------------ |
| 订阅的瞬间正好在裁剪             | 快照里的事件已经不在窗口里                | 注册与快照在同一个同步段里完成（§7.4），裁剪也在同步段里，两者不可能交错                                   |
| **订阅早于首帧**                 | 客户端在 worker 发出第一帧之前订阅，缓冲还不存在 | 缓冲**在订阅时按需创建**（`setdefault`）；只有墓碑（`reclaimed`）才立即 END（§7.4）              |
| END 与最后一批 publish 抢    | 有事件排在 END 后面                 | `ended` 一置位就拒收（丢弃 + 告警）；两个方法都在同步段里，顺序等于调用顺序（§7.3）                       |
| 订阅者慢                   | 投递队列满                        | 标记 `lagged`、不入队；它下一次唤醒发 gap 然后停。**桥的缓冲继续裁，run 继续跑**（§7.3）                |
| cleanup 时还有活订阅者        | 有人永远挂在队列上                    | cleanup 给每个活订阅者补一个 END 再删缓冲；生成器的 `finally` 里注销（缓冲已从字典摘掉，无害）               |
| 同一个游标重复订阅              | 两条流重放同样的事件                   | **允许**：`subscribe` 无状态、幂等；重复由 SDK 按 `id` 去重（M1 §6.2）                     |
| 关机时还有活订阅者              | 前端看到断流，而不是 `end`            | 排空先让 worker 走完 `finally` 发 END（§6.3 ①）；仍挂着的随 uvicorn 一起收                     |


- **只有最后两行是"允许/交给上层"，前四行的答案都是同一个手法：把不确定性收敛进同步段。**
  内存桥里没有任何跨 `await` 的中间状态，所以不需要锁，也不需要"重试一次"。
- **不要给内存桥加 `asyncio.Lock`。** 加了会让后来的人以为存在需要保护的 `await` 窗口，
  于是开始在锁里做 IO。真要加 `await`（M4 顺手写账本）时，先加锁再改——顺序反了就是 §5.1
  那条纪律的反例。

### 7.7 可测性：把时间和容量做成参数

桥的两个"难测"维度——**容量**和**时间**——都是构造参数，测试不需要 mock 任何东西：


| 参数                                   | 生产值  | 测试值      | 用来验证                                        |
| ------------------------------------ | ---- | -------- | ------------------------------------------- |
| `MemoryStreamBridge(queue_maxsize=…)` | 256  | `2`    | 裁剪、watermark 前进、gap（§12 验收里那条 `queue_maxsize=5` 的等价物） |
| `MemoryStreamBridge(heartbeat_interval=…)` | 15.0 | `0.01` | 空闲时产出 `HEARTBEAT_SENTINEL`；断开检查周期（§8.1）      |
| `subscribe(heartbeat_interval=0)`     | ——   | `0`    | 关掉心跳后不再产出哨兵；IM 通道直读的那条路径                    |
| `SUBSCRIBER_QUEUE_MAXSIZE`           | 64   | 不改常量     | 慢订阅者路径：先塞满队列（`put_nowait` 到底），再断言收到 gap   |


```python
async def test_gap_after_watermark_advances():
    bridge = MemoryStreamBridge(queue_maxsize=2, heartbeat_interval=0)
    for i in range(5):
        await bridge.publish("run-1", "values", {"i": i})          # id 1..5，窗口只剩 [4, 5]

    first = await anext(bridge.subscribe("run-1", last_event_id="1"))
    assert isinstance(first, StreamGap)
    assert first.requested_event_id == "1"
    assert (first.earliest_available_event_id, first.latest_available_event_id) == ("4", "5")
```


- **不要用 `sleep` 等心跳。** `heartbeat_interval=0.01` 配上
  `asyncio.wait_for(anext(stream), timeout=1)`——把"等多久"交给断言超时，
  而不是给测试加固定延迟（那会让整套用例慢一个数量级，而且照样偶发）。
- **`heartbeat_interval=0` 是测试的默认姿势**：绝大多数用例关心的是事件序列，
  不关心中间插了几个哨兵。
- §11 的 `test_stream_bridge.py` 覆盖上表每一行；`test_sse_translate.py` 覆盖 mode 名与编帧（§8）。

---





## 8. SSE 层（W2）

#### SSE 层的公开符号

这一层是**唯一认识 HTTP 的地方**：编帧、判断开、把流语义翻译成 HTTP 语义。它不 publish
（那是 worker）、不判终态（那是 store）、不重放（那是桥，§7）。


| 符号                                                                                             | 谁调                                | 做什么                                     | 见         |
| ---------------------------------------------------------------------------------------------- | --------------------------------- | --------------------------------------- | --------- |
| `format_sse(event, data, *, event_id=None) -> str`                                              | `sse_consumer`                    | 一帧 SSE 字节（字段顺序 + 结尾空行）                   | §8.1      |
| `sse_response(services, launch, request, *, apply_on_disconnect=True)`                          | 三个路由                              | `StreamingResponse` + 五个响应头             | §8.1      |
| `sse_consumer(bridge, record, request, run_mgr, *, apply_on_disconnect=True)`                    | `sse_response`                    | 订阅 → 编帧 → 断开判定 → 按策略取消                   | §8.1、§8.2 |
| `wait_for_run_completion(services, record, request, *, apply_on_disconnect=True) -> RunRecord`    | `POST /runs/wait` 的路由            | 等 END（不 `await task`）、gap 后换订阅、断开抛异常   | §8.3      |
| `ClientDisconnected`                                                                           | `wait_for_run_completion`         | 499 领域异常：断开时不返回半截快照                     | §8.3      |
| `stream_run` / `wait_run` / `join_run`                                                        | FastAPI                           | 参数绑定 + 调 `start_run` 或查 run + 返响应       | §8.2      |


三条纪律：

- **帧的字节是契约。** 字段顺序、结尾空行、`id:` 的有无，都被官方 SDK 的解码器依赖；
  改这些等于换 wire 格式（§3）。要"美化"帧，先改 SDK。
- **路由只做参数绑定和响应编码。** 准入、取消、终态判定都在 `services` 和 `RunManager` 里
  （§4.1）；路由里出现 `asyncio.create_task` 就是第二条执行栈。
- **生成器里没有"重试"。** 断开就是断开，gap 就是这条订阅的终结；唯一会换订阅继续等的地方是
  `/wait`（§8.3），因为它的目标不是内容，而是"什么时候结束"。

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
data: null
```

字段顺序 `event` → `data` → `id` → 空行，是 SDK 解码器的约定。

**END 的载荷是 `null`。** SDK 只认事件名、不读载荷，而 `sse_consumer` 调的是
`format_sse("end", None)`。示例里写成 `{}` 看着更整齐，但任何按字节比对帧的测试和日志解析
都会因此对不上——这类“看起来更对”的改动最难回头查。

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

#### `sse_response`：响应头与 `Content-Location`

```python
def sse_response(
    services: RunServices, launch: RunLaunch, request: Request, *, apply_on_disconnect: bool = True
) -> StreamingResponse:
    """创建端和观察端共用；差别只有 apply_on_disconnect（§8.2）。"""
    record = launch.record
    return StreamingResponse(
        sse_consumer(
            services.bridge, record, request, services.run_manager,
            apply_on_disconnect=apply_on_disconnect,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",                       # 不给 nginx 攒批的机会
            "Content-Location": f"/api/threads/{record.thread_id}/runs/{record.run_id}",
        },
    )
```


| 响应头                | 值                                        | 为什么                                                                   |
| ------------------ | ---------------------------------------- | --------------------------------------------------------------------- |
| `Content-Type`     | `text/event-stream`                     | 官方 SDK 和浏览器 `EventSource` 都认它；写成 `application/json` 会被 SDK 直接拒       |
| `Cache-Control`    | `no-cache`                              | 中间缓存把 SSE 响应存下来，下一个用户会拿到别人的流                                          |
| `Connection`       | `keep-alive`                            | 明确告诉代理这条连接要长开                                                         |
| `X-Accel-Buffering` | `no`                                  | nginx 专用。不加，nginx 会攒批下发，体感就是"模型不流式了"                                   |
| `Content-Location` | `/api/threads/{tid}/runs/{run_id}`      | `useStream` 的 `onCreated` **只从这里**拿 `run_id`（§3）；跨源时还要在 CORS 的 `Access-Control-Expose-Headers` 里放行 |


- **`sse_response` 只编响应，不做事。** 准入在 `start_run`（§4），断开处理在 `sse_consumer` 的
  `finally`（§8.2）。把两者混进这里，观察端就没法复用它。
- **`apply_on_disconnect` 默认** `True`：§4.1 的路由写的是三参数形式，只有 `/join` 显式传
  `False`（§8.2）。
- **`launch.reused` 在这里没有分支。** 复用幂等键拿到的记录同样可以订阅——它可能就是正在跑的那条；
  "不要重复挂 worker"是 `start_run` 的判断（§4.5），不是这里的。

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

#### `format_sse` 的参数


| 参数         | 类型              | 谁给                                     | 说明                                                          |
| ---------- | --------------- | -------------------------------------- | ----------------------------------------------------------- |
| `event`    | `str`         | `sse_consumer`（`end` / `gap`）或桥里的 `StreamEvent.event` | 事件名。业务名由 worker 定（§6.2 的 `sse_event_name`），`end` / `gap` 由这一层产生 |
| `data`     | `Any`         | `StreamEvent.data` 或 gap 载荷           | **原样** `json.dumps`；不接受"已经序列化好的字符串"——那会被再包一层引号                |
| `event_id` | `str \| None` | `entry.id`                            | `None` 和空串都不写 `id:` 行                                     |


返回值是一帧完整字节（含结尾空行），**不抛**（`default=str` 兜底，理由见上）。

- `if event_id:` **不能写成** `if event_id is not None:`**。** 心跳和 END 的 id 是空串（§7.2），
  写成 `is not None` 会产出 `id: ` 这样一行，而客户端会把空串记成 `Last-Event-ID`
  并在重连时带回来。调用点写 `event_id=entry.id or None`，把空串归一成 `None`——两层防御，
  因为这一行的 bug 只在"客户端恰好重连"时才现形。
- **结尾必须是空行**（`\n\n`）。少一个换行，SDK 的解码器会一直等下一帧的字节，表现为
  "服务端发了，前端不动"。
- **不要在这层做缩进或换行美化。** 帧长变了，日志、抓包和按字节比对的测试全都要跟着改，
  而收益只是好看。

#### 消费循环

```python
async def sse_consumer(
    bridge: StreamBridge,
    record: RunRecord,
    request: Request,
    run_mgr: RunManager,
    *,
    apply_on_disconnect: bool = True,
) -> AsyncIterator[str]:
    """一条订阅 → 一串 SSE 帧。不返回值：客户端拿到的就是全部结果。"""
    subscription = bridge.subscribe(record.run_id, last_event_id=request.headers.get("Last-Event-ID"))
    try:
        async for entry in subscription:
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
        try:
            await subscription.aclose()                 # ★ break / return 不会替你关异步生成器
        except Exception:
            logger.warning("Failed to close bridge subscription", exc_info=True)
        if apply_on_disconnect and record.status in (RunStatus.pending, RunStatus.running):
            if record.on_disconnect == DisconnectMode.cancel:
                await run_mgr.cancel(record.run_id)
```



#### `sse_consumer` 的五个参数


| 参数                      | 类型             | 谁给                                                          | 说明                                          |
| ----------------------- | -------------- | ----------------------------------------------------------- | ------------------------------------------- |
| `bridge`                | `StreamBridge` | `services.bridge`                                           | 只 `subscribe`，从不 publish——生产是 worker 的事（§6.2） |
| `record`                | `RunRecord`    | `RunLaunch.record`，或 `/join` 查出来的那条                        | 只读三样：`run_id`、`status`、`on_disconnect`     |
| `request`               | `Request`      | FastAPI                                                     | 只读两样：`Last-Event-ID` 头和 `is_disconnected()` |
| `run_mgr`               | `RunManager`   | `services.run_manager`                                      | 断开时按策略取消（§8.2）                              |
| `apply_on_disconnect`   | `bool`，默认 `True` | 路由（§8.2）                                                    | 观察端传 `False`                               |


#### 四种出口，每一种都要把订阅关掉


| 出口       | 谁触发                        | 发出什么                        | 之后做什么                                     |
| -------- | -------------------------- | --------------------------- | ----------------------------------------- |
| gap      | 游标已经不在保留窗口里（§7.4）          | 一帧 `event: gap`，带三个 id 和 `recovery` | 结束订阅；客户端 `reload_durable_state`          |
| end      | worker 的 `publish_end`（§6.2） | 一帧 `event: end`（`data: null`） | 正常结束；客户端拉 `/state` 对账                    |
| 客户端断开    | `is_disconnected()` 为真     | 无                           | 按 `apply_on_disconnect` + `on_disconnect` 决定要不要取消（§8.2） |
| 响应被取消    | uvicorn / 服务器关连接              | 无                           | 走同一个 `finally`，取消判定照做                    |


- **`aclose()` 必须显式调用。** `async for` 在 `break` / `return` 离开时**不会**替你关闭异步生成器；
  它要等 GC，而 GC 可能发生在别的 context 里。在那之前，桥那一侧的订阅者还挂在
  `_RunBuffer.subscribers` 上（§7.3），每次 `publish` 都白投一次。这和 §6.2 对图流的
  `aclose()` 是同一条纪律，只是对象从 LangGraph 的流换成了桥的订阅。
- **关闭失败只告警。** 走到这里时通常已经有一个异常在路上（客户端断开、服务器关闭），
  关闭的异常不能盖掉它。
- **`yield` 之后不做事。** 生成器每次 `yield` 都可能永久挂起（客户端不读），所以状态推进
  （`cursor`、`ended`）一律放在 `yield` 之前；把副作用写在 `yield` 之后，
  一个停止读取的客户端就能让这条订阅永远停在半路。

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

#### 三个入口的参数与断开语义


| 入口       | 路由                                        | `apply_on_disconnect` | 断开时发生什么                                                       | 返回     |
| -------- | ----------------------------------------- | --------------------- | ------------------------------------------------------------- | ------ |
| 创建端（流式）  | `POST /api/threads/{id}/runs/stream`      | `True`              | `sse_consumer` 的 `finally`：`on_disconnect=cancel` 就取消这个 run | SSE 流  |
| 创建端（阻塞）  | `POST /api/threads/{id}/runs/wait`        | `True`              | `wait_for_run_completion` 捕到断开：先取消，再抛 `ClientDisconnected`  | JSON 快照 |
| 观察端      | `GET /api/threads/{id}/runs/{rid}/join`    | `False`             | 只结束这条订阅，run 不受影响                                              | SSE 流  |


#### `/join` 的前置检查

```python
@router.get("/{thread_id}/runs/{run_id}/join")
async def join_run(thread_id: str, run_id: str, request: Request) -> StreamingResponse:
    services = get_run_services(request)
    caller = caller_from_request(request)
    record = await services.run_manager.get(run_id, user_id=caller.user_id)   # 属主在这里判
    if record is None or record.thread_id != thread_id:
        raise RunNotFound(f"Run {run_id} not found")
    if record.store_only and not services.bridge.supports_cross_process:
        raise RunNotLocal(f"Run {run_id} is owned by another process")
    return sse_response(services, RunLaunch(record=record, reused=True), request, apply_on_disconnect=False)
```


- **属主和存在性必须在这里判，不能交给桥。** 桥的 `subscribe` 会**按需建缓冲**（订阅早于首帧是常态），所以"没有缓冲"不再等价于"run 不存在"（§7.4）：
  那和"run 已经结束"长得一模一样，客户端分不出来。
- **`RunLaunch(reused=True)` 就是"不要挂 worker"的现成表达**（§4.1 的表）。观察端永远不会
  `create_task`；换一种写法，迟早有人顺手加上一条。
- **`store_only` 加内存桥 = 409。** 跑在别的进程里的 run 在这条进程里没有缓冲（§7.5）。
  M9 把 `supports_cross_process` 翻成 `True` 之后这一行自动放行，别的代码不用改。
- `RunNotFound` / `RunNotLocal` 是 `StartRunError` 的子类，加在 §4 那组异常的同一个模块里：
  观察端虽然不经过 `start_run`，也用同一套领域异常——客户端只需要处理一种错误体。

### 8.3 `/wait` 必须订阅 StreamBridge，不能 `await record.task`

诱惑很明显：`/wait` 要等 run 结束，而 `record.task` 就是那个 task，直接 await 不就行了？

不行，两个原因：

`on_disconnect` **语义只活在创建该 run 的 worker 内存里。** `await task` 拿不到客户端断开事件，
用户关掉连接后 run 还在烧钱。

**多进程下** `record.task` **可能是** `None`（`store_only`）。订桥则在 Redis 实现下天然跨进程。

`/wait` 的订阅者只关心 END，但有个细节：**遇到 gap 时不要当成断开，
而是从最新可用事件 id 续订继续等 END**。它不需要完整重放，只需要知道什么时候结束。

#### `wait_for_run_completion`：等 END，不等 task

> **它成立的前提是 §7.4 的"订阅早于首帧"修复。** `/wait` 拿到的是一个刚准入的 run：
> 如果 `subscribe` 把"还没有缓冲"当成"已经结束"，这里会立刻收到 END、
> 然后用**当前**的 checkpoint 拼一个 200 返回——正好踩中本节那条硬规则
> （"客户端断开导致的返回，绝不能把半截 checkpoint 当成功响应返回"的孪生情形：
> **订阅太早导致的返回，同样不能把半截快照当终态**）。

```python
class ClientDisconnected(StartRunError):          # 499：客户端在等待期间断开
    status_code, code = 499, "client_disconnected"


async def wait_for_run_completion(
    services: RunServices, record: RunRecord, request: Request, *, apply_on_disconnect: bool = True
) -> RunRecord:
    """阻塞到这条 run 的 END。返回从 store 重读的记录，不是内存里那份快照。"""
    cursor: str | None = None
    try:
        while True:
            ended = False
            subscription = services.bridge.subscribe(record.run_id, last_event_id=cursor)
            try:
                async for entry in subscription:
                    if await request.is_disconnected():           # ★ 心跳给了这个判断一个节拍
                        raise ClientDisconnected(f"Client disconnected while waiting for {record.run_id}")
                    if isinstance(entry, StreamGap):
                        cursor = entry.latest_available_event_id   # 从窗口尾部续订，不重放
                        break                                      # 换一条订阅继续等
                    if entry is HEARTBEAT_SENTINEL:
                        continue
                    if entry is END_SENTINEL:
                        ended = True
                        break
                    cursor = entry.id                               # 只推进游标，不解析载荷
            finally:
                try:
                    await subscription.aclose()                    # 同 §8.1：退出循环不会替你关
                except Exception:
                    logger.warning("Failed to close bridge subscription", exc_info=True)
            if ended:
                break
    except ClientDisconnected:
        if apply_on_disconnect and record.status in (RunStatus.pending, RunStatus.running):
            if record.on_disconnect == DisconnectMode.cancel:
                await services.run_manager.cancel(record.run_id)
        raise                                          # 路由不要用当前 checkpoint 拼一个 200
    final = await services.run_manager.get(record.run_id, user_id=record.user_id)
    return final or record
```


| 参数                    | 类型             | 谁给              | 说明                                              |
| --------------------- | -------------- | --------------- | ----------------------------------------------- |
| `services`            | `RunServices`  | 路由              | 只用 `bridge` 和 `run_manager` 两样                 |
| `record`              | `RunRecord`    | `RunLaunch.record` | 读 `run_id` / `user_id` / `on_disconnect` / `status` |
| `request`             | `Request`      | FastAPI         | 只用 `is_disconnected()`——`/wait` 唯一的断开来源        |
| `apply_on_disconnect` | `bool`，默认 `True` | 路由              | `/wait` 是创建端语义，恒为 `True`（§8.2）               |


| 出口    | 返回 / 抛                            | 之后                                    |
| ----- | --------------------------------- | ------------------------------------- |
| END 到达 | 从 store 重读的 `RunRecord`            | 路由再读一份 checkpoint 快照返回（§3 的 `/wait` 契约） |
| 客户端断开 | 抛 `ClientDisconnected`（499）       | 先按 `on_disconnect` 取消，再抛              |


- **从 store 重读，不返回内存里那份。** END 到达时 `_finalize` ② 已经写过 store（§6.2），
  两者内容一致；重读的意义是让 `/wait` 的响应等于**落盘的事实**，而不是"这个进程记得的事"——
  M9 里收到 END 的可能根本是另一个进程。
- **gap 不是失败，是换一条订阅。** 从 `latest_available_event_id` 续订（缓冲为空时它是 `None`，
  等价于"从头订"，§7.3）——`/wait` 不需要内容，只需要 END。
- **断开先取消再抛。** `/wait` 是创建端语义（§8.2）：客户端都走了，run 不该继续烧钱；
  抛出去是为了让路由**不要**用当前 checkpoint 拼一个 200。这条是本节唯一的"硬规则"的技术落点。

还有一条硬规则：**客户端断开导致的返回，绝不能把半截 checkpoint 当成功响应返回。**

### 8.4 可测性：不用浏览器也能测 SSE


| 注入点      | 换成什么                                  | 用来验证                        | 用例                                 |
| -------- | ------------------------------------- | --------------------------- | ---------------------------------- |
| `bridge` | `MemoryStreamBridge(heartbeat_interval=0)` | 帧的顺序、gap 载荷、END 一定是最后一帧     | `test_sse_format.py`               |
| `request` | 只实现 `headers` 和 `is_disconnected()` 的假对象 | 断开路径、`Last-Event-ID` 续传    | `test_disconnect.py`               |
| `run_mgr` | 记录调用序列的假 RunManager                   | "断开才取消"和"正常结束不取消"两条都要覆盖     | `test_disconnect.py`               |
| 端到端      | `httpx.ASGITransport` + ASGI app      | 响应头、`Content-Location`、CORS 暴露 | `test_sse_format.py`               |


```python
class FakeRequest:
    """只实现 sse_consumer 用到的两样东西：请求头和断开询问。"""

    def __init__(self, *, last_event_id: str | None = None, disconnect_at: int | None = None) -> None:
        self.headers = {"Last-Event-ID": last_event_id} if last_event_id else {}
        self._disconnect_at, self.polls = disconnect_at, 0

    async def is_disconnected(self) -> bool:
        self.polls += 1
        return self._disconnect_at is not None and self.polls >= self._disconnect_at


async def test_frames_are_byte_exact_and_end_is_last():
    bridge = MemoryStreamBridge(heartbeat_interval=0)
    await bridge.publish("run-1", "values", {"i": 1})
    await bridge.publish_end("run-1")

    frames = [frame async for frame in sse_consumer(bridge, _record("run-1"), FakeRequest(), FakeRunManager())]

    assert frames == [
        'event: values\ndata: {"i": 1}\nid: 1\n\n',
        "event: end\ndata: null\n\n",
    ]


async def test_only_the_creator_cancels_on_disconnect():
    run_mgr = FakeRunManager()
    _ = [f async for f in sse_consumer(bridge, _record("run-1"), FakeRequest(disconnect_at=1), run_mgr)]
    assert run_mgr.cancelled == ["run-1"]           # 创建端：apply_on_disconnect=True

    run_mgr.cancelled.clear()
    _ = [f async for f in sse_consumer(bridge, _record("run-1"), FakeRequest(disconnect_at=1), run_mgr,
                                       apply_on_disconnect=False)]
    assert run_mgr.cancelled == []                  # 观察端：断开不取消（§8.2）
```


- **不要起真服务器。** `sse_consumer` 是纯异步生成器，直接 `[f async for f in ...]` 就能断言帧字节；
  只有要验证响应头时才用 `httpx.ASGITransport` 打 ASGI app。`uvicorn` + 真 socket 会把测试
  变成时间竞态（端口、启动、超时），而不是在测协议。
- **帧断言用整帧相等，不用子串。** 字段顺序、结尾空行、`id:` 的有无都是契约（§8.1）；
  子串断言在这些地方错位时照样通过。
- **心跳用构造参数 `heartbeat_interval=0.01`**，不要改全局配置、也不要 `sleep`：
  "等多久"交给 `asyncio.wait_for` 的断言超时。
- §11 的 `test_sse_format.py` / `test_disconnect.py` / `test_publish_end_always.py` 覆盖上表每一行；
  `/wait` 的续订路径用一个"裁到只剩尾部"的缓冲来构造 gap。

---



## 9. 取消（W3）

#### 取消的公开符号


| 符号                                                                     | 谁调                                                              | 做什么                                | 见         |
| ---------------------------------------------------------------------- | --------------------------------------------------------------- | ---------------------------------- | --------- |
| `RunManager.cancel(run_id, *, action="interrupt") -> CancelOutcome`      | cancel 路由、`sse_consumer` 的 `finally`、`wait_for_run_completion` | 内存里决出胜负，再写 store                   | §5.9      |
| `CancelOutcome`                                                        | 路由查表                                                            | 六个枚举值；M3 只产生前四个                   | §9.2      |
| `RunCancelRequest`                                                     | FastAPI 绑定 `POST .../cancel` 的 body                             | `action` + `wait` 两个字段           | §9.1      |
| `cancel_run` 路由                                                       | 客户端                                                             | 能力门 → 属主 → `cancel()` → 查表编响应    | §9.1      |
| `_abort_locked(record, *, action)`                                      | `cancel()`、`create_or_reject(supersede_active=True)`            | 内存侧的取消动作（§5.5）                     | §5.5、§9.1 |
| `RunAborted(action)`                                                    | worker 在每帧检查时抛                                                  | 把 abort 变成控制流                     | §6.2      |
| `request_cancel` / `finalize_if_not_cancelled`                           | M9                                                              | 同一套语义在 store 上的 CAS                | §9.3      |


三条纪律：

- **取消是协作式的。** 唯一的入口是置 `abort_event`；`task.cancel()` 只是 manager 在锁内对
  running 记录补的一下（§5.9、§5.5），路由永远不碰 task。
- **HTTP 映射只在路由层做。** `RunManager` 返回枚举，路由查表变状态码和响应体；反过来会让
  harness 层认识 HTTP（§1）。
- **"是不是回滚"只有一个来源：**`record.abort_action`**。** worker 在 `_finalize` ① 里读它（§6.2）；
  别的地方再判一次，就会有两份真相。

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



#### 取消路由：参数与编响应

```python
# app/gateway/run_models.py
class RunCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["interrupt", "rollback"] = "interrupt"
    wait: bool = False         # 等到 worker 收尾完成再返回（对齐官方 SDK 的 runs.cancel）


# app/gateway/routers/thread_runs.py
_OUTCOME_ERRORS: dict[CancelOutcome, type[StartRunError]] = {
    CancelOutcome.unknown: RunNotFound,                 # 404
    CancelOutcome.not_active_locally: RunNotLocal,      # 409：这个 run 在别的进程手里
    CancelOutcome.not_cancellable: RunNotCancellable,   # 409：已经是终态
}


@router.post("/{thread_id}/runs/{run_id}/cancel")
async def cancel_run(
    thread_id: str, run_id: str, request: Request, body: RunCancelRequest | None = None,
) -> dict[str, Any]:
    services = get_run_services(request)
    caller = caller_from_request(request)
    req = body or RunCancelRequest()
    _require_cancel_capability(caller, req.action)      # 403：和准入第 1 步同一道门（§4.3）

    record = await services.run_manager.get(run_id, user_id=caller.user_id)   # 属主先判（§5.1）
    if record is None or record.thread_id != thread_id:
        raise RunNotFound(f"Run {run_id} not found")

    outcome = await services.run_manager.cancel(run_id, action=req.action)
    if (error := _OUTCOME_ERRORS.get(outcome)) is not None:
        raise error(f"Run {run_id}: {outcome.value}")
    if req.wait:                                        # 等收尾：复用 §8.3 的同一条路径
        final = await wait_for_run_completion(services, record, request, apply_on_disconnect=False)
        return {"outcome": outcome.value, "run": run_to_wire(final)}
    return {"outcome": outcome.value}
```


| 参数 / 字段                    | 类型                                       | 谁给                   | 说明                                                                     |
| -------------------------- | ---------------------------------------- | -------------------- | ---------------------------------------------------------------------- |
| `body`                     | `RunCancelRequest \| None`               | FastAPI；缺省当 `{}`    | 空 body 和 `{}` 等价：默认 interrupt、不等收尾                                     |
| `action`                   | `Literal["interrupt", "rollback"]`，默认 `interrupt` | body                 | 只有两个值；`extra="forbid"` 让拼错的值 422，而不是静默按 interrupt 处理                   |
| `wait`                     | `bool`，默认 `False`                     | body                 | `True` 时用 §8.3 的 `wait_for_run_completion` 等到 END                |
| `thread_id` / `run_id`    | `str`                                   | 路径参数                 | `thread_id` 用来核对归属，防越权：`RunStore.get` 的属主过滤只管 `user_id`（§5.1）      |


- **能力门在两处都要过。** 准入看 `multitask_strategy`（interrupt / rollback 会停掉别人的 run），
  取消看 `action`（rollback 比 interrupt 多改 checkpoint）——两条都要求 `runs:cancel`。
  只挡住 cancel 路由是不够的。
- **`wait=True` 不另写等待循环**：复用 `wait_for_run_completion`，但传
  `apply_on_disconnect=False`——这次的断开不该再触发一次取消（已经取消过了）。
- `run_to_wire` **与** `GET /runs/{rid}` **共用一份**（§3）：别为这个响应写第二个序列化函数。

#### 取消在内存里改了什么

`_abort_locked`（§5.5）是唯一的落点，它按顺序改这几个字段：


| 字段                     | 变成                                       | 谁读它                                        |
| ---------------------- | ---------------------------------------- | ------------------------------------------ |
| `record.status`        | `interrupted`（`_transition_locked` 成功才继续） | 准入（离开活跃集）、侧栏                              |
| `record.abort_action`  | `"interrupt"` / `"rollback"`            | worker 的 `_finalize` ①（§6.2）              |
| `record.abort_event`   | 置位                                       | worker 的五个检查点（§6.2）                        |
| `record.finalizing`    | 有 task 且未结束时 `True`                     | `wait_for_prior_finalizing`（§5.6）         |
| `record.task`          | 仅 `running` 且**不在收尾**时 `task.cancel()`  | 打断 worker 当前那次 `await`（§4.6 说的唯一例外）        |


**收尾期间不要再** `task.cancel()`**——这是对 §5.5 那段的补丁，只有一行：**

```python
def _abort_locked(record: RunRecord, *, action: str) -> None:
    was_running = record.status is RunStatus.running
    if not _transition_locked(record, RunStatus.interrupted):
        return
    record.abort_action = action
    record.abort_event.set()          # ★ 这一位必须置：收尾的 ① 靠它决定回不回滚（§6.2）
    if record.finalizing:
        return                        # ★ 已经在收尾：让它把 ②③ 跑完，别再插一次取消
    record.finalizing = record.task is not None and not record.task.done()
    if was_running and record.finalizing:
        record.task.cancel()
```

- 不补这一行的后果很具体：取消落在 `_finalize` ① 或 ② 的 `await` 上时，`CancelledError` 会穿过
  `_best_effort`（它只接 `Exception`，§6.2）一路冒到内层 `finally`——**END 照发，但 ② 没跑完，
  store 里的行留在活跃状态**。M3 的内存 store 下，这条 thread 会一直 409（准入看见一行活跃的 run），
  直到进程重启。
- 取消的语义已经由 `status=interrupted` + `abort_event` 表达完了；收尾是 worker 自己的事，
  它会写终态、发 END、清 `finalizing`。
- **关机强制阶段是例外**（§5.9 的超时分支）：那时进程要退出，不需要 ②，直接 `task.cancel()`。

#### 取消到达时，run 在哪一格

同一个 `cancel()` 落在状态机的不同格子里，走的是完全不同的路径——这张表就是协作式协议的全貌：


| 取消到达时                      | 谁先发现                                          | 多久生效                    | 终态            | 回滚                      |
| -------------------------- | --------------------------------------------- | ----------------------- | ------------- | ----------------------- |
| pending（还没组图）              | `try_start` 的两次检查（§5.6）                      | 立即，不组图                  | `interrupted` | 不需要（没写过 checkpoint）      |
| running，正在出帧              | `_stream_graph` 的下一帧                        | 一帧之内                    | `interrupted` | 按 `abort_action`        |
| running，卡在一次模型 / 工具调用      | `task.cancel()` 打断 `await`                 | 取决于被调用方是否响应取消           | `interrupted` | 按 `abort_action`        |
| running，已经进 `finally` 收尾  | `_abort_locked` 仍然赢（status 还是 running）      | 立即，但**不再** `task.cancel()` | `interrupted` | 若 ① 还没跑完，仍按它回滚           |
| 已是终态                       | `cancel()` 的提前返回                            | ——                      | 不变            | 不适用                     |


- **第二、三行合起来才是完整的"运行中取消"。** 只靠每帧检查，模型 90 秒不吐 token 的取消不生效；
  只靠 `task.cancel()`，一个处处 `except CancelledError: pass` 的工具就能把取消吞掉——
  这时 `abort_event` 是兜底，它在下一帧（如果有）生效（§6.2 的五个检查点）。
- **第五行的"不变"是承诺的一部分**：取消已经结束的 run 返回 `not_cancellable`，
  而且**不会**动 store 里的行（§9.3）。

### 9.2 `CancelOutcome` → HTTP 映射由路由查表，不要自己猜


| 结果                   | 含义                            | HTTP  |
| -------------------- | ----------------------------- | ----- |
| `cancelled`          | 本进程执行了取消                      | 200   |
| `not_active_locally` | 记录不在本进程内存里                    | 409   |
| `not_cancellable`    | 已经是终态                         | 409   |
| `unknown`            | 找不到或 store 失败                 | 404   |
| `requested`          | （M9）非 owner，已写 durable cancel | 202   |
| `taken_over`         | （M9）租约过期，本进程 claim            | 按接管处理 |


M3 只会产生前四种，但**枚举现在就定全**。后两种是多进程才有的结果，
等到 M9 再加枚举值意味着所有 `match` 分支都要回头补。

#### 响应体与错误体


| outcome                | HTTP | 响应体                                                       | 客户端该做什么                          |
| ---------------------- | ---- | --------------------------------------------------------- | -------------------------------- |
| `cancelled`            | 200  | `{"outcome": "cancelled"}`；`wait=True` 时再带 `"run"`       | 停止读流；SSE 会在 END 处自己结束（§6.2）      |
| `not_cancellable`     | 409  | `{"detail", "code": "run_not_cancellable"}`               | run 已经结束：拉 `/state` 或 `/join` 拿结果 |
| `not_active_locally`  | 409  | `{"detail", "code": "run_not_local"}`                     | 本进程不是 owner（§5.9 的第四种结果）         |
| `unknown`             | 404  | `{"detail", "code": "run_not_found"}`                     | run 不存在，或 store 持续失败              |
| `requested`（M9）      | 202  | `{"outcome": "requested"}`                                | 取消已落 durable，等 owner 心跳执行        |
| `taken_over`（M9）     | 200  | `{"outcome": "taken_over", "run": …}`                     | 按接管结果处理                          |


- **`not_cancellable` 和 `not_active_locally` 都是 409，靠 `code` 区分。** 两者对客户端的意思完全不同
  （"已经结束" vs "不在这个实例"），但 HTTP 层的冲突语义是同一个；用 `code` 而不是两个状态码，
  是因为 M9 还要把 `requested` / `taken_over` 编成 200 / 202——状态码只表意粗粒度结果。
  上面那张表里的 `4xx` 现在定死成 **409**：客户端要按 `code` 分支，状态码不能留活口。
- **`unknown` 把"找不到"和"store 抖了一下"合成一个结果**（§5.9 的实现），映射成 404。
  这是刻意的：到这一层时 store 已经经历过自己的重试与 backoff（M9 §3.2），持续失败时
  "重试一次取消"和"run 不存在"的处理其实一样——客户端刷新页面重来。
- **取消成功不等于已经回滚完。** 200 只表示"取消被接受了"（内存胜者已定，§9.3）；
  回滚在 worker 的 `_finalize` ① 里做，要等它就得传 `wait=True`。
- 三个错误体走 §4 的 `StartRunError` 处理器，形状统一成 `{detail, code}`；
  `RunNotCancellable` 和 §8 的 `RunNotFound` / `RunNotLocal` 放在同一个模块里。

### 9.3 取消和完成会抢同一行

`request_cancel` 和 `finalize_if_not_cancelled` 都是对活跃行的 CAS：
**已经被接受的取消，不能被后到的 success 覆盖掉。**

`update_status` 要带 `status IN ('pending','running')` 守卫——
已经是终态的行，后到的写入进不去。

#### 两种交错顺序，逐个推一遍

`cancel()` 和 `complete()` 都是"先在管理器锁内改内存，再到锁外写 store"：

```python
# complete（worker 收尾，§5.6）
async with self._lock:
    record = self._runs[run_id]
    won = _transition_locked(record, status, error=error)      # ← 胜负在这里决出
    final_status, final_error = record.status, record.error     # 输了就是赢家的终态
    ...
await self._store.update_run_completion(run_id, status=final_status, error=final_error, **totals)

# cancel（路由 / 断开，§5.9）
async with self._lock:
    if record.status not in _ACTIVE:
        return CancelOutcome.not_cancellable                    # ← 提前返回，连 store 都不写
    _abort_locked(record, action=action)
await self._store.update_status(run_id, RunStatus.interrupted)  # 守卫对终态行是空操作
```


| 顺序              | 内存终态                | store 最终行                                                                      | `cancel()` 返回  |
| --------------- | ------------------- | ------------------------------------------------------------------------------ | -------------- |
| cancel 先拿到锁     | `interrupted`       | `interrupted`（cancel 写的；随后 `complete` 用赢家终态再写一次——同一个终态允许重复写，§5.1）             | `cancelled`    |
| complete 先拿到锁   | `success` / `error` | `success` / `error`（cancel 根本没写：它在锁内就返回了）                                     | `not_cancellable` |


- **两张表都只看锁内那一步。** 锁外的 store 写入带的永远是内存胜者的终态：`complete` 输了就写赢家的状态，
  `cancel` 输了根本不写。所以**两次写入谁先落盘都不影响结果**——这就是 §9.3 那句话在单进程里的实现。
- **`update_status` 的** `WHERE status IN ('pending','running')` **守卫是第二道防线。** 单进程里它挡掉的
  只是"已经终态的行被再写一次"；M9 跨进程时，别人的 `request_cancel` 与 owner 的
  `finalize_if_not_cancelled` 会真的并发打在同一行上（§5.1 的 M9 预留签名）。
- **token 也要写。** 被取消的 run 同样烧了钱：`complete` 用赢家终态写的时候带着 `totals`（§5.6、§6.5）。
  取消路径只写终态，不碰 token——那是 worker 收尾的事。

### 9.4 七条路径，同一个 `cancel()`


| 路径          | 谁调                                                   | `action`                    | 之后发生什么                          |
| ----------- | ---------------------------------------------------- | --------------------------- | ------------------------------- |
| 客户端点停止      | `POST .../cancel` 路由                                | body 里的 `action`（默认 `interrupt`） | 200 + outcome；`wait=True` 时等到 END |
| 创建端流断开      | `sse_consumer` 的 `finally`（`on_disconnect=cancel`） | 默认 `interrupt`              | 没有响应可发；run 变 `interrupted`（§8.2） |
| `/wait` 断开 | `wait_for_run_completion` 的 `except`               | 默认 `interrupt`              | 先取消，再抛 `ClientDisconnected`（§8.3） |
| 观察端断开       | ——                                                   | ——                          | **不取消**：`apply_on_disconnect=False`（§8.2） |
| 被新 run 顶替   | `create_or_reject(supersede_active=True)` → `_abort_locked` | 新 run 的 `multitask_strategy` | 旧 run 收尾完，新 run 才进图（§5.5、§5.6）   |
| 关机排空        | `RunManager.shutdown`                                | 恒 `interrupt`               | 超时后才强制 `task.cancel()`（§5.9、§6.3） |
| M9 跨进程      | store 的 `request_cancel`                             | 写进行                         | owner 心跳读到后执行（M9 §3.4）           |


- **断开一律用默认的** `interrupt`**。** 前端只有一个"停止"按钮，没有"停止并回滚"的语义；
  给断开路径加一个 action 参数，等于凭空造一个没人会用的开关。
- **关机恒** `interrupt`**，不回滚。** 回滚要写 checkpoint，而关机窗口里最不该做的就是发起新的写；
  而且回滚失败本来也只是"没回滚"，不影响关闭（§6.4 的失败模式表）。
- **顶替的 action 来自新 run 的策略，不来自被顶替的 run。** `multitask_strategy=rollback` 表达的是
  新 run 的意愿："从干净的 checkpoint 开始"（§5.5）。

### 9.5 可测性：取消的确定性从哪来

取消是竞态最多的功能。测试必须**不依赖 sleep、也不依赖"大概来得及"**：


| 要测的东西           | 怎么构造                                         | 断言                                                   |
| --------------- | -------------------------------------------- | ---------------------------------------------------- |
| pending 阶段取消不组图 | 准入后立刻 `cancel()`，中间不 await                  | 假工厂调用次数为 0；桥里只有一帧 `end`（§11 的 `test_cancel.py`）    |
| 长调用期间取消生效       | 假图的 `astream` 挂在 `asyncio.Event` 上           | `abort_event` 和 `task.cancel()` 两条路径**分开**两条用例    |
| 每帧检查生效          | 假图先吐一帧、再挂住                                   | 取消后不再有新帧进桥（`_raise_if_aborted` 在 publish 之前，§6.2）   |
| 取消赢过完成          | 交错调用 `cancel()` 与 `complete()`              | store 终态 = 内存胜者；**token 在输家一侧也写进去**（§9.3）             |
| 取消不被后到的 success 覆盖 | 先 `cancel()`，再手工调 `complete(success)`        | 第二次写入返回 `False`，store 仍是 `interrupted`             |
| 收尾期间取消          | 在 `_finalize` ① 里挂住（假的 `restore_rollback_point`） | END 仍然到达；store 终态是 `interrupted`（§9.1 的收尾补丁）       |


```python
async def test_cancel_wins_over_a_later_success():
    bridge, run_mgr, record = await _start_run(script=[("values", {"i": 1})])
    assert await run_mgr.cancel(record.run_id) is CancelOutcome.cancelled

    await record.task            # 测试可以等 worker；worker 自己不许（§6.1 的"不准做的事"）
    assert record.status is RunStatus.interrupted
    assert bridge.events[-1] == (record.run_id, "end", None)     # END 一定在最后（§6.2）
    assert (await run_mgr.get(record.run_id, user_id=None)).status is RunStatus.interrupted
```


- **用 `asyncio.Event` 造"卡住"，不要 `sleep`。** `block = asyncio.Event()` 让假图停在一个确定的点上；
  `await asyncio.wait_for(record.task, timeout=1)` 给收尾一个上限。"等 0.1 秒应该够了"的测试
  会在 CI 上偶发，而且看不出到底走的是哪条路径。
- **`await record.task` 只允许出现在测试里。** worker 自己等自己会自死锁（§6.1），
  测试里它是"等这条 run 收尾完"的最短写法。
- **`abort_event` 与 `task.cancel()` 必须分两条用例。** 它们覆盖的时间段不同（§6.2 的五个检查点）；
  合成一条时，一条坏了另一条会替它兜住，测试照样绿。

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


| 文件                                 | 覆盖                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_run_manager.py`              | 状态机全部合法/非法迁移：从终态出发返回 `False`，活跃状态下的非法迁移抛 `InvalidRunTransition`；`record.status =` 只出现在 `_transition_locked`（grep）；`create_or_reject` 并发只有一个赢；预留不能被 interrupt 顶替；pending 被顶替或取消时 task 没有被 `cancel()`；`try_start` 的双重 abort 检查；store 抛异常时 `try_start` 抛 `RunStartupError` 而不是返回 cancelled；`get` 内存命中也按 `user_id` 过滤；cancel 与 complete 任意交错，store 终态总等于内存里的胜者，token 在输家一侧也写进去；`schedule_cleanup` 不摘活跃记录、不带请求的 ContextVar；`_closing` 后新准入 409、幂等命中仍返回旧记录；`has_later_run` 只看同 thread 的 run 行 |
| `test_thread_operation_lock.py`    | 同 thread 并发两次 `POST /runs` → 第二次 409；非 run 操作不出现在列表 API                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `test_publish_end_always.py`       | **注入各种失败路径**（模型异常、组图失败、启动栅栏拒绝、取消），断言 END 一定到达；**额外注入** `_finalize` **本身抛异常**，断言嵌套 finally 仍然发出 END                                                                                                                                                                                                                                                                                                                                                                                      |
| `test_run_store_contract.py`       | 准入冲突返回 `False` 而不是抛 `IntegrityError`；`start_run` 对非 pending 行返回 `False`；`update_status` 不能改写终态行；`update_run_completion` 拒绝替换不同的终态、同一终态可以重复写；`supersede_active` 在同一原子操作里标旧行、插新行，遇到非 run 预留返回 `False`；同一幂等键第二次插入返回 `False` 且 `get_by_idempotency_key` 取得回赢家；`update_run_progress` 对非 running 行静默；`delete_thread_operation` 对 run 行抛 `ValueError`；读方法返回副本。内存实现和 M4 的 SQL 实现跑同一套                                                                                                            |
| `test_stream_bridge.py`            | 有界裁剪；gap 后订阅停止；`StreamGap` **三个 id 的取值正确**（含缓冲为空时全 `None`）；`Last-Event-ID` 续传；未知 id 的处理；心跳；**订阅早于首帧**（订阅后第一帧到达必须被收到，断言不是"立刻 end"）；**`cleanup` 之后订阅立即拿到 END**（墓碑）；墓碑超过 `TOMBSTONE_LIMIT` 后订阅退回"等待"而不是报错；**END 不被队列满吞掉**（`pending_end` 优先于 `lagged`）；同一批事件的**到达顺序与 `id` 顺序一致**；多订阅者互不影响（一个 lagged，其余照常收到全部事件）；`pending_end` 与 gap 的判定顺序                                                                                                                                                                                                                                                                                                                                                                                              |
| `test_sse_translate.py`            | 公开 `messages-tuple` 映射到内部 `messages`；不支持的 mode → 422 而不是降级成 `values`；`format_sse` 遇到 `datetime`/`UUID` 不抛异常                                                                                                                                                                                                                                                                                                                                                                             |
| `test_cancel.py`                   | pending 取消不组图；running 取消在一轮内生效；取消不被后到的 success 覆盖                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `test_disconnect.py`               | 创建端断开触发取消；**join 断开不触发**；`/wait` 断开不返回半截 checkpoint；**模型长时间无输出时，断开在一个心跳间隔内被发现**；正常结束后客户端断开**不**触发 `cancel()`                                                                                                                                                                                                                                                                                                                                                                            |
| `test_idempotency.py`              | 同 key 复用同一 run；`idempotency_reused` 时不挂第二个 worker；不同 input 同 key → 409；**重叠重试把共享记录的** `idempotency_reused` **翻成 True 时，原始请求仍是** `reused=False` **并挂上 worker**；HTTP 和 internal 用同一个原始 key 不串号                                                                                                                                                                                                                                                                                            |
| `test_start_run_no_await.py`       | **AST 断言**：`create_or_reject` 和 `create_task` 之间没有 `await`                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `test_start_run_validation.py`     | 第 1–12 步逐步注入失败：run store、bridge、thread_store 都没有新写入；每步的状态码和 §4.2 的表一致；`StartRunError` 编成 `{detail, code}`；外部调用方发 `system` 消息、写 `sandbox` 通道、带 `command` 都是 422；`recursion_limit` 超上限被钳制、`true` 被拒                                                                                                                                                                                                                                                                                       |
| `test_run_context_whitelist.py`    | 三个袋子同一套规则、`context` 优先；未知键 422，`__` / 身份 / 坐标 / `user_role` 静默丢；`secrets` 不进 `configurable`、不进落库的 kwargs；`non_interactive` / `channel_user_id` 只认 internal 的顶层 `context`；`is_internal=False` 也写进 context                                                                                                                                                                                                                                                                                |
| `test_start_run_attach_failure.py` | `create_task` 抛异常：记录变 `error`、END 到达、协程被 close、同一 thread 能再开新 run                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `test_run_after_metadata.py`       | thread_store 挂住 → 5 秒后照常开跑；pending 阶段 abort 不等元数据；严格模式下 thread 已删 → `error` + END、不组图；元数据阶段被 cancel → 仍然收到 END，task 仍以 cancelled 结束                                                                                                                                                                                                                                                                                                                                                     |
| `test_sse_format.py`               | 帧字段顺序；`Content-Location`；heartbeat 无 id                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `test_run_agent.py`                | `try_start` 拒绝仍发 END、不组图、不发 metadata；`RunAborted(interrupt/rollback)` 终态 `interrupted`，rollback 才调 `restore`；整轮 `TimeoutError` 且 `deadline.expired()` → `timeout`，图内 `TimeoutError` → `error`；`CancelledError` 先写 `interrupted` 再 raise；`_finalize` 抛异常仍发 END；`thread_store.set_status("running")` 抛 → `error` + END；AST：`run_agent` 恰 4 个参数、模块不 import FastAPI                                                                                                                         |
| `test_stream_graph.py`             | `astream` 的 `stream_mode` 恒为 list、`subgraphs=False`；先 abort 再 publish（已取消的帧不进桥）；离开循环必 `aclose`（异常路径也是）；`unpack` 三元/二元/裸 chunk；`sse_event_name` 空 ns 是裸 mode、有 ns 是 `mode                                                                                                                                                                                                                                                                                                                |
| `test_stream_modes.py`             | `None`/`[]` → `["values"]`；`messages-tuple` → 内部 `messages`、去重保序；`events` / 内部名 `messages` / 非 str → `UnsupportedStreamModeError`；不静默降级                                                                                                                                                                                                                                                                                                                                                 |
| `test_rollback_point.py`           | 空 thread → `checkpoint_config is None`，还原走 `adelete_thread`；有 head 时 `aupdate_state` 带 `Overwrite` 且 `as_node="rollback_restore"`；`channel_values` 缺失返回 `None`；`messages` 缺失记 `()`；捕获抛异常返回 `None` 且之后 rollback 退化；`pending_writes` 按 `task_id` 分组重放；mutation 图只有一个空节点                                                                                                                                                                                                                   |
| `test_usage_collector.py`          | 同一 LangChain `run_id` 两次 `on_llm_end` 只计一次；先空 usage 再回放只计回放、`_calls==1`；始终为空则 `_calls==0`；`llm_output.token_usage` 的 `prompt_tokens` 回退；未知模型进 `"unknown"`；各桶之和等于总数；`cache_read` 不从 `input` 扣除；`run_inline is True`                                                                                                                                                                                                                                                                      |
| `test_shutdown_drain.py`           | `shutdown` 置 `_closing` 后新准入 409、幂等重试仍返回旧记录；pending 只置 abort 不 `task.cancel()`；强制阶段才 cancel 未退出的 task；checkpointer 在 worker 排空之后才关；`cancel_detached` 在关连接池之前                                                                                                                                                                                                                                                                                                                            |




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