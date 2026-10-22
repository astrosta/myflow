# 路由层详细设计（Router Layer）

> 配套文档：[分阶段开发方案](2026-09-16-greenfield-agent-service-roadmap.md) ·
> [M0 地基](2026-09-16-m0-foundation-design.md) · [M1 最小 Agent](2026-09-16-m1-minimal-agent-design.md) ·
> [M2 工具与沙箱](2026-09-16-m2-tools-sandbox-design.md) · [M3 Gateway 与流式](2026-09-16-m3-gateway-streaming-design.md) ·
> [M4 持久化](2026-09-16-m4-persistence-design.md) · [M9 生产化](2026-09-16-m9-production-design.md)
>
> 本文覆盖 `app/gateway/**`：**路由、依赖注入、请求/响应模型、错误映射、分页、幂等、中间件接线**。
> 它不重复 M3 的执行栈（`start_run` / `RunManager` / worker）和 SSE 编帧，也不重复 M4 的表结构——
> 那些在各自文档里已经定死。本文回答的是另一个问题：**HTTP 边界上究竟长什么样，谁来保证它一直长这样。**
>
> **读代码块的约定。** 每个代码块的第一行注释是所属文件（仓库内路径，greenfield 布局，不是 DeerFlow 现状的 `backend/app/...`）。
> 参数表里的「所属文件」列与之相同。执行顺序以 M3 §4 为准，表结构以 M4 为准，**HTTP 形状、状态码、`code`、handler 步骤以本文为准**。

---

## 1. 为什么单独一份路由层文档

M3 §3 定义了六条路由，§4 定义了 `start_run`，§8 定义了 SSE 编帧。但路由层还有一批**横向关注点**
没有任何一份文档拥有它们：请求/响应模型的字段约定、错误体形状、分页游标、幂等头、能力门、
路由注册顺序、`request.state` 的契约、以及"新加一条路由时该抄哪一份"。

这些关注点散着放的后果是可预测的：第 7 条路由和第 17 条路由的错误体长得不一样，
第 3 条列表端点没有分页，第 5 条路由把业务分支写进了 handler。**接口面的不一致，
客户端要用一辈子去兼容**（Hyrum 定律：凡是可观察的，都会有人依赖）。

### 1.1 路由层只做四件事

```python
# app/gateway/routers/thread_runs.py（示意：一条路由的完整生命周期）
@router.post("/{thread_id}/runs")
async def create_run(
    thread_id: str,                          # 1. 绑定参数（FastAPI 做，含类型与格式校验）
    body: RunCreateRequest,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)] = None,
) -> RunResponse:
    services = get_run_services(request)      # 2. 取依赖（deps.py，路由不碰 app.state）
    caller = caller_from_request(request)     #    身份来自认证结果，不来自 body
    launch = await start_run(                 # 3. 调 services（唯一收敛点，M3 §4）
        body, thread_id=thread_id, caller=caller, services=services, idempotency_key=idempotency_key,
    )
    return run_to_wire(launch.record)         # 4. 编响应（唯一的序列化函数）
```

每条路由的完整签名、`Depends` 和分支在 §3.3。上面这段只标出四步，参数名以 §3.3 为准。


| 允许出现在路由里的                                          | 不允许                            | 为什么                                      |
| -------------------------------------------------- | ------------------------------ | ---------------------------------------- |
| 参数绑定、`Annotated` 约束、依赖注入                           | 业务分支（"如果是 internal 就……"）       | 那是 `start_run` 与 `RunCaller` 的事（M3 §4.1） |
| 调一个 `services.*` 函数                                | 直接 `asyncio.create_task`       | 第二条执行栈（M3 §1）                            |
| 把领域异常交给全局处理器                                       | 自己 `try/except` 拼状态码           | 错误映射只有一张表（§6）                            |
| 调 `run_to_wire` / `sse_response` / `page_response` | 自己拼 JSON、自己 `json.dumps`       | 响应形状是接口面的一部分（§5）                         |
| 读 `request.headers` 里白名单内的头                        | 从 body 里读身份、线程属主、checkpoint 坐标 | M3 §4.3：这些键只从可信来源读                       |


### 1.2 这一层的产出物是"接口面"，不是"能跑"

判断标准很简单：**同一份客户端代码，能不能只换 base_url 就打另一套实现？**
官方 `@langchain/langgraph-sdk` 的 `useStream` 能直接用、Python SDK 能直接用、
IM 通道只需要构造 `RunCreateRequest`——这就是这一层的验收标准（M3 §12）。

---

## 2. 目录与文件职责

```
app/gateway/
├── app.py                     ★  FastAPI 装配：lifespan、中间件顺序、router 注册顺序、异常处理器
├── deps.py                    ★  依赖注入：全部 provider 的唯一出处
├── errors.py                  ★  领域异常 → HTTP 的映射表 + 三个 handler
├── models/
│   ├── request.py             ★  所有 *Request 模型（extra="forbid"）
│   └── response.py            ★  所有 *Response 模型 + run_to_wire / thread_to_wire / message_to_wire
├── pagination.py              ★  keyset 分页的参数解析与信封构造
├── idempotency.py             ★  Idempotency-Key 的形状校验（不哈希）
├── state_reader.py            ★  checkpoint → ThreadStateResponse / RunStateResponse
├── run_models.py                  RunCreateRequest / RunCancelRequest / RunCaller / RunLaunch（M3 §4.1）
├── services.py                    start_run / sse_consumer / wait_for_run_completion / delete_thread（M3 §4、§8）
├── middleware/
│   ├── trace.py                   trace_id 注入 request.state + 响应头
│   ├── auth.py                    M9：认证结果写 request.state.auth
│   └── csrf.py                    M9：Cookie 会话的 CSRF
├── routers/
│   ├── threads.py             ★  thread CRUD、state、messages、compact
│   ├── thread_runs.py         ★  runs 的六条路由（创建/流式/等待/旁观/取消/列表）
│   ├── uploads.py                 M2 §10：上传
│   ├── artifacts.py               M2：产物下载
│   ├── models.py                  M1：模型清单
│   ├── mcp.py                     M7 §2.4：配置与缓存
│   ├── skills.py                  M7 §2.4：列表、启用、reload
│   ├── scheduled_tasks.py         M9 §4.10
│   ├── channels.py                M9 §5.9
│   └── memory.py                  M10 §2.12
# 认证路由不在 routers/ 下，见 app/gateway/auth/routers.py（M9 §2.12，prefix=/v1/auth）
└── request_path.py                路径规范化（nginx 前缀、尾部斜杠）
```


| 文件                | 只做                                                     | 不许做                                        |
| ----------------- | ------------------------------------------------------ | ------------------------------------------ |
| `app.py`          | 装配顺序（中间件 → 异常处理器 → router）、lifespan                    | 任何业务逻辑；任何 `if` 分支决定"这条路由要不要鉴权"（那是依赖的事）     |
| `deps.py`         | 从 `app.state` / `request.state` 取单例，做类型收敛              | import harness 之外的框架类型去构造业务对象              |
| `errors.py`       | 异常 → `(status, code, detail)` 的查表                      | 打日志之外的处理；不吞异常                              |
| `models/*.py`     | 字段、约束、别名                                               | 任何 IO、任何跨字段业务校验（那属于 `start_run` 的第 1–12 步） |
| `routers/*.py`    | §1.1 的四件事                                              | 上面那张"不允许"列里的任何一条                           |
| `idempotency.py`  | 校验 `Idempotency-Key` 的形状，原样交给 `start_run`              | 自己做 sha256、自己查 `runs` 表                    |
| `state_reader.py` | `read_thread_state`：checkpoint → `ThreadStateResponse` | 准入、挂 worker、改 checkpoint                   |


**harness 层（`agentkit/`**）永远不 import 这一层的任何模块。** 反向依赖一旦出现，
M1 的嵌入式客户端和 M8 的子代理就会被拖进 Web 框架（M3 §1 的硬边界）。

### 2.1 符号归属

路由能调用的名字都在这张表里。表外的名字出现在 `routers/*.py`，就是 §13.3 要拦的越界。


| 符号                                                            | 所属文件                                                                             | 谁调用                                        | 做什么                                       |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------- | ------------------------------------------ | ----------------------------------------- |
| `ThreadId` / `RunId`                                          | `app/gateway/models/request.py`                                                  | 所有带路径参数的 handler                           | `Path(pattern=...)`，非法 → 422              |
| `ThreadCreateRequest` 等 `*Request`                            | `app/gateway/models/request.py`                                                  | FastAPI 绑定 body                            | 字段约束；`extra="forbid"`                     |
| `RunCreateRequest` / `RunCancelRequest`                       | `app/gateway/run_models.py`                                                      | 同上；内部 launch 也构造 `RunCreateRequest`        | 与 M3 §3 同一份模型，不在 `models/request.py` 再抄一份 |
| `RunCaller` / `RunLaunch` / `CallerKind`                      | `app/gateway/run_models.py`                                                      | `deps.py`、`services.py`、路由                 | M3 §4.1                                   |
| `RunServices`                                                 | `app/gateway/deps.py`                                                            | `get_run_services`                         | lifespan 挂上 `app.state.run_services`      |
| `get_*` / `caller_from_request` / `require_scope`             | `app/gateway/deps.py`                                                            | `Depends(...)`                             | §4、§8.3                                   |
| `parse_idempotency_key`                                       | `app/gateway/idempotency.py`                                                     | create / stream / wait                     | §10.1；不哈希                                 |
| `start_run`                                                   | `app/gateway/services.py`                                                        | 五条创建路由                                     | M3 §4；幂等 scope 在这里做                       |
| `sse_response` / `sse_consumer` / `wait_for_run_completion`   | `app/gateway/services.py`                                                        | stream / wait / join / cancel(`wait=true`) | M3 §8                                     |
| `read_thread_state`                                           | `app/gateway/state_reader.py`                                                    | `GET .../state`、`POST .../wait`            | §5.5                                      |
| `delete_thread`                                               | `app/gateway/services.py`                                                        | `DELETE .../threads/{id}`                  | 先查活跃 run，再按 M4 §10 的顺序删                   |
| `run_to_wire` / `thread_to_wire` / `message_to_wire` / `Page` | `app/gateway/models/response.py`                                                 | 每个 JSON handler 的 `return`                 | §5.3                                      |
| `page_params` / `message_page_params` / `page_response`       | `app/gateway/pagination.py`                                                      | 三个列表端点                                     | §9                                        |
| `install_error_handlers`                                      | `app/gateway/errors.py`                                                          | `create_app`                               | §6.2                                      |
| `StartRunError` 及其子类                                          | `app/gateway/services.py`                                                        | `start_run`、路由里的存在性检查                      | 自带 `status_code` / `code`（M3 §4.2）        |
| `ThreadOwnershipConflictError` / `InvalidMetadataFilterError` | `agentkit/persistence/thread_meta/base.py`、`agentkit/persistence/json_compat.py` | store                                      | **不带** HTTP 字段；`errors.py` 按类型查表          |
| `install_middleware`                                          | `app/gateway/app.py`                                                             | `create_app`                               | §12.1                                     |
| `TraceMiddleware`                                             | `app/gateway/middleware/trace.py`                                                | 中间件栈                                       | 写 `request.state.trace_id`                |
| `row_to_dict` / `_encode_cursor`                              | `agentkit/persistence/base.py`、各 `sql.py`                                        | store，不是路由                                 | 别名与游标；路由只透传                               |


---

## 3. 路由清单

**这张表是接口面的权威。** 新增一条路由 = 往表里加一行 + 加一个契约测试（§13.2），
两件事少一件 CI 就红。

### 3.1 M3–M5 的路由（本文负责定义）


| Method | Path                                            | 请求模型                             | 成功响应                     | 流式  | 能力               | 阶段    |
| ------ | ----------------------------------------------- | -------------------------------- | ------------------------ | --- | ---------------- | ----- |
| POST   | `/api/threads`                                  | `ThreadCreateRequest`            | `ThreadResponse`（201）    | —   | `threads:write`  | M3/M4 |
| GET    | `/api/threads`                                  | 分页 query（§9）                     | `Page[ThreadResponse]`   | —   | `threads:read`   | M4    |
| POST   | `/api/threads/search`                           | `ThreadSearchRequest`            | `Page[ThreadResponse]`   | —   | `threads:read`   | M4    |
| GET    | `/api/threads/{thread_id}`                      | ——                               | `ThreadResponse`         | —   | `threads:read`   | M4    |
| PATCH  | `/api/threads/{thread_id}`                      | `ThreadPatchRequest`             | `ThreadResponse`         | —   | `threads:write`  | M4    |
| DELETE | `/api/threads/{thread_id}`                      | ——                               | `{"deleted": thread_id}` | —   | `threads:delete` | M4    |
| GET    | `/api/threads/{thread_id}/state`                | `?checkpoint_id=`                | `ThreadStateResponse`    | —   | `threads:read`   | M3/M4 |
| GET    | `/api/threads/{thread_id}/messages`             | `?limit=&before_seq=&after_seq=` | `Page[MessageResponse]`  | —   | `threads:read`   | M4    |
| POST   | `/api/threads/{thread_id}/runs`                 | `RunCreateRequest`               | `RunResponse`（202）       | —   | `runs:create`    | M3    |
| POST   | `/api/threads/{thread_id}/runs/stream`          | `RunCreateRequest`               | `text/event-stream`      | ✓   | `runs:create`    | M3    |
| POST   | `/api/threads/{thread_id}/runs/wait`            | `RunCreateRequest`               | `RunStateResponse`       | —   | `runs:create`    | M3    |
| GET    | `/api/threads/{thread_id}/runs`                 | 分页 query + `?status=`            | `Page[RunResponse]`      | —   | `runs:read`      | M4    |
| GET    | `/api/threads/{thread_id}/runs/{run_id}`        | ——                               | `RunResponse`            | —   | `runs:read`      | M3/M4 |
| POST   | `/api/threads/{thread_id}/runs/{run_id}/cancel` | `RunCancelRequest`               | `CancelResponse`         | —   | `runs:cancel`    | M3    |
| GET    | `/api/threads/{thread_id}/runs/{run_id}/join`   | `Last-Event-ID` 头                | `text/event-stream`      | ✓   | `runs:read`      | M3    |
| POST   | `/api/runs/stream`                              | `RunCreateRequest`               | `text/event-stream`      | ✓   | `runs:create`    | M5    |
| POST   | `/api/runs/wait`                                | `RunCreateRequest`               | `RunStateResponse`       | —   | `runs:create`    | M5    |


