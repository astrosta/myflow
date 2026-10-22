# M4 持久化详细设计

> 配套文档：[分阶段开发方案](2026-09-16-greenfield-agent-service-roadmap.md) ·
> [M0 地基](2026-09-16-m0-foundation-design.md) · [M1 最小 Agent](2026-09-16-m1-minimal-agent-design.md) ·
> [M2 工具与沙箱](2026-09-16-m2-tools-sandbox-design.md) · [M3 Gateway 与流式](2026-09-16-m3-gateway-streaming-design.md)
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


| 天     | 内容                                                 |
| ----- | -------------------------------------------------- |
| D1–2  | 引擎 / session 工厂 / 三后端选择 + `bootstrap_schema`       |
| D3–5  | 三张核心表（`threads_meta` / `runs` / `run_events`）+ 仓库层 |
| D6–7  | `RunEventStore` 三后端 + `seq` 分配 + `RunJournal` 回调映射 |
| D8    | Checkpointer 接 SQLite / Postgres + alembic 排除      |
| D9–10 | SQLite / Postgres 一致性测试（同一套测试跑两遍）                  |



### M4 新增的文件

**SQL 归属在 harness 层，Gateway 只在 lifespan 里** `init_engine` **/** `close_engine`**。**
仓库被 M1 客户端、M3 worker、M9 调度器共用，不能 import FastAPI。

```
agentkit/config/
├── database_config.py         ★  # DatabaseConfig：一条配置驱动 ORM + checkpointer
└── run_events_config.py          # RunEventsConfig

agentkit/persistence/
├── __init__.py
├── base.py                       # SQLAlchemy DeclarativeBase + to_dict()
├── engine.py                  ★  # init_engine / close_engine / get_session_factory
├── bootstrap.py               ★  # 空库 create_all+stamp vs 老库 upgrade
├── json_compat.py             ★  # json_value_matches / JsonMatch（SQLite+PG）
├── postgres_schema.py            # search_path 钉住、schema 名白名单
├── recovery.py                   # recover_inflight_runs：启动时把孤儿活跃行收成 error
├── schema.py                     # ★ 唯一一处把**所有** ORM 类 import 一遍的地方
├── thread_meta/                  # 三套仓储一个形状：ABC + 内存 + SQL + ORM 行
│   ├── base.py                   # ThreadMetaStore ABC
│   ├── memory.py
│   ├── model.py                  # ThreadMetaRow
│   ├── sql.py
│   └── __init__.py               # make_thread_meta_store()
├── run/
│   ├── base.py                   # RunStore ABC + ACTIVE_STATUSES（M3 定的接口）
│   ├── memory.py                 # MemoryRunStore
│   ├── model.py                  # RunRow
│   ├── sql.py                    # SqlRunStore
│   └── __init__.py               # make_run_store()
├── events/
│   ├── base.py                   # RunEventStore ABC
│   ├── codec.py                  # 信封校验 + content 的 JSON 编码（唯一编码器）
│   ├── memory.py / db.py / jsonl.py
│   ├── model.py                  # RunEventRow（只有 db 后端用它）
│   └── __init__.py               # make_run_event_store()
└── migrations/
    ├── alembic.ini
    ├── env.py
    ├── _env_filters.py        ★  # LANGGRAPH_OWNED_TABLES
    ├── _helpers.py               # safe_add_column / safe_drop_column
    └── versions/0001_baseline.py

agentkit/runtime/user_context.py       # AUTO 哨兵；persistence 读、gateway 写
agentkit/runtime/checkpointer/
└── async_provider.py          ★  # make_checkpointer()：独立连接池，不是 ORM 引擎
agentkit/runtime/checkpoint_state.py   # CheckpointStateAccessor
agentkit/persistence/run/                # 与 thread_meta 同一个形状
├── base.py                            # RunStore ABC + ACTIVE_STATUSES
├── memory.py                          # MemoryRunStore
├── model.py                           # RunRow
├── sql.py                             # SqlRunStore
└── __init__.py                        # make_run_store(session_factory)

agentkit/persistence/events/
├── base.py                            # RunEventStore ABC
├── codec.py                           # 信封校验 + content 的 JSON 编码（唯一编码器）
├── memory.py / db.py / jsonl.py       # 三份实现
└── __init__.py                        # make_run_event_store()

agentkit/runtime/events/
├── catalog.py                 ★  # event_type / category 权威名单
└── journal.py                 ★  # RunJournal：callback → put_batch
```

**ORM 行跟着它的仓储走，不再单开 `models/` 目录。** 每张表只有一个仓储会碰它
（`run_events` 只有 `events/db.py`），把行定义放在那个包里，"表 ↔ 仓储 ↔ 工厂"
三者就在同一个目录下。`schema.py` 只承担一件事：**把所有行类 import 一遍**。

**两套 ABC 都住在 `persistence/`，和 `thread_meta` 一样。** 仓储的接口属于持久化
这一层：`RunManager` 与 `RunJournal` 依赖的是 ABC，不是某一份实现；
"哪份实现"由工厂按 `database.backend` / `run_events.backend` 决定。
runtime 那边只留**跑的时候才存在**的东西（catalog、journal、manager、worker）。

早先的写法把 `RunEventStore` 放在 runtime，理由是"账本是运行时概念"——
实际用下来那条理由站不住：`persistence/` 里已经同时住着`thread_meta` 与
`run` 两套仓储和它们的 ORM 行，再让第三套按"概念上属于谁"分家，只会让
"接口在哪、实现在哪、工厂在哪"变成三个要分别记的事实。

**每个仓库都是「同一份 ABC、两份实现」。** `database.backend=memory` 时
`get_session_factory()` 返回 `None`，工厂函数必须退化到 memory 实现，
而不是让调用方自己 `if session_factory is None`。调用方只认 ABC。

`persistence/schema.py` 必须把所有 ORM 类 import 一遍（`create_all`、`env.py`、
`0001_baseline` 都 import 它）。漏掉任何一个，`create_all` 建不出那张表，
`autogenerate` 也看不见它——然后 CI 的「空 diff」测试会绿，
上线才 500 `no such table`。**新加表时唯一要做的事就是往这个文件里加一行。**

---



## 2. 四个持久化面

**先把这张图画在白板上，再动手写表。**

```
┌─ SQL：自己的 alembic 链 ────────────────────────┐
│  threads_meta    runs    run_events             │  ← M4 要建的
│  （后面会长出 users / scheduled_* / channel_* …） │
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
# agentkit/persistence/migrations/_env_filters.py
LANGGRAPH_OWNED_TABLES = {
    "checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations",
}

def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and name in LANGGRAPH_OWNED_TABLES:
        return False
    return True
```



### 一次对话的数据落在哪


| 数据                     | 落在哪                                | 权威性 |
| ---------------------- | ---------------------------------- | --- |
| **模型看到的对话历史**          | checkpoint 的 `messages` 通道         | 权威  |
| **前端显示的历史消息**          | `run_events`（`category="message"`） | 权威  |
| **侧栏显示什么**             | `threads_meta`                     | 投影  |
| **这次执行成没成、花了多少 token** | `runs`                             | 权威  |
| **这一轮的直播**             | StreamBridge                       | 易失  |


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
# config.example.yaml（摘录）
database:
  backend: sqlite          # memory | sqlite | postgres   startup-only
  sqlite_dir: .myflow
  url: postgresql+asyncpg://...   # 仅 postgres
  checkpoint_channel_mode: full   # full | delta，启动时冻结
  postgres_schema: ""             # 非空时钉 search_path

run_events:
  backend: memory          # memory | db | jsonl          startup-only
  max_trace_content: 10240 # 仅 db：category=trace 超此字节截断
  track_token_usage: true  # journal 是否把 usage 抄进事件 metadata（累加在 M3 §6.5 的 UsageCollector）
```


| backend    | ORM 引擎                                 | 应用数据       | checkpoint      |
| ---------- | -------------------------------------- | ---------- | --------------- |
| `memory`   | 不初始化，`get_session_factory()` 返回 `None` | 仓库退化成进程内实现 | `InMemorySaver` |
| `sqlite`   | `{sqlite_dir}/app.db`，WAL              | 同一个文件      | 同一个文件           |
| `postgres` | `database.url`                         | 同库         | 同库              |


`memory` **后端不是可有可无的。** 它让整个测试套件能在没有任何外部依赖的情况下跑，
也让 `make dev` 的第一次启动不需要用户配数据库。代价是每个仓库都要显式处理
`session_factory is None` 的分支——写的时候烦，但比测试要起 Postgres 容器划算得多。

引擎和 session 工厂是**模块级单例**：Gateway 启动 `init_engine()`，关闭 `close_engine()`。
`backend="memory"` 时 `init_engine` 直接 return。

一个小而有用的细节：**JSON 列统一用** `ensure_ascii=False` **序列化**。
不这么做的话中文在库里全是 `\uXXXX`，人肉排查数据时极其痛苦。

```python
# agentkit/persistence/engine.py
def _json_serializer(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)
```

`default=str` 不能省：`datetime` / `UUID` 进 JSON 列时没有它会在 worker `finally`
里炸掉——而那正是 M3 嵌套 finally 保护的那条路径。序列化失败表现为「run 终态写不进去」。

#### `init_engine` 的后端分叉

```python
# agentkit/persistence/engine.py
async def init_engine(backend, *, url="", echo=False, pool_size=5,
                      sqlite_dir="", postgres_schema="") -> None:
    if backend == "memory":
        return                                          # _engine 保持 None

    if backend == "sqlite":
        await asyncio.to_thread(os.makedirs, sqlite_dir or ".", exist_ok=True)
        _engine = create_async_engine(url, echo=echo, json_serializer=_json_serializer)
        @event.listens_for(_engine.sync_engine, "connect")
        def _enable_sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute("PRAGMA foreign_keys=ON;")
            cur.execute("PRAGMA busy_timeout=30000;")
            cur.close()

    elif backend == "postgres":
        # asyncpg 缺失时给可执行的安装提示，不要 ModuleNotFoundError
        _engine = create_async_engine(url, pool_size=pool_size, pool_pre_ping=True,
                                      pool_recycle=300, json_serializer=_json_serializer,
                                      connect_args={"command_timeout": 30, ...})

    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    await bootstrap_schema(_engine, backend=backend)
```

**SQLite 的 PRAGMA 必须挂在** `connect` **事件上，不能只在启动时跑一次。**
它们是 per-connection 的；连接池里每条新连接都是默认设置。漏掉 WAL，
并发读写会直接 `database is locked`。`busy_timeout=30000` 是给「两个 Gateway
进程同时 bootstrap」用的：第二个要等第一个跑完 `CREATE TABLE`。

`makedirs` **必须** `asyncio.to_thread`**。** `init_engine` 跑在 FastAPI lifespan
的事件循环上，同步 `os.makedirs` 会卡住启动。M2 的 blocking-IO 套件会抓住这条。

**Postgres 连接池三项现在就设：** `pool_pre_ping=True`（丢掉闲置死连接）、
`pool_recycle=300`（在代理掐掉之前主动回收）、`command_timeout=30`
（单条 SQL 上限，防止一个卡住的查询占死整个池）。这三项看起来像运维调优，
但缺了任何一项，M9 多实例下的症状都是「偶发的 30 秒卡死」，功能测试看不见。

`asyncpg` **走 extras**（M0 §3.2 同一条原则）。配了 `backend: postgres` 却没装
extra 时，报错必须包含 `uv sync --extra postgres`，不要让用户看到
`No module named 'asyncpg'`。

`expire_on_commit=False`**。** 默认 `True` 会让 commit 之后访问 ORM 对象触发
隐式 IO。仓库把 row 转成 dict 返回之后就不该再碰 session；设成 False 是明确
「我们不走 identity map，返回的是快照」。

引擎是模块级单例。测试隔离靠 `close_engine()` + 重新 `init_engine`，和 M0 的
`reset_app_config` 同一类纪律：`conftest.py` autouse fixture 每个测试后清掉。

#### `engine.py` 的公开面

```python
# agentkit/persistence/engine.py
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_sqlite_lock = asyncio.Lock()          # 同进程的建表串行化；跨进程靠文件锁 + busy_timeout（§3.2）


async def init_engine_from_config(db: DatabaseConfig) -> None:
    """lifespan 调一次。backend=memory 时什么都不做，session factory 保持 None。"""
    await init_engine(
        db.backend,
        url=db.app_sqlalchemy_url,     # 已经带方言；这里不再拼 URL（§3.4）
        echo=db.echo_sql,
        pool_size=db.pool_size,
        sqlite_dir=db.sqlite_dir,
        postgres_schema=db.postgres_schema,
    )


def get_session_factory() -> async_sessionmaker[AsyncSession] | None:
    return _session_factory            # 仓库工厂靠它决定要不要退化到 memory 实现（§3.3）


async def close_engine() -> None:
    """关机的最后一步（§9）。先 dispose、再置空，两步都不能省。"""
    global _engine, _session_factory
    engine, _engine = _engine, None
    _session_factory = None            # 只关连接不置空，测试会拿到一个已 dispose 的引擎
    if engine is not None:
        await engine.dispose()
```


| 参数                | 类型                                        | 默认      | 谁给                                  | 说明                                                                              |
| ----------------- | ----------------------------------------- | ------- | ----------------------------------- | ------------------------------------------------------------------------------- |
| `backend`         | `Literal["memory", "sqlite", "postgres"]` | ——      | `DatabaseConfig.backend`            | `memory` 直接 return：不建引擎、不跑 bootstrap                                            |
| `url`             | `str`                                     | `""`    | `DatabaseConfig.app_sqlalchemy_url` | **已经带方言**（`+aiosqlite` / `+asyncpg`）。`init_engine` 里再拼一次就会拼出 `+asyncpg+asyncpg` |
| `echo`            | `bool`                                    | `False` | `DatabaseConfig.echo_sql`           | 打印 SQL；排障时开，生产关（日志量按 superstep 放大）                                              |
| `pool_size`       | `int`                                     | `5`     | `DatabaseConfig.pool_size`          | 只对 Postgres 生效；SQLite 用 aiosqlite 的默认池                                          |
| `sqlite_dir`      | `str`                                     | `""`    | `DatabaseConfig.sqlite_dir`         | 目录不存在就建（`to_thread`）；空串按 `"."` 处理                                               |
| `postgres_schema` | `str`                                     | `""`    | `DatabaseConfig.postgres_schema`    | 非空时钉 `search_path`，并在 `bootstrap_schema` **之前** `CREATE SCHEMA IF NOT EXISTS`   |


返回值是 `None`：成功即"模块级单例已就绪"；任何失败都往上抛，让 lifespan 起不来——
**一个连不上库的 Gateway 不应该开始接 HTTP**。

`create_async_engine` **两个后端各自的关键字**（照这张表核代码，多一个少一个都会在
M9 变成偶发故障）：


| 关键字               | SQLite                                 | Postgres                                                         | 为什么                                  |
| ----------------- | -------------------------------------- | ---------------------------------------------------------------- | ------------------------------------ |
| `url`             | `sqlite+aiosqlite:////abs/path/app.db` | `postgresql+asyncpg://…`                                         | 方言表见 §3.4                            |
| `json_serializer` | `_json_serializer`                     | 同左                                                               | 中文不转义成 `\uXXXX`                      |
| `pool_pre_ping`   | ——                                     | `True`                                                           | 丢掉闲置期间被中间设备掐掉的死连接                    |
| `pool_recycle`    | ——                                     | `300`                                                            | 在代理的超时之前主动回收                         |
| `pool_size`       | ——                                     | `pool_size`                                                      | 定容；缺省 5 对单进程够用                       |
| `connect_args`    | ——                                     | `{"command_timeout": 30, "server_settings": {"search_path": …}}` | 单条 SQL 上限 + schema 钉住（只读一次配置，见 §3.4） |
| `connect` 事件      | 4 条 PRAGMA                             | ——                                                               | per-connection，必须挂事件（见下）             |



### 3.2 建表：`create_all` 和 migration 的陷阱

```python
# agentkit/persistence/bootstrap.py
async def bootstrap_schema():
    if await _is_empty_database():
        await _create_all()          # 按当前 ORM 模型直接建
        await _stamp_head()          # 标记 alembic 版本为最新
    else:
        await _upgrade_head()        # 走 migration 链
```

**这里有一个极其容易踩的坑，值得单独一节：**

> **ORM** `__table_args__` **里声明的索引和约束，不能只写在 migration 里。**

理由：空库走 `create_all` + `stamp head` 这条路，**那条 migration 永远不会被执行**。
如果你的部分唯一索引只写在 migration 里，那么所有新部署的库都没有这个索引——
M3 辛苦设计的"每 thread 一个活跃操作"约束在新环境上直接失效，
而在你自己的老开发库上一切正常。

正确做法是**两处都写**：ORM 模型里声明（给 `create_all` 用），
migration 里也写一遍（给已有库升级用）。这看起来是重复，但它们服务于两条不同的路径。

并发建表要加锁：Postgres 用 advisory lock 跨进程串行，SQLite 靠文件锁 + `busy_timeout`。
多个 Gateway 进程同时启动时，没有这个锁会撞成一片 `IntegrityError`。

#### 两条路径就够，第三条留给以后

Greenfield 只需两种库状态：


| 库状态                                    | 动作                                  |
| -------------------------------------- | ----------------------------------- |
| 空（没有任何自己的表）                            | `create_all` + `alembic stamp head` |
| 已有 `alembic_version` 且 revision 在本地链上  | `alembic upgrade head`              |
| 未知 revision / 空 version 表 / 多行 version | **拒绝启动**                            |


**未知 revision 必须 fail-closed。** 典型场景：运维用新二进制跑过一次，又切回旧分支。
旧代码看不见新 revision，如果「好心」地 `stamp` 回去，新列还在、代码却按旧 schema 读写，
数据会静默写错。正确反应是拒绝启动并告诉运维跑文档里的恢复步骤。

空库判定是「自己的表一张都没有」，不是「没有 `alembic_version`」：

```python
# agentkit/persistence/bootstrap.py
_OWNED_TABLES = frozenset({"threads_meta", "runs", "run_events"})

async def _is_empty_database(engine) -> bool:
    async with engine.connect() as conn:
        names = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
    return names.isdisjoint(_OWNED_TABLES)
```

有自己的表、但没有 `alembic_version`（或 version 表空 / 多行 / revision 不在本地链）→
**拒绝启动**。Greenfield 不收「legacy：有表没 alembic」这条分支。
`alembic_version` 自己不算 owned table，空库路径上它是 `stamp` 建出来的。

`0001_baseline.upgrade()` 在空库路径上**永远不会执行**——`stamp head` 直接跳到链尾。
它存在的全部理由是：给「从零用 alembic 建库」留一个链根，以及让
`test_alembic_autogenerate.py` 有东西可 diff。所以 **baseline 必须和当前 ORM 模型产出
完全一致**，否则空库（走 create_all）和「手动 alembic upgrade」（走 0001）会建成两套 schema。

Greenfield 不需要「legacy：有表但没有 alembic_version」这条分支。那是给已经跑了一阵、
后来才引入 alembic 的项目用的。现在就用 alembic，这条路不会出现。

**Postgres advisory lock 包住整个 reflect-and-act 序列**，不是只包 `create_all`。
第二个进程排队等到第一个 stamp/upgrade 完成，再 reflect，看到已经是 head，变成 no-op。
SQLite 没有真正的跨进程 mutex：同进程用 `asyncio.Lock`，跨进程靠文件锁 + 30s busy_timeout，
再靠 idempotent revision 做正确性兜底。**多实例部署必须用 Postgres**，这是 M9 的硬前提，
M4 就要写进配置注释。

---



#### `bootstrap.py` 的接口

```python
# agentkit/persistence/bootstrap.py
class SchemaStateError(RuntimeError):
    """库的 alembic 状态不是本代码能理解的。fail-closed：拒绝启动。"""


async def bootstrap_schema(engine: AsyncEngine, *, backend: str) -> None:
    """空库 → create_all + stamp head；已有库 → upgrade head；其余 → 抛 SchemaStateError。"""
    async with _bootstrap_lock(engine, backend=backend):        # 跨进程串行（见下）
        if await _is_empty_database(engine):
            await _create_all(engine)                          # 按 ORM 模型直接建（含索引）
            await _stamp_head(engine)                          # 把 alembic_version 写到链尾
            return
        revision = await _installed_revision(engine)           # alembic_version 必须是单行
        if revision is None or revision not in _local_revisions():
            raise SchemaStateError(
                f"database is at revision {revision!r}, which is not in this build's alembic chain; "
                "refusing to start (see docs/plans/2026-09-16-m4-persistence-design.md §3.2)"
            )
        if revision != _head_revision():
            await _upgrade_head(engine)                        # 已有库只走这条


@asynccontextmanager
async def _bootstrap_lock(engine: AsyncEngine, *, backend: str) -> AsyncIterator[None]:
    """包住整个 reflect-and-act 序列，不是只包 create_all。"""
    if backend == "postgres":
        async with engine.begin() as conn:                     # advisory lock 绑在事务上，退出即释放
            await conn.execute(sa.text("SELECT pg_advisory_xact_lock(hashtext('agentkit_bootstrap'))"))
            yield
        return
    async with _sqlite_lock:                                   # 同进程；跨进程靠文件锁 + busy_timeout
        yield
```


| 符号                              | 参数                  | 返回 / 抛                        | 说明                                                              |
| ------------------------------- | ------------------- | ----------------------------- | --------------------------------------------------------------- |
| `bootstrap_schema`              | `engine`, `backend` | `None` / `SchemaStateError`   | 只有三条路径：空库、可识别的旧版本、拒绝启动（§3.2 的表）                                 |
| `_bootstrap_lock`               | `engine`, `backend` | 异步上下文管理器                      | Postgres 用 `pg_advisory_xact_lock`；SQLite 用进程内 `asyncio.Lock`   |
| `_is_empty_database`            | `engine`            | `bool`                        | "自己的表一张都没有"，不是"没有 `alembic_version`"（代码见下）                      |
| `_installed_revision`           | `engine`            | `str | None`；多行 / 空表 → `None` | 多行 version 表本身就是异常状态，当成"认不出的 revision"处理                        |
| `_local_revisions`              | 无                   | `frozenset[str]`              | 从本进程的 alembic `ScriptDirectory` 读，不查库——库说什么不算数                  |
| `_stamp_head` / `_upgrade_head` | `engine`            | `None`                        | 都通过 alembic 的 `command` API 跑；**同步 API 要包** `asyncio.to_thread` |


- **两个 alembic 调用都是同步阻塞的**（alembic 没有 async API），必须
`await asyncio.to_thread(command.stamp, cfg, "head")`。直接在事件循环里跑，
M2 的 blocking-IO 套件会抓住它——而且症状是"启动偶尔卡住几秒"。
- `_local_revisions()` **读的是本进程的链**，不是库里的行。这就是"未知 revision 拒绝启动"
能成立的原因：旧代码看不见新 revision，只能拒。



### 3.3 仓库层的三个约定

每个仓库（`ThreadMetaStore` / `RunStore` / …）都遵守同一套形状。

#### 工厂函数，调用方不看 backend

```python
# agentkit/persistence/thread_meta/__init__.py
def make_thread_meta_store(session_factory) -> ThreadMetaStore:
    if session_factory is None:
        return MemoryThreadMetaStore()
    return SqlThreadMetaStore(session_factory)
```

调用方（worker、router、客户端）永远拿 ABC。测试注入 mock 也走这个工厂。
**禁止在业务代码里** `if get_session_factory() is None`**。** 那种分支会复制到每个调用点，
M9 加第三个后端时要改 N 处。

#### `user_id` 的三个取值，默认不是 `None`

```python
# agentkit/persistence/thread_meta/sql.py
async def get(self, thread_id: str, *, user_id: str | None | _AutoSentinel = AUTO) -> dict | None:
    resolved = resolve_user_id(user_id, method_name="ThreadMetaStore.get")
    ...
```


| 传入         | 含义                                           |
| ---------- | -------------------------------------------- |
| 省略（`AUTO`） | 从 ContextVar 取当前用户；没有用户上下文 → 失败（fail-closed） |
| 显式 `str`   | 按这个用户过滤                                      |
| 显式 `None`  | **全局查询**，仅迁移脚本 / 管理路径使用                      |


M3 已经要求读方法带 `user_id` 且不给 `None` 默认值。M4 把默认值定成 `AUTO` 哨兵，
而不是去掉默认值——因为 90% 的调用点就是「用当前用户」，每次都显式传很吵。
关键是 `None` **和「没传」必须是两个东西**：没传 = 当前用户，显式 `None` = 我知道自己在
做跨用户查询。把默认值设成 `None`，M9 时会有一半调用点在无声地全局扫。

`resolve_user_id(AUTO)` 在没有 ContextVar 时要抛，不要静默返回 `"default"`。
worker 后台 task 常常没有用户上下文，所以 **worker 写路径必须把** `record.user_id`
**显式传进去**，不能靠 AUTO。

#### 返回 dict，不返回 ORM 对象

仓库方法返回普通 dict（时间戳已转 ISO、`metadata_json` 已改名为 `metadata`）。
理由：memory 实现没有 ORM 对象可返回；HTTP 层也不该看到 SQLAlchemy。
SQLite 读回的 datetime 是 naive 的（即便列声明了 `timezone=True`），
转 dict 时必须按 UTC 补 tzinfo，否则 JSON 响应里的时间会少一个 `Z`，前端按本地时区解析就偏了。

列名到接口名的对照写死在 `_row_to_dict` 里，不要让 HTTP 层自己 `pop`：


| ORM 列                                            | 仓库 dict / API   |
| ------------------------------------------------ | --------------- |
| `metadata_json`                                  | `metadata`      |
| `kwargs_json`                                    | `kwargs`        |
| `event_metadata`                                 | `metadata`      |
| `created_at` / `updated_at` / `lease_expires_at` | ISO-8601，`Z` 结尾 |



#### 用户上下文：`AUTO` 哨兵与 `resolve_user_id`

```python
# agentkit/runtime/user_context.py
class _AutoSentinel:
    def __repr__(self) -> str:
        return "AUTO"


AUTO = _AutoSentinel()
_current_user: ContextVar[str | None] = ContextVar("agentkit_current_user", default=None)


def set_current_user(user_id: str | None) -> Token:
    """HTTP 中间件 / 内部入口写；persistence 只读。"""
    return _current_user.set(user_id)


def resolve_user_id(user_id: str | None | _AutoSentinel, *, method_name: str) -> str | None:
    """把 AUTO 解析成"当前用户"。解析不出来就抛，绝不静默返回 None。"""
    if not isinstance(user_id, _AutoSentinel):
        return user_id                       # 显式 str 或显式 None（跨用户查询，调用方自己负责）
    resolved = _current_user.get()
    if resolved is None:
        raise MissingUserContextError(
            f"{method_name}() called without an explicit user_id and no user context is set; "
            "background writers must pass record.user_id explicitly"
        )
    return resolved
```


| 参数            | 类型                           | 谁给                             | 语义                                             |
| ------------- | ---------------------------- | ------------------------------ | ---------------------------------------------- |
| `user_id`     | `str | None | _AutoSentinel` | 每个仓库方法的同名关键字参数                 | 省略 = `AUTO` = 当前用户；显式 `None` = 全局查询（迁移 / 管理路径） |
| `method_name` | `str`                        | 调用点手写（`"ThreadMetaStore.get"`） | 只进异常消息。**必须有**：否则运维只知道"少了用户上下文"，不知道是谁在漏传       |



| 抛什么                          | 什么时候                                      | 为什么不让它静默                                           |
| ---------------------------- | ----------------------------------------- | -------------------------------------------------- |
| `MissingUserContextError`    | `AUTO` 且 ContextVar 为空                    | 静默返回 `"default"` 会把匿名写操作混进某个真实用户的数据里（M9 的租户隔离直接失效） |
| `InvalidMetadataFilterError` | `search(metadata=...)` 的 key 不在白名单里（§4.4） | 那个 key 会被插进 SQL 路径表达式，静默忽略等于把注入面留在那里               |


**worker 写路径必须显式传** `record.user_id`**。** 后台 task 是 `create_task` 出来的，
ContextVar 也许还带着，也许已经被清掉——不要赌。M3 的 `RunRecord` 上有 `user_id`，
一路传下来是一行的事。

#### `base.py`：row → dict 的唯一实现

```python
# agentkit/persistence/base.py
class Base(DeclarativeBase):
    pass


def iso_utc(value: datetime | None) -> str | None:
    """SQLite 读回来是 naive 的（即便列写了 timezone=True）；按 UTC 补 tz 再序列化。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


_JSON_COLUMN_ALIASES = {"metadata_json": "metadata", "kwargs_json": "kwargs", "event_metadata": "metadata"}


def row_to_dict(row: Base, *, aliases: Mapping[str, str] = _JSON_COLUMN_ALIASES) -> dict:
    out: dict[str, Any] = {}
    for column in sa_inspect(type(row)).columns:
        name = aliases.get(column.key, column.key)
        value = getattr(row, column.key)
        out[name] = iso_utc(value) if isinstance(value, datetime) else value
    return out
```

- **时间戳和列名映射都收在这一个函数里。** 每个仓库各写一份 `_row_to_dict`，
最后一定会有一个仓库忘了补 `Z`——而症状是前端把时间按本地时区解析，偏 8 小时，
没人会怀疑后端。
- **别名表只在这里**。HTTP 层不许再 `pop("metadata_json")`；要改对外字段名，
改这一张表。



### 3.4 `DatabaseConfig`：一条配置驱动两套连接

用户只配一个 `database.backend`。系统自己拆出 ORM 引擎和 checkpointer 两套连接——
它们**不能共用 SQLAlchemy 引擎**。LangGraph 的 saver 要自己的连接生命周期
（`setup()` / `setup` 期间的 DDL、per-checkpoint 事务），和 ORM 的
`async_sessionmaker` 叠在一起会互相抢事务。

```python
# agentkit/config/database_config.py
class DatabaseConfig(BaseModel):
    backend: Literal["memory", "sqlite", "postgres"] = "memory"
    checkpoint_channel_mode: Literal["full", "delta"] = "full"
    sqlite_dir: str = ".myflow/data"
    postgres_url: str = ""          # 用户写 postgresql://...，$ENV 已在 M0 解析
    echo_sql: bool = False
    pool_size: int = 5              # 仅 ORM；checkpointer 有自己的池
    pool_recycle: int = 300
    command_timeout: float | None = 30
    postgres_schema: str = ""       # 仅 [A-Za-z_][A-Za-z0-9_]* ；钉 search_path

    @property
    def sqlite_path(self) -> str:
        return os.path.join(Path(self.sqlite_dir).resolve(), "app.db")

    @property
    def app_sqlalchemy_url(self) -> str:
        if self.backend == "sqlite":
            return f"sqlite+aiosqlite:///{self.sqlite_path}"
        url = self.postgres_url
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url
```

**URL 方言分叉是硬规则，写进属性不要让调用方拼：**


| 谁用           | SQLite                                        | Postgres                                                   |
| ------------ | --------------------------------------------- | ---------------------------------------------------------- |
| ORM 引擎       | `sqlite+aiosqlite:////abs/path/app.db`        | `postgresql+asyncpg://...`                                 |
| checkpointer | 同一文件的裸路径（`AsyncSqliteSaver.from_conn_string`） | 用户写的 `postgresql://...`（libpq / psycopg，**去掉** `+asyncpg`） |
| alembic（同步）  | `sqlite:////abs/path/app.db`                  | `postgresql+psycopg://...`                                 |


用户配置里只出现 `postgresql://`。`+asyncpg` / `+psycopg` 由属性加。
如果用户自己写了 `postgresql+asyncpg://`，ORM 属性原样用；checkpointer 路径
必须剥掉 `+asyncpg`，否则 libpq 解析失败，报错会看起来像「URL 写错了」。

`postgres_schema` 白名单 `^[A-Za-z_][A-Za-z0-9_]*$`。它会被插进
`SET search_path` 和 `CREATE SCHEMA`，任何更松的字符集都是注入。
非空时：引擎 `connect_args.server_settings.search_path = schema`，
`init_engine` 在 `bootstrap_schema` **之前** `CREATE SCHEMA IF NOT EXISTS`。
`CREATE SCHEMA` 不受 `search_path` 影响，所以即便目标 schema 还不存在也能跑。

**Postgres 库不存在时自动建。** `bootstrap` 抛出且消息含 `does not exist` 时，
连到维护库 `postgres`（`AUTOCOMMIT`，因为 `CREATE DATABASE` 不能在事务里），
`CREATE DATABASE "name"`，然后**用同一套** `connect_args` **重建引擎**再 bootstrap。
漏掉「同一套 connect_args」，重试会落到 `public`，配置的 schema 是空的。

### 3.5 Session 必须短命

```python
# agentkit/persistence/run/sql.py
class SqlRunStore(RunStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def get(self, run_id: str, *, user_id=AUTO) -> dict | None:
        async with self._sf() as session:
            row = await session.get(RunRow, run_id)
            return self._row_to_dict(row) if row else None
```

**每个仓库方法自己开关 session，不要把 session 当参数传来传去。**
run 能跑几分钟，持有连接等于把池打满。worker 的 `astream` 循环里没有任何
`async with session`——token 累加在内存，落库走 `update_run_progress` 的短事务。

写路径：

```python
# agentkit/persistence/run/sql.py（写路径）
async with self._sf() as session:
    session.add(row)
    await session.commit()          # 显式 commit；别靠 context manager 隐式
```

`async with session` 退出时如果没 commit 会 rollback。漏掉 commit 的症状是
「函数返回成功、下一请求看不见行」，单测用同一个 session 还看得到——
所以仓库测试必须 **commit 后再开一个新 session 读**。

### 3.6 登记为 `STARTUP_ONLY`

接到 M0 §5.6 的注册表。这几项在 lifespan 里被冻进单例，热改 YAML 不会生效：

```python
# agentkit/config/database_config.py（注册到 M0 §5.6 的 STARTUP_ONLY_FIELDS）
STARTUP_ONLY_FIELDS |= {
    "database.backend": "init_engine() / make_checkpointer() 在 lifespan 里绑死",
    "database.sqlite_dir": "SQLite 文件路径在引擎创建时解析为绝对路径",
    "database.postgres_url": "连接串在引擎和 checkpointer 池创建时捕获",
    "database.postgres_schema": "search_path 钉在 connect_args 上",
    "database.checkpoint_channel_mode": "编译进图的通道表，跨进程必须一致",
    "database.pool_size": "ORM 连接池在 create_async_engine 时定容",
    "run_events.backend": "make_run_event_store() 在 lifespan 里构造一次",
}
```

`GATEWAY_WORKERS > 1` 且 `run_events.backend != "db"` 时拒绝启动（§5.2），
这条检查也在 lifespan，不算配置字段但属于同一类「启动时门」。

---



## 4. 三张核心表（D3–5）

M4 只建三张。它们的职责**刻意不重叠**。

### 4.1 `threads_meta`——投影，不是权威


| 字段              | 来源                                        | 写入时机             |
| --------------- | ----------------------------------------- | ---------------- |
| `thread_id`     | 客户端给或服务端生成                                | create           |
| `incarnation`   | `uuid4().hex`，服务端生成                       | create           |
| `user_id`       | 从 contextvar 取当前用户                        | create           |
| `display_name`  | run 结束时**从 checkpoint 的** `title` **抄过来** | worker `finally` |
| `status`        | `idle` / `running` / run 的终态              | 见下               |
| `metadata_json` | 客户端 metadata，**剥掉服务端保留键**                 | create / update  |


两个要点：

`incarnation` **现在就要有。** 它是"同名但不是同一条 thread"的判别依据——
用户删掉一条 thread 又用同样的 id 重建，所有持有旧引用的后台任务
（M9 的 MCP 长任务、定时任务）必须能发现自己指向的是一条已经不存在的会话。
没有这一列，它们会把结果写进一条陌生的新会话里。

`display_name` **是投影，**`title` **的权威在 checkpoint。** 侧栏需要一个能直接 SQL 排序
和搜索的列，所以要冗余一份。但两者不一致时，以 checkpoint 为准。

M4 的 ORM（不要加 `project_id`——那是 M9）：

```python
# agentkit/persistence/thread_meta/model.py
class ThreadMetaRow(Base):
    __tablename__ = "threads_meta"

    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incarnation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    assistant_id: Mapped[str | None] = mapped_column(String(128), index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    display_name: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(20), default="idle")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
```

`incarnation` 可空是为了给「手动插的测试行」留口；生产 `create` 必须填 `uuid4().hex`。
`user_id` 可空是为了 M9 引入认证之前的孤儿行能被 `claim_unowned` 认领——M4 的
`create` 仍然每次都写当前用户（没认证时写配置里的单用户 id，不要写 `NULL`）。

`status` 的写入时机要和 M3 的状态机对齐：`create` 给 `idle`；
**worker 的** `try_start` **成功之后**才写 `running`（不是准入时）；收尾时成功回 `idle`，
失败写对应终态。

`metadata_json` **要剥掉服务端保留键。** 客户端可以塞任意 metadata，但下面这些键
由服务端盖章，请求体里出现就丢掉：

```python
# agentkit/persistence/thread_meta/sql.py（create / update_metadata 共用）
_SERVER_RESERVED_METADATA_KEYS = frozenset({
    "owner_id", "user_id",           # 身份，走认证
    "deerflow_trace_id",             # start_run 第 8 步覆盖
})
```

M9 会往这个集合加 `deerflow_project_id`。现在就把过滤写成一个函数，调用点只有
`create` / `update_metadata` 两处。

**行什么时候建：** `POST /api/threads` 显式建；也可能被 `start_run` 顺手建——
发现没有行就用 run 的 metadata 建一条。所以「不先建 thread、直接发消息」能工作，
这是个值得支持的用法，测试要覆盖。

SQLite 上 `create` 要 `BEGIN IMMEDIATE`。默认的 deferred 事务在读完再写时才升级锁，
两个并发 create 会撞成一方 `database is locked`。`IMMEDIATE` 一开始就拿写锁，
另一方等 `busy_timeout`。Postgres 不需要这条，它的 `INSERT` 自己有行锁。

#### `threads_meta` 的列，一列一句


| 列               | 类型                        | 可空  | 默认 / 索引                 | 谁写                                       | 说明                                             |
| --------------- | ------------------------- | --- | ----------------------- | ---------------------------------------- | ---------------------------------------------- |
| `thread_id`     | `String(64)`              | 否   | 主键                      | `create`                                 | 客户端给或服务端生成；同时是 checkpointer 的分区键               |
| `incarnation`   | `String(32)`              | 是   | ——                      | `create`（`uuid4().hex`）                  | 同 id 重建后，旧引用必须失配（见上）                           |
| `assistant_id`  | `String(128)`             | 是   | `index=True`            | `create`                                 | 侧栏按 assistant 过滤用                              |
| `user_id`       | `String(64)`              | 是   | `index=True`            | `create`（当前用户；无认证时写单用户 id，**不写** `NULL`） | 可空只为让 M9 之前的孤儿行能被 `claim_unowned` 认领           |
| `display_name`  | `String(256)`             | 是   | ——                      | worker `finally`                         | **投影**，权威在 checkpoint 的 `title`                |
| `status`        | `String(20)`              | 否   | `default="idle"`        | `try_start` 成功后 / `finally`              | `idle` / `running` / run 的终态；**不是** run 状态机的复制 |
| `metadata_json` | `JSON`                    | 否   | `default=dict`          | `create` / `update_metadata`             | 已剥服务端保留键（见下）                                   |
| `created_at`    | `DateTime(timezone=True)` | 否   | `datetime.now(UTC)`     | `create`                                 | ——                                             |
| `updated_at`    | `DateTime(timezone=True)` | 否   | `now(UTC)` + `onupdate` | 任何写（`touch=False` 除外）                    | 侧栏排序键；`touch=False` 的写路径要显式跳过它                 |



#### 保留键：先删，后写

```python
# agentkit/persistence/thread_meta/sql.py
def strip_reserved_metadata(metadata: Mapping[str, Any] | None) -> dict:
    """create 和 update_metadata 共用的唯一入口。请求体里出现保留键就丢掉，不报错。"""
    return {k: v for k, v in (metadata or {}).items() if k not in _SERVER_RESERVED_METADATA_KEYS}
```

- **丢弃而不是 422。** 保留键是服务端盖章的位置；客户端带上来多半是从别处复制的对象。
报 422 会让"复制一条 thread 的 metadata 再改"这种正常操作失败——不是校验后使用，是删掉。
- 调用点只有两处：`create` / `update_metadata`。出现第三处时，把它挪进这个函数而不是复制一行。



#### SQLite 上要 `BEGIN IMMEDIATE`

```python
# agentkit/persistence/engine.py（只挂在 sqlite 分支的引擎上）
@event.listens_for(_engine.sync_engine, "begin")
def _sqlite_begin_immediate(conn) -> None:
    """默认的 deferred 事务在"先读后写"时才升级锁：两个并发 create 会撞成 database is locked。
    BEGIN IMMEDIATE 一开始就拿写锁，另一方在 busy_timeout（30s）里排队。"""
    conn.exec_driver_sql("BEGIN IMMEDIATE")
```

- 这是 SQLAlchemy 文档给的 SQLite 写事务配方，比在每个仓库方法里手写 `BEGIN IMMEDIATE` 可靠——
手写会因为"事务已经隐式开始"而报 `cannot start a transaction within a transaction`。
- WAL 下读者不受影响，写者本来就要串行，所以这条不牺牲并发。Postgres 不需要：
它的 `INSERT` 自己有行锁。



### 4.2 `runs`——把 M3 的 `RunRecord` 序列化

字段按**来源**分五组，这个分组本身就是设计文档：

**① 准入时一次写定**：`run_id`、`thread_id`、`assistant_id`、`user_id`、
`status='pending'`、`operation_kind`、`idempotency_key`、`multitask_strategy`、
`metadata_json`、`kwargs_json`、`model_name`。

**② 状态机**：只有 `start_run()` 的 CAS 能进 `running`；
终态由 worker 写，且 `update_run_completion` **必须拒绝覆盖另一个终态**。
这条守卫堵的是"peer 接管之后，原 owner 迟到收尾"的竞争——M3 里还没有 peer，
但守卫现在加上是零成本的。

**③ 租约与取消**：`owner_worker_id`、`lease_expires_at`、`cancel_action`、
`cancel_requested_at`。M4 只建列不用，M9 才有意义。

**④ token 与便利字段**：`total_input_tokens` / `total_output_tokens` /
`llm_call_count` / `first_human_message` / `last_ai_message`（后两个截断到 2000 字符）。

> **这些数字来自运行时的内存累加，不是从** `run_events` **反算的。**
> 反算看起来更"单一真相源"，但它要求账本覆盖每一次模型调用、
> 每次查询都要扫一遍行、而且中间件产生的调用很容易漏掉。
> 内存累加 + 定期落库是正确的权衡。

写入有两个时机：跑的过程中 `update_run_progress`（**节流约 5 秒，且只在**
`status='running'` **的行上生效，不动 status**），结束时和终态一起写。

**⑤ 并发保证**：

```sql
# 0001_baseline 产出的索引（Postgres 视角；ORM 里的同一条见 §4.2）
CREATE UNIQUE INDEX uq_runs_thread_active
    ON runs (thread_id) WHERE status IN ('pending', 'running');
CREATE UNIQUE INDEX uq_runs_idempotency_key ON runs (idempotency_key);
```

这是 M3 §5.3 那个约束的物理落地。**它是跨进程的硬保证，不靠应用层锁。**

部分唯一索引的 `WHERE` 子句在 SQLite 和 Postgres 上语法不同，ORM 里要两个都写：

```python
# agentkit/persistence/run/model.py（__table_args__ 里的那一条）
Index(
    "uq_runs_thread_active",
    "thread_id",
    unique=True,
    sqlite_where=text("status IN ('pending', 'running')"),
    postgresql_where=text("status IN ('pending', 'running')"),
)
```

只写一边，另一边的 `create_all` 会建成普通（非部分）唯一索引——所有终态行也参与唯一，
第二条历史 run 就插不进去。

`kwargs_json` 存的是**脱敏之后**的副本（`redact_config_secrets(body.config)`），
因为 runs 的查询 API 会原样回显这个字段。

M4 的 ORM（M8/M9 才用到的列现在就建，空着零成本；后加列要 migration）：

```python
# agentkit/persistence/run/model.py
class RunRow(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    assistant_id: Mapped[str | None] = mapped_column(String(128))
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    operation_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="run", server_default=text("'run'")
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    model_name: Mapped[str | None] = mapped_column(String(128))
    multitask_strategy: Mapped[str] = mapped_column(String(20), default="reject")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    kwargs_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    stop_reason: Mapped[str | None] = mapped_column(String(50))

    message_count: Mapped[int] = mapped_column(default=0)
    first_human_message: Mapped[str | None] = mapped_column(Text)
    last_ai_message: Mapped[str | None] = mapped_column(Text)

    total_input_tokens: Mapped[int] = mapped_column(default=0)
    total_output_tokens: Mapped[int] = mapped_column(default=0)
    total_tokens: Mapped[int] = mapped_column(default=0)
    llm_call_count: Mapped[int] = mapped_column(default=0)
    lead_agent_tokens: Mapped[int] = mapped_column(default=0)
    subagent_tokens: Mapped[int] = mapped_column(default=0)
    middleware_tokens: Mapped[int] = mapped_column(default=0)
    token_usage_by_model: Mapped[dict] = mapped_column(
        JSON, default=dict, server_default=text("'{}'")
    )

    follow_up_to_run_id: Mapped[str | None] = mapped_column(String(64))
    owner_worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_action: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index("ix_runs_thread_status", "thread_id", "status"),
        Index("ix_runs_lease", "lease_expires_at"),
        Index("uq_runs_idempotency_key", "idempotency_key", unique=True),
        Index(
            "uq_runs_thread_active",
            "thread_id",
            unique=True,
            sqlite_where=text("status IN ('pending', 'running')"),
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )
```

`operation_kind` 的 `server_default` 不能省：空库 `create_all` 靠 ORM default，
老库加列靠 server_default，两条路径都要有值。`token_usage_by_model` 同理。

`model_name` 写入前 strip，超 128 截断。模型名来自请求，不截断会在 PG 上
直接 `StringDataRightTruncation`，表现为偶发 500。

`create_thread_operation_atomic`（M3 已定）的 SQL 实现：

```python
# agentkit/persistence/run/sql.py
async def create_thread_operation_atomic(self, record: dict, *, kind: str) -> bool:
    try:
        async with self._sf() as session:
            session.add(RunRow(**self._to_row_kwargs(record, kind)))
            await session.commit()
        return True
    except IntegrityError:
        return False    # 活跃冲突或幂等键冲突，上层看 False 再 get_by_idempotency_key
```

**禁止把** `IntegrityError` **抛出仓库。** SQLite 和 Postgres 的异常类型/消息都不同，
路由层不该 import 数据库方言。幂等命中 vs 活跃冲突的区分：`False` 之后
`get_by_idempotency_key`，命中就是 200 原 run，未命中就是 409 busy。

`runs` **行不删**，它是可读历史。只有非 run 的 thread 操作
（compact / state_update / delete）在收尾时删掉自己那行短命的占位记录。

#### 五组字段落在哪，谁在什么时候写


| 组             | 列                                                                                                                                                                                                                   | 谁写                                              | 什么时候              | 走 CAS 吗                      |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- | ----------------- | ---------------------------- |
| ① 准入一次写定      | `run_id` `thread_id` `assistant_id` `user_id` `status='pending'` `operation_kind` `idempotency_key` `multitask_strategy` `metadata_json` `kwargs_json` `model_name`                                                 | `create_thread_operation_atomic`                | 准入（§4.2 的 SQL 实现） | 插入时的两个唯一约束就是它的 CAS           |
| ② 状态机         | `status` `error` `stop_reason`                                                                                                                                                                                      | `start_run` / `update_run_completion`           | try_start / 收尾    | **是**（§4.5）                  |
| ③ 租约与取消       | `owner_worker_id` `lease_expires_at` `cancel_action` `cancel_requested_at`                                                                                                                                          | M9                                              | ——                | M4 只建列，不写                    |
| ④ token 与便利字段 | `total_input_tokens` `total_output_tokens` `total_tokens` `llm_call_count` `lead_agent_tokens` `subagent_tokens` `middleware_tokens` `token_usage_by_model` `message_count` `first_human_message` `last_ai_message` | `update_run_progress` / `update_run_completion` | 节流 5 秒 / 收尾       | 进度是 `WHERE status='running'` |
| ⑤ 并发保证        | 两个唯一索引（见上）                                                                                                                                                                                                          | DDL                                             | 建表 / migration    | ——                           |



#### 三个写路径的 SQL

```python
# agentkit/persistence/run/sql.py
TERMINAL_STATUSES = ("success", "error", "timeout", "interrupted")


async def start_run(self, run_id: str) -> bool:
    """pending → running。唯一入库入口；返回 False 表示行已经不是 pending（多半被取消了）。"""
    async with self._sf() as session:
        result = await session.execute(
            update(RunRow)
            .where(RunRow.run_id == run_id, RunRow.status == "pending")
            .values(status="running", updated_at=datetime.now(UTC))
        )
        await session.commit()
        return bool(result.rowcount)


async def update_run_progress(self, run_id: str, **totals: Any) -> None:
    """WHERE status='running'。CAS 落空就静默：run 已终态，进度数字不再有意义（M3 §5.6）。"""
    async with self._sf() as session:
        await session.execute(
            update(RunRow)
            .where(RunRow.run_id == run_id, RunRow.status == "running")
            .values(**totals, updated_at=datetime.now(UTC))
        )
        await session.commit()


async def update_run_completion(
    self, run_id: str, *, status: str, error: str | None = None, **totals: Any
) -> bool:
    """终态 + token 汇总一次写完。同一个终态可以写两遍（崩溃重试），换成另一个终态被拒。"""
    async with self._sf() as session:
        result = await session.execute(
            update(RunRow)
            .where(
                RunRow.run_id == run_id,
                or_(RunRow.status.not_in(TERMINAL_STATUSES), RunRow.status == status),
            )
            .values(status=status, error=error, updated_at=datetime.now(UTC), **totals)
        )
        await session.commit()
        return bool(result.rowcount)
```

- `rowcount` **就是返回值。** 三个 CAS 方法都靠它区分"写进去了"和"被别人抢先了"；
返回 `None` 的话，取消与完成撞车（M3 §9.3）只能靠再读一遍库去猜。
- `update_run_progress` **不带** `status`**。** 进度快照永远不许改状态机，
否则一个迟到的进度写会把终态行拉回 `running`。
- **时间戳在 Python 里取**（`datetime.now(UTC)`），不用 `func.now()`：SQLite 的
`CURRENT_TIMESTAMP` 没有时区，读回来还得再补一次 UTC（§7.2 的第一条差异）。
- `**totals` **的键必须是列名。** 传进来一个不存在的键，SQLAlchemy 会抛
`CompileError`——比悄悄丢掉好，但要在 M3 的 `RunTotals` → 列名这一层做映射
（`lead_agent_tokens` 之类是同名的，唯一要小心的是 `token_usage_by_model` 的 JSON 化）。



#### `list_by_thread` 的 keyset 分页

```python
# agentkit/persistence/run/sql.py
def _encode_cursor(row: RunRow) -> str:
    return f"{iso_utc(row.created_at)}|{row.run_id}"          # 分隔符用 | ，别用 + ——
                                                             # query string 会把 + 解码成空格

def _parse_cursor(cursor: str) -> tuple[datetime, str]:
    created_at, _, run_id = cursor.partition("|")
    return datetime.fromisoformat(created_at.replace("Z", "+00:00")), run_id


async def list_by_thread(self, thread_id: str, *, user_id=AUTO, limit: int = 100,
                         before: str | None = None) -> list[dict]:
    stmt = select(RunRow).where(RunRow.thread_id == thread_id, RunRow.operation_kind == "run")
    if (uid := resolve_user_id(user_id, method_name="RunStore.list_by_thread")) is not None:
        stmt = stmt.where(RunRow.user_id == uid)
    if before is not None:
        stmt = stmt.where(tuple_(RunRow.created_at, RunRow.run_id) < _parse_cursor(before))
    stmt = stmt.order_by(RunRow.created_at.desc(), RunRow.run_id.desc()).limit(limit)
    async with self._sf() as session:
        rows = (await session.scalars(stmt)).all()
    return [self._row_to_dict(row) for row in rows]
```

- `(created_at, run_id)` **元组比较**两边都支持（SQLite ≥ 3.15 的行值比较 / Postgres 原生）。
只按 `created_at` 翻页会在同一毫秒创建的两条 run 上漏行或重复。
- `list_by_thread` **只列** `operation_kind='run'` **的行。** 非 run 的短命占位行
（compact / state_update）不该出现在 API 里，它们在自己收尾时就被删掉了。



### 4.3 `run_events`——append-only 的账本


| 列                         | 说明                    |
| ------------------------- | --------------------- |
| `thread_id` / `run_id`    | 账本按 thread 连续，行上带 run |
| `event_type` / `category` | 来自一份集中的 catalog       |
| `content`                 | 事件正文（消息类是 JSON 串）     |
| `event_metadata`          | 附加结构                  |
| `seq`                     | **thread 内严格递增**      |


索引：`(thread_id, seq)` 唯一、`(thread_id, category, seq)`、`(thread_id, run_id, seq)`。

`seq` **是 thread 全局递增的，不是每个 run 从 0 开始。** 前端要按时间顺序渲染整条会话
的事件流，跨 run 排序必须靠单调序号。用 `(run_id, seq)` 复合排序在 run 并发创建时
会错乱，用 `created_at` 排序则会在同毫秒的事件上不稳定。

`category` 决定谁读它：


| category    | 谁在乎                                                          |
| ----------- | ------------------------------------------------------------ |
| `message`   | **前端历史消息**，`list_messages` 只返回这一类                            |
| `trace`     | 调试                                                           |
| `outputs`   | 投递回执                                                         |
| `error`     | 错误                                                           |
| `subagent`  | 子代理生命周期（`subagent.start/step/end`），M8 写入；`list_messages` 不返回 |
| `context`   | 注入块身份（`context:memory`），M10 写入                               |
| `workspace` | 工作区变更（`workspace.changes`），M10 写入                            |


`category` **现在就要有，哪怕 M4 只用到两三个值。** 它是 `list_messages`
和 `list_events` 这两个查询的分区维度，后加意味着所有历史行都没有正确的值。

#### `event_type` / `category` 的权威在 catalog，不在字符串字面量

```python
# agentkit/runtime/events/catalog.py
RUN_START_EVENT      = RunEventDefinition("run.start",       "trace")
RUN_END_EVENT        = RunEventDefinition("run.end",         "outputs")
RUN_ERROR_EVENT      = RunEventDefinition("run.error",       "error")
LLM_HUMAN_INPUT      = RunEventDefinition("llm.human.input", "message")
LLM_AI_RESPONSE      = RunEventDefinition("llm.ai.response", "message")
LLM_TOOL_RESULT      = RunEventDefinition("llm.tool.result", "message")
LLM_ERROR_EVENT      = RunEventDefinition("llm.error",       "trace")
```

生产者 `from catalog import LLM_AI_RESPONSE`，不要自己拼 `"llm.ai.response"`。
`event_type` 最长 32 字符，`category` 最长 16 字符——这两个上限写在
`constants.py`（无依赖模块），catalog 的 dataclass `__post_init__` 强制执行。
M6 的中间件事件是 `middleware:{tag}` 这种动态名，tag 最长
`32 - len("middleware:")` = 21，超了在构造时就炸，不要等到 INSERT。

配套一条 CI 测试：catalog 里的每一条定义都要出现在
`contracts/run_event_stream_contract.json`，反过来也是。改一边漏另一边立刻红。
M4 先把合同文件建出来，哪怕只有上面那 7 条：

```json
# contracts/run_event_stream_contract.json
{
  "events": [
    {"event_type": "run.start", "category": "trace"},
    {"event_type": "run.end", "category": "outputs"},
    {"event_type": "run.error", "category": "error"},
    {"event_type": "llm.human.input", "category": "message"},
    {"event_type": "llm.ai.response", "category": "message"},
    {"event_type": "llm.tool.result", "category": "message"},
    {"event_type": "llm.error", "category": "trace"}
  ]
}
```



#### 信封字段

写入方传入的是：

```python
# RunEventStore.put / put_batch 的信封（写入方构造；seq / created_at 不传）
{
    "thread_id": str,
    "run_id": str,
    "event_type": str,          # 来自 catalog
    "category": str,            # 来自 catalog，不要让调用方自己填一个对不上的
    "content": str | dict,      # dict 会被 json.dumps；db 后端打 content_is_json
    "metadata": dict | None,    # 落库列名 event_metadata，API 仍叫 metadata
}
```

`seq` 和 `created_at` 由 store 分配，调用方不要传。`user_id` 由 store 软读 ContextVar，
读不到就留 `NULL`——worker 后台写常常没有用户上下文，这是正常的，不要为此失败。

`RunJournal` **不实现** `on_llm_new_token`**。** token 走 SSE；账本只收完整消息。
把每个 token 写成一行会让 `run_events` 按输出长度线性膨胀，翻历史会慢到不可用。

M4 的 ORM：

```python
# agentkit/persistence/events/model.py
class RunEventRow(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)     # RUN_EVENT_TYPE_MAX_LENGTH
    category: Mapped[str] = mapped_column(String(16), nullable=False)      # RUN_EVENT_CATEGORY_MAX_LENGTH
    content: Mapped[str] = mapped_column(Text, default="")
    event_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    seq: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        UniqueConstraint("thread_id", "seq", name="uq_events_thread_seq"),
        Index("ix_events_thread_cat_seq", "thread_id", "category", "seq"),
        Index("ix_events_run", "thread_id", "run_id", "seq"),
    )
```

`id` 是内部主键，不对外。对外游标永远是 `(thread_id, seq)`。
`content` 永远是 `Text`：传入 dict 时 `json.dumps(..., ensure_ascii=False)`，
并在 `event_metadata` 打 `content_is_json: true`。读回时若有这个标记（或
`category=="message"`）再 `json.loads`。不要用 JSON 列存 content——消息体是
给前端当字符串/对象两种形状用的，列类型锁死一种会让调试事件（纯文本）别扭。

### 4.4 `ThreadMetaStore`：M4 最小集

M9 会加 `claim_unowned` / `set_project` / `update_owner`。M4 只实现下面这些：

```python
# agentkit/persistence/thread_meta/base.py
class ThreadMetaStore(abc.ABC):
    async def create(self, thread_id, *, assistant_id=None, user_id=AUTO,
                     display_name=None, metadata=None) -> dict: ...
    async def get(self, thread_id, *, user_id=AUTO) -> dict | None: ...
    async def search(self, *, metadata=None, status=None, limit=100, offset=0,
                     user_id=AUTO) -> list[dict]: ...
    async def update_display_name(self, thread_id, display_name, *, user_id=AUTO) -> None: ...
    async def update_status(self, thread_id, status, *, user_id=AUTO) -> None: ...
    async def update_metadata(self, thread_id, metadata, *, touch=True, user_id=AUTO) -> None: ...
    async def delete(self, thread_id, *, user_id=AUTO) -> None: ...
    async def check_access(self, thread_id, user_id: str, *, require_existing=False) -> bool: ...
```

`search` 默认按 `updated_at DESC, thread_id DESC`。`metadata` 过滤走
`json_value_matches` / `JsonMatch`，非法 key 抛 `InvalidMetadataFilterError` → 路由 400。
`update_metadata` 是 merge（传入的 key 覆盖，没传的保留）；`touch=False` 给 pin
这类「不是对话活动」的元数据，不刷新 `updated_at`，侧栏排序不动。
`create` 在已存在且 `user_id` 不同时抛 `ThreadOwnershipConflictError` → 路由 409，
同用户重复 create 视为幂等，返回已有行。

`check_access`：行不存在时，`require_existing=False` 返回 True（允许「先发消息再建
thread」），`True` 返回 False。这是 M3 准入路径 `_ensure_thread_metadata` 的前置。

#### `ThreadMetaStore` 的八个方法


| 方法                    | 参数                                                                                     | 返回 / 抛                                      | 要点                                                                     |
| --------------------- | -------------------------------------------------------------------------------------- | ------------------------------------------- | ---------------------------------------------------------------------- |
| `create`              | `thread_id`, `assistant_id=None`, `user_id=AUTO`, `display_name=None`, `metadata=None` | `dict` / `ThreadOwnershipConflictError`     | 已存在且同用户 → 幂等返回已有行；不同用户 → 抛（§4.4）                                       |
| `get`                 | `thread_id`, `user_id=AUTO`                                                            | `dict | None`                               | 属主过滤在 SQL 里；`AUTO` 无上下文就抛（§3.3）                                        |
| `search`              | `metadata=None`, `status=None`, `limit=100`, `offset=0`, `user_id=AUTO`                | `list[dict]` / `InvalidMetadataFilterError` | 排序 `updated_at DESC, thread_id DESC`；metadata 过滤走 `json_value_matches` |
| `update_display_name` | `thread_id`, `display_name`, `user_id=AUTO`                                            | `None`                                      | 抄 checkpoint 的 `title`；空串当"没标题"，不要写空                                   |
| `update_status`       | `thread_id`, `status`, `user_id=AUTO`                                                  | `None`                                      | 只由 worker 的 `try_start` / `finally` 调（§4.1 的时机）                        |
| `update_metadata`     | `thread_id`, `metadata`, `touch=True`, `user_id=AUTO`                                  | `None`                                      | merge 语义；`touch=False` 不刷新 `updated_at`（pin 这类非对话活动）                   |
| `delete`              | `thread_id`, `user_id=AUTO`                                                            | `None`                                      | **只删投影行**；checkpoint 和磁盘由 §10 的顺序负责                                    |
| `check_access`        | `thread_id`, `user_id: str`, `require_existing=False`                                  | `bool`                                      | 行不存在时：`require_existing=False` → `True`（允许先发消息再建 thread）               |

### 4.5 `RunStore`：把 M3 的十个方法接到 SQL

接口不改。SQL 实现要注意的只有三处：

1. `create_thread_operation_atomic` 吞 `IntegrityError` 返回 `False`（§4.2）。
2. `update_run_completion` 的 WHERE 是
  `status NOT IN ('success','error','timeout','interrupted') OR status = :new_status`——
   允许把同一个终态写两遍（崩溃重试），拒绝换成另一个终态。返回 `False` 表示没更新到行。
3. `update_run_progress` 的 WHERE 是 `status = 'running'`。CAS 失败就静默
  return——run 已经终态了，进度数字不再有意义。
4. `list_by_thread` 按 `(created_at DESC, run_id DESC)` 做 keyset 分页，
  游标用 `Z` 结尾的 ISO 时间，避免 query string 把 `+` 解码成空格。
5. `list_inflight()` 返回所有 `status IN ('pending','running')` 的行，
  **不按 user_id 过滤**——启动回收是进程级的，不是用户级的。

`delete_thread_operation` 必须校验 `operation_kind != 'run'`。对普通 run 调用它
是 bug，要抛而不是静默删掉用户的历史。

#### `RunStore`：M3 的十个方法接到 SQL


| 方法                               | 参数                                                   | 返回            | SQL 要点                                                               |
| -------------------------------- | ---------------------------------------------------- | ------------- | -------------------------------------------------------------------- |
| `create_thread_operation_atomic` | `row: dict`, `kind`, `supersede_active=False`        | `bool`        | 吞 `IntegrityError` 返回 `False`（§4.2）；`supersede_active` 时先标旧行再插，同一个事务 |
| `start_run`                      | `run_id`                                             | `bool`        | `WHERE status='pending'`                                             |
| `update_status`                  | `run_id`, `status`, `error=None`, `stop_reason=None` | `bool`        | `WHERE status IN ('pending','running')`                              |
| `update_run_progress`            | `run_id`, `**totals`                                 | `None`        | `WHERE status='running'`；落空静默                                        |
| `update_run_completion`          | `run_id`, `status`, `error=None`, `**totals`         | `bool`        | `status NOT IN (终态) OR status = :new`（§4.5 第 2 条）                    |
| `get`                            | `run_id`, `user_id`                                  | `dict | None` | `user_id` 无默认值：每个调用点显式表态（M3 §5.1）                                    |
| `list_by_thread`                 | `thread_id`, `user_id`, `limit=100`, `before=None`   | `list[dict]`  | keyset 分页（见上）；只列 `operation_kind='run'`                              |
| `list_inflight`                  | 无                                                    | `list[dict]`  | `status IN ('pending','running')`，**不按 user_id 过滤**（启动回收是进程级的）       |
| `get_by_idempotency_key`         | `scoped_key`                                         | `dict | None` | 幂等命中与活跃冲突的区分就靠它（§4.2）                                                |
| `delete_thread_operation`        | `run_id`, `user_id`                                  | `None`        | `operation_kind != 'run'` 才允许删；对 run 行抛 `ValueError`                 |



---

## 5. RunEventStore 与 RunJournal（D6–7）



#### 分工：一个是存储器，一个是生产者

账本这条链上只有两个对象，**职责不能混**：

```
LangChain 回调 ──► RunJournal ──put_batch──► RunEventStore ──► run_events / *.jsonl / 进程 dict
                       ▲                          ▲
                图的执行（callback）        worker finally 的 flush、
                                          终态回执（put_if_absent）、
                                          checkpoint seed（§6.4）
```


|                | `RunEventStore`（§5.1–§5.6）             | `RunJournal`（§5.7）                       |
| -------------- | -------------------------------------- | ----------------------------------------- |
| 是什么            | 存储接口 + 三个后端实现                          | 一个 LangChain callback handler，账本的**唯一常规生产者** |
| 认识什么           | 信封的六个字段（§5.6 的表）                       | LangChain 的回调语义（chain / llm / tool）、本 run 的 `RunRecord` |
| **不认识什么**      | 不认识 LangChain 类型（`BaseMessage` / `LLMResult`），不知道"这条消息是什么意思" | 不认识 SQL，不知道 `seq` 怎么分配，不 import `sqlalchemy` |
| 谁调它            | RunJournal（常规）、worker 收尾的终态回执、checkpoint seed、M8 子代理 | LangChain 在图执行过程中调；worker 收尾调它的 `flush()` |
| 谁决定 `event_type` | ——                                     | journal（从 catalog 取常量）                     |
| 谁决定 `seq` / `created_at` | store                                  | ——                                         |
| 契约在哪           | §5.1 接口保证、§5.6 存什么                      | §5.7 回调映射                                  |


**两条硬边界**：

- `RunJournal` **不许自己拼 SQL、不许自己算 `seq`**——那是 store 的事（§5.3 的双层保护）。
- `RunEventStore` **不许 import LangChain 的类型**。它的输入永远是那六个字段的 dict（§4.3 的信封）。
  这样换一个生产者（M8 的子代理写手、M10 的记忆模块）时，store 一行都不用改。

### 5.1 接口保证

```python
# agentkit/persistence/events/base.py
class RunEventStore(abc.ABC):
    async def put(self, ...) -> None: ...                  # 单条，低频路径
    async def put_batch(self, events: list[...]) -> None: ...   # 常规路径
    async def put_if_absent(self, event: dict) -> tuple[dict, bool]: ...   # 终态回执，可重试

    async def list_messages(self, thread_id, limit, before_seq=None, after_seq=None): ...
    async def list_events(self, thread_id, run_id, event_types=None, after_seq=None): ...
```

五条所有后端都要满足的保证：

1. `put` 之后立刻能查到。
2. `seq` **在同一** `thread_id` **内严格递增。**
3. `list_messages()` 只返回 `category="message"`。
4. `list_events()` 返回该 run 的全部类别。
5. 消费者忽略未知字段（后端可以多返回列）。

双向游标：`before_seq` 拿更旧的一页，`after_seq` 拿更新的一页，**返回都按 seq 升序**。
两边都不传等于最新 `limit` 条。返回顺序统一成升序，
省掉前端在两个方向上写两套拼接逻辑。

#### `RunEventStore` 四个方法的参数与异常


| 方法                                                                 | 参数                                 | 返回 / 抛              | 要点                                           |
| ------------------------------------------------------------------ | ---------------------------------- | ------------------- | -------------------------------------------- |
| `put(event)`                                                       | 信封（§4.3）                           | `None`              | 低频路径；每条一次锁 + 一次往返，正常执行不要用（§5.3）              |
| `put_batch(events)`                                                | `Sequence[dict]`，同 thread          | `list[dict]` 等长同序   | 常规路径；seq 连续分配、整批原子（§5.4）                     |
| `put_if_absent(event)`                                             | 信封；必须带 `event_type`                | `tuple[dict, bool]` | `created=False` 表示这行已经有人写过（终态回执重试）；实现签名见下    |
| `list_messages(thread_id, limit, before_seq=None, after_seq=None)` | `thread_id`, `limit`, 两个可选游标       | `list[dict]` **升序** | 只返回 `category='message'`；`before_seq` 取更旧的一页 |
| `list_events(thread_id, run_id, event_types=None, after_seq=None)` | `thread_id`, `run_id`, 可选类型集合、可选游标 | `list[dict]` **升序** | 返回该 run 的全部类别（含 `trace`）                     |


`put_if_absent` **的返回是** `(record, created)`**，上面 ABC 里的** `-> None` **是草图。**
调用方（补写终态回执的路径）必须知道是不是自己写的：`created=True` 才发布对应的 SSE 事件，
否则两个路径会各发一次。

### 5.2 三个后端与 `seq` 分配


| backend  | 落在哪                              | `seq` 保证                                                          |
| -------- | -------------------------------- | ----------------------------------------------------------------- |
| `memory` | 进程 dict                          | 单进程单调                                                             |
| `db`     | SQL 表                            | 进程内 per-thread `asyncio.Lock` + DB 行锁；`UNIQUE(thread_id, seq)` 兜底 |
| `jsonl`  | `threads/{tid}/runs/{rid}.jsonl` | **仅单进程**，多进程同目录会重复/乱序                                             |


`max_trace_content` **只切** `category=trace`**。** 消息类事件不能截断——截断后前端
会渲染出半截 JSON，比丢失更糟。超限时把 content 截到上限，metadata 打
`content_truncated: true`。memory / jsonl 后端可以不实施这条，因为它们本来就不是
给生产 trace 用的。

`seq` 的分配是 `max(seq) + 1`，必须串行化。双层保护：应用层的 per-thread 锁负责
正常路径的性能（避免每次都撞唯一索引重试），数据库的唯一约束负责正确性兜底。

`run_events.backend` **和** `database.backend` **不联动，而且启动时冻结。**
这两点都是刻意的：本地开发常常是 `database: sqlite` 但 `run_events: memory`
（不想让调试事件污染库）；而冻结是因为 store 实例在 lifespan 里构造一次，
热切后端会让已经持有旧引用的组件写进一个没人读的地方。

默认值建议 `memory`，并在文档里写清"要持久化历史必须显式设 `db`"——
让用户显式选择比猜一个默认值安全。

`database.backend=memory` 但 `run_events.backend=db` 时，没有 session factory，
工厂必须退回 `MemoryRunEventStore` 并打 warning，不要等到第一笔 `put` 才 500。

`GATEWAY_WORKERS > 1` **必须** `run_events.backend=db`**。** memory / jsonl 是进程本地的，
跨进程无法共享有序账本。这条门在 lifespan 里检查，配错就拒绝启动。M9 才真的多 worker，
但门现在就写——否则有人先开了 `GATEWAY_WORKERS=2` 做负载测试，账本会静默分叉。

jsonl 的 `list_messages()` 必须扫该 thread 下**所有 run 文件**才能按全局 seq 排；
`list_events()` 只读一个文件。文件 IO 走 `asyncio.to_thread`。这是 jsonl 作为调试后端
能接受、作为生产后端不能接受的原因。

### 5.3 `put` vs `put_batch`

`put` **是低频路径，正常执行必须用** `put_batch`**。**

每条事件一次 `put` 意味着每条都要拿一次 thread 锁 + 一次数据库往返。
M8 的深层子代理一步一个事件，能把 Postgres 的锁打满。
正确做法是在运行时缓冲（比如 20 条一批），在阶段边界和 `finally` 里 flush。

**缓冲、定名、截断与 flush 全部归 §5.7 的 `RunJournal`；本节只管 store 这一侧的批量语义**
（一个事务、连续 seq、整批成败）。两边不要互相实现对方的职责。

`put_if_absent` 给终态回执用：worker 崩溃后由别的路径补写一条，
重复调用不产生第二行。检查和写入必须跟该 thread 的普通 writer 串行化。
返回 `(record, created)`，调用方靠 `created` 判断是不是自己写的。

#### `seq` 分配的双层保护

```python
# agentkit/persistence/events/db.py
async def _next_seq(self, thread_id: str) -> int:
    async with self._locks[thread_id]:          # 进程内：避免每次都撞唯一索引
        async with self._sf() as session:
            last = await session.scalar(
                select(func.max(RunEventRow.seq))
                .where(RunEventRow.thread_id == thread_id)
                .with_for_update()              # Postgres：跨进程
            )
            return (last or 0) + 1
```

SQLite 没有真正的 `FOR UPDATE`，靠 `BEGIN IMMEDIATE` + 唯一约束。
唯一约束冲突时**重试**，不要把 `IntegrityError` 抛给 worker——那会让
`finally` 里的 flush 失败，用户刷新后缺消息。重试上限 3 次，再失败才记 error。

`_locks` **用** `weakref` **或定期清理。** 长驻进程里 thread_id 无界，
普通 dict 会无限增长。和 M2 的文件锁是同一类问题。

#### `db` 后端的写入路径：一批一个事务，seq 连续分配

```python
# agentkit/persistence/events/db.py
SEQ_RETRIES = 3


class DbRunEventStore(RunEventStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def _lock_for(self, thread_id: str) -> asyncio.Lock:
        lock = self._locks.get(thread_id)
        if lock is None:
            lock = self._locks[thread_id] = asyncio.Lock()      # weakref：thread 数无界，别用普通 dict
        return lock

    async def put_batch(self, events: Sequence[dict]) -> list[dict]:
        if not events:
            return []
        thread_id = events[0]["thread_id"]                      # 一批只服务一条 thread（journal 保证）
        async with self._lock_for(thread_id):
            for _ in range(SEQ_RETRIES):                        # 跨进程撞唯一索引：重算 seq 再来
                try:
                    return await self._insert_with_seqs(thread_id, events)
                except IntegrityError:
                    continue
            raise RunEventSeqConflict(f"could not allocate seq for thread {thread_id}")

    async def _insert_with_seqs(self, thread_id: str, events: Sequence[dict]) -> list[dict]:
        """一个事务里：算 max(seq) → add_all → commit；中途失败整批回滚（§5.4）。"""
        async with self._sf() as session:
            last = await session.scalar(
                select(func.max(RunEventRow.seq)).where(RunEventRow.thread_id == thread_id)
            )
            rows = [self._to_row(event, seq=(last or 0) + index + 1) for index, event in enumerate(events)]
            session.add_all(rows)
            await session.commit()
            return [self._to_dict(row) for row in rows]         # 与传入等长、同序
```


| 参数 / 返回 / 抛           | 类型               | 说明                                                                 |
| --------------------- | ---------------- | ------------------------------------------------------------------ |
| `events`              | `Sequence[dict]` | 信封列表（§4.3）；**必须同** `thread_id`，空列表直接返回 `[]`                        |
| 返回                    | `list[dict]`     | 与传入等长、同序，`seq` 连续；调用方（journal）靠它更新自己的游标                            |
| `RunEventSeqConflict` | `RuntimeError`   | `SEQ_RETRIES` 次仍冲突。**记 error，不要抛给 worker 的** `finally`——那会吞掉最后一批消息 |


- `_lock_for` **只负责性能，不负责正确性。** 正常路径上它让同 thread 的写入排队，
避免每次都撞唯一索引；跨进程的正确性由 `UNIQUE(thread_id, seq)` + 重试兜底（§5.2 的双层保护）。
- **重试用的是"重算 seq"，不是"重试插入"。** 冲突意味着有人在你算完之后插了行，
拿着旧 seq 再插一次还是冲突——必须重新 `max(seq)`。
- `weakref.WeakValueDictionary` **不能换成普通 dict。** 长驻进程里 thread_id 无界，
普通 dict 会随会话数线性增长（和 M2 的文件锁是同一类问题，§5.3）。

### 5.4 信封回读形状

三个后端 `put` / `put_batch` / `list_*` 返回的 dict 字段必须一致：

```python
# 三个后端 put / put_batch / list_* 的返回形状（实现在各自后端，形状必须一致）
{
    "thread_id": str,
    "run_id": str,
    "event_type": str,
    "category": str,
    "content": str | dict,     # 写入时是 dict 的，读回仍是 dict
    "metadata": dict,
    "seq": int,
    "created_at": str,         # ISO-8601 Z
}
```

db 后端可以多返回 `user_id`、`id`；消费者按 §5.1 第 5 条忽略未知字段。
测试用同一份 fixture 对三个后端断言这 8 个键都在，类型对得上。

`put_batch` 必须按传入顺序分配连续 seq，返回的 list 与传入等长、同序。
中途失败要整批 rollback（SQL 一个事务；jsonl 先写临时文件再 rename；
memory 先算完 seq 再一次性 extend）。部分成功会让 journal 重试时重复写入。

### 5.5 jsonl 的文件格式和 seq 冷启动

路径：`{sqlite_dir 的父目录}/threads/{thread_id}/runs/{run_id}.jsonl`。
`thread_id` / `run_id` 必须匹配 `^[A-Za-z0-9_\-]+$`，否则拒绝——它们会进文件路径。

每一行一个完整信封（含已分配的 `seq`）。进程内 `_seq_counters[thread_id]`
记当前 max。**进程重启后计数器是空的**：该 thread 第一次 `put` 之前，
必须扫所有 `*.jsonl` 找出 max seq，否则会从 1 重编号，和库里已有行冲突。
这个扫描走 `asyncio.to_thread`。

jsonl **没有** `put_if_absent` 的真正原子性。实现是：读该 run 文件，
若已有相同 `event_type` 就返回已有行。单进程够用；这是 jsonl 不能上生产的
另一条原因。

---



#### jsonl 后端的参数与冷启动

```python
# agentkit/persistence/events/jsonl.py
SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")      # thread_id / run_id 都会进文件路径


class JsonlRunEventStore(RunEventStore):
    def __init__(self, base_dir: str, *, max_trace_content: int | None = None) -> None:
        self._base = Path(base_dir).resolve()
        self._seq: dict[str, int] = {}                    # thread_id → 当前 max(seq)
        self._locks: dict[str, asyncio.Lock] = {}

    def _run_file(self, thread_id: str, run_id: str) -> Path:
        for value in (thread_id, run_id):
            if not SAFE_ID_PATTERN.match(value):
                raise ValueError(f"unsafe id for a filesystem path: {value!r}")
        return self._base / "threads" / thread_id / "runs" / f"{run_id}.jsonl"

    async def _current_seq(self, thread_id: str) -> int:
        """进程重启后计数器是空的：第一次 put 之前扫全部 *.jsonl 找出 max，否则会从 1 重编号。"""
        if thread_id not in self._seq:
            self._seq[thread_id] = await asyncio.to_thread(self._scan_max_seq, thread_id)
        return self._seq[thread_id]
```


| 参数                  | 默认     | 谁给                             | 说明                                                    |
| ------------------- | ------ | ------------------------------ | ----------------------------------------------------- |
| `base_dir`          | ——     | `database.sqlite_dir` 的父目录     | `threads/{tid}/runs/{rid}.jsonl` 的根；换目录会让调试数据和工作区混在一起 |
| `max_trace_content` | `None` | `run_events.max_trace_content` | `None` = 不截断。jsonl 本来就不是生产 trace 后端，这条可以不实施（§5.2）     |


- **id 白名单是安全边界，不是洁癖。** 这两个值直接拼进路径，`../` 能写到工作区外面去。
拒绝而不是转义：转义后的文件名和库里对不上，排查时更麻烦。
- **冷启动扫描走** `to_thread`（文件 IO 全是阻塞的，§5.2）。扫描只发生一次，
之后 `self._seq` 就是权威——但**多进程同目录会各扫各的**，这正是 jsonl 不能上生产的原因。



### 5.6 RunEventStore 存什么、不存什么

#### 写入的信封：每个字段谁填


| 字段           | 类型                | 谁填                    | store 侧的校验                                                | 落库列              |
| ------------ | ----------------- | --------------------- | -------------------------------------------------------- | ---------------- |
| `thread_id`  | `str`           | 生产者（journal 从 record 取） | 非空、≤64                                                   | `thread_id`      |
| `run_id`     | `str`           | 同上                    | 非空、≤64                                                   | `run_id`         |
| `event_type` | `str`           | 生产者，**必须来自 catalog**  | ≤32（`RUN_EVENT_TYPE_MAX_LENGTH`）；超长在构造时就炸（§4.3）           | `event_type`     |
| `category`   | `str`           | **由 catalog 的定义带出来**，调用方不手填 | ≤16；不在 catalog 的取值集合里 → 抛                                  | `category`       |
| `content`    | `str \| dict`   | 生产者                   | dict → `json.dumps(ensure_ascii=False)`，并在 metadata 打 `content_is_json` | `content`（Text） |
| `metadata`   | `dict \| None`  | 生产者                   | JSON 可编码即可，键值不限                                           | `event_metadata` |
| `seq`        | `int`           | **store**             | 同 thread 内严格递增（§5.3）                                      | `seq`            |
| `created_at` | `str`（ISO `Z`） | **store**             | tz-aware UTC                                             | `created_at`     |
| `user_id`    | `str \| None`   | **store 软读 ContextVar** | 读不到就留 `NULL`，**不失败**（worker 后台写常常没有用户上下文）                  | `user_id`        |
| `id`         | `int`           | **db 后端内部**           | 不对外；游标永远是 `(thread_id, seq)`                             | `id`             |


- **前六个字段是接口，后四个是 store 的领域。** 生产者传 `seq` 或 `created_at` 是 bug，
  不是"更精确"——store 会用自己分配的值覆盖，冲突时以 store 为准。
- `category` **不让调用方自填**：它由 catalog 的 `RunEventDefinition` 带出来
  （`LLM_AI_RESPONSE.category`）。生产者手写 category 的结局是抄错一次，
  然后那条记录永远不出现在 `list_messages` 里。
- **判断一个字段该进 `content` 还是 `metadata`**：要原样交给前端的（消息体）进 `content`；
  只给自己排查用的（`usage`、`latency_ms`、`caller`、`tool_name`）进 `metadata`。

#### 每列的可变性

| 列                              | 谁能写        | 能不能改                                       |
| ------------------------------ | ---------- | ------------------------------------------ |
| `seq` / `created_at` / `id` | store      | 写完不变，**永不重编号**——前端的续传游标就是 `seq`            |
| `content` / `event_metadata`  | 生产者写一次     | 不更新：账本是 append-only（§4.3）                   |
| `user_id`                     | store 在 put 时盖一次 | M9 的属主迁移**不回填**历史行（回填会改写审计事实）              |
| 整行                             | ——          | M4 不删（§10）；删 thread 也留（它是历史消息的唯一来源）        |


#### 截断只切 `trace`

```python
# agentkit/persistence/events/db.py（memory / jsonl 可以不实施，见 §5.2）
if event["category"] == "trace" and len(text) > self._max_trace_content:
    event["content"] = text[: self._max_trace_content]
    event["metadata"] = {**event["metadata"], "content_truncated": True}
```

- **消息类（`category="message"`）绝不截断。** 截断后前端会渲染出半截 JSON，
  比丢一整条更糟（§5.2）。
- **截断标记写进 metadata，不写进 content**——content 要原样交给前端。

#### 明确不存的东西


| 不存                                | 它存在哪                            | 为什么                                                    |
| --------------------------------- | ------------------------------- | ------------------------------------------------------ |
| token 流（每个 delta）                 | StreamBridge（SSE，M3 §7）         | 按输出长度线性膨胀，翻历史慢到不可用；journal **不实现** `on_llm_new_token` |
| token 汇总数字                        | `runs` 的列（§4.2 ④）              | 内存累加 + `complete` 一次写定（与 M3 §6.5 的分工见 §5.7）            |
| checkpoint 状态（messages / title / todos） | checkpoint 表（LangGraph 拥有，§6）  | 两个权威，互不替代（§2 的四个持久化面）                                  |
| 未落库的直播帧                           | StreamBridge                    | 易失层，过期即无                                               |
| 工具参数与产物的原始大对象                     | 工作区 / 产物（M2）                    | 账本不做对象存储；`llm.tool.result` 存的是 ToolMessage 的形状         |
| 回调的"开始"（`llm.start` / `tool.start` / 非根 `chain.start`）  | 不存                              | 行数翻倍而信息量近零；耗时用同一条事件的 `metadata.latency_ms`            |


#### 三个后端存的是同一份


| 后端       | 物理落点                              | 一条事件怎么被找到                                              |
| -------- | --------------------------------- | ------------------------------------------------------ |
| `db`     | `run_events` 表                   | `(thread_id, seq)` 是主查询键；`(thread_id, category, seq)` 给 `list_messages` |
| `jsonl`  | `threads/{tid}/runs/{rid}.jsonl`  | 一行一个信封；`seq` 在 thread 内跨文件递增                          |
| `memory` | 进程 dict                           | 单进程；重启即丢（默认后端，§5.2）                                    |


**"存什么"只有一个答案**：三个后端的读回形状必须一致（§5.4），否则换后端就等于换 API。

### 5.7 RunJournal：哪个回调写哪条事件

#### 它是一个 async handler，不是 sync handler

```python
# agentkit/runtime/events/journal.py
class RunJournal(AsyncCallbackHandler):
    """账本的唯一常规生产者（§5 开头的分工表）。

    为什么是 AsyncCallbackHandler：
      - 图由 graph.astream() 驱动，LangChain 会走 async 回调分支；
      - store.put_batch 是 async 的。写在 sync 回调里就只能"调度一个 task"，
        于是把三种失败模式都引进收尾路径：写入失败没人接、task 泄漏、
        finally 里 flush 和调度中的 flush 抢同一个缓冲。
    run_inline = True：回调在触发它的那个 task 上跑完再继续，缓冲不会被并行工具交错污染
      （同 M3 §6.5 的 UsageCollector）。
    """

    run_inline = True
```

#### 回调 → 事件：全表

**这是本节的核心交付物。** "哪条事件从哪来"只有这一张表，别在别处再写一遍。


| 回调                                                      | 触发时机      | 写?          | `event_type`（`category`）    | `content`                        | `metadata`                                                          | 条件 / 去重                                                |
| ------------------------------------------------------- | --------- | ----------- | ---------------------------- | -------------------------------- | ------------------------------------------------------------------- | ------------------------------------------------------ |
| `on_chain_start`                                        | 图 / 节点开始  | **仅根链**     | `run.start`（`trace`）       | `{"chain": serialized["name"]}`  | `{"caller": ...}`                                                   | `parent_run_id is not None` 直接返回（节点级开始不写）                 |
| `on_chat_model_start`                                   | **chat 模型的每次调用前（本项目唯一路径）** | **仅 human** | `llm.human.input`（`message`） | human 消息的 `model_dump()`         | `{"caller": ..., "llm_call_index": n}`                              | 按消息身份去重；`tags` 带 `middleware:*` 的调用跳过                     |
| `on_llm_start`                                          | **仅文本补全模型（`BaseLLM`）；本项目不触发** | **不写**      | ——                           | ——                               | ——                                                                  | 只记 start 时间，给 `latency_ms` 用                            |
| `on_llm_end`                                            | **模型返回（chat 与补全都会触发）** | 是           | `llm.ai.response`（`message`） | 每条 generation 的 AI 消息 `model_dump()` | `{"caller": ..., "usage": {...}, "latency_ms": n, "llm_call_index": n}` | 同一 LangChain `run_id` 只写一次；空 usage 先挂起（见下）                |
| `on_llm_error`                                          | 模型抛异常     | 是           | `llm.error`（`trace`）        | `"TypeError: ..."`（截 2000）      | `{"error_type": ..., "llm_call_index": n}`                          | 与 `on_llm_end` 共用同一个 `llm_call_index`                  |
| `on_tool_start`                                         | 工具开始      | **不写**      | ——                           | ——                               | ——                                                                  | 参数已经在前一条 AI 消息的 `tool_calls` 里；只记 `tool_call_id → tool_name` |
| `on_tool_end`                                           | 工具结束      | 是           | `llm.tool.result`（`message`） | `ToolMessage.model_dump()`        | `{"tool_name": ..., "status": ...}`                                 | 按 `tool_call_id` 去重；**工具错误也是这一条**（ToolMessage 自带 `status="error"`） |
| `on_tool_error`                                         | 工具抛异常     | **不写**      | ——                           | ——                               | ——                                                                  | LangChain 会把它变成一条 ToolMessage，走 `on_tool_end`           |
| `on_llm_new_token`                                      | 每个 delta  | **不实现**     | ——                           | ——                               | ——                                                                  | token 走 SSE（M3 §7）；写进来会让表按输出长度膨胀（§5.6）                    |
| `on_chain_end`                                          | 图结束       | **仅根链**     | `run.end`（`outputs`）        | `outputs`（截断后）                  | `{"status": "success"}`                                             | 写之前先做一次**消息对账**（Tool / Human，见下），写之后**立刻 flush**              |
| `on_chain_error`                                        | 图抛异常      | 是           | `run.error`（`error`）        | `"TypeError: ..."`（截 2000）      | `{"error_type": ...}`                                               | 写完**立刻 flush**（错误路径最不能丢）                                 |
| `on_retriever_start` / `_end` / `_error`               | 检索        | **不写**      | ——                           | ——                               | ——                                                                  | M3 没有检索器；将来接入时按 `context:*` 新增一类（M10）                    |


#### 回调会不会真的触发（`create_agent` + `astream`）

**会。** 但触发的是 `on_chat_model_start`，不是 `on_llm_start`——这个区别就是本节最容易被搞错的地方。

```
create_agent(...)                       ← 本项目建 agent 的唯一方式
   └─ 图节点调用 model.ainvoke(messages) ← 模型是 BaseChatModel
        ├─ on_chat_model_start  ✅ 触发（chat 模型的主路径）
        ├─ on_llm_end           ✅ 触发（成功）／ on_llm_error ✅（失败）
        └─ on_llm_start         ❌ 不触发（那是 BaseLLM 文本补全模型的回调）
```


| 回调                  | chat 模型（`create_agent` 用的） | 文本补全模型（`BaseLLM`） | 说明                                                        |
| ------------------- | --------------------------- | ------------------ | --------------------------------------------------------- |
| `on_chat_model_start` | ✅ 每次调用                      | ❌                  | `llm.human.input` 就靠它——它比 `on_chain_start` 可靠：后者每个节点都触发（见下） |
| `on_llm_end`        | ✅                           | ✅                  | 两条路径唯一的公共点；`llm.ai.response` 与 usage 都在这里                |
| `on_llm_error`      | ✅                           | ✅                  | ——                                                        |
| `on_llm_start`      | ❌                           | ✅                  | 只实现它的 handler 在 `create_agent` 下**一条都收不到**                |
| `on_llm_new_token`  | ✅（流式时）                      | ✅                  | 本设计**故意不实现**（§5.6）                                        |


**四个前提**，缺一个就会静默漏事件：

1. **回调挂在根 config 上**（M3 §6.2 的 `callbacks=[..., journal]`）。LangChain 用
   ContextVar 把它继承给子 runnable，`create_agent` 的模型节点因此拿得到。
2. **中间件不许用自己的 config 覆盖继承链。** 中间件里写
   `model.ainvoke(msgs, config={"callbacks": [...]})` 是**替换**而不是追加——
   这些调用的 `llm.ai.response` 会丢。要加自己的 handler 就 append 到继承来的列表上。
3. **模型必须是真的** `BaseChatModel`**。** `bind_tools` / `with_structured_output` 返回的还是它；
   但绕过 LangChain 直接打供应商 HTTP 的"模型"不会触发任何回调——**M3 §6.5 的
   `UsageCollector` 同样收不到**，所以这不是账本一家的问题。
4. **`on_chain_start` 不是模型信号。** 它每个节点都会触发（`parent_run_id` 非空），
   拿它当"模型开始了"会得到 N 倍的行数。所以本设计只在根链写 `run.start`。


**漏回调的兜底：对账要覆盖三类消息。** 上面第 2、3 条一旦被违反，或者工具在
ToolNode 里被取消（`on_tool_end` 不触发，M8 §17），账本会缺消息。所以
`on_chain_end` 的对账不能只补 ToolMessage：

| 消息类型      | 对账要不要补 | 怎么判重                                        |
| --------- | ------ | ------------------------------------------- |
| `ToolMessage` | **要**  | `tool:{tool_call_id}`（LangChain 保证有）        |
| `HumanMessage` | **要** | `message:{id}`                             |
| `AIMessage`   | 有条件  | 只在 `message.id` 存在时补；没有 id 就不补，避免与 `on_llm_end` 双写 |

**这条依赖要有一条测试锁住**（§11 的 `test_llm_callbacks_fire.py`）：用假 chat model 跑一次
`create_agent`，断言 `on_chat_model_start` 与 `on_llm_end` 各触发一次、`on_llm_start` 零次。
langchain 升级改了回调语义时，这条测试是唯一会红的地方。

后续阶段往这张表里加行（**形状照抄，不改机制**）：


| 归属  | 回调 / 触发点                       | `event_type`（`category`）                                    | 谁写                                                 |
| --- | ----------------------------- | ------------------------------------------------------------ | -------------------------------------------------- |
| M6  | 中间件自己的模型调用（`TAG_NOSTREAM`）    | `middleware:{tag}`（category 由 catalog 的模式解析）                 | journal（`tags` 里认 `middleware:*`，不进 `llm.ai.response`） |
| M8  | 子代理生命周期                       | `subagent.start` / `subagent.step` / `subagent.end`（`subagent`） | 子代理控制器调 `journal.put(...)`（同一个 store，不同生产者）        |
| M9  | 出站投递回执                        | `run.delivery`（`outputs`）                                    | 投递路径用 `put_if_absent`（崩溃重试不产生第二行）                   |
| M10 | 记忆 / 工作区                       | `context:memory`（`context`）、`workspace.changes`（`workspace`） | 各自模块（`list_messages` 不返回它们）                          |


#### 三条去重规则（不写清楚就会双写）

```python
# agentkit/runtime/events/journal.py
@staticmethod
def _message_identity(message: BaseMessage) -> str | None:
    """消息身份：工具结果优先用 tool_call_id，其余用 LangChain 的 message.id。"""
    if isinstance(getattr(message, "tool_call_id", None), str) and message.tool_call_id:
        return f"tool:{message.tool_call_id}"
    if isinstance(getattr(message, "id", None), str) and message.id:
        return f"message:{message.id}"
    return None
```


| 规则                | 作用对象               | 为什么需要                                                                        |
| ----------------- | ------------------ | ---------------------------------------------------------------------------- |
| **同一 LangChain** `run_id` **只写一次** | `on_llm_end`       | 流式提供商会先给一次空 usage 的响应，再回放一次带 usage 的；写两次就是两条 AI 消息（M3 §6.5 同一条规则）              |
| **消息身份去重**        | `llm.tool.result` / `llm.human.input` | 同一条消息会在不同回调里被重复交出（回放、对账）；按身份记 `set`，写过就跳过                                    |
| **最终对账**          | `on_chain_end`     | 工具被取消 / 报错时 `on_tool_end` 可能**根本没被调用**（M8 §17 的取消路径）；中间件覆盖 config 时整个 `on_llm_end` 都可能不来。结束时拿 outputs 里的消息与已写身份比对，补写漏掉的 Tool / Human 消息（AI 消息见上表的有条件规则） |


```python
async def _reconcile_tool_messages(self, outputs: Any) -> None:
    """on_chain_end 的最后一道保险：把 outputs 里还没落库的 ToolMessage 补上。"""
    for message in _messages_of(outputs):
        if isinstance(message, ToolMessage) and self._mark_message(message):   # 已写过 → False
            await self._put(LLM_TOOL_RESULT, content=message.model_dump())
```

#### 两段式：usage 为空的响应先挂起

流式提供商的典型顺序是：`on_llm_end(usage={})` → `on_llm_end(usage={...})`。
**不能**把第一份直接写掉，也不能把第一份丢掉：

1. 第一次 `on_llm_end`：`_extract_usage(response)` 为空 → 把整份**挂起**（`self._pending_llm`），不写；
2. 第二次带 usage：写挂起的那份 + 这一份（同 `run_id` 只写一次），并把 usage 交给 M3 §6.5 的累加器；
3. 一直没来第二次：**在下一条事件入缓冲时或 `flush()` 时把挂起的那份提交**——
   内容不能丢，只是缺 usage。

判定顺序必须是"先入缓冲、后截断"：截断按 `category` 走（§5.6），挂起的响应还没定 category。

#### 代码骨架

```python
# agentkit/runtime/events/journal.py
class RunJournal(AsyncCallbackHandler):
    run_inline = True

    def __init__(self, store: RunEventStore, record: RunRecord, *, batch_size: int = 20,
                 flush_interval: float = 5.0, track_token_usage: bool = True,
                 max_trace_content: int = 10240) -> None: ...

    # ---- chain ----
    async def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, tags=None,
                             metadata=None, **kw) -> None:
        if parent_run_id is None:                       # 只有根链代表这次 run 的生命周期
            await self._put(RUN_START_EVENT, content={"chain": (serialized or {}).get("name", "unknown")},
                            metadata={"caller": self._caller(tags)})

    async def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kw) -> None:
        if parent_run_id is not None:
            return
        await self._reconcile_tool_messages(outputs)     # ★ 最后一道保险
        await self._put(RUN_END_EVENT, content=outputs, metadata={"status": "success"})
        await self.flush()

    async def on_chain_error(self, error, *, run_id, **kw) -> None:
        await self._put(RUN_ERROR_EVENT, content=self._error_text(error),
                        metadata={"error_type": type(error).__name__})
        await self.flush()

    # ---- llm ----
    async def on_chat_model_start(self, serialized, messages, *, run_id, tags=None, **kw) -> None:
        for message in self._human_messages(messages):   # 只取 human，且按身份去重
            if self._mark_message(message):
                await self._put(LLM_HUMAN_INPUT, content=message.model_dump(),
                                metadata={"caller": self._caller(tags)})

    async def on_llm_end(self, response, *, run_id, tags=None, **kw) -> None: ...   # 见"两段式"

    async def on_llm_error(self, error, *, run_id, tags=None, **kw) -> None:
        await self._put(LLM_ERROR_EVENT, content=self._error_text(error),
                        metadata={"error_type": type(error).__name__})

    # ---- tool ----
    async def on_tool_end(self, output, *, run_id, **kw) -> None:
        if isinstance(output, ToolMessage) and self._mark_message(output):   # 已写过 → False
            await self._put(LLM_TOOL_RESULT, content=output.model_dump(),
                            metadata={"tool_name": getattr(output, "name", None)})

    # ---- 缓冲与落库 ----
    async def _put(self, definition: RunEventDefinition, *, content: Any, metadata: dict | None = None) -> None:
        self._commit_pending_llm()                       # 挂起的那份先入缓冲
        self._buf.append(self._envelope(definition, content=content, metadata=metadata))
        if len(self._buf) >= self._batch_size:
            await self.flush()

    def _envelope(self, definition: RunEventDefinition, *, content: Any, metadata: dict | None) -> dict:
        return {
            "thread_id": self._record.thread_id, "run_id": self._record.run_id,
            "event_type": definition.event_type, "category": definition.category,   # ★ 来自 catalog
            "content": content, "metadata": metadata or {},
        }

    async def flush(self) -> None:
        """worker 的 finally 也必须调它（§9 的时间线）。失败时整批放回队首再抛。"""
        self._commit_pending_llm()
        if not self._buf:
            return
        batch, self._buf = self._buf, []
        try:
            await self._store.put_batch(batch)           # seq / created_at / user_id 由 store 填（§5.6）
        except Exception:
            self._buf = batch + self._buf                # 放回队首：下一次 flush 重试
            raise
```


#### 参数

| 参数                  | 默认      | 谁给                             | 说明                                                       |
| ------------------- | ------- | ------------------------------ | -------------------------------------------------------- |
| `store`             | ——      | lifespan 建的 `RunEventStore` 单例 | journal 只认 ABC；memory / db / jsonl 都一样                     |
| `record`            | ——      | 本 run 的 `RunRecord`            | 取 `run_id` / `thread_id` / `user_id`（后台写没有 ContextVar，必须显式传） |
| `batch_size`        | `20`    | `run_events_config`            | 满批就 flush；太小会把 thread 锁打满，太大丢消息的窗口变长                       |
| `flush_interval`    | `5.0`   | `run_events_config`            | 时间兜底；与 token 进度的 5 秒节流是**两条管子**（见下），不共用一个计时器              |
| `max_trace_content` | `10240` | `run_events.max_trace_content` | 只截 `category=trace`；消息类**绝不截断**（§5.6）                     |
| `track_token_usage` | `True`  | `run_events.track_token_usage` | **语义已收窄**：只表示"把这次调用的 usage 写进事件的 `metadata`"，不参与累加（见下）    |


#### flush 时机


| 时机                            | 谁触发                         | 漏掉的后果                          |
| ----------------------------- | --------------------------- | ------------------------------ |
| 缓冲满 `batch_size`              | 每次 `_put` 之后               | 内存里堆着，直到下一个边界                  |
| `on_chain_end` / `on_chain_error` | journal 自己                   | 终态事件留在内存里；run 结束而账本缺失          |
| worker `finally`（**必须发生**）     | M3 §6.2 的收尾（§9 的时间线）         | **用户刷新后看不见最后几条消息**——最严重的一类      |


> **⚠ 与 M3 §6.5 的分工（不能两个都算）**
>
> M3 §6.5 的 `UsageCollector` 已经挂在调用根统计 token：累加 usage、5 秒节流写
> `runs` 的列。**RunJournal 不参与累加**，它只把这次调用的 usage 抄进事件的
> `metadata`（排查用）。两份实现同时累加 = token 双计，而账面上的数字没人能对出来。
>
> 所以 `run_events.track_token_usage` 这个配置项的语义是
> **"事件里要不要带 usage"**，不是"要不要统计 token"。要关掉统计，改的是 M3 的
> `UsageCollector`（它写 `runs`），不是这里。

#### 失败处理

| 失败点                                 | 行为                                              | 为什么                                       |
| ----------------------------------- | ----------------------------------------------- | ----------------------------------------- |
| `put_batch` 抛（store 挂了 / seq 冲突用尽）   | 整批放回缓冲队首，异常上抛                                    | 丢掉的是用户刷新后看不见的消息；重试由下一次 flush 承担           |
| worker `finally` 里的 `flush()` 抛    | 由 M3 §6.2 的 `_best_effort` 接住，**END 照发**        | END 是前端转圈的唯一解药；账本缺一段不能连累它                 |
| journal 自己的回调抛（比如 catalog 长度校验失败）    | 让异常传到图里 → run 变 `error`                         | 这类异常是代码 bug，静默吞掉会让账本长期缺字段                  |
| `record.user_id` 为 `None`（内部调用方）  | 正常写入，`user_id` 列留 `NULL`                      | 后台写没有用户上下文是正常状态，不是错误（§5.6）                |

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
# config.example.yaml（摘录）
database:
  checkpoint_channel_mode: full     # full | delta，启动时冻结
```


|                      | `full`           | `delta`                     |
| -------------------- | ---------------- | --------------------------- |
| `messages` 怎么存       | 每拍一整份列表          | sentinel + 增量写，隔 N 拍打一份全量快照 |
| 存储随轮次                | 约 O(轮次²)         | 约 O(轮次)                     |
| 从旧 checkpoint resume | LangGraph 真 fork | **不能 fork**，要先物化再覆写到当前 head |
| 组图失败时                | 可以退化成裸读          | **没有这条退路**                  |


**M4 先只做** `full`**。** 它简单、可裸读、调试友好。`delta` 是 O(N²) 存储真的成为问题时
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

**选基座和读快照是两层。** `has_pending_tasks` 的过滤属于 replay 选基座，
放在 Gateway 的谱系行走里（落地后是 `app/gateway/checkpoint_lineage.py`）。
它回答「替换这条消息，应该从哪一拍接着生成」：认消息 id、跳过中途快照、
没有 `parent_config` 的旧数据走有界的时间序兜底。每向上读一拍仍调用 accessor。

`CheckpointStateAccessor` 回答「这一拍的状态是什么」：绑定通道表、注入 mode、
把 delta 的 sentinel 物化成可读的 `messages`。`get_state` 返回当前 head。
一轮还在跑时，这一拍的 `next` 可以非空，`GET /threads/{id}/state` 要把这个
`next` 交出去。把 settled 过滤放进 `get_state`，进行中的对话会从状态接口消失，
退回到上一轮已经停稳的快照。

### 6.4 读取只走一个入口

```python
# app/gateway/routers/thread_state.py（唯一的调用点）
accessor = build_thread_checkpoint_state_accessor(thread_id)
state = await accessor.get_state()
```

不要让业务代码直接 `checkpointer.get_tuple()`。Accessor 负责绑定
"这张图的通道表 + checkpointer + 进程冻结的 mode"，并在读之前注入 mode 标记。

M4 只有 full 模式时这层看起来是多余的包装。但 delta 模式下裸 `get_tuple`
看到的是 sentinel，会把 `messages` **读成空列表**——不是报错，是静默读空。
这类 bug 极难定位，所以入口现在就要收敛。收敛的是读路径，不是 replay 策略。
编辑重跑、重新生成挑选 settled 祖先仍在谱系行走（§6.3）。accessor 的
`get_state` / `history` 原样返回 head 和谱系上的各拍，包括 `next` 非空的中途快照。

```python
# agentkit/runtime/checkpoint_state.py
class CheckpointStateAccessor:
    def __init__(self, graph, checkpointer, *, mode: Literal["full", "delta"]):
        self._graph = graph
        self._checkpointer = checkpointer
        self._mode = mode

    async def get_state(self, config) -> dict: ...
    async def history(self, config) -> AsyncIterator[dict]: ...
```

`mode` 从进程启动时冻结的配置读取，不要每次调用再看 YAML。热切 mode 会让
一个 full 进程读到 delta 写的 sentinel。

**遗留 thread 的一次性 seed。** 升级前只有 checkpoint、没有账本的会话，
第一次开 run 之前：

```python
# agentkit/runtime/checkpoint_state.py
async def ensure_checkpoint_history_seeded(thread_id, store, accessor):
    if await store.count_messages(thread_id) > 0:
        return
    state = await accessor.get_state(...)
    messages = state.get("messages") or []
    if not messages:
        return
    await store.put_batch(messages_to_events(thread_id, messages))
```

这是「模型历史」和「前端历史」两个权威之间唯一允许的单向复制。
只在账本为空时跑，跑完就不再碰。不要做成持续同步——压缩之后两边本来就不等长。

#### `CheckpointStateAccessor` 的参数


| 参数             | 类型                                     | 谁给                                          | 说明                                                |
| -------------- | -------------------------------------- | ------------------------------------------- | ------------------------------------------------- |
| `graph`        | 编译后的图                                  | M1 的 `build_lead_agent`（进程内编译一次，§6.6）       | 提供**实际的通道表**；拿基础 `ThreadState` 顶替会静默丢掉 M6 中间件加的通道 |
| `checkpointer` | `BaseCheckpointSaver`                  | `make_checkpointer()`                       | 必须和注入 `graph.compile` 的是同一个实例                     |
| `mode`         | `Literal["full", "delta"]`，默认 `"full"` | 进程启动时冻结的 `database.checkpoint_channel_mode` | 决定"读之前要不要物化"；热切会让 full 进程读到 delta 写下的 sentinel    |



| 方法          | 参数                                  | 返回                       | 要点                                          |
| ----------- | ----------------------------------- | ------------------------ | ------------------------------------------- |
| `get_state` | `config: dict | None`               | `dict`（含 `messages` 等通道） | `None` 时用构造时绑定的 `thread_id` + 空 ns          |
| `history`   | `config`、`limit: int | None = None` | `AsyncIterator[dict]`    | **沿** `parent_config` **走谱系**，不是按时间倒序（§6.3）。含中途快照，不按 settled 过滤 |

### 6.5 checkpointer 自己的连接池

```python
# agentkit/runtime/checkpointer/async_provider.py
@asynccontextmanager
async def make_checkpointer(app_config) -> AsyncIterator[Checkpointer]:
    db = app_config.database
    if db.backend == "memory":
        yield InMemorySaver()
        return
    if db.backend == "sqlite":
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        conn_str = await asyncio.to_thread(_prepare_sqlite_path, db.sqlite_path)
        async with AsyncSqliteSaver.from_conn_string(conn_str) as saver:
            await saver.setup()          # 建那 4 张表，幂等
            yield saver
        return
    if db.backend == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        pool = AsyncConnectionPool(      # psycopg，不是 asyncpg
            dsn_with_search_path(normalize_libpq_dsn(db.postgres_url), db.postgres_schema),
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row,
                    "keepalives": 1, "keepalives_idle": 60, ...},
            check=AsyncConnectionPool.check_connection,
        )
        async with pool:
            await create_schema_if_needed(pool, db.postgres_schema)
            saver = AsyncPostgresSaver(conn=pool)
            await saver.setup()
            yield saver
```

三条硬规则：

1. **不要把 ORM 的** `AsyncEngine` **传给 saver。** LangGraph Postgres 实现吃的是
  psycopg 连接池（同步协议 + libpq DSN），ORM 吃的是 asyncpg。方言都不同。
2. `setup()` **在 yield 之前。** 第一次跑的库没有 checkpoint 表，第一次
  `astream` 会 500 `relation checkpoints does not exist`。`setup()` 是幂等的。
3. **SQLite 父目录创建走** `to_thread`**。** 和 `init_engine` 同一条 blocking-IO 纪律。
  WAL/busy_timeout 已经由 ORM 引擎的 `connect` 事件设过；checkpointer 走的是
   另一条 sqlite3 连接，**也要设同样的 PRAGMA**，否则 checkpointer 写 WAL、
   ORM 读还在 rollback journal 模式，表现为「刚 checkpoint 完，ORM 读不到」。
   最简单的做法：checkpointer 的 sqlite 连接同样挂 `connect` listener。

缺 extra 时的报错要可执行，和 ORM 的 asyncpg 提示同一风格：

```
database.backend is postgres but langgraph-checkpoint-postgres is not installed.
    cd backend && uv sync --extra postgres
```

Greenfield 不保留 DeerFlow 的遗留 `checkpointer:` 独立配置段。只有 `database:`。

#### `make_checkpointer` 的参数与产出

```python
# agentkit/runtime/checkpointer/async_provider.py
@asynccontextmanager
async def make_checkpointer(app_config: AppConfig) -> AsyncIterator[BaseCheckpointSaver]:
    """进程级：lifespan 里 async with 包住整个服务生命周期，yield 之前 setup() 必须跑完。"""


async def delete_thread_checkpoints(saver: BaseCheckpointSaver, thread_id: str) -> None:
    """按 thread_id 清掉**所有 checkpoint_ns**（主图 + 子图）；由 DELETE /threads/{id} 调用（§10）。"""
    await saver.adelete_thread(thread_id)
```


| 后端         | 产出                                  | 连接参数从哪来                                                                            |
| ---------- | ----------------------------------- | ---------------------------------------------------------------------------------- |
| `memory`   | `InMemorySaver()`                   | 无                                                                                  |
| `sqlite`   | `AsyncSqliteSaver.from_conn_string` | `db.sqlite_path`；父目录 `to_thread` 建；**同一组 PRAGMA 也要挂上**（见下）                         |
| `postgres` | `AsyncPostgresSaver(conn=pool)`     | `normalize_libpq_dsn(db.postgres_url)`（剥掉 `+asyncpg`）+ `dsn_with_search_path(...)` |



| psycopg 池关键字                     | 值                                      | 为什么                                                                       |
| -------------------------------- | -------------------------------------- | ------------------------------------------------------------------------- |
| `autocommit`                     | `True`                                 | LangGraph 自己管事务；psycopg 的隐式事务会把 `setup()` 的 DDL 和 checkpoint 写混进同一个事务     |
| `prepare_threshold`              | `0`                                    | 关掉预处理语句缓存：连接池（以及中间的 pgbouncer）下复用语句名会撞                                    |
| `row_factory`                    | `dict_row`                             | saver 按列名读行                                                               |
| `keepalives` / `keepalives_idle` | `1` / `60`                             | 长空闲的 checkpoint 连接被 NAT / 代理静默掐掉后，第一次写会失败——和 ORM 的 `pool_pre_ping` 是同一个问题 |
| `check`                          | `AsyncConnectionPool.check_connection` | 取连接时探活                                                                    |

### 6.6 图编译时注入 checkpointer

M1 的 `build_lead_agent` 现在从 `make_checkpointer()` 拿到的实例注入
`graph.compile(checkpointer=...)`。**编译一次、进程内复用。** 每次请求
重新 compile 会丢掉图缓存，而且可能拿到不同的 saver 引用。

`thread_id` 进 LangGraph 的路径是 `config["configurable"]["thread_id"]`。
checkpoint 表的逻辑主键是 `(thread_id, checkpoint_ns, checkpoint_id)`。
`checkpoint_ns=""` 是主图；子图/子代理会写非空 ns。M4 的删除必须按
`thread_id` 前缀清掉所有 ns，不要只删 `ns=""`。

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

内存实现直接调 Python 版；SQL 实现编译成 `JsonMatch` 表达式：

```python
# agentkit/persistence/json_compat.py（SqlJsonMatch 的编译结果对照）
# SQLite:  json_type(col, '$.key') / json_extract(col, '$.key')
# Postgres: json_typeof(col -> 'key') / col ->> 'key'
```

**key 要白名单校验** `^[A-Za-z0-9_\-]+$`**。** 它会被插进 SQL 路径表达式，
任何更松的字符集都是注入面。value 只接受 `None / bool / int / float / str`，
int 还要在 signed int64 范围内——SQLite 绑定更大的整数会溢出，Postgres 在
`BIGINT` cast 时溢出。

`bool` 不能用 `isinstance(x, int)` 判断（`bool` 是 `int` 子类）。正确写法：

```python
# agentkit/persistence/json_compat.py（Python 侧用同一套语义）
if isinstance(expected, bool):
    return isinstance(actual, bool) and actual is expected
if isinstance(expected, int):
    return isinstance(actual, int) and not isinstance(actual, bool) and actual == expected
```

`JsonMatch` 是 SQLAlchemy `ColumnElement`，`inherit_cache = True`。缓存键必须包含
`value_type = type(value).__name__`——Python 里 `True == 1 == 1.0` 且 hash 相同，
不把类型编进 cache key，bool 谓词会被复用到 int 查询上。

编译结果（开发时对照这条，不要自己写 `json_extract`）：


| expected   | SQLite                                                 | Postgres                                                               |
| ---------- | ------------------------------------------------------ | ---------------------------------------------------------------------- |
| 缺失 vs null | `json_type(col, '$.k') IS NOT NULL` 才继续比               | `col ? 'k'`（key exists）才继续比                                            |
| `None`     | `json_type(...) = 'null'`                              | `json_typeof(col -> 'k') = 'null'`                                     |
| `bool`     | `json_type = 'true'/'false'`，**不要** `json_extract = 1` | `json_typeof = 'boolean' AND (col ->> 'k')::boolean`                   |
| `int`      | `json_type IN ('integer')` + `CAST(... AS INTEGER)`    | `json_typeof = 'number' AND (col ->> 'k') ~ '^-?[0-9]+$'` 再 `::BIGINT` |
| `float`    | `json_type IN ('integer','real')` + `CAST AS REAL`     | `json_typeof = 'number'` + `::DOUBLE PRECISION`                        |
| `str`      | `json_type = 'text'`                                   | `json_typeof = 'string'`                                               |


Postgres 的 `json_typeof` 对 `1` 和 `1.0` 都返回 `'number'`，所以 int 过滤必须加
「看起来像整数」的正则，否则 `CAST('1.5' AS BIGINT)` 会炸整条查询。

#### `json_value_matches` 的参数与校验顺序

```python
# agentkit/persistence/json_compat.py
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")           # key 会进 SQL 路径表达式
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1
JsonScalar = None | bool | int | float | str


def json_value_matches(column: ColumnElement, key: str, expected: JsonScalar) -> ColumnElement[bool]:
    """把"metadata 里 key 等于 expected"编译成两个后端都成立的谓词。

    内存实现直接调 python_value_matches(actual, expected)；SQL 实现返回 SqlJsonMatch。
    两边共用同一套语义，不允许各写各的（§7.1）。"""
    if not KEY_PATTERN.match(key):
        raise InvalidMetadataFilterError(f"unsafe metadata key: {key!r}")
    if expected is not None and not isinstance(expected, (bool, int, float, str)):
        raise InvalidMetadataFilterError(f"unsupported metadata value type: {type(expected).__name__}")
    if isinstance(expected, int) and not isinstance(expected, bool) and not (INT64_MIN <= expected <= INT64_MAX):
        raise InvalidMetadataFilterError("integer metadata filters must fit in signed int64")
    return SqlJsonMatch(column, key, expected, value_type=type(expected).__name__)
```


| 参数 / 抛                    | 类型                                        | 说明                                                           |
| ------------------------- | ----------------------------------------- | ------------------------------------------------------------ |
| `column`                  | `ColumnElement`（JSON 列）                   | 只接 JSON 列；传普通列会生成一个永远不匹配的谓词                                  |
| `key`                     | `str`，`^[A-Za-z0-9_\-]+$`                 | 白名单不通过 → `InvalidMetadataFilterError` → 路由 400（§4.4）         |
| `expected`                | `None` / `bool` / `int` / `float` / `str` | 越界 int 或其它类型 → 同一个异常。**bool 必须先于 int 判断**（`bool` 是 `int` 子类） |
| `value_type`（进 cache key） | `type(expected).__name__`                 | `True == 1 == 1.0` 且 hash 相同；不把类型编进缓存键，bool 谓词会被复用到 int 查询上  |


- **校验发生在编译之前**，所以非法输入永远不会变成 SQL。这一层是注入面，白名单不是可选项。
- `int` **与** `bool` **的顺序**：先 `isinstance(expected, bool)`，再 `isinstance(expected, int)`。
反过来的话 `True` 会走整数分支，生成 `json_type IN ('integer')` ——而 SQLite 里
`json_type('true')` 是 `'true'`，查询静默返回空。



### 7.2 其它差异清单


| 差异                              | 处理                                        |
| ------------------------------- | ----------------------------------------- |
| SQLite 读回 naive datetime        | 统一按 UTC 规范化成 tz-aware                     |
| SQLite 需要 per-connection PRAGMA | WAL、`busy_timeout`、`foreign_keys` 在连接事件里设 |
| Postgres 需要 `search_path`       | 有 `postgres_schema` 配置时钉住                 |
| 部分唯一索引语法                        | 两边都支持 `WHERE`，但要测到                        |
| 序列化时间戳绑到 `DateTime` 列           | 绑定前做类型强制，不要传字符串                           |


`NULL != NULL` 这条标准语义在两边是一致的，可以放心用来做
"允许无限多个 NULL、但非 NULL 值唯一"的部分唯一索引——M9 的 OAuth 身份会用到。

### 7.3 测试策略

```python
# backend/tests/conftest.py
@pytest.fixture(params=["sqlite", "postgres"])
def db_backend(request): ...
```

**仓库层的每一个测试都参数化跑两遍。** Postgres 那一档在没有可达实例时自动 skip，
但 CI 必须真的提供一个——只在 SQLite 上绿的测试套件对这个阶段没有意义。

---



## 8. 数据库结构怎么升级（Alembic，D8）

Python 里的表模型会变：加一列、加一张表、加一条「同一条 thread 同时只能有一个进行中的 run」的约束。
已经在跑的数据库里有数据，不能删库重建。Alembic 是负责这件事的工具：把每一次结构变化写成一份小脚本，
按顺序对已有库执行，数据留在原地。

一份这样的脚本叫一条 **revision**。每条脚本做两件事：

- `upgrade()`：把库往前推一格。例如「给 `runs` 加一列」。
- `downgrade()`：把这一格撤回去。Greenfield 的运行路径不会调用它，但每条脚本都必须写上，见 §8.4。

脚本用 `down_revision` 指向上一条，串成一条链。链的最末端叫 **head**（当前代码认为的最新结构）。
数据库自己用一张一行的表 `alembic_version` 记住「我已经执行到哪一条」。这张表是 Alembic 建的，不是业务表。

和 §3.2 的两条建库路径对上：

| 库现在是什么样 | 实际发生的事 | 那些升级脚本会不会跑 |
| --- | --- | --- |
| 空库，自己的表一张都没有 | 按当前 Python 模型直接建表（`create_all`），再把 `alembic_version` 写成 head（`stamp head`） | **不会。** 版本号被直接写成最新，中间脚本全部跳过 |
| 已有库，版本号是这条链上的某一格，但还不是 head | 从那一格往后，把没跑过的 `upgrade()` 依次执行（`upgrade head`） | **会** |
| 版本号认不出（空的、多行、或者比这份代码更新） | 拒绝启动 | 不跑 |

所以同一条「加一列」必须出现在两个地方：Python 模型里（空库直接建出来），以及一条 revision 里（老库升级时补上）。只写在脚本里，新部署的库永远缺这一列，因为那些脚本根本不会执行。

下面这张表是后文用到的词。后文不再单独解释它们。

| 词 | 它是什么 |
| --- | --- |
| revision | `versions/` 里的一份脚本。文件头有自己的 id，以及它紧挨着的上一条 id |
| 链 / head | 脚本串起来的顺序。head 是最新一节 |
| `upgrade head` | 按链把库追到 head，会改表、会搬数据 |
| `stamp head` | **不改表**。只把 `alembic_version` 写成 head。空库用它，因为表已经由模型建好了 |
| `downgrade` | 按链往回撤一格。我们不靠它回滚生产库，但脚本里不能空着 |
| autogenerate | 拿「Python 模型描述的结构」和「库里实际的结构」做对比，起草下一份脚本。起草结果要人看过再提交 |
| `alembic_version` | 库里那一行版本号。手改它等于谎报「这些变更已经执行过」 |
| `env.py` | 跑脚本时的入口配置：连哪个库、模型在哪、哪些表不要碰 |
| `Base.metadata` | 当前进程里全部 ORM 模型合在一起的「想要的结构」。autogenerate 拿它当正确答案 |
| `include_object` | 对比时的过滤器。返回 `False` 的对象当作不存在，Alembic 就不会为它生成「删掉这张表」 |
| `render_as_batch` | SQLite 旧版本不能直接删列。打开之后，删列会变成「建一张新表、把行拷过去、改名」 |

### 8.1 作者怎么加一次结构变更

模型改完之后，用下面这条命令起草脚本：

```bash
# backend/Makefile（migrate-rev 目标）
cd backend && make migrate-rev MSG="add foo column to runs"
```

起草时的对比对象必须是**用当前脚本链现建的一次性 SQLite**，不能是开发者正在用的 `app.db`。
活库里还有 LangGraph 自己建的 checkpoint 表、扩展表，以及人手改过的列。
拿它和模型对比，起草结果会夹进「删掉这些表」「改回那些列」。

四个动作，`scripts/make_migration.py` 做完，**这个脚本永远不打开开发者的 `app.db`**：

1. 在临时目录里建一个空的 SQLite，用现有 `versions/` 链执行 `upgrade head`，得到一个干干净净的库。
2. 把这个库的实际结构和 `Base.metadata` 做对比（autogenerate）。
3. 把起草出来的那份脚本放进 `versions/`。
4. 把脚本里裸的「加列 / 删列」换成 `_helpers.py` 的 `safe_add_column` / `safe_drop_column`。列已经在就什么也不做。

第 4 步的原因：空库路径和升级路径可能在同一个库上碰上。列已经由 `create_all` 建好，脚本再无条件 `add_column` 会因为「列已存在」失败。先看一眼，有了就跳过。

SQLite 连接上要打开 `render_as_batch`。旧版 SQLite 不能直接 `ALTER TABLE DROP COLUMN`，批处理模式用「建新表、拷数据、改名」完成删列。Postgres 不需要这一步。

#### 跑脚本时的入口，以及「列已经在就跳过」

`env.py` 是每次执行脚本（以及 autogenerate）都会进来的配置。它告诉 Alembic 三件事：想要的结构在 `Base.metadata`，对比时用 `include_object` 把别人的表排除掉，SQLite 用批处理模式改表。

```python
# agentkit/persistence/migrations/env.py
import agentkit.persistence.schema          # noqa: F401  ★ 少这一行，autogenerate 会生成"删掉所有表"
from agentkit.persistence.base import Base
from agentkit.persistence.migrations._env_filters import include_object

target_metadata = Base.metadata


def run_migrations_online() -> None:
    connectable = create_engine(sync_url_from_config())      # 同步方言，见 §3.4 的方言表
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,                   # 见下：表 + 索引/约束
            compare_type=True,                               # 列类型变化也要 diff 出来
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
```

那行 `import models` 是在把所有 ORM 类注册进 `Base.metadata`。漏掉它，Alembic 认为「想要的结构是零张表」，对比结果就是「把库里的表全部删掉」。

```python
# agentkit/persistence/migrations/_helpers.py
def safe_add_column(table: str, column: sa.Column) -> None:
    """已存在就 no-op。空库走 create_all、老库走 migration，两条路径会在同一个库上相遇。"""
    if column.name in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}:
        return
    op.add_column(table, column)


def safe_drop_column(table: str, name: str) -> None:
    if name not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}:
        return
    with op.batch_alter_table(table) as batch:               # SQLite：建新表 + 拷数据 + 改名
        batch.drop_column(name)
```

`op` 是 Alembic 提供的「改表动作」：`add_column`、`create_table`、`create_index`。业务代码不调用它，只有 `versions/` 里的脚本调用。

#### 过滤器拿到的五个参数

autogenerate 每看到一个表、索引或约束，就调用一次 `include_object`。五个参数是 Alembic 传进来的，我们只读。返回 `False` 表示「这个对象不要参与对比」。

| 参数 | 白话 | 我们怎么用 |
| --- | --- | --- |
| `type_` | 这是表、索引，还是约束 | 表按名字排除；索引和约束改看它挂在哪张表上（§8.3） |
| `name` | 对象自己的名字 | 只在 `type_ == "table"` 时拿来对 `LANGGRAPH_OWNED_TABLES` |
| `object_` | 对象本身 | 索引和约束没有自己的「属于哪张表」的名字参数，要从 `.table.name` 读父表 |
| `reflected` | `True` 表示「这是从真实数据库里读出来的」，`False` 表示「这是模型里声明的」 | 不参与判断。排障时打日志，能看出某个对象是库里多出来的还是模型里多出来的 |
| `compare_to` | 非空表示「库里有这个，模型里没有」 | 这就是「生成一条删表 / 删索引」的来源。LangGraph 的表正是这种情况，所以过滤器要把它们挡在对比之外 |

#### 命令的入口

```make
# backend/Makefile
migrate-rev:                       ## 生成一条 revision：MSG="..."
	python scripts/make_migration.py --message "$(MSG)"
```

`MSG` 会写进脚本的说明文字，例如「给 runs 加 foo 列」。它不决定脚本内容，内容来自上面的对比。



### 8.2 三条必须守住的规则

**加新表要同时改两处。** 一处是 ORM 模型，并且在 `models/__init__.py` 里 import 它。不 import，autogenerate 看不见这张表，也不会为它起草脚本。另一处是生成一条 revision，让已经在跑的库补上这张表。空库只看模型；已有库只跑脚本。**索引和约束同样两边都要写**（§3.2）：只写在脚本里，新部署的库没有这条约束，而开发者自己的老库看起来一切正常。

**库的版本比代码新时，有一份写好的恢复步骤。** 典型情况是切到一个旧的 git 分支：新分支已经把库升到 `0003`，旧分支的代码只认识 `0001`。Alembic 会拒绝继续，报错也看不出该怎么办。这个情况在多人切换分支时每周都会遇到。恢复步骤写进 README：先备份数据库，再切回那份较新的代码执行 `upgrade head`。不要手改 `alembic_version` 把版本号改小——版本号改了，新加的列还在，旧代码按旧结构读写，数据会静默写错。这也是 §3.2 拒绝启动的原因。

**扩展将来自建的表，用表名前缀声明，然后和 LangGraph 的表一起排除。** 用的是同一个 `include_object`。前缀不能是任何一张我们自己的表名的开头：`register_extension_table_prefix("run")` 会把 `runs` 和 `run_events` 一起藏起来，autogenerate 接着就会起草「把这两张表删掉」。注册前缀时就要做这个撞名检查。M4 还没有扩展系统，过滤器先留一个空集合：`EXTENSION_TABLE_PREFIXES: set[str] = set()`。

### 8.3 排除表的时候，把它上面的索引和约束一起排除

```python
# agentkit/persistence/migrations/_env_filters.py
def include_object(object_, name, type_, reflected, compare_to):
    if type_ == "table" and name in LANGGRAPH_OWNED_TABLES:
        return False
    parent = getattr(object_, "table", None)
    parent_name = getattr(parent, "name", None) if parent is not None else None
    if parent_name in LANGGRAPH_OWNED_TABLES:
        return False
    return True
```

只挡表名不够。Alembic 把「表」和「这张表上的索引」当成两次独立的对比。checkpoint 表被排除了，它上面的索引仍然出现在对比里，而我们的模型没有这些索引，起草结果就是一串 `drop_index`。第二段用父表名把挂在这些表上的索引和约束一并挡掉。

`_env_filters.py` 单独成模块，不要写进 `env.py`。import `env.py` 会把 Alembic 整套运行时拉起来，单测这个几十行的过滤器会变得很重。

### 8.4 第一条脚本 `0001_baseline` 必须建成什么样

链要有一个起点。`0001_baseline` 就是这个起点：`down_revision = None`，它的 `upgrade()` 从零建出和当前模型逐列一致的三张表。

它在空库启动时**不会执行**（§3.2：空库是 `create_all` 然后 `stamp head`）。它存在是为了两件事：有人选择「不要 `create_all`，从零只跑 Alembic」时有一个链根；以及 `test_alembic_autogenerate.py` 能拿它和模型对比，断言「没有尚未写成脚本的结构差异」。

`upgrade()` 必须建出：

- `threads_meta` 的全部列，加上 `assistant_id`、`user_id` 两个索引
- `runs` 的全部列，加上 `ix_runs_thread_status`、`ix_runs_lease`、`uq_runs_idempotency_key`，以及**带 WHERE 的** `uq_runs_thread_active`
- `run_events` 的全部列，加上 `uq_events_thread_seq` 和两个复合索引

「带 WHERE 的唯一索引」意思是：唯一这条规则只对满足条件的行生效。这里的条件是 `status` 为 `pending` 或 `running`，所以同一条 thread 可以有很多已结束的 run，但不能有两条进行中的。SQLite 和 Postgres 的写法不同，脚本里两句都要写：

```python
# agentkit/persistence/migrations/versions/0001_baseline.py
op.create_index(
    "uq_runs_thread_active",
    "runs",
    ["thread_id"],
    unique=True,
    sqlite_where=sa.text("status IN ('pending', 'running')"),
    postgresql_where=sa.text("status IN ('pending', 'running')"),
)
```

`downgrade()` 按建表的反序删掉这三张表。Greenfield 不会在运行中调用它。Alembic 的完整性检查 `alembic check` 要求每条脚本的 `upgrade()` 和 `downgrade()` 成对，空着会失败。

#### `0001_baseline` 的骨架

```python
# agentkit/persistence/migrations/versions/0001_baseline.py
revision = "0001_baseline"
down_revision = None


def upgrade() -> None:
    op.create_table("threads_meta", ..., sa.PrimaryKeyConstraint("thread_id"))
    op.create_index("ix_threads_meta_user_id", "threads_meta", ["user_id"])
    op.create_index("ix_threads_meta_assistant_id", "threads_meta", ["assistant_id"])

    op.create_table("runs", ..., sa.PrimaryKeyConstraint("run_id"))
    op.create_index("ix_runs_thread_status", "runs", ["thread_id", "status"])
    op.create_index("ix_runs_lease", "runs", ["lease_expires_at"])
    op.create_index("uq_runs_idempotency_key", "runs", ["idempotency_key"], unique=True)
    op.create_index(                                          # 带 WHERE，两边都要写（§4.2）
        "uq_runs_thread_active", "runs", ["thread_id"], unique=True,
        sqlite_where=sa.text("status IN ('pending', 'running')"),
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )

    op.create_table("run_events", ..., sa.PrimaryKeyConstraint("id"))
    op.create_unique_constraint("uq_events_thread_seq", "run_events", ["thread_id", "seq"])
    op.create_index("ix_events_thread_cat_seq", "run_events", ["thread_id", "category", "seq"])
    op.create_index("ix_events_run", "run_events", ["thread_id", "run_id", "seq"])


def downgrade() -> None:
    op.drop_table("run_events")
    op.drop_table("runs")
    op.drop_table("threads_meta")
```

删表顺序和建表相反：先 `run_events`，再 `runs`，最后 `threads_meta`。这三张表之间没有外键，这个顺序是为了和 `upgrade()` 对称。

- **每一列都要和** `Base.metadata` **一致**：类型、能否为空、`server_default`。不一致不会在启动时报错。空库走 `create_all` 建成一套，有人从零执行 `upgrade head` 走 `0001` 建成另一套。`test_alembic_autogenerate.py` 就是用来抓住这个分叉的（§11）：对当前模型做一次 autogenerate，断言起草结果是空的，也就是「模型里的结构，脚本链已经全部覆盖」。
- `downgrade()` **要有函数体。** 上面说的完整性检查会拒绝一个空的 `downgrade()`。



### 8.5 装哪些包（接到 M0 `pyproject.toml`）

这一小节不是 Alembic 的概念。它规定 Postgres 路径要多装哪些包，以及漏装时失败发生在什么时候。

```toml
# backend/pyproject.toml
# harness 主依赖（sqlite 是默认路径，直接进 dependencies）
dependencies = [
    "sqlalchemy[asyncio]>=2.0,<3.0",
    "aiosqlite>=0.19",
    "alembic>=1.13",
    "langgraph-checkpoint-sqlite>=3.1.1,<3.2",
    ...
]

[project.optional-dependencies]
postgres = [
    "asyncpg>=0.29",
    "langgraph-checkpoint-postgres>=3.1.1,<3.2",
    "psycopg[binary]>=3.3.3",
    "psycopg-pool>=3.3.0",
]
```

四条缺一不可：`asyncpg` 给 ORM，`psycopg` + `psycopg-pool` 给 checkpointer，
`langgraph-checkpoint-postgres` 给 `AsyncPostgresSaver`。只装 asyncpg
能让引擎起来、第一次 `astream` 才炸在 saver 的 ImportError 上。

---



## 9. 一次"发送消息"落了哪些行

把前面几节串起来。这张时间线是 M4 的验收依据。下面每一跳都标了文件；参数表只列这一跳真正用到的字段，完整签名在括号里的前文章节。

```
POST /threads/{id}/runs/stream          app/gateway/routers/thread_runs.py
  │
  ├─ create_or_reject                   agentkit/runtime/runs/manager.py
  │    └─ create_thread_operation_atomic  agentkit/persistence/run/sql.py
  │         → runs INSERT (pending)     ★ 部分唯一索引在这里定生死
  ├─ create_task(run_after_metadata)    app/gateway/services.py     HTTP 在这里返回
  │    └─ _ensure_thread_metadata       agentkit/runtime/runs/worker.py
  │         → threads_meta INSERT（缺行才建）
  │
  └─ run_agent                          agentkit/runtime/runs/worker.py
       ├─ try_start                     agentkit/runtime/runs/manager.py
       │    └─ RunStore.start_run       agentkit/persistence/run/sql.py
       │         → runs UPDATE status=running   ★ CAS，唯一入口
       ├─ update_status                 agentkit/persistence/thread_meta/sql.py
       │    → threads_meta.status=running
       ├─ astream 循环
       │    ├─ 每个 superstep           LangGraph checkpointer（不进我们的 alembic）
       │    │    → checkpoints / blobs / writes
       │    ├─ 缓冲满                   agentkit/runtime/events/journal.py
       │    │    → run_events INSERT ×N
       │    └─ 每 5s                    agentkit/persistence/run/sql.py
       │         → runs UPDATE（token 进度，不动 status）
       └─ finally · _finalize           agentkit/runtime/runs/worker.py
            ├─ journal.flush()          → run_events 剩余事件 + 终态回执
            ├─ RunManager.complete      → runs UPDATE 终态 + token 汇总
            ├─ update_display_name      → threads_meta.display_name
            ├─ update_status            → threads_meta.status 终态
            └─ publish_end              → 易失，不落库
```

`threads_meta` 的 `running` / 终态投影，M3 草图写成 `set_status`。M4 的方法名是 `update_status`（§4.4），不要再留一个别名。

#### 路由：不写任何行

```python
# app/gateway/routers/thread_runs.py
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
    return sse_response(services, launch, request)
```

| 参数 | 谁给 | 这一跳用它做什么 |
| --- | --- | --- |
| `thread_id` | URL | 交给 `start_run`。body 里的同名字段不看（M3 §4.1） |
| `body` | JSON | `RunCreateRequest`：`assistant_id`、`input`、`config`、`context`、`on_disconnect`、`multitask_strategy` |
| `idempotency_key` | `Idempotency-Key` 头 | 原样传下去；加 scope 在 `start_run` 里，不在路由里（M3 §4.5） |

路由不碰 session、不碰三张表。调度器和 IM 通道以后直接调 `start_run`，不经过这个函数。

#### 准入：`runs` 插入一行 `pending`

`start_run`（`app/gateway/services.py`）的第 1–12 步只校验和组 config，不写库。第一行出现在第 13 步：

```python
# app/gateway/services.py
record = await services.run_manager.create_or_reject(
    thread_id,
    body.assistant_id,
    run_id=run_id,                                          # 第 12 步预分配，幂等复用靠它判断
    on_disconnect=DisconnectMode(body.on_disconnect),
    multitask_strategy=body.multitask_strategy,             # Web 发送是 "reject"
    metadata=run_metadata,
    kwargs=_persisted_kwargs(body),                         # 脱敏后的 input/config/context
    model_name=model_name,
    user_id=caller.user_id,
    idempotency_key=scoped_key,                             # scope_idempotency_key 的结果
)
```

| 参数 | 类型 | 落进 `runs` 的哪一列 |
| --- | --- | --- |
| `thread_id` | `str` | `thread_id` |
| `assistant_id` | `str \| None` | `assistant_id` |
| `run_id` | `str` | `run_id`，主键 |
| `on_disconnect` | `DisconnectMode` | **不落库**。重启后没有 SSE 订阅者（§9.3） |
| `multitask_strategy` | `str` | `multitask_strategy`。`"reject"` 时本进程或库里已有活跃行就 409 |
| `metadata` | `dict` | `metadata_json` |
| `kwargs` | `dict` | `kwargs_json`。`secrets` 和 `__` 前缀已删 |
| `model_name` | `str \| None` | `model_name`。写入前 strip，超 128 截断（§4.2） |
| `user_id` | `str \| None` | `user_id` |
| `idempotency_key` | `str \| None` | `idempotency_key`。已加过 scope |

`create_or_reject` 在 `agentkit/runtime/runs/manager.py`。它握住进程锁，先查幂等键，再查本进程是否已有活跃操作，然后才调仓库。Web 发送的 `multitask_strategy="reject"`，`supersede_active` 为假：库里这条 thread 已经有 `pending` / `running` 行时，插入被唯一索引拒绝，函数抛 `ConflictError`，`start_run` 把它变成 `ThreadBusy`（HTTP 409）。幂等键命中则返回已有的 `RunRecord`，**不再插入第二行**，也**不再** `create_task`。

真正的 INSERT 在仓库：

```python
# agentkit/persistence/run/sql.py
async def create_thread_operation_atomic(self, record: dict, *, kind: str, supersede_active: bool = False) -> bool:
    try:
        async with self._sf() as session:
            session.add(RunRow(
                **self._to_row_kwargs(record, kind),
                status="pending",
                operation_kind=kind,                       # 这次是 "run"
            ))
            await session.commit()
        return True
    except IntegrityError:
        return False                                        # 活跃冲突或幂等键冲突；异常不穿出仓库
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `record` | `dict` | `_record_to_row` 从 `RunRecord` 抽出的可序列化字段（§9.3 的第一组） |
| `kind` | `str` | `ThreadOperationKind.run`。占位操作（compact 等）用别的 kind，收尾时删行；普通 run 行不删 |
| `supersede_active` | `bool` | `"reject"` 时为 `False`。`True` 时先把旧的活跃行标成 `interrupted` 再插，同一个事务（M3 §5.5） |
| 返回 | `bool` | `True` 插入成功。`False` 时 manager 再查 `get_by_idempotency_key`：命中是复用，未命中是 409 |

`uq_runs_thread_active` 只约束 `status IN ('pending','running')` 的行，所以同一条 thread 可以有很多已结束的 run，不能有两条进行中的。`uq_runs_idempotency_key` 挡住同一个 scope 键插两次。

`create_or_reject` 返回之后、`create_task` 之前不许 `await`（M3 §4.5）。挂上 task 之后 `start_run` 就返回，HTTP 开始写 SSE。后面的行都在这个后台 task 里写。

#### 补 thread 行：`threads_meta` 缺了才 INSERT

```python
# agentkit/runtime/runs/worker.py
async def run_after_metadata(
    record: RunRecord,
    run_ctx: RunContext,
    *,
    bridge: StreamBridge,
    run_manager: RunManager,
    owner_user_id: str | None,
    require_existing_thread: bool,
) -> None:
    failure = await _ensure_thread_metadata_bounded(
        record, run_ctx.thread_store,
        owner_user_id=owner_user_id,
        require_existing=require_existing_thread,
    )
    # 成功、超时、abort、严格校验失败，全部进 run_agent。终态和 END 只有那一条路径（M3 §4.6）
    await run_agent(bridge, run_manager, record, run_ctx)
```

| 参数 | 类型 | 谁给 | 说明 |
| --- | --- | --- | --- |
| `record` | `RunRecord` | `create_or_reject` 的返回值 | 读 `thread_id` / `assistant_id` / `metadata` / `abort_event` |
| `run_ctx` | `RunContext` | `start_run` 的 `for_run()` | 只用其中的 `thread_store` |
| `bridge` | `StreamBridge` | `services.bridge` | 这一跳不 publish。传下去给 `run_agent` |
| `run_manager` | `RunManager` | `services.run_manager` | 严格模式失败时调 `fail_start_if_pending` |
| `owner_user_id` | `str \| None` | `caller.user_id` | 显式传给 `get` / `create`。后台 task 上的 ContextVar 可能是空的（§3.3） |
| `require_existing_thread` | `bool` | 调用方。HTTP 发送默认 `False` | `True` 且行已经没了：先把 run 标成 error，不许顺手再建 |

```python
# agentkit/runtime/runs/worker.py
async def _ensure_thread_metadata(
    thread_store: ThreadMetaStore,
    record: RunRecord,
    *,
    owner_user_id: str | None,
    require_existing: bool,
) -> None:
    user_id = owner_user_id if owner_user_id is not None else AUTO
    if await thread_store.get(record.thread_id, user_id=user_id) is not None:
        return
    if require_existing:
        raise LookupError(f"Thread {record.thread_id} was deleted during run admission")
    await thread_store.create(                                 # agentkit/persistence/thread_meta/sql.py
        record.thread_id,
        assistant_id=record.assistant_id,
        user_id=user_id,
        metadata={k: v for k, v in record.metadata.items() if k not in _RESERVED_METADATA_KEYS},
    )
```

| 参数 | 说明 |
| --- | --- |
| `thread_id` | 主键。已存在且同一用户：`create` 幂等返回已有行，不插第二行 |
| `assistant_id` | 抄自 `RunRecord`。侧栏和组图都靠它 |
| `user_id` | `owner_user_id`，没有则 `AUTO`（读 ContextVar；读不到就失败，不写成全局行） |
| `metadata` | 去掉 trace id 等保留键。一个 thread 跨很多 run，run 级的 id 不属于它 |
| `display_name` | 这一跳不传。标题要等 checkpoint 里有 `title` 之后，在收尾投影 |

外面包着 `_ensure_thread_metadata_bounded`：最多等 5 秒，`abort_event` 一到就取消这次 IO。非严格模式下失败只打 warning，run 照跑——缺的是侧栏那一行，不是这一轮对话。`require_existing_thread=True`（MCP 通知、子代理回调）失败则先 `fail_start_if_pending`，随后的 `try_start` 看到非 `pending` 就不组图。

这一跳**不**把 `threads_meta.status` 写成 `running`。那个投影要等 `try_start` 成功（下一步）。

#### 启动栅栏：`runs.status` 从 `pending` 改成 `running`

```python
# agentkit/runtime/runs/worker.py · run_agent 的 try 体
await run_manager.wait_for_prior_finalizing(thread_id, run_id, abort_event=record.abort_event)
if await run_manager.try_start(run_id) is RunStartOutcome.cancelled:
    return                                              # 不组图。END 由 finally 发
await ctx.thread_store.update_status(thread_id, "running", user_id=record.user_id or AUTO)
```

`try_start`（`agentkit/runtime/runs/manager.py`）的三步：

1. 锁内看 `abort_event` 或 `status != pending` → 返回 `cancelled`，不碰库。
2. 锁外调 `RunStore.start_run(run_id)`。这是 `pending → running` 的唯一入口。
3. CAS 回来后再看一次 abort。期间取消先到：内存标 `interrupted` 并补写 store，返回 `cancelled`。

```python
# agentkit/persistence/run/sql.py
async def start_run(self, run_id: str) -> bool:
    async with self._sf() as session:
        result = await session.execute(
            update(RunRow)
            .where(RunRow.run_id == run_id, RunRow.status == "pending")
            .values(status="running", updated_at=datetime.now(UTC))
        )
        await session.commit()
        return bool(result.rowcount)                   # 0：行已经不是 pending，多半被取消了
```

| 参数 / 返回 | 说明 |
| --- | --- |
| `run_id` | 只按主键 + 旧状态更新。不接收调用方传来的 `status`，避免写成别的值 |
| 返回 `True` | 这一行现在是 `running`，离开不了部分唯一索引（它仍然算活跃） |
| 返回 `False` | manager 把内存标成 `interrupted`，worker 不组图 |

store 抛异常不能当成取消：CAS 可能已经提交。manager 抛 `RunStartupError`，worker 按 `error` 收尾。

`update_status`（`agentkit/persistence/thread_meta/sql.py`）在 `try_start` 成功之后才调。它失败就让这次 run 失败：库里的 run 已经是 `running`，侧栏却还是旧状态，比「侧栏晚一秒」更难查。参数是 `thread_id`、`status="running"`、`user_id`（显式，理由同上）。

#### 循环里的三路写入

组图和 `graph.astream` 在 `agentkit/runtime/runs/worker.py` 的 `_stream_graph`。这一段有三路输出，不要合成一次写：

| 路 | 文件 | 函数 | 写入 | 失败了怎样 |
| --- | --- | --- | --- | --- |
| checkpoint | LangGraph checkpointer（`graph.astream` 内部） | saver 自己的 `aput` | `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` | 我们的 alembic 不建、不改这些表（§2、§6） |
| 事件账本 | `agentkit/runtime/events/journal.py` | `RunJournal.flush` → `RunEventStore.put_batch` | `run_events` 插入一批 | 用户刷新后中间那段消息缺失 |
| token 进度 | `agentkit/runtime/runs/manager.py` | `update_run_progress` → `RunStore.update_run_progress` | `runs` 上的 token 列 | 侧栏数字停一下。**不改 status，不写 `run_events`** |

账本的生产者是挂在图上的 callback，不是 worker 里手写的 `put`：

```python
# agentkit/runtime/events/journal.py（完整回调映射与参数见 §5.7）
class RunJournal(AsyncCallbackHandler):
    run_inline = True

    def __init__(self, store: RunEventStore, record: RunRecord, *,
                 batch_size: int = 20, flush_interval: float = 5.0,
                 track_token_usage: bool = True, max_trace_content: int = 10240) -> None: ...

    async def flush(self) -> None: ...     # worker 的 finally 必须调它
```

回调映射（哪个回调写哪条事件）与参数表只在 §5.7 定义一次，这里不重复。

`put_batch` 里每条事件的 `seq` 由 `agentkit/persistence/events/db.py` 的 `_next_seq(thread_id)` 分配：进程内锁加上 `SELECT max(seq) ... FOR UPDATE`，冲突重试最多 3 次（§5.3）。调用方不传 `seq`，也不传 `created_at`。

进度这一路：

```python
# agentkit/runtime/runs/manager.py
async def update_run_progress(self, run_id: str, totals: RunTotals) -> None:
    async with self._lock:
        record = self._runs.get(run_id)
        if record is None or record.status is not RunStatus.running:
            return
        _apply_totals(record, totals)
    await self._store.update_run_progress(run_id, **asdict(totals))
```

```python
# agentkit/persistence/run/sql.py
async def update_run_progress(self, run_id: str, **totals: Any) -> None:
    async with self._sf() as session:
        await session.execute(
            update(RunRow)
            .where(RunRow.run_id == run_id, RunRow.status == "running")
            .values(**totals, updated_at=datetime.now(UTC))
        )
        await session.commit()                             # rowcount == 0：已经终态，静默丢掉
```

| 参数 | 说明 |
| --- | --- |
| `run_id` | 只更新这一行 |
| `totals` | `RunTotals` 的字段，键必须是列名：`total_input_tokens`、`total_output_tokens`、`token_usage_by_model` 等（§4.2 ④）。**不能**带 `status` |

节流在 worker：距上次进度写不足 5 秒就不调。CAS 的 `WHERE status='running'` 保证一个迟到的进度写不会把终态行拉回 `running`。

#### 收尾：终态可见之前先把账本落盘

`run_agent` 的内层 `finally` 调用 `_finalize`。终态一旦写上，这一行就离开部分唯一索引，别的请求可以再准入。所以要改 checkpoint、要补的事件，必须在终态之前做完。

**`ownership_lost` 为真时，下面所有落库全部跳过**（M9 的租约丢失才为真；M4 列建好了但不写，这个判断现在就要有）。新增的收尾写入挂在这一次 return 下面，不要各自再写一个 `if`。

```python
# agentkit/runtime/runs/worker.py
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
    journal: RunJournal,
) -> None:
    if record.ownership_lost:
        return

    # ① 终态可见之前
    if record.abort_event.is_set() and record.abort_action == "rollback":
        await _best_effort("rollback", restore_rollback_point(ctx, rollback))
    await _best_effort("journal", journal.flush())          # 剩余事件；终态回执走 put_if_absent

    # ② 终态 + token，一次写完
    await _best_effort(
        "complete",
        run_manager.complete(record.run_id, status=terminal, error=error, totals=usage.totals()),
    )

    # ③ 只依赖终态的投影。同 thread 上已有更新的 run 时不写，避免把侧栏打回 idle
    if not await run_manager.has_later_run(record.thread_id, record.run_id):
        await _best_effort(
            "display name",
            _project_display_name(ctx, record),             # 读 checkpoint title → update_display_name
        )
        await _best_effort(
            "thread status",
            ctx.thread_store.update_status(
                record.thread_id, _thread_status(record.status), user_id=record.user_id or AUTO,
            ),
        )
```

| 参数 | 类型 | 谁给 | 收尾怎么用 |
| --- | --- | --- | --- |
| `bridge` | `StreamBridge` | `run_agent` | ② 之后、且胜者是 `error` / `timeout` 时发 `error` 帧。取消获胜不发 |
| `run_manager` | `RunManager` | 同上 | `complete`、`has_later_run` |
| `record` | `RunRecord` | 本 run 活体 | 读 `ownership_lost`、`abort_action`、`run_id`、`thread_id`、`user_id`。`status` 以 `complete` 之后的胜者为准 |
| `ctx` | `RunContext` | `for_run()` 的快照 | `thread_store`、checkpointer（回滚和读 title） |
| `terminal` | `RunStatus` | 各 `except` 算出的意图 | 交给 `complete`。取消先写过终态时，manager 忽略它，改写赢家的状态 |
| `error` | `str \| None` | `_public_error(exc)` | 进 `runs.error`。取消获胜时不覆盖赢家的 `error` |
| `error_type` | `str \| None` | `type(exc).__name__` | 只进 error 帧的 `error` 字段，不进库 |
| `rollback` | `RollbackPoint \| None` | `try_start` 成功后捕获的运行前 head | 仅 `abort_action=="rollback"` 时还原 |
| `usage` | `UsageCollector` | 本 run | `totals()` 和终态一次写入。图没跑也交一份全 0 |
| `journal` | `RunJournal` | `run_agent` 在组图前建好 | `flush()`。对象在 `try_start` 被拒绝时也要能 `flush` 空缓冲 |

`complete`（`agentkit/runtime/runs/manager.py`）先在锁里改内存，再用赢家的终态调仓库：

```python
# agentkit/persistence/run/sql.py
async def update_run_completion(
    self, run_id: str, *, status: str, error: str | None = None, **totals: Any,
) -> bool:
    async with self._sf() as session:
        result = await session.execute(
            update(RunRow)
            .where(
                RunRow.run_id == run_id,
                or_(RunRow.status.not_in(TERMINAL_STATUSES), RunRow.status == status),
            )
            .values(status=status, error=error, updated_at=datetime.now(UTC), **totals)
        )
        await session.commit()
        return bool(result.rowcount)
```

| 参数 / 返回 | 说明 |
| --- | --- |
| `status` | 必须是 `success` / `error` / `timeout` / `interrupted` 之一。`complete` 收到活跃状态直接 `ValueError` |
| `error` | 写入 `runs.error`。成功路径是 `None` |
| `totals` | 和进度那一路同一组列。和 `status` 在同一次 UPDATE 里，避免崩溃留下「终态有了、token 全是 0」 |
| 返回 `True` | 写进去了。同一个终态可以再写一遍（崩溃重试） |
| 返回 `False` | 行已经是**另一个**终态。后到的 `success` 覆盖不了已经接受的取消 |

`_project_display_name` 读这条 thread 的 checkpoint head 里的 `title`，空标题不写。然后：

```python
# agentkit/persistence/thread_meta/sql.py
async def update_display_name(self, thread_id: str, display_name: str, *, user_id: str | _AutoSentinel = AUTO) -> None: ...
async def update_status(self, thread_id: str, status: str, *, user_id: str | _AutoSentinel = AUTO) -> None: ...
```

| 参数 | 这一跳的值 |
| --- | --- |
| `thread_id` | `record.thread_id` |
| `display_name` | checkpoint 的 `title`。空串当没有标题，不把库里的名字擦掉 |
| `status` | `_thread_status`：`error` / `timeout` → `"error"`，其余（含用户取消）→ `"idle"`。thread 的 `interrupted` 留给 M6 的人机确认，不是「被用户停了」 |
| `user_id` | `record.user_id or AUTO` |

`_finalize` 返回之后，`run_agent` 最内层的 `finally` 还要做两件不落库的事：`bridge.publish_end(run_id)`（`agentkit/runtime/stream_bridge/`，成功、失败、取消、没组图都发），以及 `run_manager.schedule_cleanup(run_id)`。END 没发，前端会一直转圈。

每一步用 `_best_effort` 单独接 `Exception`：账本 flush 失败不能挡住终态，终态失败不能挡住侧栏投影。`CancelledError` 不接，关机的强制取消照样往上抛，外层 `finally` 仍然发 END。

#### 关机顺序：先停写入，再关引擎

M3 §6.3 已经要求：abort inflight → 等最后一次 checkpoint 写完 → 关 checkpointer 连接池。
M4 在这后面再加一步：

```
1. 给 inflight task 设 abort、task.cancel()
2. 超时内等它们把 checkpoint + run_events flush + 终态写完
3. dispose checkpointer 连接（LangGraph 的）
4. `await close_engine()`                            ★ agentkit/persistence/engine.py；必须在 2 之后
```

第 4 步提前的话，worker `finally` 里的 `update_run_completion` 会打到已关闭的引擎，
产生一个关不掉的异常循环，进程无法退出——和 M3 把 checkpointer 关早了是同一个症状，
只是换成了另一条连接池。

`close_engine()` 要先 `await _engine.dispose()`，再把模块级 `_engine` / `_session_factory`
置回 `None`。只关连接不置空，测试隔离会看到一个已 dispose 的引擎被继续拿去建 session。

### 9.1 启动时回收 inflight

M4 是单 worker。进程挂了，所有 `pending` / `running` 的行都是孤儿——内存里的
`RunRecord` 和 task 已经没了，没有人会再写终态。lifespan 在开始接 HTTP **之前**：

```python
# agentkit/persistence/recovery.py
async def recover_inflight_runs(run_store: RunStore, thread_meta: ThreadMetaStore) -> int:
    rows = await run_store.list_inflight()          # 不按 user_id 过滤
    for row in rows:
        await run_store.update_run_completion(
            row["run_id"],
            status="error",
            error="interrupted: gateway restarted",
        )
        await thread_meta.update_status(row["thread_id"], "error")
    return len(rows)
```

**不要尝试 resume** `astream`**。** checkpoint 可能停在 superstep 中间，接着跑会重放
当时正要执行的工具调用。正确的用户体验是：刷新后看到一条错误，可以重新发送。
M9 多 worker 才用租约 + `claim_for_takeover` 做真正的接管；M4 把租约列建好但回收
路径不读它们。`GATEWAY_WORKERS>1` **时禁止再走本函数**——它会把 peer 正在跑的
inflight 标成 error。替换路径见 [M9 §3.3](2026-09-16-m9-production-design.md)。

`update_run_completion` 的终态守卫在这里是友军：如果关机排空已经把某行写成
`success`，启动回收再写 `error` 会被拒——这正是想要的。

#### `recover_inflight_runs` 的参数与边界

```python
# agentkit/persistence/recovery.py
RESTART_ERROR = "interrupted: gateway restarted"


async def recover_inflight_runs(run_store: RunStore, thread_meta: ThreadMetaStore) -> int:
    """启动时把孤儿活跃行收成 error；返回处理了几条。GATEWAY_WORKERS > 1 时禁止调用。"""
    rows = await run_store.list_inflight()                  # 不按 user_id 过滤：这是进程级回收
    for row in rows:
        await run_store.update_run_completion(
            row["run_id"], status="error", error=RESTART_ERROR,
        )
        await thread_meta.update_status(row["thread_id"], "error", user_id=row["user_id"])
    return len(rows)
```


| 参数 / 返回         | 类型                     | 说明                                                          |
| --------------- | ---------------------- | ----------------------------------------------------------- |
| `run_store`     | `RunStore`（ABC）        | 只调 `list_inflight` 和 `update_run_completion` 两个方法           |
| `thread_meta`   | `ThreadMetaStore`（ABC） | 同步投影；`user_id` 显式传 `row["user_id"]`（后台路径没有 ContextVar，§3.3） |
| 返回              | `int`                  | 收掉的行数；lifespan 打一条 warning 就够了，不要为此拒绝启动                     |
| `RESTART_ERROR` | `str`                  | 也是 §12 验收清单里要 grep 的那个字符串                                   |


- `update_run_completion` **的终态守卫在这里是友军**：关机排空已经把某行写成 `success` 时，
这里的 `error` 会被拒——这正是想要的（§9.1 末尾）。
- **不 resume** `astream`**，也不读租约列。** 真正的接管是 M9 的 `claim_for_takeover`；
M4 只建列不用（§9.1）。



### 9.2 lifespan 接线顺序

```
1. load AppConfig
2. init_engine_from_config(database)          # ORM + bootstrap_schema
3. async with make_checkpointer(config) as cp:
4.     make_thread_meta_store(get_session_factory())
5.     make_run_store(get_session_factory())
6.     make_run_event_store(run_events)        # GATEWAY_WORKERS 门在这里
7.     recover_inflight_runs(...)              # 必须在 yield 之前
8.     yield                                   # 开始接 HTTP
9.     # shutdown：M3 §6.3 的 1-3 步，然后 close_engine()
```

3 和 2 的顺序不能反：checkpointer `setup()` 可能和 ORM `create_all` 抢同一把
SQLite 写锁。先 ORM bootstrap（带着 advisory/file lock），再 saver.setup()，
saver 看到表已在（如果有人误把 checkpoint 表放进 Base——不应该）或自己建。
两者都幂等，但锁顺序要稳定，否则双进程启动会超时。

工厂函数返回的 store 挂到 `app.state`，路由通过 `deps.py` 取。不要在模块级
`store = make_...(get_session_factory())`——`init_engine` 还没跑时 factory 是 None，
会永久绑到 Memory 实现。

#### lifespan 的接线代码

```python
# app/gateway/app.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    app_config = get_app_config()
    await init_engine_from_config(app_config.database)                # ② ORM + bootstrap_schema
    thread_meta = make_thread_meta_store(get_session_factory())       # ④
    run_store = make_run_store(get_session_factory())                 # ⑤
    event_store = make_run_event_store(app_config.run_events)         # ⑥ 多 worker 的门也在这里
    async with make_checkpointer(app_config) as checkpointer:         # ③ 独立连接池 + setup()
        services = build_run_services(                                #    装配进程级单例
            app_config, checkpointer,
            thread_meta=thread_meta, run_store=run_store, event_store=event_store,
        )
        recovered = await recover_inflight_runs(run_store, thread_meta)   # ⑦ 必须在 yield 之前
        if recovered:
            logger.warning("Recovered %d inflight run(s) as error", recovered)
        app.state.run_services = services
        try:
            yield                                                     # ⑧ 开始接 HTTP
        finally:
            await services.run_manager.shutdown(                      # M3 §6.3 的 ①②
                timeout=app_config.runs.shutdown_timeout_seconds,
            )
            cancel_detached()
    await close_engine()                                              # ⑨ 最后一步，在 async with 之外
```


| 步   | 做什么                         | 失败会怎样                                              |
| --- | --------------------------- | -------------------------------------------------- |
| ②   | ORM 引擎 + `bootstrap_schema` | 起不来。**不能降级成 memory 继续跑**——那会让数据静默写进一个重启就没的地方       |
| ③   | checkpointer 池 + `setup()`  | 起不来。第一次 `astream` 才炸的话，用户看到的是"发送失败"                |
| ④⑤⑥ | 三个 store 工厂                 | 工厂是纯函数，只有配置错误才会炸（比如 `run_events.backend=db` 却没有引擎） |
| ⑦   | `recover_inflight_runs`     | 起不来。带着孤儿行开始接请求，那条 thread 会一直 409                   |
| ⑧   | `yield`                     | ——                                                 |
| ⑨   | 排空 → `close_engine()`       | 顺序反了就是一个关不掉的异常循环（见上）                               |


- `close_engine()` **放在** `async with make_checkpointer(...)` **外面。** 缩进进去的话，
checkpointer 的连接池会在 ORM 引擎之后才关——而 worker 的收尾（终态写）用的是 ORM，
它必须在两者都还活着的时候跑完。
- **⑦ 的失败不能吞。** 孤儿行留着，那条 thread 的准入会一直 409，
而用户看到的只是"发不出消息"——比启动失败难查得多。
- **这一步要求** `RunServices` **多两个字段。** M3 §4.1 的 `RunServices` 只有 `run_manager` /
`bridge` / `run_context`；M4 要把 `run_store` 和 `event_store` 挂上去：
启动回收要直接调 store 的 `list_inflight` / `update_run_completion`（`RunManager.complete()`
只认内存里的活体，孤儿行在它那里是 `KeyError`），worker 收尾要 flush 账本。
不加这两个字段，这两件事都只能去摸 `get_session_factory()`——那就绕过了工厂分叉（§3.3）。



### 9.3 `RunRecord` 怎么和水合

M3 的 `RunRecord` 仍然是进程内活体：task、StreamBridge 订阅、`ownership_lost`
这种不能进 SQL 的字段。`RunStore` 只存可序列化的那一组（M3 §5.1 第一组）。

水合方向：


| 时机                   | 方向                                            |
| -------------------- | --------------------------------------------- |
| `create_or_reject`   | `RunRecord` → `RunStore.put`                  |
| 重启后 `GET /runs/{id}` | `RunStore.get` → 临时 dict，**不再造活体**（task 已经没了） |
| inflight 的心跳/进度      | 活体累加 → `update_run_progress`                  |
| `finally`            | 活体终态 → `update_run_completion`                |


**不要在启动时把 SQL 行水合成** `RunRecord` **再塞回** `RunManager`**。** 没有 task 的
活体是半残的，任何 `await record.task` 都会挂死。重启后 inflight 行的唯一归宿
是 §9.1 的 error。

---



#### `RunRecord` ↔ `RunRow` 的字段映射

M3 的 `RunRecord` 有三组字段，只有第一组（可序列化）和第三组（统计）进 SQL：


| `RunRecord` 字段                                                                | `RunRow` 列                             | 方向        | 说明                                                       |
| ----------------------------------------------------------------------------- | -------------------------------------- | --------- | -------------------------------------------------------- |
| `run_id` / `thread_id` / `assistant_id` / `user_id` / `model_name`            | 同名列                                    | 写 + 读     | 准入时一次写定（§4.2 ①）                                          |
| `status` / `error` / `stop_reason`                                            | 同名列                                    | 写 + 读     | 只经 CAS 写；读回来用于 `GET /runs/{id}`                          |
| `metadata` / `kwargs`                                                         | `metadata_json` / `kwargs_json`        | 写 + 读     | `kwargs` 是**脱敏**副本；读回来时别名还原成 `metadata` / `kwargs`（§3.3） |
| `idempotency_key` / `multitask_strategy` / `operation_kind` / `on_disconnect` | 同名列（`on_disconnect` 除外）                | 写 + 读     | `on_disconnect` 只活在内存里：重启后没有 SSE 订阅者，它没有意义               |
| `created_at` / `updated_at`                                                   | 同名列（ISO-8601 `Z`）                      | 写 + 读     | 由 DB 默认值产生，读回来经 `iso_utc` 规范化                            |
| token 七个字段 + `token_usage_by_model`                                           | 同名列                                    | 写 + 读     | 进度节流 + 收尾一次写定（§4.2 ④）                                    |
| `task` / `abort_event` / `start_lock` / `finalizing` / `store_only`           | **没有列**                                | 只在内存      | 进程内活体；重启后不存在（这就是"不水合成活体"的理由）                             |
| `owner_worker_id` / `lease_expires_at` / `ownership_lost`                     | `owner_worker_id` / `lease_expires_at` | 列有了，M4 不写 | `ownership_lost` 是进程内判断，不进库（M9 §3.1）                     |


**水合只发生在一个方向：** `RunStore.get()` 把行读成一个**临时 dict**，交给路由；
`RunManager` 的内存登记表在重启后是空的，没有人会拿着没有 `task` 的 `RunRecord` 去
`try_start` 或 `await record.task`（§9.3 的纪律）。

## 10. 谁删数据

**M4 要明确接受一个现实：大部分表是只增不删的。**


| 动作                     | 删掉什么                                  | 留下什么                               |
| ---------------------- | ------------------------------------- | ---------------------------------- |
| `DELETE /threads/{id}` | thread 目录、checkpoint、`threads_meta` 行 | `runs` **和** `run_events` **行会留下** |
| thread 操作收尾            | 自己那条短命 `runs` 占位行                     | —                                  |
| checkpoint 保留策略        | 见下                                    | —                                  |


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

`DELETE /threads/{id}` 的顺序：

```
1. 拒绝，如果该 thread 有 pending/running 的 run（409）
2. checkpointer.adelete_thread(thread_id)     # 所有 checkpoint_ns
3. thread_meta.delete(thread_id)              # 投影行
4. 磁盘：rm -rf threads/{thread_id}/          # M2 工作区
5. 不碰 runs / run_events
```

第 2 步失败不要继续 3——否则侧栏没了、checkpoint 还在，用户再建同 id 会
撞上旧谱系。第 4 步失败记 warning，不回滚 SQL（工作区可以手工清）。
`incarnation` 的意义在这里：同 id 重建后新行有新 incarnation，后台任务
比对失败就停手。

---



#### `DELETE /threads/{id}` 的顺序与参数

```python
# app/gateway/routers/threads.py
@router.delete("/{thread_id}")
async def delete_thread(thread_id: str, request: Request) -> dict[str, Any]:
    services = get_run_services(request)
    caller = caller_from_request(request)

    if await services.run_manager.has_inflight(thread_id):        # 1. 有活跃 run → 409
        raise ThreadBusy(f"Thread {thread_id} has an active run")

    await delete_thread_checkpoints(services.run_context.checkpointer, thread_id)   # 2. 所有 ns
    await services.run_context.thread_store.delete(thread_id, user_id=caller.user_id)  # 3. 投影行
    try:
        await asyncio.to_thread(shutil.rmtree, workspace_dir(thread_id), True)      # 4. 工作区
    except OSError:
        logger.warning("Failed to remove workspace for %s", thread_id, exc_info=True)
    # 5. 不碰 runs / run_events
    return {"deleted": thread_id}
```


| 步   | 动作                                   | 失败怎么办                | 为什么这个顺序                                           |
| --- | ------------------------------------ | -------------------- | ------------------------------------------------- |
| 1   | 有 `pending` / `running` 的 run 就 409  | 让客户端先取消再删            | 删掉一个正在跑的 thread，worker 的收尾会写回一条已经没有 meta 的 thread |
| 2   | `adelete_thread`（所有 `checkpoint_ns`） | **不要继续第 3 步**，整个请求失败 | 反过来的话侧栏没了、checkpoint 还在，用户重建同 id 会撞上旧谱系           |
| 3   | 删 `threads_meta` 行                   | 往上抛                  | 投影行没了就等于"这条 thread 不存在了"                          |
| 4   | `rmtree` 工作区                         | 只记 warning，不回滚 SQL   | 工作区可以手工清；为一个删不掉的文件把已经删好的 SQL 回滚，代价更大              |
| 5   | `runs` / `run_events` **留着**         | ——                   | 它们是可读历史和审计账本（见上）                                  |


- `has_inflight` **是进程内判断**（M3 §5 的公开方法表）。M4 单 worker 够用；
M9 多实例要改成查 store 的 `list_inflight`（M9 §3.3）。
- `rmtree` 走 `to_thread`：删一个带大产物的目录会阻塞事件循环，M2 的 blocking-IO 套件会抓到。



## 11. 测试清单


| 文件                                  | 覆盖                                                                                                      |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `test_repositories.py`              | **参数化跑 sqlite + postgres 两遍**，所有仓库 CRUD；`user_id=AUTO` 无 ContextVar 时 fail-closed；显式 `None` 才全局查        |
| `test_json_semantics.py`            | 字段缺失 vs null；bool 不匹配 int（含 `True` 不匹配 `1`）；float 接受 `1` 和 `1.0`；非法 key 字符被拒；int64 溢出被拒                 |
| `test_unique_active_run.py`         | 部分唯一索引真的在**新建的空库**上生效（走 `create_all` 那条路）；终态行不占名额，第二条历史 run 能插入                                         |
| `test_run_event_seq.py`             | `seq` per-thread 严格递增；并发 put 不产生重复；双向游标两边都升序；`put_batch` 分配连续 seq；`put_if_absent` 第二次返回 `created=False` |
| `test_run_journal.py`               | 不满 batch_size 也在 flush() 时落盘；`on_llm_new_token` 不写账本；token 进度节流期间 `status` 不变                           |
| `test_llm_callbacks_fire.py`        | **用假 chat model 跑一次 `create_agent`**：`on_chat_model_start` 与 `on_llm_end` 各触发一次、`on_llm_start` 零次；中间件用自己的 config 调模型时事件缺失（负例） |
| `test_terminal_not_overwritten.py`  | `update_run_completion` 拒绝覆盖另一个终态                                                                       |
| `test_restart_recovery.py`          | 重启后历史会话完整；inflight run 被收成 error；`run_events.backend=memory` 时消息丢失是预期                                   |
| `test_alembic_autogenerate.py`      | 对着当前 ORM 模型跑 `autogenerate`，**断言产生空 diff**（防止有人加了模型忘了 migration）                                        |
| `test_langgraph_tables_excluded.py` | autogenerate 不会试图 drop checkpoint 表或其索引                                                                 |
| `test_bootstrap_empty.py`           | 空库走 `create_all`+`stamp head` 后 `alembic_version` 等于 head，且 `uq_runs_thread_active` 存在                  |
| `test_checkpoint_accessor.py`       | 业务代码不直接 `get_tuple`（可 AST 扫）；谱系读取不按时间排序；空账本 + 非空 checkpoint 时 seed 只跑一次                                 |
| `test_engine_lifecycle.py`          | `backend=memory` 时 `get_session_factory()` 是 `None`；`close_engine` 后单例为 `None`；SQLite 新连接带 WAL          |
| `test_event_catalog_contract.py`    | catalog 定义与 `contracts/run_event_stream_contract.json` 双向一致；超长 event_type / category 构造时炸               |
| `test_put_batch_atomic.py`          | `put_batch` 中途失败整批不落；成功则 seq 连续且等长返回                                                                    |
| `test_jsonl_seq_restart.py`         | jsonl 进程重启后第一次 put 的 seq = 旧 max+1，不从 1 重计                                                              |
| `test_session_not_held.py`          | AST 扫描 worker：`astream` 循环里没有 `async with.*session`                                                     |
| `test_startup_recovery.py`          | 库里留一条 running 行，启动后变成 error，threads_meta.status 同步；已是 success 的行不被改成 error                              |
| `test_url_dialect.py`               | `postgresql://` → ORM 是 `+asyncpg`、checkpointer DSN 没有 `+asyncpg`；`postgres://` 短别名同样处理                 |
| `test_delete_thread_order.py`       | 有活跃 run 时 409；checkpointer 删除失败时 threads_meta 行仍在                                                       |
| `test_create_idempotent.py`         | 同用户重复 `ThreadMetaStore.create` 返回已有行；不同用户抛 `ThreadOwnershipConflictError`                               |


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
- [ ] 压缩一次对话之后，checkpoint `messages` 变短，`GET /messages/page` 仍能翻到压缩前的消息
- [ ] `run_events.backend` 默认 `memory`：重启后面侧栏还在、消息没了——这是预期，文档里写清
- [ ] 两个 Gateway 进程同时启动同一空 SQLite，最终只有一份 schema，无 `IntegrityError`
- [ ] 关机时 inflight run 能把终态写完再退出；把 `close_engine` 提前会在测试里被 catch
- [ ] catalog 里加一个新 event_type 却忘了更新 JSON 合同 → CI 红
- [ ] 启动时库里一条 `running` 行被收成 `error="interrupted: gateway restarted"`；已是 `success` 的行不动
- [ ] `postgresql://` 用户 URL 被拆成 ORM `+asyncpg` 和 checkpointer 裸 DSN；缺 extra 时报错含 `uv sync --extra postgres`
- [ ] `DELETE /threads/{id}` 时若有活跃 run → 409；checkpointer 删失败则 meta 行仍在
- [ ] 仓库测试 commit 后用**新 session** 再读，看得到行

---



## 13. 字段所有权矩阵

写代码时对着这张表。答不上来的格子不要开工。


| 字段                          | 权威                              | 谁写                               | 何时写                                     | 能否删                |
| --------------------------- | ------------------------------- | -------------------------------- | --------------------------------------- | ------------------ |
| checkpoint `messages`       | checkpoint                      | LangGraph saver                  | 每个 superstep                            | 否（除非删整条 thread）    |
| checkpoint `title`          | checkpoint                      | 标题中间件 / 图状态                      | 首轮结束后                                   | 否                  |
| 前端历史消息                      | `run_events` `category=message` | RunJournal                       | 完整消息落成时                                 | M4 不删；删 thread 也留  |
| `runs.status`               | `runs`                          | RunStore CAS / completion        | 准入 pending；try_start running；finally 终态 | 行不删；非 run 操作收尾删占位行 |
| `runs.total_*_tokens`       | `runs`（内存累加）                    | `UsageCollector`（M3 §6.5）→ `update_run_progress` / `complete` | 节流 5s + 收尾 | 随行留 |
| `threads_meta.display_name` | **投影**，权威在 checkpoint.title     | worker finally                   | 收尾抄过来                                   | 随 meta 行           |
| `threads_meta.status`       | **投影**，权威在该 thread 最新 run       | worker try_start / finally       | running / 终态                            | 随 meta 行           |
| `threads_meta.incarnation`  | `threads_meta`                  | create                           | 建行时 `uuid4().hex`                       | 随 meta 行；重建必换新值    |
| `run_events.seq`            | store                           | store `_next_seq`                | 每次 put                                  | 不改、不重编号            |
| StreamBridge 队列             | 易失                              | worker                           | astream 期间                              | 进程没了就没了            |
| `RunRecord.task`            | 进程内活体                           | RunManager                       | create_task                             | 重启即丢               |
| 工作区文件                       | 磁盘                              | 沙箱 / 上传                          | M2                                      | 删 thread 时 rm -rf  |


**跨面复制只允许这一条：** 空账本 + 非空 checkpoint → 一次性 seed 成 `message` 事件。
其它方向（账本回写 checkpoint、用事件反算 token、用 meta 当历史）都是 bug。

---



## 14. 刻意不做的事


| 不做                    | 理由                                                 |
| --------------------- | -------------------------------------------------- |
| `delta` checkpoint 模式 | O(N²) 真的成为问题时再做。但 metadata 标记和配置项现在就留              |
| checkpoint 保留 / 裁剪    | 见 §10。要先写合同（哪些不能删）再写实现                             |
| `run_events` 的 TTL    | 同上。它有两个身份，得先决定谁优先                                  |
| 用户表 / 认证              | M9。但 `user_id` 列现在每张表都有                            |
| 定时任务、MCP 任务、子代理批次等表   | 各自阶段。它们形状高度一致（`status` + 租约 + 部分唯一索引），M4 把这个模式立好就行 |
| 多实例的租约实际使用            | M9。M4 只建列                                          |
| 分库分表、读写分离             | 不在这个项目的规模内                                         |


---



## 15. 一句话总结

M4 的产出物不是"接了个数据库"，而是**一张明确的数据所有权地图**。

检验标准：随便指着一个字段问"它的权威在哪、谁写它、什么时候写、能不能删"，
应该都有一句话的确定答案。答不上来的字段，就是下一个数据一致性 bug 的位置。