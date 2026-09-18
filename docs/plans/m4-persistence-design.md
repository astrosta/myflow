# M4 持久化详细设计

> 配套文档：[分阶段开发方案](greenfield-agent-service-roadmap.md) ·
> [M0 地基](m0-foundation-design.md) · [M1 最小 Agent](m1-minimal-agent-design.md) ·
> [M2 工具与沙箱](m2-tools-sandbox-design.md) · [M3 Gateway 与流式](m3-gateway-streaming-design.md)
>
> 本文细化 M4（1–2 周，可与 M5 前端并行）。沿用包名：框架层 `agentkit.*`，应用层 `app.*`。

---

## 1. M4 的范围

M3 的东西全在内存里：`RunStore` 是个 dict，checkpointer 是 `InMemorySaver`，事件账本还不存在。
M4 把它们落到磁盘，并且保证同一套代码在 SQLite（开发）和 Postgres（生产）上行为一致。

**M4 的核心命题：搞清楚"哪些东西是谁的"。**

这听起来像废话，但它是这个阶段唯一真正困难的部分。一次对话产生的数据散落在
四个性质完全不同的面上，每一面的所有者、生命周期、权威性都不一样。
把它们混为一谈——比如用事件账本重算 token、用 checkpoint 当历史消息源、
把 SSE 缓冲当审计日志——是后面所有数据一致性 bug 的根源。

### 两周排期

| 天 | 内容 |
|---|---|
| D1–2 | 引擎 / session 工厂 / 三后端选择 + `bootstrap_schema` |
| D3–5 | 三张核心表（`threads_meta` / `runs` / `run_events`）+ 仓库层 |
| D6–7 | `RunEventStore` 三后端 + `seq` 分配 |
| D8 | Checkpointer 接 SQLite / Postgres + alembic 排除 |
| D9–10 | SQLite / Postgres 一致性测试（同一套测试跑两遍） |

---

## 2. 四个持久化面

**先把这张图画在白板上，再动手写表。**

```
┌─ SQL：自己的 alembic 链 ────────────────────────┐
│  threads_meta    runs    run_events             │  ← M4 要建的
│  （后面会长出 users / feedback / scheduled_* …） │
└─────────────────────────────────────────────────┘
┌─ SQL：LangGraph 自有（alembic 明确排除）────────┐
│  checkpoints  checkpoint_blobs                  │  ← 不碰 DDL
│  checkpoint_writes  checkpoint_migrations       │
└─────────────────────────────────────────────────┘
┌─ 磁盘文件 ──────────────────────────────────────┐
│  thread 工作区 / 上传 / 产出（M2 已经有了）      │
└─────────────────────────────────────────────────┘
┌─ 进程内 / Redis：刻意易失，别当账本 ────────────┐
│  StreamBridge 队列、RunRecord 活体              │
└─────────────────────────────────────────────────┘
```

**两套 SQL 共用一个库，但归属不同。** LangGraph 的 checkpointer 自己 `setup()` 建表，
自己管 schema 版本。你的 alembic 必须显式把这几张表排除，否则每次 `autogenerate`
都会生成一堆 `drop_table`——因为你的 ORM 模型里没有它们。

```python
LANGGRAPH_OWNED_TABLES = {
    "checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations",
}

def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and name in LANGGRAPH_OWNED_TABLES:
        return False
    return True
```

### 一次对话的数据落在哪

| 数据 | 落在哪 | 权威性 |
|---|---|---|
| **模型看到的对话历史** | checkpoint 的 `messages` 通道 | 权威 |
| **前端显示的历史消息** | `run_events`（`category="message"`） | 权威 |
| **侧栏显示什么** | `threads_meta` | 投影 |
| **这次执行成没成、花了多少 token** | `runs` | 权威 |
| **这一轮的直播** | StreamBridge | 易失 |

**"模型看到的历史"和"前端显示的历史"是两个不同的东西，存在两个地方。**
这是 M4 最反直觉的一点，但它是必须的：压缩（compact）之后 checkpoint 的 `messages`
会变短，而用户仍然应该能往上翻看到完整的对话。两者条数不一致是正常状态，
不是 bug。

对应的纪律：**日常不要把这两者互相当权威。** 只有一个例外——
遗留 thread 只有 checkpoint 没有账本时，第一次开 run 前做一次性 seed（升级垫片）。

---

## 3. 引擎与后端（D1–2）

### 3.1 三个后端