最后两行是**"不先建 thread、直接发消息"**的便捷入口（M3 §4.6 明确支持这个用法：
`_ensure_thread_metadata` 会顺手建行）。它们与带 `thread_id` 的两条**调的是同一个**
`start_run`，只是 `thread_id` 由服务端生成、并用 `Content-Location` 回传。
**不允许**为它们复制一份调度逻辑。`Idempotency-Key` **只在带 `thread_id` 的三条创建路由上生效**
（§10.4）：无 thread 的便捷入口每次生成新 `thread_id`，同一个 key 无法指回上一次。

### 3.2 后续阶段的路由（形状遵循本文）

这些路由归各自阶段定义，但**字段命名、错误体、分页信封、能力门、注册顺序全部沿用本文**：


| 归属  | 路由组                                                                                                                |
| --- | ------------------------------------------------------------------------------------------------------------------ |
| M2  | `POST /api/threads/{id}/uploads`、`GET .../uploads/list`、`DELETE .../uploads/{filename}`、`GET .../artifacts/{path}`（handler 与错误码见 [M2 §10](2026-09-16-m2-tools-sandbox-design.md)） |
| M1  | `GET /api/models`、`GET /api/models/{model_name}`（字段、handler、`models:read` 见 [M1 §7](2026-09-16-m1-minimal-agent-design.md)） |
| M6  | `POST /api/threads/{id}/state`、`POST .../compact`、`GET .../history`、`GET .../branches`（[M6 §7.5](2026-09-16-m6-middleware-design.md)） |
| M7  | `GET/PUT/PATCH /api/mcp/config`、`POST /api/mcp/cache/reset`、`GET /api/skills`、`POST /api/skills/reload`、`PATCH /api/skills/{name}`（[M7 §2.4](2026-09-16-m7-mcp-skills-design.md)） |
| M9  | `/api/v1/auth/**`、`/api/scheduled-tasks/**`、`/api/channels/**`（**只有飞书**）（[M9 §2.12](2026-09-16-m9-production-design.md)） |
| M10 | `/api/memory/**`、`GET/PUT/DELETE /api/threads/{id}/goal`（[M10 §2.12](2026-09-16-m10-memory-guardrails-extensions-design.md)） |


**模型目录、mcp、skills 各自一个 router 文件，不和 threads/runs 写在一起。**
模型目录的能力是 `models:read`（M1 §7）。`GET /api/skills` 是 `skills:read`；
**普通用户唯一的写操作是 `PUT /api/skills/{name}/preference`**（开关"我用不用"，也只影响自己）；
全局开关、MCP 启停、reload 是 `admin`（M7 §2.4）。
M7 **没有任何创建类路由**——技能的创建/上传与 MCP 服务器的新建/删除留给后续阶段，
用能力位 `skills:create` / `mcp:create`（业务管理员），所以 `PUT /api/mcp/config` 现在**不注册**。
混在一个文件里，迟早会复制错隔壁的 `Depends`。

### 3.3 每条路由的 handler

三个 router 的 `prefix` 在各自文件里，`create_app` 再统一加 `/api`（§7.1）。
下面每个 handler 的步骤就是函数体：步骤之间没有别的分支。领域异常直接 `raise`，不在这里 `except`。

公共依赖（每个 handler 都按这个顺序声明，OpenAPI 才稳定）：


| 参数                | 注解                               | 所属文件                                  |
| ----------------- | -------------------------------- | ------------------------------------- |
| `thread_id`       | `ThreadId`                       | `app/gateway/models/request.py`       |
| `run_id`          | `RunId`                          | 同上                                    |
| `body`            | 对应的 `*Request`                   | `models/request.py` 或 `run_models.py` |
| `request`         | `Request`                        | FastAPI                               |
| `caller`          | `Depends(require_scope(...))`    | `app/gateway/deps.py`                 |
| `services`        | `Depends(get_run_services)`      | `app/gateway/deps.py`                 |
| `idempotency_key` | `Depends(parse_idempotency_key)` | `app/gateway/idempotency.py`          |


`Depends` 在进函数体之前执行。能力不够时函数体一行都不会跑（§8.3）。

#### `app/gateway/routers/threads.py`

```python
# app/gateway/routers/threads.py
router = APIRouter(prefix="/threads", tags=["threads"])
```

`**POST ""` → `create_thread`，201，`threads:write**`

```python
# app/gateway/routers/threads.py
@router.post("", status_code=201, response_model=ThreadResponse)
async def create_thread(
    body: ThreadCreateRequest,
    caller: Annotated[RunCaller, Depends(require_scope("threads:write"))],
    store: Annotated[ThreadMetaStore, Depends(get_thread_store)],
) -> ThreadResponse:
    thread_id = body.thread_id or uuid4().hex          # hex 是 32 字符，落在 ThreadId 的 1..64
    row = await store.create(                          # agentkit/persistence/thread_meta/base.py
        thread_id,
        assistant_id=body.assistant_id,
        user_id=caller.user_id,                        # 显式传入，不用 AUTO：身份以本次 caller 为准
        metadata=body.metadata,                        # store 内 strip_reserved_metadata，不 422
    )
    return thread_to_wire(row)                         # app/gateway/models/response.py
```


| 步骤  | 逻辑                                                                              | 失败                              |
| --- | ------------------------------------------------------------------------------- | ------------------------------- |
| 1   | `thread_id` 缺省则 `uuid4().hex`；客户端给的值已由 `ThreadId` 模式校验                          | 格式非法：422（模型，进不了函数）              |
| 2   | `store.create`。同用户已存在 → 返回已有行（M4 §4.4 幂等）；不同用户 → `ThreadOwnershipConflictError` | 409 `thread_ownership_conflict` |
| 3   | `thread_to_wire`                                                                | ——                              |


`display_name` / `status` / `incarnation` / `user_id` 不在 body 里。`incarnation` 由 `create` 写 `uuid4().hex`。

`**GET ""` → `list_threads`，200，`threads:read**`

```python
# app/gateway/routers/threads.py
@router.get("", response_model=Page[ThreadResponse])
async def list_threads(
    caller: Annotated[RunCaller, Depends(require_scope("threads:read"))],
    params: Annotated[PageParams, Depends(page_params)],          # order 默认 desc
    store: Annotated[ThreadMetaStore, Depends(get_thread_store)],
) -> Page[ThreadResponse]:
    rows = await store.search_page(                                # 比 M4 search 多的 keyset 重载，见 §9.4
        user_id=caller.user_id,
        limit=params.limit + 1,                                    # 多取一条探测 has_more
        cursor=params.cursor,
        order=params.order,
    )
    page, has_more = rows[: params.limit], len(rows) > params.limit
    return page_response(page, has_more=has_more, cursor_of=lambda row: row["cursor"])
```

`row["cursor"]` 由 store 填，路由不自己拼。`thread_to_wire` 在 `page_response` 之前映射：
`page_response([thread_to_wire(r) for r in page], ...)`，`cursor_of` 改为读 `ThreadResponse` 上不存在的游标——
所以游标在映射前从原始行取出，见 §9.2 的 `page_response` 重载说明。实现时用下面这一行，避免游标粘到响应模型上：

```python
# app/gateway/routers/threads.py
wired = [thread_to_wire(row) for row in page]
cursors = [row["cursor"] for row in page]
return page_response(wired, has_more=has_more, next_cursor=cursors[-1] if has_more and cursors else None)
```

`**POST "/search"` → `search_threads`，200，`threads:read**`

静态段 `/search` 必须写在 `GET "/{thread_id}"` 之前（§7.1）。body 里的 `limit` / `cursor` / `order`
覆盖 query：搜索条件不适合塞进 query string（metadata 的值含空格和类型）。

```python
# app/gateway/routers/threads.py
@router.post("/search", response_model=Page[ThreadResponse])
async def search_threads(
    body: ThreadSearchRequest,
    caller: Annotated[RunCaller, Depends(require_scope("threads:read"))],
    store: Annotated[ThreadMetaStore, Depends(get_thread_store)],
) -> Page[ThreadResponse]:
    rows = await store.search_page(
        user_id=caller.user_id,
        metadata=body.metadata,          # 非法 key → InvalidMetadataFilterError → 400
        status=body.status,
        limit=body.limit + 1,
        cursor=body.cursor,
        order=body.order,
    )
    # 裁页与 list_threads 相同
```

`**GET "/{thread_id}"` → `get_thread`，200，`threads:read**`

```python
# app/gateway/routers/threads.py
row = await store.get(thread_id, user_id=caller.user_id)   # 属主过滤在 SQL 里
if row is None:
    raise ThreadNotFound(f"Thread {thread_id} not found")  # 404，不区分"不存在"和"不是你的"
return thread_to_wire(row)
```

`**PATCH "/{thread_id}"` → `patch_thread`，200，`threads:write**`

只 merge `metadata`。`display_name` 不在这个 body 里：它是 checkpoint `title` 的投影（M4 §4.1），
路由改它会和 worker `finally` 互相覆盖。

```python
# app/gateway/routers/threads.py
row = await store.get(thread_id, user_id=caller.user_id)
if row is None:
    raise ThreadNotFound(f"Thread {thread_id} not found")
if body.metadata is not None:
    await store.update_metadata(thread_id, body.metadata, user_id=caller.user_id)  # merge
    row = await store.get(thread_id, user_id=caller.user_id)
return thread_to_wire(row)
```

空 body `{}` 合法：不写库，返回当前行。

`**DELETE "/{thread_id}"` → `delete_thread_route`，200，`threads:delete**`

```python
# app/gateway/routers/threads.py
@router.delete("/{thread_id}", response_model=DeletedResponse)
async def delete_thread_route(
    thread_id: ThreadId,
    caller: Annotated[RunCaller, Depends(require_scope("threads:delete"))],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> DeletedResponse:
    await delete_thread(services, thread_id, caller)       # app/gateway/services.py
    return DeletedResponse(deleted=thread_id)
```

```python
# app/gateway/services.py
async def delete_thread(services: RunServices, thread_id: str, caller: RunCaller) -> None:
    """M4 §10 的删除顺序收在这一个函数里。路由不自己排 checkpoint / 文件 / 投影行。"""
    active = await services.run_store.list_by_thread(
        thread_id,
        user_id=caller.user_id,
        limit=1,
        statuses=frozenset({RunStatus.pending, RunStatus.running}),
    )
    if active:
        raise ThreadBusy(f"Thread {thread_id} has an active run")
    row = await services.run_context.thread_store.get(thread_id, user_id=caller.user_id)
    if row is None:
        raise ThreadNotFound(f"Thread {thread_id} not found")
    # 之后的磁盘与 checkpoint 删除顺序以 M4 §10 为准；投影行最后删。
    await services.run_context.thread_store.delete(thread_id, user_id=caller.user_id)
```

`**GET "/{thread_id}/state"` → `get_state`，200，`threads:read**`

```python
# app/gateway/routers/threads.py
@router.get("/{thread_id}/state", response_model=ThreadStateResponse)
async def get_state(
    thread_id: ThreadId,
    checkpoint_id: Annotated[str | None, Query(max_length=128, pattern=r"^[A-Za-z0-9_.:-]{1,128}$")] = None,
    caller: Annotated[RunCaller, Depends(require_scope("threads:read"))],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> ThreadStateResponse:
    await _require_visible_thread(services, thread_id, caller)          # 本文件私有，见下
    return await read_thread_state(services, thread_id, checkpoint_id=checkpoint_id)
```

```python
# app/gateway/routers/threads.py
async def _require_visible_thread(services: RunServices, thread_id: str, caller: RunCaller) -> None:
    await require_visible_thread(services, thread_id, caller)  # app/gateway/thread_access.py
```

函数体在 `thread_access.py`：`thread_store.get` 为空就抛 `ThreadNotFound`。
上传和产物路由（M2 §10）调用同一个函数，不在各自的 router 里再查一次库。

`read_thread_state` 的逻辑在 §5.5，文件是 `app/gateway/state_reader.py`。

`**GET "/{thread_id}/messages"` → `list_messages`，200，`threads:read**`

不用 §9 的不透明 `cursor`。消息游标就是 `run_events.seq`（M4 §4.3），query 名固定为 `before_seq` / `after_seq`。

```python
# app/gateway/routers/threads.py
@router.get("/{thread_id}/messages", response_model=Page[MessageResponse])
async def list_messages(
    thread_id: ThreadId,
    caller: Annotated[RunCaller, Depends(require_scope("threads:read"))],
    params: Annotated[MessagePageParams, Depends(message_page_params)],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> Page[MessageResponse]:
    await _require_visible_thread(services, thread_id, caller)
    rows = await services.event_store.list_messages(          # agentkit/runtime/events/store/base.py
        thread_id,
        limit=params.limit + 1,
        before_seq=params.before_seq,
        after_seq=params.after_seq,
    )                                                          # 只含 category='message'，升序
    page, has_more = _trim_message_page(rows, limit=params.limit, after_seq=params.after_seq)
    wired = [message_to_wire(row) for row in page]
    next_cursor = str(wired[-1].seq) if has_more and wired else None
    return Page(data=wired, has_more=has_more, next_cursor=next_cursor)
```

```python
# app/gateway/pagination.py
def _trim_message_page(rows: list[dict], *, limit: int, after_seq: int | None) -> tuple[list[dict], bool]:
    """store 按升序返回。向过去翻（before_seq）时多取的那条在列表头；向未来翻（after_seq）时在列表尾。"""
    has_more = len(rows) > limit
    if not has_more:
        return rows, False
    if after_seq is not None:
        return rows[:limit], True
    return rows[-limit:], True
```

`before_seq` 与 `after_seq` 同时出现 → 422，校验在 `MessagePageParams` 的 `model_validator`（§9.1）。

#### `app/gateway/routers/thread_runs.py`

```python
# app/gateway/routers/thread_runs.py
router = APIRouter(prefix="/threads", tags=["runs"])
```

声明顺序就是匹配顺序：`/stream`、`/wait` 先于 `/{run_id}`（§7.1）。

`**POST "/{thread_id}/runs"` → `create_run`，202，`runs:create**`

```python
# app/gateway/routers/thread_runs.py
@router.post("/{thread_id}/runs", status_code=202, response_model=RunResponse)
async def create_run(
    thread_id: ThreadId,
    body: RunCreateRequest,                                              # app/gateway/run_models.py
    caller: Annotated[RunCaller, Depends(require_scope("runs:create"))],
    services: Annotated[RunServices, Depends(get_run_services)],
    idempotency_key: Annotated[str | None, Depends(parse_idempotency_key)],
) -> RunResponse:
    launch = await start_run(                                            # app/gateway/services.py
        body,
        thread_id=thread_id,
        caller=caller,
        services=services,
        idempotency_key=idempotency_key,
    )
    return run_to_wire(launch.record)
```

`launch.reused` 在这条路由上**不改变状态码**：首次和重放都是 202 + 同一条 `RunResponse`。
路由不读 `record.idempotency_reused`（M3 §4.1）。

`**POST "/{thread_id}/runs/stream"` → `stream_run`，200，`runs:create`，`StreamingResponse**`

```python
# app/gateway/routers/thread_runs.py
@router.post("/{thread_id}/runs/stream")
async def stream_run(
    thread_id: ThreadId,
    body: RunCreateRequest,
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("runs:create"))],
    services: Annotated[RunServices, Depends(get_run_services)],
    idempotency_key: Annotated[str | None, Depends(parse_idempotency_key)],
) -> StreamingResponse:
    launch = await start_run(
        body, thread_id=thread_id, caller=caller, services=services, idempotency_key=idempotency_key,
    )
    return sse_response(services, launch, request)                       # apply_on_disconnect 默认 True
```

没有 `response_model`。契约测试的白名单见 §13.2。

`**POST "/{thread_id}/runs/wait"` → `wait_run`，200，`runs:create**`

```python
# app/gateway/routers/thread_runs.py
@router.post("/{thread_id}/runs/wait", response_model=RunStateResponse)
async def wait_run(
    thread_id: ThreadId,
    body: RunCreateRequest,
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("runs:create"))],
    services: Annotated[RunServices, Depends(get_run_services)],
    idempotency_key: Annotated[str | None, Depends(parse_idempotency_key)],
) -> RunStateResponse:
    launch = await start_run(
        body, thread_id=thread_id, caller=caller, services=services, idempotency_key=idempotency_key,
    )
    final = await wait_for_run_completion(services, launch.record, request)   # 断开 → ClientDisconnected
    # 读 final 自己的 checkpoint，不读 thread 当前头。
    # reused=True 时头可能已被后来的 run 推进（M3 §8.3）；分两个 return 会让人把 else 改回 read_thread_state。
    return await read_run_terminal_state(services, final)                     # app/gateway/state_reader.py
```

`**GET "/{thread_id}/runs"` → `list_runs`，200，`runs:read**`

```python
# app/gateway/routers/thread_runs.py
@router.get("/{thread_id}/runs", response_model=Page[RunResponse])
async def list_runs(
    thread_id: ThreadId,
    caller: Annotated[RunCaller, Depends(require_scope("runs:read"))],
    params: Annotated[PageParams, Depends(page_params)],
    statuses: Annotated[frozenset[RunStatus] | None, Depends(parse_status_filter)],
    metadata: Annotated[dict[str, JsonScalar], Depends(metadata_filters)],  # app/gateway/pagination.py
    services: Annotated[RunServices, Depends(get_run_services)],
) -> Page[RunResponse]:
    await _require_visible_thread(services, thread_id, caller)
    rows = await services.run_store.list_by_thread(                     # agentkit/persistence/run/sql.py
        thread_id,
        user_id=caller.user_id,
        limit=params.limit + 1,
        before=params.cursor,
        order=params.order,
        statuses=statuses,                                               # None = 不过滤；只列 operation_kind='run'
        metadata=metadata or None,
    )
    page, has_more = rows[: params.limit], len(rows) > params.limit
    wired = [run_to_wire(row) for row in page]
    next_cursor = page[-1]["cursor"] if has_more and page else None
    return Page(data=wired, has_more=has_more, next_cursor=next_cursor)
```

```python
# app/gateway/routers/thread_runs.py
_RUN_STATUSES = {status.value for status in RunStatus}

def parse_status_filter(
    status: Annotated[str | None, Query(max_length=128, description="Comma-separated run statuses")] = None,
) -> frozenset[RunStatus] | None:
    """未知值在进 store 之前 422。不静默丢掉——丢掉会让客户端以为过滤生效了。"""
    if status is None or status == "":
        return None
    names = [part.strip() for part in status.split(",") if part.strip()]
    unknown = [name for name in names if name not in _RUN_STATUSES]
    if unknown or not names:
        raise RunRequestInvalid(f"Unknown run status: {', '.join(unknown) or status}")
    return frozenset(RunStatus(name) for name in names)
```

`RunStatus` 定义在 `agentkit/runtime/runs/record.py`（M3 §5.2）：`pending` / `running` / `success` / `error` / `timeout` / `interrupted`。

`**GET "/{thread_id}/runs/{run_id}"` → `get_run`，200，`runs:read**`

```python
# app/gateway/routers/thread_runs.py
record = await services.run_manager.get(run_id, user_id=caller.user_id)   # agentkit：先内存，再 store
if record is None or record.thread_id != thread_id:
    raise RunNotFound(f"Run {run_id} not found")                           # 跨 thread 也是 404，不 403
return run_to_wire(record)
```

`**POST "/{thread_id}/runs/{run_id}/cancel"` → `cancel_run`，200，`runs:cancel**`

完整步骤在 §8.3 与 M3 §9.1。路由查的是 `CancelOutcome`，不调用 `record.task.cancel()`。

`**GET "/{thread_id}/runs/{run_id}/join"` → `join_run`，200，`runs:read`，`StreamingResponse**`

步骤见 §11.2。`apply_on_disconnect=False`。

#### `app/gateway/routers/runs.py`（无 thread 的便捷入口）

```python
# app/gateway/routers/runs.py
router = APIRouter(prefix="/runs", tags=["runs"])


@router.post("/stream")
async def stream_stateless(
    body: RunCreateRequest,
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("runs:create"))],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> StreamingResponse:
    thread_id = _stateless_thread_id(body)
    launch = await start_run(
        body, thread_id=thread_id, caller=caller, services=services, idempotency_key=None,
    )
    return sse_response(services, launch, request)


@router.post("/wait", response_model=RunStateResponse)
async def wait_stateless(
    body: RunCreateRequest,
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("runs:create"))],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> RunStateResponse:
    thread_id = _stateless_thread_id(body)
    launch = await start_run(
        body, thread_id=thread_id, caller=caller, services=services, idempotency_key=None,
    )
    final = await wait_for_run_completion(services, launch.record, request)
    return await read_run_terminal_state(services, final)


def _stateless_thread_id(body: RunCreateRequest) -> str:
    """只接受 config.configurable.thread_id。缺省则服务端生成。body 顶层没有 thread_id 字段。"""
    config = body.config or {}
    configurable = config.get("configurable") if isinstance(config, dict) else None
    raw = configurable.get("thread_id") if isinstance(configurable, dict) else None
    if raw is None:
        return uuid4().hex
    return raw   # RunCreateRequest.validate_configurable_thread_id 已按 ThreadId 校验过
```


| 参数                             | 便捷入口怎么处理                              | 为什么                                         |
| ------------------------------ | ------------------------------------- | ------------------------------------------- |
| `thread_id`                    | 生成，或取 `config.configurable.thread_id` | SDK 的 stateless run 用 configurable 带 thread |
| `idempotency_key`              | 固定 `None`，依赖不声明 `Idempotency-Key`     | 见 §10.4                                     |
| `caller` / `body` / `services` | 与 thread 作用域的 stream / wait 相同        | 同一个 `start_run`                             |


`config.configurable.thread_id` 在 `start_run` 第 3 步会被从开关里丢掉，第 9 步再用这里选出的 `thread_id` 写回。客户端不能靠这个字段改属主。

---

## 4. 依赖注入（`deps.py`）

### 4.1 全部 provider

**这是路由能拿到东西的唯一途径。** 路由里出现 `request.app.state.xxx` 就是漏了一个 provider。


| provider                       | 返回                    | 生命周期 | 从哪取                                          | 谁调              |
| ------------------------------ | --------------------- | ---- | -------------------------------------------- | --------------- |
| `get_app_config()`             | `AppConfig`           | 进程级  | M0 §5 的配置单例                                  | 参数校验、模型白名单      |
| `get_run_services(request)`    | `RunServices`         | 进程级  | `request.app.state.run_services`（M3 §4.1）    | runs 路由         |
| `get_thread_store(request)`    | `ThreadMetaStore`     | 进程级  | `services.run_context.thread_store`（M4 §9.2） | threads 路由      |
| `get_run_store(request)`       | `RunStore`            | 进程级  | `services.run_store`（M4 §9.2 新增字段）           | runs 列表/get     |
| `get_event_store(request)`     | `RunEventStore`       | 进程级  | `services.event_store`（M4 §9.2 新增字段）         | messages 分页     |
| `get_checkpointer(request)`    | `BaseCheckpointSaver` | 进程级  | `services.run_context.checkpointer`          | `/state` 路由     |
| `caller_from_request(request)` | `RunCaller`           | 每请求  | M3 §4.1；M9 改成读 `request.state.auth`          | 所有需要身份的路由       |
| `get_principal(request)`       | `Principal`           | 每请求  | M9 §2.10：从 `runtime.context` 重建              | M9 的授权依赖        |
| `get_request_id(request)`      | `str`                 | 每请求  | trace 中间件写进 `request.state.trace_id`         | 日志、run metadata |


```python
# app/gateway/deps.py
@dataclass(frozen=True, slots=True)
class RunServices:
    """lifespan 建一次，挂在 app.state.run_services。

    前三个字段是 M3 §4.1。run_store / event_store 是 M4 §9.2 提到的一级字段：
    列表和消息分页不钻 run_context，避免路由依赖 worker 的那一份上下文对象。
    """

    run_manager: RunManager
    bridge: StreamBridge
    run_context: RunContext
    run_store: RunStore
    event_store: RunEventStore


def get_run_services(request: Request) -> RunServices:
    """进程级单例的打包（M3 §4.1）。lifespan 建一次，挂 app.state。"""
    return request.app.state.run_services


def get_thread_store(request: Request) -> ThreadMetaStore:
    return get_run_services(request).run_context.thread_store


def get_run_store(request: Request) -> RunStore:
    return get_run_services(request).run_store


def get_event_store(request: Request) -> RunEventStore:
    return get_run_services(request).event_store


def get_checkpointer(request: Request) -> BaseCheckpointSaver:
    return get_run_services(request).run_context.checkpointer


def caller_from_request(request: Request) -> RunCaller:
    """M3 没有认证：匿名 HTTP 调用方。
    M9 在这里读 AuthMiddleware 的结果：会话 → HTTP + user_id；PAT → 再带 scopes；
    内部 token（IM 通道走内部 HTTP）→ INTERNAL + owner 头里的 user_id。"""
    return RunCaller(kind=CallerKind.HTTP, user_id=None)
```

三条纪律：

- **provider 里不做 IO，也不做校验。** `caller_from_request` 只读中间件已经写好的
`request.state`；在这里再查一次库，等于每个请求多一次往返，而且失败路径没有统一的错误体。
- **测试用 `app.dependency_overrides`，不要打补丁 `app.state`。** 前者是 FastAPI 的契约，
后者会在 lifespan 里被覆盖回去，症状是"测试里改了，跑起来还是老对象"。
- **不要模块级 `services = build_run_services(...)`。** `init_engine` 还没跑时工厂是 `None`，
模块级初始化会把它永久绑到 memory 实现（M4 §9.2 的同一条纪律）。

### 4.2 M9 会往这张表里加什么


| 新增 provider                 | 返回           | 说明                                             |
| --------------------------- | ------------ | ---------------------------------------------- |
| `require_scope(scope: str)` | 依赖工厂         | 函数本身在 M1 §7.4 落地（模型目录是第一条要门的路由）。M9 只改 `caller_from_request` 读到的 scopes；缺 scope → 403（§8.2） |
| `get_current_user(request)` | `UserRecord` | 会话认证的用户；无认证时抛 401                              |
| `get_admin_user(request)`   | `UserRecord` | M9 的会话用户快捷方式。M7 的门是 `require_scope("admin")`，不是这个函数。模型目录走 `models:read`，`GET /api/skills` 走 `skills:read` |


**加 provider 的顺序是：先加 `deps.py` 的函数，再改路由。** 反过来会出现"路由先 `import` 一个还不存在的名字"，
而这类错误在 `from __future__ import annotations` 的项目里要到运行时才炸。

---

## 5. 请求与响应模型

### 5.1 五个约定


| 约定               | 规则                                            | 为什么                                                      |
| ---------------- | --------------------------------------------- | -------------------------------------------------------- |
| `extra="forbid"` | 所有 `*Request` 模型                              | SDK 升级会带新字段（`stream_resumable` 之类）；静默丢掉比 422 危险得多（M3 §3） |
| 字段命名             | wire 上用 `snake_case`，与仓库 dict、ORM 列名一致        | 三处同名，排查时不用做心算；M4 §3.3 的别名表是唯一的例外                         |
| 时间戳              | ISO-8601、`Z` 结尾                               | 少一个 `Z`，前端就按本地时区解析，偏 8 小时且没人怀疑后端（M4 §3.3）                |
| 枚举               | 小写字符串（`StrEnum` 的值）                           | 与 LangGraph Platform 的 wire 一致，别自创 `UPPER_SNAKE`         |
| 可选字段             | 显式 `None`，不用 `Field(default_factory=...)` 藏语义 | 客户端要能区分"没传"和"传了 null"；服务端行为不同就该是两个字段                     |