```yaml
database:
  backend: sqlite          # memory | sqlite | postgres
  sqlite_dir: .myflow
  url: postgresql+asyncpg://...   # 仅 postgres
```

| backend | ORM 引擎 | 应用数据 | checkpoint |
|---|---|---|---|
| `memory` | 不初始化，`get_session_factory()` 返回 `None` | 仓库退化成进程内实现 | `InMemorySaver` |
| `sqlite` | `{sqlite_dir}/app.db`，WAL | 同一个文件 | 同一个文件 |
| `postgres` | `database.url` | 同库 | 同库 |

**`memory` 后端不是可有可无的。** 它让整个测试套件能在没有任何外部依赖的情况下跑，
也让 `make dev` 的第一次启动不需要用户配数据库。代价是每个仓库都要显式处理
`session_factory is None` 的分支——写的时候烦，但比测试要起 Postgres 容器划算得多。

引擎和 session 工厂是**模块级单例**：Gateway 启动 `init_engine()`，关闭 `dispose()`。
`backend="memory"` 时 `init_engine` 直接 return。

一个小而有用的细节：**JSON 列统一用 `ensure_ascii=False` 序列化**。
不这么做的话中文在库里全是 `\uXXXX`，人肉排查数据时极其痛苦。

```python
def _json_serializer(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)
```

### 3.2 建表：`create_all` 和 migration 的陷阱

```python
async def bootstrap_schema():
    if await _is_empty_database():
        await _create_all()          # 按当前 ORM 模型直接建
        await _stamp_head()          # 标记 alembic 版本为最新
    else:
        await _upgrade_head()        # 走 migration 链
```

**这里有一个极其容易踩的坑，值得单独一节：**

> **ORM `__table_args__` 里声明的索引和约束，不能只写在 migration 里。**

理由：空库走 `create_all` + `stamp head` 这条路，**那条 migration 永远不会被执行**。
如果你的部分唯一索引只写在 migration 里，那么所有新部署的库都没有这个索引——
M3 辛苦设计的"每 thread 一个活跃操作"约束在新环境上直接失效，
而在你自己的老开发库上一切正常。

正确做法是**两处都写**：ORM 模型里声明（给 `create_all` 用），
migration 里也写一遍（给已有库升级用）。这看起来是重复，但它们服务于两条不同的路径。

并发建表要加锁：Postgres 用 advisory lock 跨进程串行，SQLite 靠文件锁 + `busy_timeout`。
多个 Gateway 进程同时启动时，没有这个锁会撞成一片 `IntegrityError`。

---

## 4. 三张核心表（D3–5）

M4 只建三张。它们的职责**刻意不重叠**。

### 4.1 `threads_meta`——投影，不是权威

| 字段 | 来源 | 写入时机 |
|---|---|---|
| `thread_id` | 客户端给或服务端生成 | create |
| `incarnation` | `uuid4().hex`，服务端生成 | create |
| `user_id` | 从 contextvar 取当前用户 | create |
| `display_name` | run 结束时**从 checkpoint 的 `title` 抄过来** | worker `finally` |
| `status` | `idle` / `running` / run 的终态 | 见下 |
| `metadata_json` | 客户端 metadata，**剥掉服务端保留键** | create / update |

两个要点：

**`incarnation` 现在就要有。** 它是"同名但不是同一条 thread"的判别依据——
用户删掉一条 thread 又用同样的 id 重建，所有持有旧引用的后台任务
（M9 的 MCP 长任务、定时任务）必须能发现自己指向的是一条已经不存在的会话。
没有这一列，它们会把结果写进一条陌生的新会话里。

**`display_name` 是投影，`title` 的权威在 checkpoint。** 侧栏需要一个能直接 SQL 排序
和搜索的列，所以要冗余一份。但两者不一致时，以 checkpoint 为准。

`status` 的写入时机要和 M3 的状态机对齐：`create` 给 `idle`；
**worker 的 `try_start` 成功之后**才写 `running`（不是准入时）；收尾时成功回 `idle`，
失败写对应终态。

### 4.2 `runs`——把 M3 的 `RunRecord` 序列化

字段按**来源**分五组，这个分组本身就是设计文档：

**① 准入时一次写定**：`run_id`、`thread_id`、`assistant_id`、`user_id`、
`status='pending'`、`operation_kind`、`idempotency_key`、`multitask_strategy`、
`metadata_json`、`kwargs_json`、`model_name`。