### 5.2 `RunCreateRequest`：每个字段的去向

M3 §3 给了模型定义，这里补上**字段去哪**——写路由或 worker 的人不用再翻三份文档：


| 字段                             | 类型 / 约束                                    | 默认                    | 去向                                                              |
| ------------------------------ | ------------------------------------------ | --------------------- | --------------------------------------------------------------- |
| `assistant_id`                 | `str`，`min_length=1`，`max_length=128`      | 必填                    | `resolve_agent_factory`（M3 §4.2 第 6 步）                          |
| `input`                        | `dict | None`                              | `None`                | PII 脱敏 → `normalize_input` → `run_config` 的图输入；run record 也存脱敏图输入 |
| `command`                      | `dict | None`                              | `None`                | wire 上是 JSON 对象；`start_run` 第 7 步再转成 LangGraph `Command`（M6）    |
| `config`                       | `dict | None`                              | `None`                | `build_run_config`：只认白名单键（M3 §4.7）                              |
| `context`                      | `dict | None`                              | `None`                | `collect_client_switches` → `configurable` + `context`          |
| `metadata`                     | `dict | None`                              | `None`                | `_build_run_metadata` → `runs.metadata_json`                    |
| `checkpoint_id` / `checkpoint` | `str | None` / `dict | None`               | `None`                | `apply_checkpoint`（第 10 步）；两者都传以 `checkpoint` 为准                |
| `stream_mode`                  | `list[str] | None`                         | `None` → `["values"]` | `normalize_stream_modes` → `RunContext.stream_modes`            |
| `on_disconnect`                | `Literal["cancel","continue"]`             | `"cancel"`            | `RunRecord.on_disconnect`；SSE 断开时读它（M3 §8.2）                    |
| `multitask_strategy`           | `Literal["reject","interrupt","rollback"]` | `"reject"`            | `create_or_reject`（M3 §5.5）；interrupt/rollback 需要 `runs:cancel` |


**没有任何一个字段允许客户端指定身份、属主或 checkpoint 坐标。** `user_id` / `run_id` /
`trace_id` / `thread_id` 即使出现在 `context` 或 `config` 里也会被第 3 步丢弃
（M3 §4.7 的白名单表），因为它们是服务端盖章的位置。
`config.configurable.thread_id` 是例外中的读法，不是身份：无 thread 的便捷入口用它选分区键（§3.3），
选完之后第 3 步仍然丢掉它，第 9 步用路由已经决定的 `thread_id` 写回。

#### 类体与校验器（`app/gateway/run_models.py`）

官方 SDK 的默认 payload 会带上一批它自己的字段（`stream_resumable=false`、`if_not_exists="create"` 等）。
这些字段必须出现在模型里，否则 `extra="forbid"` 会把每一次 `useStream` 打成 422。
它们是**兼容占位**：只接受 SDK 的默认值，别的值 422，并且**不写入** `RunRecord`、不传给 worker。

```python
# app/gateway/run_models.py
_THREAD_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # 与 models/request.py 的 ThreadId 同一正则；§13.2 断言两处相等


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assistant_id: str = Field(min_length=1, max_length=128)
    input: dict[str, Any] | None = None
    command: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    checkpoint_id: str | None = Field(default=None, max_length=128)
    checkpoint: dict[str, Any] | None = None
    stream_mode: list[str] | str | None = None
    stream_subgraphs: bool = False
    on_disconnect: Literal["cancel", "continue"] = "cancel"
    multitask_strategy: Literal["reject", "interrupt", "rollback"] = "reject"

    # ── 兼容占位：只接受列出的值，不进 worker ──────────────────────────
    # feedback_keys 继续收下并忽略：SDK 会带它，但**反馈功能不做**（M9 §12），
    # 所以它既没有存储也没有读路径；收下只是为了让官方客户端不 422。
    webhook: None = None
    on_completion: None = None
    after_seconds: None = None
    feedback_keys: None = None
    if_not_exists: Literal["create"] = "create"
    stream_resumable: Literal[False] | None = None

    @model_validator(mode="after")
    def validate_configurable_thread_id(self) -> RunCreateRequest:
        """无 thread 路由要用这个字段（§3.3）。格式与路径参数同一份 ThreadId。"""
        if not isinstance(self.config, dict):
            return self
        configurable = self.config.get("configurable")
        if not isinstance(configurable, dict) or "thread_id" not in configurable:
            return self
        raw = configurable["thread_id"]
        if raw is not None and not _THREAD_ID.match(str(raw)):
            raise PydanticCustomError("thread_id", "thread_id must match ^[A-Za-z0-9_-]{1,64}$")
        return self

    @field_validator("stream_resumable", "webhook", "on_completion", "after_seconds",
                     "feedback_keys", "if_not_exists", "multitask_strategy", mode="before")
    @classmethod
    def reject_unsupported_run_options(cls, value: Any, info: ValidationInfo) -> Any:
        """非字符串的 multitask_strategy / if_not_exists 留给 pydantic 自己报 literal_error。"""
        if info.field_name in {"multitask_strategy", "if_not_exists"} and not isinstance(value, str):
            return value
        allowed = {
            "webhook": None,
            "on_completion": None,
            "after_seconds": None,
            "feedback_keys": None,
            "if_not_exists": "create",
            "multitask_strategy": {"reject", "rollback", "interrupt"},
            "stream_resumable": {None, False},
        }[info.field_name]
        ok = value in allowed if isinstance(allowed, set) else value == allowed
        if not ok:
            raise PydanticCustomError(
                "unsupported_run_option",
                "Run option '{option}' is not supported",
                {"option": info.field_name},
            )
        return value

    @field_validator("stream_mode", mode="before")
    @classmethod
    def reject_unsupported_stream_modes(cls, value: Any) -> Any:
        if value is None:
            return value
        # 单个字符串是 SDK 的合法写法；真正的模式表在 agentkit/runtime/stream_modes.py::normalize_stream_modes
        from agentkit.runtime.stream_modes import UnsupportedStreamModeError, normalize_stream_modes
        try:
            normalize_stream_modes(value)
        except UnsupportedStreamModeError as exc:
            raise PydanticCustomError("unsupported_stream_mode", "Unsupported stream mode(s): {modes}", {"modes": ", ".join(exc.modes)}) from exc
        return value
```


| 校验器                               | 所属文件                                                  | 输入                                        | 通过                                    | 失败                                 |
| --------------------------------- | ----------------------------------------------------- | ----------------------------------------- | ------------------------------------- | ---------------------------------- |
| `validate_configurable_thread_id` | `run_models.py`                                       | `config.configurable.thread_id`           | 缺省、`null`、或匹配 `^[A-Za-z0-9_-]{1,64}$` | 422 `invalid_request`              |
| `reject_unsupported_run_options`  | `run_models.py`                                       | 占位字段与 `multitask_strategy`                | 表里的默认值；`multitask_strategy` 三个字面量     | 422，`type=unsupported_run_option`  |
| `reject_unsupported_stream_modes` | `run_models.py` 调用 `agentkit/runtime/stream_modes.py` | `str` 或 `list[str]`                       | `normalize_stream_modes` 不抛           | 422，`type=unsupported_stream_mode` |
| `extra="forbid"`                  | pydantic                                              | 未声明的键，例如 `checkpoint_during`、`durability` | ——                                    | 422                                |


`enqueue` 不在 `multitask_strategy` 里。SDK 若传来，是 422 而不是静默改成 `reject`。

`command` 在模型里是合法字段，但 **M3 的 `start_run` 第 7 步对非空 `command` 抛 `RunRequestInvalid`**（M3：还没有中断点）。
模型提前拒绝会让 M6 接人机确认时不得不改 wire；留到 `start_run` 拒绝，客户端在 M3 和 M6 看到的都是 422 `invalid_run_request`，只是 M6 开始接受 `{"resume": ...}`。这个判断的所属文件是 `app/gateway/services.py`，不是 `run_models.py`。

#### 线程侧请求（`app/gateway/models/request.py`）

```python
# app/gateway/models/request.py
_THREAD_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ThreadId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
RunId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
JsonScalar = str | int | float | bool | None


class ThreadCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    assistant_id: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] | None = None
    # 故意没有 user_id / display_name / status / incarnation


class ThreadPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: dict[str, Any] | None = None          # None = 不改；{} = merge 一个空 dict，等于不改


class ThreadSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: dict[str, JsonScalar] | None = None   # 值必须是标量；对象/数组在进 store 前就 422
    status: Literal["idle", "running", "success", "error", "timeout", "interrupted"] | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=256)
    order: Literal["asc", "desc"] = "desc"
```

```python
# app/gateway/run_models.py
class RunCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["interrupt", "rollback"] = "interrupt"
    wait: bool = False
```

`RunCancelRequest` 的物理位置是 `app/gateway/run_models.py`（M3 §9.1），不要在 `models/request.py` 再定义一次。


| 模型                    | 所属文件                | 字段                                           | 约束                                      | 不在模型里的东西                                     |
| --------------------- | ------------------- | -------------------------------------------- | --------------------------------------- | -------------------------------------------- |
| `ThreadCreateRequest` | `models/request.py` | `thread_id` `assistant_id` `metadata`        | `thread_id` 与路径同一正则；缺省由路由生成             | `user_id`（来自 caller）、`incarnation`（store 生成） |
| `ThreadPatchRequest`  | `models/request.py` | `metadata`                                   | 只 merge，不替换整行                           | `display_name`（投影，worker 写）                  |
| `ThreadSearchRequest` | `models/request.py` | `metadata` `status` `limit` `cursor` `order` | metadata 值是 `JsonScalar`；limit `1..200` | query 上再挂一套分页参数——body 已经有了                   |
| `RunCancelRequest`    | `run_models.py`     | `action` `wait`                              | 默认 interrupt、不等收尾                       | 空 body 由路由收成 `None`，再换成默认实例                  |


### 5.3 响应模型：只有三个 `*_to_wire`

```python
# app/gateway/models/response.py
class Page(BaseModel, Generic[T]):
    data: list[T]
    next_cursor: str | None = None      # 不透明；编码在 store，不在这里
    has_more: bool = False


class ThreadResponse(BaseModel):
    thread_id: str
    incarnation: str | None
    assistant_id: str | None
    display_name: str | None
    status: str
    metadata: dict[str, Any]
    created_at: str                     # ISO-8601 Z，row_to_dict 已经格式化
    updated_at: str
    # 不包含 user_id：列表接口不必把属主再回给属主本人，也避免日志复制响应时扩散


class RunResponse(BaseModel):
    run_id: str
    thread_id: str
    assistant_id: str | None
    status: str
    on_disconnect: Literal["cancel", "continue"]
    multitask_strategy: Literal["reject", "interrupt", "rollback"]
    metadata: dict[str, Any]
    kwargs: dict[str, Any]              # 已脱敏的 input（handle）/ config；不含 secrets
    model_name: str | None
    error: str | None
    created_at: str
    updated_at: str
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    llm_call_count: int = 0


class MessageResponse(BaseModel):
    thread_id: str
    run_id: str
    seq: int
    event_type: str
    content: dict[str, Any] | str
    metadata: dict[str, Any]
    created_at: str


class ThreadStateResponse(BaseModel):
    values: dict[str, Any]
    checkpoint_id: str | None
    checkpoint_ns: str | None = None


class RunStateResponse(BaseModel):
    """/wait 用。比 ThreadStateResponse 多这次 run 的身份，客户端不用再 GET 一次。"""

    values: dict[str, Any]
    checkpoint_id: str | None
    checkpoint_ns: str | None = None
    run_id: str
    status: str
    error: str | None = None


class CancelResponse(BaseModel):
    outcome: Literal["cancelled", "requested", "taken_over"]
    run: RunResponse | None = None


class DeletedResponse(BaseModel):
    deleted: str


_RUN_WIRE_FIELDS = {name for name in RunResponse.model_fields}


def run_to_wire(record: RunRecord | Mapping[str, Any]) -> RunResponse:
    """活体 RunRecord 与 RunStore 的 dict 共用一条出口。进程内字段（task、abort_event）不在 _RUN_WIRE_FIELDS 里，自然被丢掉。"""
    raw = record.__dict__ if isinstance(record, RunRecord) else dict(record)
    kwargs = dict(raw.get("kwargs") or {})
    kwargs.pop("secrets", None)                         # 双保险：落库前应已脱敏（M3 §4.5）
    data = {key: raw.get(key) for key in _RUN_WIRE_FIELDS}
    data["kwargs"] = kwargs
    data["metadata"] = raw.get("metadata") or {}
    return RunResponse.model_validate(data)


def thread_to_wire(row: Mapping[str, Any]) -> ThreadResponse:
    return ThreadResponse.model_validate(row)           # 键名已经是 row_to_dict 之后的 wire 名


def message_to_wire(event: Mapping[str, Any]) -> MessageResponse:
    if event.get("category") not in (None, "message"):
        raise RuntimeError("message_to_wire only accepts category='message'")
    return MessageResponse.model_validate(event)
```

`category` 不出现在 `MessageResponse` 上：能进这个函数的行已经被 `list_messages` 滤成 `message`。
响应里再带一个恒为 `"message"` 的字段，客户端会开始依赖它。


| 响应模型               | 字段来源                               | 备注                                                   |
| ------------------ | ---------------------------------- | ---------------------------------------------------- |
| `RunResponse`      | `runs` 行（活体 `RunRecord` 也行）        | 全字段只读；**不含** `kwargs` 里的 `secrets`（M3 §4.5 落库前已脱敏）   |
| `RunStateResponse` | checkpoint 快照（`/state`、`/wait`）    | `values` 原样 + `checkpoint_id`；不含 `__pregel_` 内部键     |
| `ThreadResponse`   | `threads_meta` 行                   | `display_name` 是投影，权威在 checkpoint 的 `title`（M4 §4.1） |
| `MessageResponse`  | `run_events`（`category='message'`） | 带 `seq`（客户端游标）与 `run_id`（归属）                         |
| `CancelResponse`   | `CancelOutcome`                    | `{"outcome": ..., "run": RunResponse?}`（M3 §9.2）     |
| `Page[T]`          | 仓库的 keyset 结果                      | 三个列表端点共用一份信封（§9.3）                                   |


### 5.4 一个字段只有一个名字

```
ORM 列名          →  仓库 dict 键        →  wire 字段
metadata_json         metadata              metadata          （M4 §3.3 的别名表）
kwargs_json           kwargs                kwargs
event_metadata        metadata              metadata          （只出现在消息类响应里）
created_at            created_at            created_at        （ISO-8601 Z）
```

**HTTP 层不许 `pop`/`rename` 字段。** 别名映射写在 `agentkit/persistence/base.py` 的
`row_to_dict` 里（M4 §3.3）；路由再改一次，就会出现"列表接口和详情接口字段名不同"这类
只有客户端才发现的 bug。

### 5.5 读 checkpoint：`state_reader.py`

`GET .../state` 和 `POST .../wait` 都从这里出快照。路由不调用 checkpointer。

```python
# app/gateway/state_reader.py
_HIDDEN_PREFIX = "__pregel_"


async def read_thread_state(
    services: RunServices, thread_id: str, *, checkpoint_id: str | None = None
) -> ThreadStateResponse:
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    if checkpoint_id is not None:
        config["configurable"]["checkpoint_id"] = checkpoint_id
    tup = await services.run_context.checkpointer.aget_tuple(config)   # langgraph BaseCheckpointSaver
    if tup is None:
        if checkpoint_id is not None:
            raise CheckpointNotFound(f"Checkpoint {checkpoint_id} not found")
        return ThreadStateResponse(values={}, checkpoint_id=None, checkpoint_ns="")
    values = {key: value for key, value in (tup.checkpoint.get("channel_values") or {}).items() if not str(key).startswith(_HIDDEN_PREFIX)}
    configurable = tup.config.get("configurable") or {}
    return ThreadStateResponse(
        values=values,
        checkpoint_id=configurable.get("checkpoint_id"),
        checkpoint_ns=configurable.get("checkpoint_ns") or "",
    )


def _status_value(status: RunStatus | str) -> str:
    return status.value if isinstance(status, RunStatus) else str(status)


async def read_run_terminal_state(services: RunServices, record: RunRecord) -> RunStateResponse:
    """/wait 专用。坐标只来自这条 run 收尾时写入的 metadata["checkpoint_id"]（M3 §6.2）。

    缺键时返回空 values，不退回 thread 当前头：复用的 run 结束之后，头可能已经是下一次 run 的。
    """
    checkpoint_id = (record.metadata or {}).get("checkpoint_id")
    status = _status_value(record.status)
    if not isinstance(checkpoint_id, str):
        return RunStateResponse(
            values={}, checkpoint_id=None, checkpoint_ns="",
            run_id=record.run_id, status=status, error=record.error,
        )
    state = await read_thread_state(services, record.thread_id, checkpoint_id=checkpoint_id)
    return RunStateResponse(
        values=state.values,
        checkpoint_id=state.checkpoint_id,
        checkpoint_ns=state.checkpoint_ns,
        run_id=record.run_id,
        status=status,
        error=record.error,
    )
```


| 参数              | 类型            | 谁给                              | 逻辑                                             |
| --------------- | ------------- | ------------------------------- | ---------------------------------------------- |
| `services`      | `RunServices` | 路由的 `Depends(get_run_services)` | 只用 `run_context.checkpointer`                  |
| `thread_id`     | `str`         | 路径，或 `record.thread_id`         | 已通过 `ThreadId`                                 |
| `checkpoint_id` | `str | None`  | query；`None` 表示当前头              | 有值但 saver 返回空 → `CheckpointNotFound`           |
| `record`        | `RunRecord`   | `wait_for_run_completion` 的返回值  | 读 `metadata["checkpoint_id"]`、`status`、`error` |


空 thread（还没有任何 checkpoint）走 `read_thread_state(..., checkpoint_id=None)`，返回 `values={}`，HTTP 200。
这和「id 写错」不同：query 或 metadata 里带了 `checkpoint_id` 但 saver 返回空，必须 404 `checkpoint_not_found`。

---

## 6. 错误映射

### 6.1 一张表，所有异常都在这

**路由不写 `try/except` 去拼状态码**（唯一例外见 §6.4）。所有领域异常在这里查表：


| 异常                                           | HTTP | `code`                      | 谁抛                                                                    |
| -------------------------------------------- | ---- | --------------------------- | --------------------------------------------------------------------- |
| `RequestValidationError`（pydantic / FastAPI） | 422  | `invalid_request`           | 绑定参数时框架自动抛                                                            |
| `Unauthenticated`                            | 401  | `unauthenticated`           | AuthMiddleware：白名单外没有有效会话、PAT 或内部 token（M9 §2.12）                  |
| `RunPermissionDenied`                        | 403  | `permission_denied`         | `_require_cancel_capability`（M3 §4.3）                                 |
| `RunRequestInvalid`                          | 422  | `invalid_run_request`       | `start_run` 第 2–3、7、9 步                                               |
| `ModelNotAllowed`                            | 400  | `model_not_allowed`         | `start_run` 第 4 步：这次 run 不许用该模型                                      |
| `ModelNotFound`                              | 404  | `model_not_found`           | `GET /api/models/{name}`（M1 §7.4）。不继承 `StartRunError`，模型路由不 import 执行栈 |
| `UploadInvalid`                              | 422  | `invalid_upload`            | 上传文件名、空列表、单次个数（M2 §10.4）                                        |
| `UploadTooLarge`                             | 413  | `upload_too_large`          | 单文件或单次合计超限（M2 §10.2）                                             |
| `UploadLimitReached`                         | 409  | `upload_limit_reached`      | 该 thread 的上传个数到顶（M2 §10.2）                                        |
| `UploadNotFound`                             | 404  | `upload_not_found`          | `DELETE .../uploads/{filename}`                                      |
| `ArtifactPathInvalid`                        | 422  | `invalid_artifact_path`     | 产物路径没过 §5.1，或不在 `outputs` 下（M2 §10.2）                            |
| `ArtifactNotFound`                           | 404  | `artifact_not_found`        | 路径合法但不是现存文件（M2 §10.4）                                            |
| `InvalidStateUpdate`                         | 422  | `invalid_state_update`      | `POST .../state` 的 `values` 含图不认识的通道（M6 §7.5）                      |
| `CompactionDisabled`                         | 409  | `compaction_disabled`       | `summarization.enabled` 为假时的手动 compact（M6 §7.5）                     |
| `SummarizationFailed`                        | 500  | `summarization_failed`      | 手动 compact 的摘要模型失败；detail 不含模型原文（M6 §7.5）                         |
| `McpServerNotFound`                          | 404  | `mcp_server_not_found`      | `PATCH /api/mcp/config` 的 `name` 不在文件里（M7 §2.4）                       |
| `SkillNotFound`                              | 404  | `skill_not_found`           | `PATCH /api/skills/{name}` 的目录未被扫描收录（M7 §2.4）                       |
| `StdioCommandRejected`                       | 422  | `stdio_command_rejected`    | 启用 stdio 时 `command` 不在允许列表；detail 无命令行（M7 §2.4、§5.7）            |
| `InvalidExtensionsConfig`                    | 422  | `invalid_extensions_config` | 打码串没有原值可还原，或写入校验失败（M7 §2.4）                                    |
| `InvalidCredentials`                         | 401  | `invalid_credentials`       | 登录或改密失败；不区分邮箱不存在和密码错误（M9 §2.12）                                  |
| `LoginRateLimited`                           | 429  | `login_rate_limited`        | 同一 peer IP 超过 `max_login_attempts`（M9 §2.5、§2.12）                    |
| `AdminAlreadyExists`                         | 409  | `admin_already_exists`      | 已有 admin 再 `POST /initialize`（M9 §2.12）                               |
| `RegistrationClosed`                         | 403  | `registration_closed`       | `auth.allow_registration` 为假（M9 §2.12）                                |
| `SessionRequired`                            | 403  | `session_required`          | PAT 打了只允许会话的路由，例如改密（M9 §2.12）                                    |
| `InvalidSchedule`                            | 422  | `invalid_schedule`          | cron / 时区 / `run_at` 不合法（M9 §4.10）                                  |
| `ScheduledTaskNotFound`                      | 404  | `scheduled_task_not_found`  | 任务不存在或不属于 caller（M9 §4.10）                                         |
| `TaskActive`                                 | 409  | `task_active`               | 有 queued/launching/running 的 occurrence 时改或删任务（M9 §4.7、§2.12）      |
| `MemoryNotSupported`                         | 501  | `memory_not_supported`      | 该 memory kind 未注册 service，或后端能力不支持（M10 §2.12）                    |
| `InvalidMemoryProfile`                       | 422  | `invalid_memory_profile`    | 画像字段路径/类型/长度非法，或 tombstone 写入失败（M10 §2.12）                     |
| `MemoryConflictError`                        | 409  | `memory_conflict`           | `agentkit` 的 profile/record revision 并发冲突；不继承 Gateway 异常（M10 §2.12） |
| `MemoryCorrupted`                            | 500  | `memory_corrupted`          | detail 固定为 `Memory storage is unreadable`，无路径（M10 §2.12）            |
| `RecordNotFound`                             | 404  | `record_not_found`          | `DELETE /api/memory/items/{kind}/{record_id}`（M10 §2.12）                 |
| `ThreadNotFound`                             | 404  | `thread_not_found`          | 第 5 步；`/join` 的 thread 核对                                             |
| `AssistantNotFound`                          | 404  | `assistant_not_found`       | 第 6 步                                                                 |
| `CheckpointNotFound`                         | 404  | `checkpoint_not_found`      | 第 10 步                                                                |
| `ThreadBusy`                                 | 409  | `thread_busy`               | 第 13 步准入；`DELETE /threads/{id}`                                       |
| `IdempotencyKeyReused`                       | 409  | `idempotency_key_reused`    | 幂等键命中且 `input` 不一致（M3 §5.7）                                           |
| `RunNotFound`                                | 404  | `run_not_found`             | `/join`、`/cancel`（M3 §8.2、§9.1）                                       |
| `RunNotLocal`                                | 409  | `run_not_local`             | 记录在别的进程手里（M9 §3.6）                                                    |
| `RunNotCancellable`                          | 409  | `run_not_cancellable`       | 已经是终态（M3 §9.2）                                                        |
| `ThreadOwnershipConflictError`               | 409  | `thread_ownership_conflict` | `agentkit/.../thread_meta` 的 `create`（M4 §4.4）；**不是** `StartRunError` |
| `InvalidMetadataFilterError`                 | 400  | `invalid_metadata_filter`   | `agentkit/persistence/json_compat.py`（M4 §7.1）；**不是** `StartRunError` |
| `InvalidCursor`                              | 422  | `invalid_cursor`            | store 解析游标失败（`agentkit/persistence/base.py`）                          |
| `ClientDisconnected`                         | 499  | `client_disconnected`       | `wait_for_run_completion`（M3 §8.3）；是 `StartRunError`                  |
| `Exception`（兜底）                              | 500  | `internal_error`            | 任何没被上面接住的                                                             |


### 6.2 四个 handler

持久化异常定义在 `agentkit/**`，不能继承 `StartRunError`（那会让 harness import `app`）。
它们的 HTTP 映射只存在于 `errors.py` 的 `_PERSISTENCE` 表。Gateway 自己的异常则把 `status_code` / `code` 放在类上（M3 §4.2，文件 `app/gateway/services.py`）。

```python
# app/gateway/services.py
class StartRunError(Exception):
    status_code: ClassVar[int] = 400
    code: ClassVar[str] = "bad_request"

class RunPermissionDenied(StartRunError):
    status_code, code = 403, "permission_denied"
class RunRequestInvalid(StartRunError):
    status_code, code = 422, "invalid_run_request"
class ModelNotAllowed(StartRunError):
    status_code, code = 400, "model_not_allowed"
class ThreadNotFound(StartRunError):
    status_code, code = 404, "thread_not_found"
class AssistantNotFound(StartRunError):
    status_code, code = 404, "assistant_not_found"
class CheckpointNotFound(StartRunError):
    status_code, code = 404, "checkpoint_not_found"
class ThreadBusy(StartRunError):
    status_code, code = 409, "thread_busy"
class IdempotencyKeyReused(StartRunError):
    status_code, code = 409, "idempotency_key_reused"
class RunNotFound(StartRunError):
    status_code, code = 404, "run_not_found"
class RunNotLocal(StartRunError):
    status_code, code = 409, "run_not_local"
class RunNotCancellable(StartRunError):
    status_code, code = 409, "run_not_cancellable"
class ClientDisconnected(StartRunError):
    status_code, code = 499, "client_disconnected"
```

```python
# agentkit/persistence/base.py
class InvalidCursor(ValueError):
    """游标无法解析，或与本次 order 不一致。路由不捕获它。"""
```