**② 状态机**：只有 `start_run()` 的 CAS 能进 `running`；
终态由 worker 写，且 **`update_run_completion` 必须拒绝覆盖另一个终态**。
这条守卫堵的是"peer 接管之后，原 owner 迟到收尾"的竞争——M3 里还没有 peer，
但守卫现在加上是零成本的。

**③ 租约与取消**：`owner_worker_id`、`lease_expires_at`、`cancel_action`、
`cancel_requested_at`。M4 只建列不用，M9 才有意义。

**④ token 与便利字段**：`total_input_tokens` / `total_output_tokens` /
`llm_call_count` / `first_human_message` / `last_ai_message`（后两个截断到 2000 字符）。

> **这些数字来自运行时的内存累加，不是从 `run_events` 反算的。**
> 反算看起来更"单一真相源"，但它要求账本覆盖每一次模型调用、
> 每次查询都要扫一遍行、而且中间件产生的调用很容易漏掉。
> 内存累加 + 定期落库是正确的权衡。

写入有两个时机：跑的过程中 `update_run_progress`（**节流约 5 秒，且只在
`status='running'` 的行上生效，不动 status**），结束时和终态一起写。

**⑤ 并发保证**：

```sql
CREATE UNIQUE INDEX uq_runs_thread_active
    ON runs (thread_id) WHERE status IN ('pending', 'running');
CREATE UNIQUE INDEX uq_runs_idempotency_key ON runs (idempotency_key);
```

这是 M3 §5.3 那个约束的物理落地。**它是跨进程的硬保证，不靠应用层锁。**

`kwargs_json` 存的是**脱敏之后**的副本（`redact_config_secrets(body.config)`），
因为 runs 的查询 API 会原样回显这个字段。

**`runs` 行不删**，它是可读历史。只有非 run 的 thread 操作
（compact / state_update / delete）在收尾时删掉自己那行短命的占位记录。

### 4.3 `run_events`——append-only 的账本

| 列 | 说明 |
|---|---|
| `thread_id` / `run_id` | 账本按 thread 连续，行上带 run |
| `event_type` / `category` | 来自一份集中的 catalog |
| `content` | 事件正文（消息类是 JSON 串） |
| `event_metadata` | 附加结构 |
| `seq` | **thread 内严格递增** |

索引：`(thread_id, seq)` 唯一、`(thread_id, category, seq)`、`(thread_id, run_id, seq)`。

**`seq` 是 thread 全局递增的，不是每个 run 从 0 开始。** 前端要按时间顺序渲染整条会话
的事件流，跨 run 排序必须靠单调序号。用 `(run_id, seq)` 复合排序在 run 并发创建时
会错乱，用 `created_at` 排序则会在同毫秒的事件上不稳定。

`category` 决定谁读它：

| category | 谁在乎 |
|---|---|
| `message` | **前端历史消息**，`list_messages` 只返回这一类 |
| `trace` | 调试 |
| `outputs` | 投递回执 |
| `error` | 错误 |
| （后面会加 `subagent` / `context` / `workspace`） | M7–M8 |

**`category` 现在就要有，哪怕 M4 只用到两三个值。** 它是 `list_messages`
和 `list_events` 这两个查询的分区维度，后加意味着所有历史行都没有正确的值。

---

## 5. RunEventStore（D6–7）

### 5.1 接口保证

```python
class RunEventStore(abc.ABC):
    async def put(self, ...) -> None: ...                  # 单条，低频路径
    async def put_batch(self, events: list[...]) -> None: ...   # 常规路径
    async def put_if_absent(self, ...) -> None: ...        # 终态回执，可重试

    async def list_messages(self, thread_id, limit, before_seq=None, after_seq=None): ...
    async def list_events(self, thread_id, run_id, event_types=None, after_seq=None): ...
```

五条所有后端都要满足的保证：

1. `put` 之后立刻能查到。
2. **`seq` 在同一 `thread_id` 内严格递增。**
3. `list_messages()` 只返回 `category="message"`。
4. `list_events()` 返回该 run 的全部类别。
5. 消费者忽略未知字段（后端可以多返回列）。

双向游标：`before_seq` 拿更旧的一页，`after_seq` 拿更新的一页，**返回都按 seq 升序**。
两边都不传等于最新 `limit` 条。返回顺序统一成升序，
省掉前端在两个方向上写两套拼接逻辑。

### 5.2 三个后端与 `seq` 分配

| backend | 落在哪 | `seq` 保证 |
|---|---|---|
| `memory` | 进程 dict | 单进程单调 |
| `db` | SQL 表 | 进程内 per-thread `asyncio.Lock` + DB 行锁；`UNIQUE(thread_id, seq)` 兜底 |
| `jsonl` | `threads/{tid}/runs/{rid}.jsonl` | **仅单进程**，多进程同目录会重复/乱序 |

`seq` 的分配是 `max(seq) + 1`，必须串行化。双层保护：应用层的 per-thread 锁负责
正常路径的性能（避免每次都撞唯一索引重试），数据库的唯一约束负责正确性兜底。

**`run_events.backend` 和 `database.backend` 不联动，而且启动时冻结。**
这两点都是刻意的：本地开发常常是 `database: sqlite` 但 `run_events: memory`
（不想让调试事件污染库）；而冻结是因为 store 实例在 lifespan 里构造一次，
热切后端会让已经持有旧引用的组件写进一个没人读的地方。

默认值建议 `memory`，并在文档里写清"要持久化历史必须显式设 `db`"——
让用户显式选择比猜一个默认值安全。

### 5.3 `put` vs `put_batch`

**`put` 是低频路径，正常执行必须用 `put_batch`。**

每条事件一次 `put` 意味着每条都要拿一次 thread 锁 + 一次数据库往返。
M8 的深层子代理一步一个事件，能把 Postgres 的锁打满。
正确做法是在运行时缓冲（比如 20 条一批），在阶段边界和 `finally` 里 flush。

`put_if_absent` 给终态回执用：worker 崩溃后由别的路径补写一条，
重复调用不产生第二行。

---

## 6. Checkpoint：LangGraph 拥有它（D8）

### 6.1 边界

DDL 不是你的 alembic 建的，是 LangGraph saver 启动时自己 `setup()` 建的。
你要做的只有两件事：把表加进 `LANGGRAPH_OWNED_TABLES` 排除掉，
以及把 checkpointer 的后端和 `database.backend` 对齐。

> **注意 LangGraph Store 是第五张脸。** 它和 checkpointer 共用后端，
> 表也由 `store.setup()` 建，但它**不在** checkpoint 那四张表的名单里。
> 用到 Store 时要记得把它的表名也加进排除集合。

### 6.2 `full` 和 `delta`

```yaml
database:
  checkpoint_channel_mode: full     # full | delta，启动时冻结
```

| | `full` | `delta` |
|---|---|---|
| `messages` 怎么存 | 每拍一整份列表 | sentinel + 增量写，隔 N 拍打一份全量快照 |
| 存储随轮次 | 约 O(轮次²) | 约 O(轮次) |
| 从旧 checkpoint resume | LangGraph 真 fork | **不能 fork**，要先物化再覆写到当前 head |
| 组图失败时 | 可以退化成裸读 | **没有这条退路** |

**M4 先只做 `full`。** 它简单、可裸读、调试友好。`delta` 是 O(N²) 存储真的成为问题时
才需要的优化，而且它会牵连 fork 语义、读路径、迁移三件事。

但**配置项和 metadata 标记现在就要留**：delta 的 checkpoint 要带
`checkpoint_channel_mode: "delta"` 元数据（没有标记等于 full）。
一个 full 模式的进程打开 delta 写过的 thread 必须报 409 而不是读出空 messages——
这个判别只能靠标记，后补的话已有数据无从区分。

### 6.3 谱系不是时间线

```
checkpoint A ──► B ──► C
                  └──► C'   （regenerate 产生的兄弟分支）
```

多次 regenerate 会在同一条 thread 下长出**兄弟分支**。
**按"最新 checkpoint"或按时间倒序扫，会选到已经被替代的那一条。**

正确做法是沿 `parent_config` 往回走。M4 不实现 regenerate（那是 M6），
但读取路径现在就要按谱系写，不要按时间排序——后者在单分支下完全正确，
等长出分支才暴露，那时候所有读路径都要回头改。

还有一条：**replay 的基座必须是 settled 的**（`has_pending_tasks` 为假）。
中途快照接着跑会重放当时正要执行的节点写入。

### 6.4 读取只走一个入口

```python
accessor = build_thread_checkpoint_state_accessor(thread_id)
state = await accessor.get_state()
```

不要让业务代码直接 `checkpointer.get_tuple()`。Accessor 负责绑定
"这张图的通道表 + checkpointer + 进程冻结的 mode"，并在读之前注入 mode 标记。