```python
# app/gateway/errors.py
_PERSISTENCE: dict[type[BaseException], tuple[int, str]] = {
    ThreadOwnershipConflictError: (409, "thread_ownership_conflict"),   # agentkit/persistence/thread_meta/base.py
    InvalidMetadataFilterError: (400, "invalid_metadata_filter"),       # agentkit/persistence/json_compat.py
    InvalidCursor: (422, "invalid_cursor"),                             # agentkit/persistence/base.py
    MemoryConflictError: (409, "memory_conflict"),                      # agentkit/agents/memory/manager.py（M10 §2.12）
}


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StartRunError, _handle_domain_error)
    app.add_exception_handler(ModelNotFound, _handle_domain_error)  # M1 §7.4
    for exc_type in (
        UploadInvalid, UploadTooLarge, UploadLimitReached, UploadNotFound, ArtifactPathInvalid, ArtifactNotFound,  # M2 §10.4
        InvalidStateUpdate, CompactionDisabled, SummarizationFailed,  # M6 §7.5
        McpServerNotFound, SkillNotFound, StdioCommandRejected, InvalidExtensionsConfig,  # M7 §2.4
        Unauthenticated, InvalidCredentials, LoginRateLimited, AdminAlreadyExists, RegistrationClosed, SessionRequired,  # M9 §2.12
        InvalidSchedule, ScheduledTaskNotFound, TaskActive,
        MemoryNotSupported, InvalidMemoryProfile, MemoryCorrupted, RecordNotFound,  # M10 §2.12
    ):
        app.add_exception_handler(exc_type, _handle_domain_error)  # 类上都有 status_code / code
    for exc_type in _PERSISTENCE:
        app.add_exception_handler(exc_type, _handle_persistence_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected)


def _trace_id(request: Request) -> str:
    """错误体里的 trace_id 永远是非空字符串。中间件正常时直接读；中间件没跑到（启动期异常）就补一个。"""
    current = getattr(request.state, "trace_id", None)
    if isinstance(current, str) and current:
        return current
    generated = uuid4().hex
    request.state.trace_id = generated
    return generated


def _error_body(request: Request, *, detail: str, code: str, errors: list[dict] | None = None) -> dict:
    body: dict = {"detail": detail, "code": code, "trace_id": _trace_id(request)}
    if errors:
        body["errors"] = errors                       # 只在 422 上出现
    return body


async def _handle_domain_error(request: Request, exc: Exception) -> JSONResponse:
    """类上自带 status_code 和 code 的异常走这里。StartRunError 在 services.py；其余登记在 errors.py（M1、M2 §10.4、M6 §7.5、M7 §2.4）。"""
    return JSONResponse(status_code=exc.status_code, content=_error_body(request, detail=str(exc), code=exc.code))


async def _handle_persistence_error(request: Request, exc: Exception) -> JSONResponse:
    """harness 异常没有 HTTP 字段，只在这张表里翻译一次。沿 MRO 找，子类不用再登记。"""
    for cls in type(exc).__mro__:
        if cls in _PERSISTENCE:
            status, code = _PERSISTENCE[cls]
            return JSONResponse(status_code=status, content=_error_body(request, detail=str(exc), code=code))
    raise RuntimeError(f"{type(exc).__name__} was registered without a mapping")


async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """pydantic 的默认形状（list[dict]）不对外：统一压成 {detail, code, errors}。"""
    return JSONResponse(
        status_code=422,
        content=_error_body(
            request,
            detail="Request validation failed",
            code="invalid_request",
            errors=[{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()],
        ),
    )


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content=_error_body(request, detail="Internal server error", code="internal_error"))
```

三条规则：

- **错误体只有一种形状** `{detail, code, trace_id}`（422 多一个 `errors`）。
客户端因此只需要写一个错误解析函数；M3 §4.3 的领域异常设计就是为了这条能成立。
- **500 不带任何内部信息。** 异常类型、栈、SQL 都不进响应——`trace_id` 才是给用户报障用的，
运维拿它去日志里捞完整栈。
- `**ClientDisconnected` 映射到 499 而不是 500。** 客户端已经走了，这个响应发不出去；
但把它记成 499 能让日志和指标区分"客户端主动断开"和"服务端出错"。

### 6.3 `code` 是给机器看的，`detail` 是给人看的


| 字段         | 谁读         | 规则                                                         |
| ---------- | ---------- | ---------------------------------------------------------- |
| `code`     | 客户端分支逻辑    | 稳定的 snake_case 常量，**永不变更**；新语义加新 code，不改老的                 |
| `detail`   | 人、日志       | 可以改文案；不保证稳定；不含内部标识（除了 `trace_id`）                          |
| `trace_id` | 用户报障       | 由 trace 中间件生成，同时出现在响应头 `X-Trace-Id`（§12）                   |
| `errors`   | 422 的字段级定位 | `loc` 用 pydantic 的路径数组（`["body", "input", "messages", 0]`） |


### 6.4 路由里唯一允许的 `try/except`

只有两处，都是"提前退出路径"，不是"错误处理"：

```python
# app/gateway/routers/thread_runs.py
@router.post("/{thread_id}/runs/stream")
async def stream_run(...) -> StreamingResponse:
    launch = await start_run(...)               # 领域异常往上传，由 §6.2 处理
    return sse_response(services, launch, request)   # 断开语义在 sse_consumer 的 finally 里（M3 §8.2）
```

1. **流式路由的 `finally`** 在 `sse_consumer` 里，不在路由里（M3 §8.1）——
  路由一旦 `try` 住生成器，就等于把订阅的生命周期拦在了 HTTP 层。
2. `**/wait` 的 `ClientDisconnected` 要往上传**，让 §6.2 编成 499；
  在路由里 catch 住然后"返回当前状态"，就是 M3 §8.3 那条硬规则禁止的半截快照。

---

## 7. 路由注册与路径规范

### 7.1 注册顺序：静态段必须排在参数段前面

```python
# app/gateway/routers/thread_runs.py
router = APIRouter(prefix="/threads", tags=["runs"])

@router.post("/{thread_id}/runs/stream")            # ★ 静态段：必须先声明 → /api/threads/{id}/runs/stream
@router.post("/{thread_id}/runs/wait")              # ★ 同上
@router.post("/{thread_id}/runs")                   # 集合
@router.get("/{thread_id}/runs")                    # 集合
@router.get("/{thread_id}/runs/{run_id}")           # 参数段：最后
@router.get("/{thread_id}/runs/{run_id}/join")
@router.post("/{thread_id}/runs/{run_id}/cancel")
```

```python
# app/gateway/routers/threads.py
router = APIRouter(prefix="/threads", tags=["threads"])

@router.post("/search")                             # ★ 必须在 /{thread_id} 之前
@router.post("")
@router.get("")
@router.get("/{thread_id}/state")
@router.post("/{thread_id}/state")                 # M6 §7.5
@router.post("/{thread_id}/compact")
@router.get("/{thread_id}/history")
@router.get("/{thread_id}/branches")
@router.get("/{thread_id}/goal")                   # M10 §2.12
@router.put("/{thread_id}/goal")
@router.delete("/{thread_id}/goal")
@router.get("/{thread_id}/messages")
@router.get("/{thread_id}")
@router.patch("/{thread_id}")
@router.delete("/{thread_id}")
```

`prefix="/threads"` 写在 router 上，`create_app` 只加 `/api`。写反了——把 `/threads` 只放在 `include_router` 的 prefix、decorator 用 `/{thread_id}/runs`——得到的路径是 `/api/{thread_id}/runs`，线程这个词从 URL 里消失，SDK 对不上。

**反过来的症状很隐蔽**：`/runs/stream` 会被 `/runs/{run_id}` 吃掉，
`run_id="stream"`，然后 `RunStore.get("stream")` 返回 `None` → 404。
请求是通的、日志是干净的，只有客户端发现"流式接口没了"。

```python
# app/gateway/app.py
def create_app() -> FastAPI:
    app = FastAPI(title="agentkit gateway", version="1.0.0", redirect_slashes=False)
    install_middleware(app)          # §12：顺序在这里定
    install_error_handlers(app)      # §6.2
    app.include_router(threads.router, prefix="/api")          # router 自带 prefix=/threads、tags
    app.include_router(thread_runs.router, prefix="/api")      # router 自带 prefix=/threads
    app.include_router(runs.router, prefix="/api")             # router 自带 prefix=/runs（§3.3）
    app.include_router(uploads.router, prefix="/api")          # M2 §10
    app.include_router(artifacts.router, prefix="/api")        # M2 §10
    app.include_router(models.router, prefix="/api")           # M1
    app.include_router(mcp.router, prefix="/api")              # M7 §2.4，prefix=/mcp
    app.include_router(skills.router, prefix="/api")           # M7 §2.4，prefix=/skills
    app.include_router(auth_router, prefix="/api")             # M9 §2.12，prefix=/v1/auth
    app.include_router(scheduled_tasks.router, prefix="/api")  # M9 §4.10，prefix=/scheduled-tasks
    app.include_router(channels.router, prefix="/api")         # M9 §5.9，prefix=/channels
    app.include_router(memory.router, prefix="/api")           # M10 §2.12，prefix=/memory
    app.include_router(health.router)                          # 不带 /api；路径 /healthz
    return app
```


| 规则                     | 为什么                                                           |
| ---------------------- | ------------------------------------------------------------- |
| 业务路由统一挂 `/api` 前缀      | 与 nginx 的 location 划分、SDK 的 base_url 对齐；健康检查留在根路径（`/healthz`） |
| `tags` 必填              | OpenAPI 文档直接按 tag 分组，前端按组生成客户端                                |
| 注册顺序：生命周期 → 管理类 → 健康检查 | 生命周期路由带 `request.state` 依赖（§12），必须在中间件之后                      |
| 一条路由只在一个 router 里定义    | 同一 path 注册两次，FastAPI 用**先注册**的那个，第二次无声失效                      |


### 7.2 路径参数：白名单，不是"任意字符串"

```python
# app/gateway/models/request.py
ThreadId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
RunId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
```


| 情况                   | 结果                         | 为什么不是另一个                       |
| -------------------- | -------------------------- | ------------------------------ |
| 格式非法（含 `/`、`..`、空格）  | **422** `invalid_request`  | 这是"请求本身不合法"，不是"资源不存在"          |
| 格式合法但库里没有            | **404** `thread_not_found` | 只有查过库才知道不存在                    |
| `run_id` 属于别的 thread | **404**（不是 403）            | 不泄漏"这个 run 存在但不属于你"——属主过滤在查询里做 |


这三个 id 会进文件路径（M2 工作区）、日志、checkpoint 的 config。白名单在这里是**唯一的收口点**。

### 7.3 尾部斜杠：显式 404，不要 307

`redirect_slashes=False`。FastAPI 默认会把 `/api/threads/` 307 重定向到 `/api/threads`；
307 保留 method 和 body，但很多 SDK 和代理不跟随——症状是"某些客户端发消息静默失败"。
显式 404 至少能被日志和客户端立刻看见。

---

## 8. 鉴权与能力门

### 8.1 身份只有一个来源

```
请求 → AuthMiddleware → request.state.auth = AuthResult(user_id, scopes, role, kind)
                            ↓
                    caller_from_request()  → RunCaller   （M3 §4.1）
                            ↓
                    start_run(..., caller=caller)        （第 1、12 步用它）
```

**body 里的任何身份字段都不看。** `context.user_id`、`metadata.owner_id`、
`configurable.auth_user` 一律在第 3 步丢弃（M3 §4.7 的白名单表）——
"客户端能声明自己是谁"的 API 不需要被攻击，它自己就会串号。

### 8.2 能力表：每条路由要什么


| 能力               | 覆盖的路由                                                                                |
| ---------------- | ------------------------------------------------------------------------------------ |
| `runs:create`    | `POST .../runs`、`.../runs/stream`、`.../runs/wait`、`POST /api/runs/stream`、`.../wait`。定时任务还要同时有 `threads:write`（M9 §4.10） |
| `runs:read`      | `GET .../runs`、`GET .../runs/{run_id}`、`GET .../runs/{run_id}/join`（M9 §2.12） |
| `runs:cancel`    | `POST .../runs/{run_id}/cancel`；**以及** `multitask_strategy=interrupt/rollback` 的准入   |
| `threads:read`   | thread 详情、`GET .../state`、`/messages`、threads 列表、`GET .../uploads/list`、`GET .../artifacts/{path}`（M2 §10）、`GET .../history`、`GET .../branches`（M6 §7.5）、`GET .../goal`（M10 §2.12） |
| `threads:write`  | `POST /api/threads`、`PATCH`、`POST .../uploads`、`DELETE .../uploads/{filename}`（M2 §10）、`POST .../state`、`POST .../compact`（M6 §7.5）、`PUT/DELETE .../goal`（M10 §2.12）。定时任务路由要同时有这一项和 `runs:create`（M9 §4.10） |
| `memory:read`    | `GET /api/memory`、`/search`、`/items`、`/profile`、`/conflicts`、`/export`（M10 §2.12）       |
| `memory:write`   | `PATCH/DELETE /api/memory/profile*`、`DELETE /api/memory/items/*`、`POST /api/memory/items/*/remember`、`DELETE /api/memory`（M10 §2.12） |
| `memory:admin`   | `POST /api/memory/maintenance/run`（M10 §2.12）                                             |
| `threads:delete` | `DELETE /api/threads/{id}`                                                           |
| `models:read`    | `GET /api/models`、`GET /api/models/{model_name}`（M1 §7；选择器要读，不是 admin 写接口） |
| `skills:read`    | `GET /api/skills`、`PUT /api/skills/{name}/preference`（M7 §2.4；斜杠菜单要读，个人开关也只影响自己） |
| `admin`          | `GET/PATCH /api/mcp/config`、`POST /api/mcp/cache/reset`、`POST /api/skills/reload`、`PATCH /api/skills/{name}`（全局开关）（M7 §2.4）、`POST /api/channels/{name}/restart`（M9 §5.9） |
| `skills:create` / `mcp:create` | **M7 不实现**：技能的创建/上传、MCP 服务器的新建/删除/整份替换（`PUT /api/mcp/config`）。留给后续阶段，授予业务管理员 |
| `channels:read`  | `GET /api/channels`（M9 §5.9）                                                            |
| `channels:write` | `POST/DELETE /api/channels/{name}/bind`（M9 §5.9）                                          |


**join 只要 `runs:read`。** 一个只读令牌连上再断开必须**不能**取消别人的 run
（M3 §8.2 的安全边界）；观察端 `apply_on_disconnect=False` 是这条能力的实现。

### 8.3 代码：能力门只有一个实现

```python
# app/gateway/deps.py
def require_scope(scope: str) -> Callable[[Request], RunCaller]:
    """返回一个 FastAPI 依赖：PAT 缺 scope → 403；会话用户 / 内部调用方直接放行（M9 §2.11）。"""
    def _dep(request: Request) -> RunCaller:
        caller = caller_from_request(request)
        if not caller.allows(scope):
            raise RunPermissionDenied(f"missing scope: {scope}")
        return caller
    return _dep


# app/gateway/routers/thread_runs.py
@router.post("/{thread_id}/runs/{run_id}/cancel")
async def cancel_run(
    thread_id: ThreadId, run_id: RunId, request: Request,
    body: RunCancelRequest | None = None,
    caller: RunCaller = Depends(require_scope("runs:cancel")),     # ★ 门在依赖里
) -> CancelResponse: ...
```

- **门写在依赖里，不写在函数体第一行。** 写在函数体里时，一个 `return` 提前分支就能绕过它；
依赖是声明式的，OpenAPI 里也看得见。
- `**multitask_strategy` 那一处不能漏。** 准入时带 `interrupt`/`rollback` 等于停掉别人的 run，
所以 `start_run` 第 1 步也要查同一个能力（M3 §4.3 第 1 步）。两条路径共用
`_require_cancel_capability`，别写第二份判断。

---

## 9. 分页与过滤

### 9.1 参数定义

`GET /api/threads` 与 `GET .../runs` 共用 `PageParams`。
`GET .../messages` **不用**不透明 `cursor`：游标就是 `seq`（整数），参数名是 `before_seq` / `after_seq`。
`POST /api/threads/search` 的分页字段在 body 里（`ThreadSearchRequest`），不在 query 里。


| 参数                         | 类型 / 约束                    | 默认     | 用在                             | 所属文件                                                   |
| -------------------------- | -------------------------- | ------ | ------------------------------ | ------------------------------------------------------ |
| `limit`                    | `int`，`1..200`             | `50`   | 三个列表 + search body             | `pagination.py` 的 `Query`；search 在 `models/request.py` |
| `cursor`                   | `str`，`max_length=256`，不透明 | `None` | threads 列表、runs 列表、search body | 同上。解析在 store，失败抛 `InvalidCursor`                       |
| `order`                    | `Literal["asc","desc"]`    | `desc` | threads / runs / search        | 消息列表没有这个参数，固定升序                                        |
| `status`                   | 逗号分隔的 `RunStatus` 值        | `None` | 只在 `GET .../runs`              | `thread_runs.py::parse_status_filter`                  |
| `metadata.<key>`           | query 上的标量文法，见下            | 不传     | 只在 `GET .../runs`              | `pagination.py::metadata_filters`                      |
| `before_seq` / `after_seq` | `int`，`ge=0`               | `None` | 只在 `GET .../messages`          | `pagination.py::message_page_params`                   |


### 9.2 解析与信封：一份实现

```python
# app/gateway/pagination.py
DEFAULT_LIMIT, MAX_LIMIT = 50, 200


@dataclass(frozen=True, slots=True)
class PageParams:
    limit: int
    cursor: str | None
    order: Literal["asc", "desc"]

    @property
    def ascending(self) -> bool:
        return self.order == "asc"


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    order: Annotated[Literal["asc", "desc"], Query()] = "desc",
) -> PageParams:
    """作为一个 FastAPI 依赖用：`params: PageParams = Depends(page_params)`。
    约束写在 `Query` 上，越界由框架编成 422（§6.1）。"""
    return PageParams(limit=limit, cursor=cursor, order=order)


def page_response(items: Sequence[T], *, has_more: bool, next_cursor: str | None) -> Page[T]:
    """信封只有一个实现。游标由调用方从 store 行上取来；has_more 为假时这里强制写成 None。"""
    return Page(data=list(items), has_more=has_more, next_cursor=next_cursor if has_more else None)


@dataclass(frozen=True, slots=True)
class MessagePageParams:
    limit: int
    before_seq: int | None
    after_seq: int | None


def message_page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    before_seq: Annotated[int | None, Query(ge=0)] = None,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
) -> MessagePageParams:
    """两个游标同时出现没有定义：是「这一页的上界」还是「下界」？直接 422。"""
    if before_seq is not None and after_seq is not None:
        raise RunRequestInvalid("before_seq and after_seq are mutually exclusive")
    return MessagePageParams(limit=limit, before_seq=before_seq, after_seq=after_seq)


def metadata_filters(request: Request) -> dict[str, JsonScalar]:
    """读取 `metadata.<key>=<scalar>`。query 值全是字符串，按下表还原类型，不让 store 再猜。"""
    found: dict[str, JsonScalar] = {}
    for key, raw in request.query_params.multi_items():
        if not key.startswith("metadata."):
            continue
        name = key.removeprefix("metadata.")
        if not name or name in found:
            raise RunRequestInvalid(f"Invalid metadata filter: {key}")
        found[name] = _parse_metadata_scalar(raw)
    return found


def _parse_metadata_scalar(raw: str) -> JsonScalar:
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null":
        return None
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw)
    return raw
```

`metadata_filters` 的文件是 `app/gateway/pagination.py`。`JsonScalar` 从 `app/gateway/models/request.py` import。
`"true"` 是布尔，`"True"` 是字符串——大小写敏感，避免 `"True"` 和 `true` 滤出不同的行还没人知道为什么。


| 决策                       | 取值                   | 理由                                     |
| ------------------------ | -------------------- | -------------------------------------- |
| 越界 `limit`               | 422                  | 明确的拒绝比静默截断可发现；客户端能立刻改                  |
| 游标格式                     | **不透明字符串**           | 一旦客户端开始解析它（Hyrum 定律），换分页实现就是破坏性变更      |
| `has_more` 怎么来           | 仓库层多取一条（`limit + 1`） | 少一次 `COUNT(*)`；`COUNT` 在长会话上是全表扫       |
| `next_cursor` 何时为 `None` | `has_more=False` 时   | 客户端只要判断 `has_more` 就够，"最后一页还有一个游标"是常见坑 |


### 9.3 为什么是 keyset，不是 offset


| 方案             | 翻到第 100 页的代价                    | 并发插入时                       |
| -------------- | ------------------------------- | --------------------------- |
| `?offset=5000` | 数据库仍要扫过前 5000 行                 | 新行插在前面 → **同一行出现两次、有的行被跳过** |
| keyset 游标      | 恒定（走 `(created_at, run_id)` 索引） | 稳定：游标是"我读到过哪一行"的坐标          |


会话列表和消息列表都是**只增不改、最新在前**的数据，offset 分页的两条缺点会同时命中。
M4 §4.2 已经用 `(created_at, run_id)` 元组比较实现了 keyset；路由层只负责把
游标**原样透传**，不做任何解析。

### 9.4 store 侧：路由新加的那个重载

M4 的 `ThreadMetaStore.search` 用 `offset`。路由不暴露 `offset`，所以 store 增加一个 keyset 方法，旧的 `search` 留给还没改的内部调用方，HTTP 不走它。

```python
# agentkit/persistence/thread_meta/base.py
async def search_page(
    self,
    *,
    user_id: str | None,
    metadata: Mapping[str, JsonScalar] | None = None,
    status: str | None = None,
    limit: int = 51,                 # 路由传入的是 PageParams.limit + 1
    cursor: str | None = None,
    order: Literal["asc", "desc"] = "desc",
) -> list[dict]:
    """每行多一个不入库的键 `cursor`，供路由放进 next_cursor。比较键是 (updated_at, thread_id)。"""
```

```python
# agentkit/persistence/run/sql.py
# list_by_thread 在 M4 签名上增加三个关键字参数，默认值保持旧行为：
#   order: Literal["asc", "desc"] = "desc"
#   statuses: frozenset[str] | None = None
#   metadata: Mapping[str, JsonScalar] | None = None
# 返回的 dict 同样多一个 `cursor` 键。编码仍是 f"{iso_utc(created_at)}|{run_id}"，
# 前面再加 order 的一个字符（"d" / "a"），这样客户端中途改 order 会在 _parse_cursor 里变成 InvalidCursor，而不是漏行。
```

`RunStore.list_by_thread` 的所属文件是 `agentkit/persistence/run/sql.py`（接口声明在 `agentkit/runtime/runs/store/base.py`）。游标解析失败抛 `InvalidCursor`（`agentkit/persistence/base.py`），路由不 `except`。

---

## 10. 幂等

### 10.1 头定义


| 头                 | 类型 / 约束                 | 谁生成            | 说明                                  |
| ----------------- | ----------------------- | -------------- | ----------------------------------- |
| `Idempotency-Key` | `str`，`1..255`，可见 ASCII | **客户端**，一次意图一个 | 重试复用同一个；换一个 key 就是"新的一次意图"（会真的再跑一轮） |


```python
# app/gateway/idempotency.py
_VISIBLE_ASCII = re.compile(r"^[\x21-\x7e]{1,255}$")


def parse_idempotency_key(
    key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)] = None,
) -> str | None:
    """只校验形状。sha256 与 scope 在 start_run 里（M3 §5.7，文件 app/gateway/services.py）。"""
    if key is None:
        return None
    key = key.strip()
    if not _VISIBLE_ASCII.match(key):
        raise RunRequestInvalid("Idempotency-Key must be 1..255 visible ASCII characters")
    return key
```


| 输入                   | 结果                | `code`                                                      |
| -------------------- | ----------------- | ----------------------------------------------------------- |
| 头不存在                 | `None`，走非幂等准入     | ——                                                          |
| `" abc "`            | `"abc"`（strip 之后） | ——                                                          |
| `""`、纯空白、含空格、非 ASCII | 422               | `invalid_run_request`                                       |
| 超过 255               | 422               | `invalid_request`（FastAPI 的 `Header(max_length=255)`，进不了函数） |


路由把返回值原样放进 `start_run(..., idempotency_key=)`。路由里出现 `hashlib` 就是 §13.3 要拦的。

### 10.2 四种情形


| 情形                   | 服务端行为                                    | 响应                             |
| -------------------- | ---------------------------------------- | ------------------------------ |
| 首次                   | 准入新 run                                  | 正常形状（202 / SSE）                |
| 同 key + 同 `input` 重放 | 复用那条 run；**绝不再挂一个 worker**（M3 §4.5、§5.7） | 与首次同形状；`RunLaunch.reused=True` |
| 同 key + 不同 `input`   | 抛 `IdempotencyKeyReused`                 | 409 `idempotency_key_reused`   |
| 并发同 key              | 输的那个在 `get_by_idempotency_key` 里拿到赢家的记录  | 与首次同形状（两边都不报错）                 |


**键自带 scope。** `start_run` 用 `"run:" + sha256(caller.kind \| user_id \| thread_id \| key)`
（M3 §5.7），所以 HTTP 客户端和内部调用方用同一个字符串不会串号，两个用户的 `"1"` 也不会。

### 10.3 保留期

键跟着 run 行走：`runs` 行在，键就有效；M4 的保留策略清掉行时键随之失效
（M4 §4.2 的 `uq_runs_idempotency_key`）。**保留期必须长于"最长重试链"**——
客户端重试、代理重发、调度器补跑，任何一条能在之后重新投递同一意图的路径，
都必须落在键的有效期内。

### 10.4 哪几条路由读这个头


| 路由                                            | 读 `Idempotency-Key` | 原因                                                                                                                             |
| --------------------------------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `POST /api/threads/{id}/runs`                 | 是                   | 客户端拿着同一个 thread 重试                                                                                                             |
| `POST .../runs/stream`                        | 是                   | 与上一条共享 scoped key（M3 §5.7）                                                                                                     |
| `POST .../runs/wait`                          | 是                   | 同上                                                                                                                             |
| `POST /api/runs/stream`、`POST /api/runs/wait` | **否**               | 每次生成新 `thread_id`（除非 body 里带了 configurable 的那个）。同一个头字符串会落进不同 thread，看起来像命中、实际上是两次创建。重试必须改打 `Content-Location` 指回的 thread 作用域路由 |
| 其它                                            | 否                   | 读了也不会用，OpenAPI 里就不声明                                                                                                           |


---

## 11. 流式路由

### 11.1 三个入口的差别


| 路由                           | `apply_on_disconnect` | 断开时的行为                                       | 返回                  |
| ---------------------------- | --------------------- | -------------------------------------------- | ------------------- |
| `POST .../runs/stream`       | `True`                | 按 `on_disconnect` 取消（创建端语义）                  | `text/event-stream` |
| `POST .../runs/wait`         | `True`                | 先取消，再抛 `ClientDisconnected`（499），**不返回半截快照** | JSON（终态 + 快照）       |
| `GET .../runs/{run_id}/join` | `False`               | 只结束这条订阅；run 继续跑                              | `text/event-stream` |


### 11.2 三个 handler

参数表与能力门在 §3.3。这里只保留和断开语义有关的调用。`idempotency_key` 的类型是 `Depends(parse_idempotency_key)`（`app/gateway/idempotency.py`）。

```python
# app/gateway/routers/thread_runs.py
@router.post("/{thread_id}/runs/stream")
async def stream_run(
    thread_id: ThreadId,
    body: RunCreateRequest,
    request: Request,
    idempotency_key: Annotated[str | None, Depends(parse_idempotency_key)],
    caller: RunCaller = Depends(require_scope("runs:create")),
) -> StreamingResponse:
    services = get_run_services(request)
    launch = await start_run(
        body, thread_id=thread_id, caller=caller, services=services, idempotency_key=idempotency_key,
    )
    return sse_response(services, launch, request)                 # apply_on_disconnect 默认 True


@router.post("/{thread_id}/runs/wait")
async def wait_run(
    thread_id: ThreadId,
    body: RunCreateRequest,
    request: Request,
    idempotency_key: Annotated[str | None, Depends(parse_idempotency_key)],
    caller: RunCaller = Depends(require_scope("runs:create")),
) -> RunStateResponse:
    services = get_run_services(request)
    launch = await start_run(
        body, thread_id=thread_id, caller=caller, services=services, idempotency_key=idempotency_key,
    )
    final = await wait_for_run_completion(services, launch.record, request)   # 断开 → ClientDisconnected，499
    return await read_run_terminal_state(services, final)                     # app/gateway/state_reader.py，不读 thread 当前头


@router.get("/{thread_id}/runs/{run_id}/join")
async def join_run(
    thread_id: ThreadId, run_id: RunId, request: Request,
    caller: RunCaller = Depends(require_scope("runs:read")),
) -> StreamingResponse:
    services = get_run_services(request)
    record = await services.run_manager.get(run_id, user_id=caller.user_id)
    if record is None or record.thread_id != thread_id:
        raise RunNotFound(f"Run {run_id} not found")
    if record.store_only and not services.bridge.supports_cross_process:
        raise RunNotLocal(f"Run {run_id} is owned by another process")
    return sse_response(services, RunLaunch(record=record, reused=True), request, apply_on_disconnect=False)
```