M4 只有 full 模式时这层看起来是多余的包装。但 delta 模式下裸 `get_tuple`
看到的是 sentinel，会把 `messages` **读成空列表**——不是报错，是静默读空。
这类 bug 极难定位，所以入口现在就要收敛。

---

## 7. SQLite 与 Postgres 的一致性（D9–10）

**这一节是 M4 真正的风险所在。** 开发用 SQLite、生产用 Postgres，
意味着所有差异都在测试里看不见，上线才炸。

### 7.1 JSON 查询语义必须强制一致

三条最容易出问题的规则：

- **"字段缺失"必须区别于"字段为 null"**
- **bool 不能匹配 int**（SQLite 里 `true` 和 `1` 很容易混）
- **float 过滤要同时接受整数和实数两种 JSON 数字**（`1` 和 `1.0`）

做法是写一个统一的 `json_value_matches()` helper，然后**用同一套测试跑两个后端**。
不要在两个仓库实现里各写各的谓词。

### 7.2 其它差异清单

| 差异 | 处理 |
|---|---|
| SQLite 读回 naive datetime | 统一按 UTC 规范化成 tz-aware |
| SQLite 需要 per-connection PRAGMA | WAL、`busy_timeout`、`foreign_keys` 在连接事件里设 |
| Postgres 需要 `search_path` | 有 `postgres_schema` 配置时钉住 |
| 部分唯一索引语法 | 两边都支持 `WHERE`，但要测到 |
| 序列化时间戳绑到 `DateTime` 列 | 绑定前做类型强制，不要传字符串 |

`NULL != NULL` 这条标准语义在两边是一致的，可以放心用来做
"允许无限多个 NULL、但非 NULL 值唯一"的部分唯一索引——M9 的 OAuth 身份会用到。

### 7.3 测试策略

```python
@pytest.fixture(params=["sqlite", "postgres"])
def db_backend(request): ...
```

**仓库层的每一个测试都参数化跑两遍。** Postgres 那一档在没有可达实例时自动 skip，
但 CI 必须真的提供一个——只在 SQLite 上绿的测试套件对这个阶段没有意义。

---

## 8. Alembic（D8）

三条规则：

**加新表要同时改两处**：ORM 模型注册进 `models/__init__.py`
（否则 `autogenerate` 根本看不见它），以及生成一条 revision。

**forward revision 要有恢复预案。** 数据库里 stamp 的版本比代码里的 revision 链更新时
（典型场景：切到旧分支），alembic 会直接报错且信息不友好。
这个场景在多人开发分支切换时每周都会遇到，预案要写进 README 而不是让每个人现查。

**扩展表用前缀声明后一起排除。** 和 LangGraph 表同一个过滤器。

---

## 9. 一次"发送消息"落了哪些行

把前面几节串起来。这张时间线是 M4 的验收依据：

```
POST /threads/{id}/runs/stream
  │
  ├─ create_or_reject          → runs INSERT (pending)   ★ 部分唯一索引在这里定生死
  ├─ _ensure_thread_metadata   → threads_meta INSERT（缺行才建）
  │
  └─ run_agent（后台 task，HTTP 已返回）
       ├─ try_start            → runs UPDATE status=running   ★ CAS，唯一入口
       ├─ update_status        → threads_meta.status=running
       ├─ astream 循环
       │    ├─ 每个 superstep  → checkpoints / blobs / writes（LangGraph 写）
       │    ├─ 缓冲满          → run_events INSERT ×N
       │    └─ 每 5s           → runs UPDATE（token 进度，不动 status）
       └─ finally
            ├─ journal.flush()       → run_events 剩余事件 + 终态回执
            ├─ update_run_completion → runs UPDATE 终态 + token 汇总
            ├─ update_display_name   → threads_meta.display_name（抄 checkpoint 的 title）
            ├─ update_status         → threads_meta.status 终态
            └─ publish_end           → 易失，不落库
```

注意 `_ensure_thread_metadata` 这一步：thread 行**可能被 run 准入路径顺手建出来**。
这让"不先建 thread、直接发消息"也能工作，是个值得支持的用法。

**一条贯穿始终的规则**（M9 才真正生效，但现在就要建立结构）：
`ownership_lost` 为真时，`finally` 里的**所有 durable 写全部跳过**。
新增任何一处 `finally` 里的写入，都必须挂在同一个判断下面。

---

## 10. 谁删数据

**M4 要明确接受一个现实：大部分表是只增不删的。**