### 11.3 路由在这一层的职责边界


| 路由负责                                                     | 路由**不**负责                                         |
| -------------------------------------------------------- | ------------------------------------------------- |
| 解析 `Last-Event-ID` 头（`sse_consumer` 读 `request.headers`） | 编帧（`format_sse`，M3 §8.1）                          |
| 决定 `apply_on_disconnect`                                 | 订阅与重放（`bridge.subscribe`，M3 §7）                   |
| 返回 `StreamingResponse` + 响应头                             | 断开时取消（在 `sse_consumer` 的 `finally`，M3 §8.2）       |
| 在订阅之前判存在性/属主/跨进程                                         | 判终态（`RunStore`）、判 token（`UsageCollector`，M3 §6.5） |


**路由是"谁能连"的把关人，不是"发什么帧"的生产者。** 把这两件事混在一起，
`/join` 就没法复用创建端那条链路（M3 §8.2 的 `apply_on_disconnect` 就是从这个区分里长出来的）。

### 11.4 响应头与跨源


| 头                                     | 值                                   | 谁加                                          |
| ------------------------------------- | ----------------------------------- | ------------------------------------------- |
| `Content-Type`                        | `text/event-stream`                 | `sse_response`（M3 §8.1）                     |
| `Content-Location`                    | `/api/threads/{tid}/runs/{rid}`     | 同上；`useStream` 的 `onCreated` 只从这里拿 `run_id` |
| `Cache-Control` / `X-Accel-Buffering` | `no-cache` / `no`                   | 同上（防中间层攒批）                                  |
| `Access-Control-Expose-Headers`       | 至少含 `Content-Location`、`X-Trace-Id` | CORS 中间件（§12）                               |
| `X-Trace-Id`                          | 当前请求的 trace id                      | trace 中间件；也是错误体里的 `trace_id` 的来源            |


跨源部署时漏掉 `Access-Control-Expose-Headers` 的症状是"浏览器里 `onCreated` 拿不到 run_id，
同源开发环境一切正常"。

---

## 12. 中间件与 `request.state`

### 12.1 顺序

```python
# app/gateway/app.py
def install_middleware(app: FastAPI) -> None:
    app.add_middleware(TraceMiddleware)                 # ① 最先：后面所有日志都要 trace_id
    app.add_middleware(AuthMiddleware)                  # ② M9：认证结果进 request.state.auth
    app.add_middleware(CSRFMiddleware)                  # ③ M9：只对 Cookie 会话生效
    app.add_middleware(CORSMiddleware, allow_origins=..., expose_headers=["Content-Location", "X-Trace-Id"])
```

**Starlette 的中间件是"后加的在最外层"**，所以上面这段代码的**执行顺序是倒过来的**：
CORS 最先看到请求，Trace 最后包装。写反了的表现是"错误响应缺 `X-Trace-Id`"或
"401 响应缺 CORS 头，浏览器只报网络错误"——两种都很难从服务端日志发现。

```python
# app/gateway/middleware/trace.py
class TraceMiddleware(BaseHTTPMiddleware):
    """不信任客户端传来的 X-Trace-Id。外来的值能把两件不相关的事写进同一条日志。

    start_run 不收 Request。第 8 步的 ensure_trace_id()（agentkit/trace_context.py，M3 §4.2）
    读的是这里绑定的 ContextVar，所以响应头、错误体、run metadata 是同一个字符串。
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        trace_id = uuid4().hex
        request.state.trace_id = trace_id
        token = trace_id_var.set(trace_id)          # agentkit/trace_context.py 的 ContextVar
        try:
            response = await call_next(request)
        finally:
            trace_id_var.reset(token)
        response.headers["X-Trace-Id"] = trace_id
        return response
```


| 步骤  | 逻辑                                             | 所属文件                                                                  |
| --- | ---------------------------------------------- | --------------------------------------------------------------------- |
| 1   | 生成 `uuid4().hex`（32 位，无连字符），忽略请求头里的同名值         | `middleware/trace.py`                                                 |
| 2   | 同时写入 `request.state.trace_id` 和 `trace_id_var` | state 给错误处理器（`errors.py::_trace_id`）；ContextVar 给 `ensure_trace_id()` |
| 3   | `finally` 里 `reset`，避免这个 id 漏到同进程的下一个请求        | 同上                                                                    |
| 4   | 响应头 `X-Trace-Id`                               | 错误响应也写。写在 `call_next` 之后，2xx 和 4xx 走同一行                               |


进程内调用方没有中间件，用 M3 §4.1 的 `ensure_trace_context()` 绑定同一颗 ContextVar。
路由不把 trace id 传进 `start_run`。

```python
# app/gateway/deps.py
def get_request_id(request: Request) -> str:
    """日志用。业务路径不要拿它再传一遍——start_run 已经从 ContextVar 读了。"""
    trace_id = getattr(request.state, "trace_id", None)
    if not isinstance(trace_id, str) or not trace_id:
        raise RuntimeError("TraceMiddleware did not run")
    return trace_id
```

### 12.2 `request.state` 的契约


| 属性                 | 谁写           | 谁读                                    | 缺失时怎么办                                    |
| ------------------ | ------------ | ------------------------------------- | ----------------------------------------- |
| `state.trace_id`   | Trace 中间件    | 错误体（§6.2）、日志、run metadata             | 兜底生成一个新的；**永远不许是 `None` 进错误体**            |
| `state.auth`       | Auth 中间件（M9） | `caller_from_request`、`require_scope` | M3/M4 阶段没有它，`caller_from_request` 返回匿名调用方 |
| `state.app_config` | lifespan     | 需要读配置的路由                              | 缺失 = 启动失败，不是运行期错误（M0 §5）                  |


**只有中间件和 lifespan 往 `request.state` 写东西；路由只读。** 路由往 `state` 上挂"临时结果"
（比如缓存住刚查出来的 record）会在异常路径上留下半成品，而下游读到的就是它。

---

## 13. 测试策略

### 13.1 四类测试，各管一件事


| 类别        | 测什么                                                   | 怎么写                                                 |
| --------- | ----------------------------------------------------- | --------------------------------------------------- |
| **接口面清单** | 实际注册的路由 == §3 的表；没有重复 path；每个 path 有 `response_model` | 反射 `app.routes`（§13.2）                              |
| **参数校验**  | 每个 `*Request` 的 `extra="forbid"`、边界值、路径白名单            | `TestClient` 发非法 body，断言 422 + `code`               |
| **错误映射**  | §6.1 表里的每一行                                           | 注入对应异常（假 store / 假 bridge / 直接 raise），断言状态码与 `code` |
| **流式**    | 帧序列、`Content-Location`、断开语义、gap                       | `httpx.ASGITransport` + M3 §8.4 的假桥与假 request       |


### 13.2 路由清单快照测试

```python
# backend/tests/test_router_contract.py
EXPECTED_ROUTES: set[tuple[str, str]] = {
    ("POST", "/api/threads"),
    ("GET", "/api/threads"),
    ("POST", "/api/threads/{thread_id}/runs"),
    ("POST", "/api/threads/{thread_id}/runs/stream"),      # ★ 静态段
    ("POST", "/api/threads/{thread_id}/runs/wait"),
    ("GET", "/api/threads/{thread_id}/runs/{run_id}"),     # ★ 参数段
    ("GET", "/api/threads/{thread_id}/runs/{run_id}/join"),
    ("POST", "/api/threads/{thread_id}/runs/{run_id}/cancel"),
    # ……与 §3.1 的表逐行对应
}


def test_route_inventory_matches_design(app: FastAPI) -> None:
    actual = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", set())
        if method not in {"HEAD", "OPTIONS"}
    }
    assert actual == EXPECTED_ROUTES, (
        f"added: {sorted(actual - EXPECTED_ROUTES)}\n"
        f"removed: {sorted(EXPECTED_ROUTES - actual)}"
    )


# 流式路由没有 response_model。白名单必须和 §3.1 里「流式 = ✓」的行一致。
_STREAMING = {
    ("POST", "/api/threads/{thread_id}/runs/stream"),
    ("GET", "/api/threads/{thread_id}/runs/{run_id}/join"),
    ("POST", "/api/runs/stream"),
}
# 文件下载同样没有 response_model。
_FILE_DOWNLOADS = {
    ("GET", "/api/threads/{thread_id}/artifacts/{path:path}"),  # M2 §10.4
}


def test_every_route_declares_a_response_model(app: FastAPI) -> None:
    bare = []
    for route in app.routes:
        methods = getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
        path = getattr(route, "path", "")
        if not path.startswith("/api") or not methods:
            continue
        if any((method, path) in _STREAMING or (method, path) in _FILE_DOWNLOADS for method in methods):
            continue
        if getattr(route, "response_model", None) is None:
            bare.append(f"{sorted(methods)} {path}")
    assert bare == [], f"routes returning untyped JSON: {bare}"
```

- **用集合断言，不用子集断言。** 子集只能抓住"少了路由"，抓不住"多了一条没进设计文档的路由"——
而后者正是接口面漂移的开始。
- **第二条测试挡住"返回裸 dict"**。流式路由（`StreamingResponse`）与文件下载显式加入白名单，
其余路由必须有 `response_model`，否则 OpenAPI 里就是空的 schema，前端生成不出类型。

### 13.3 路由纪律的 AST 测试

```python
# backend/tests/test_router_discipline.py
FORBIDDEN_CALLS = {"create_task", "dumps"}          # asyncio.create_task / json.dumps
FORBIDDEN_ATTRS = {"app"}                            # request.app.state.*


def test_routers_do_not_reach_into_internals() -> None:
    """路由只做四件事（§1.1）。这条测试是那份清单的可执行版本。"""
    for path in Path("app/gateway/routers").glob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in FORBIDDEN_CALLS:
                raise AssertionError(f"{path}:{node.lineno} 路由里出现了 {node.func.attr}()")
```

同类的 AST 纪律在 M3 §11（`run_agent` 恰四个参数、不 import FastAPI）和 M4 §3.5
（`astream` 循环里没有 `async with session`）已经用过：**"看起来无害的一行"用 code review 拦不住，
用 AST 拦得住。**

---

## 14. 刻意不做的事


| 不做                              | 理由                                                               |
| ------------------------------- | ---------------------------------------------------------------- |
| GraphQL / JSON-RPC 风格端点         | 官方 `useStream` 与 Python SDK 只认 REST + SSE（M3 §3）；自研 wire 等于自研解码器 |
| URL 版本号（`/api/v2`）              | One-Version Rule：加字段、不改字段。破坏性变更只允许发生在 greenfield 阶段              |
| 路由层缓存（`ETag` / `If-None-Match`） | 会话数据每人一份且高频变化，缓存命中率低而失效逻辑贵                                       |
| 分页返回 `total`                    | 长会话上 `COUNT(*)` 是全表扫；客户端只需要 `has_more`                           |
| 批量端点（一次建多条 run）                 | 并发模型是"每 thread 一个活跃 run"（M3 §5.3）；批量只会把 409 变成部分成功               |
| 在路由里做字段裁剪或权限过滤                  | 仓库层的 `user_id` 过滤已经做了（M4 §3.3）；两处过滤必然漂移                          |
| 把 admin 路由和生命周期路由混在一个文件         | 鉴权要求不同（§3.2）；混在一起迟早复制错 `Depends`                                 |


---

## 15. 验收清单

- [ ] `GET /openapi.json` 里每条业务路由都有 request / response schema，没有裸 `dict`
- [ ] 任取一条路由制造错误，响应体都是 `{detail, code, trace_id}`，且 `X-Trace-Id` 头同时在
- [ ] 空库上三个列表端点返回 `{"data": [], "has_more": false, "next_cursor": null}`
- [ ] `?limit=201` → 422；`?order=sideways` → 422
- [ ] `/runs/stream` 与 `/runs/{run_id}` 同时存在时，`/runs/stream` 命中的是流式 handler（不是把 `stream` 当 run_id）
- [ ] `/join` 的客户端断开**不**取消 run；`/runs/stream` 的断开按 `on_disconnect` 取消
- [ ] 缺 `runs:cancel` 的令牌调 cancel → 403；带 `multitask_strategy=interrupt` 的准入同样 403
- [ ] 同 key + 不同 `input` → 409 `idempotency_key_reused`；同 key + 同 body → 复用同一个 run，不跑第二遍
- [ ] `/api/threads/..%2Fetc/runs` 之类的路径参数 → 422（不是 404，也不是 500）
- [ ] 浏览器跨源直连 `useStream`，`onCreated` 能拿到 `run_id`（`Access-Control-Expose-Headers` 生效）
- [ ] 500 的响应体里没有任何内部信息，只有 `trace_id`；用它能在日志里捞到完整栈
- [ ] 删除/新增一条路由时，§3.1 的表和 `test_router_contract.py` 一起改，否则 CI 红
- [ ] `?cursor=` 传来一截被截断的游标 → 422 `invalid_cursor`，不是 500，也不是空列表
- [ ] `POST /api/runs/stream` 带 `Idempotency-Key` 仍会创建新 thread（头被忽略）；用返回的 `Content-Location` 再重试才复用 run
- [ ] `stream_resumable: false` 与 `if_not_exists: "create"` 被接受；`stream_resumable: true`、`checkpoint_during` → 422

---

## 16. 一句话总结

路由层的产出物是**一份能被官方 SDK 直接消费、并且不会随人漂移的接口面**。

检验标准：新加一条路由时，只需要在 §3.1 的表里加一行、照抄 §5 的模型约定、§6 的错误映射、
§8 的能力门和 §13 的契约测试——**不需要重新讨论错误体长什么样、分页游标谁解析、
身份从哪里读**。如果这条新路由需要改动本文的任何一节，说明它不该被加进来，
或者本文有一节写漏了。