| 动作 | 删掉什么 | 留下什么 |
|---|---|---|
| `DELETE /threads/{id}` | thread 目录、checkpoint、`threads_meta` 行 | **`runs` 和 `run_events` 行会留下** |
| thread 操作收尾 | 自己那条短命 `runs` 占位行 | — |
| checkpoint 保留策略 | 见下 | — |

`run_events` 只增不删是有原因的：**它同时是历史消息来源和审计账本**。
要做保留策略，得先决定这两个身份谁优先——删了就没有历史消息，留着就无限增长。
M4 不解决这个问题，但要在文档里写明，免得后面有人顺手加个 TTL 把用户的对话历史清了。

**checkpoint 更不能随便删。** 以下几类默认都不能按"最老/最大"删除：

- 谱系上的祖先
- 显式 resume 的目标
- inflight writes
- 作为链环的元数据节点（比如只记录耗时的那一拍）
- 每条 thread 最新的可 resume head

静默删行的后果是：以后的 branch / regenerate 会撞上谱系断裂错误，
**看起来像功能坏了，而不是像存储策略生效了**。所以保留策略要先写合同再写实现。

---

## 11. 测试清单

| 文件 | 覆盖 |
|---|---|
| `test_repositories.py` | **参数化跑 sqlite + postgres 两遍**，所有仓库 CRUD |
| `test_json_semantics.py` | 字段缺失 vs null；bool 不匹配 int；float 接受 `1` 和 `1.0` |
| `test_unique_active_run.py` | 部分唯一索引真的在**新建的空库**上生效（走 `create_all` 那条路） |
| `test_run_event_seq.py` | `seq` per-thread 严格递增；并发 put 不产生重复；双向游标顺序 |
| `test_terminal_not_overwritten.py` | `update_run_completion` 拒绝覆盖另一个终态 |
| `test_restart_recovery.py` | 重启后历史会话完整；inflight run 被收成 error |
| `test_alembic_autogenerate.py` | 对着当前 ORM 模型跑 `autogenerate`，**断言产生空 diff**（防止有人加了模型忘了 migration） |
| `test_langgraph_tables_excluded.py` | autogenerate 不会试图 drop checkpoint 表 |

第三个和第七个是这一阶段最有价值的两个测试。

`test_unique_active_run.py` 必须在**全新的空库**上跑，因为它验的正是
"走 `create_all` 那条路径时索引还在不在"——在一个已经跑过 migration 的库上测
是测不出这个 bug 的。

`test_alembic_autogenerate.py` 把"改了模型忘了写 migration"这类错误
从"上线时才发现"提前到 CI。

---

## 12. 验收清单

- [ ] 重启 Gateway 后历史会话完整，侧栏、消息、token 统计都在
- [ ] 同一套仓库测试在 SQLite 和 Postgres 上都通过
- [ ] **全新空库**上，同一 thread 并发两次 `POST /runs` 仍然 409（索引生效）
- [ ] 重启时 inflight 的 run 被收成 `error`，不会永远卡在 `running`
- [ ] 中文内容在库里是可读的，不是 `\uXXXX`
- [ ] `autogenerate` 对着当前模型产生空 diff，且不含 checkpoint 表的 `drop_table`
- [ ] 前端翻历史消息读的是 `run_events`，模型看到的历史读的是 checkpoint，两者可以不等长

---

## 13. 刻意不做的事

| 不做 | 理由 |
|---|---|
| `delta` checkpoint 模式 | O(N²) 真的成为问题时再做。但 metadata 标记和配置项现在就留 |
| checkpoint 保留 / 裁剪 | 见 §10。要先写合同（哪些不能删）再写实现 |
| `run_events` 的 TTL | 同上。它有两个身份，得先决定谁优先 |
| 用户表 / 认证 | M9。但 `user_id` 列现在每张表都有 |
| 定时任务、MCP 任务、子代理批次等表 | 各自阶段。它们形状高度一致（`status` + 租约 + 部分唯一索引），M4 把这个模式立好就行 |
| 多实例的租约实际使用 | M9。M4 只建列 |
| 分库分表、读写分离 | 不在这个项目的规模内 |

---

## 14. 一句话总结

M4 的产出物不是"接了个数据库"，而是**一张明确的数据所有权地图**。

检验标准：随便指着一个字段问"它的权威在哪、谁写它、什么时候写、能不能删"，
应该都有一句话的确定答案。答不上来的字段，就是下一个数据一致性 bug 的位置。
