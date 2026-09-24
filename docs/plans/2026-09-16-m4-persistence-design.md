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

| 天 | 内容 |
|---|---|
| D1–2 | 引擎 / session 工厂 / 三后端选择 + `bootstrap_schema` |
| D3–5 | 三张核心表（`threads_meta` / `runs` / `run_events`）+ 仓库层 |
| D6–7 | `RunEventStore` 三后端 + `seq` 分配 |
| D8 | Checkpointer 接 SQLite / Postgres + alembic 排除 |
| D9–10 | SQLite / Postgres 一致性测试（同一套测试跑两遍） |

### M4 新增的文件

**SQL 归属在 harness 层，Gateway 只在 lifespan 里 `init_engine` / `close_engine`。**
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
├── models/
│   ├── __init__.py               # 必须 import 所有模型，autogenerate 才看得见
│   └── run_event.py              # RunEventRow（实现在 runtime/events/store/db.py）
├── thread_meta/
│   ├── model.py                  # ThreadMetaRow
│   ├── base.py                   # ThreadMetaStore ABC
│   ├── sql.py
│   └── memory.py
├── run/
│   ├── model.py                  # RunRow
│   └── sql.py                    # SqlRunStore，实现 M3 的 RunStore ABC
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
agentkit/runtime/runs/store/           # ABC + Memory 在 M3；M4 只加工厂分叉
└── __init__.py                        # make_run_store(session_factory)

agentkit/runtime/events/
├── catalog.py                 ★  # event_type / category 权威名单
├── journal.py                 ★  # RunJournal：callback → put_batch
└── store/
    ├── base.py
    ├── memory.py
    ├── db.py
    ├── jsonl.py
    └── __init__.py               # make_run_event_store()
```

**ABC 跟调用方走，SQL 跟 ORM 走。** `RunStore` 的接口在 M3 已经定在
`runtime/runs/store/base.py`，因为 `RunManager` 依赖它；SQL 实现放
`persistence/run/sql.py`。`RunEventStore` 反过来：接口在 runtime，因为账本是运行时
概念，ORM 行只是 db 后端的物理落地。不要把两套 ABC 都塞进 persistence——
那样 M1 客户端为了调 `list_messages` 还得 import 持久化包。

**每个仓库都是「同一份 ABC、两份实现」。** `database.backend=memory` 时
`get_session_factory()` 返回 `None`，工厂函数必须退化到 memory 实现，
而不是让调用方自己 `if session_factory is None`。调用方只认 ABC。

`models/__init__.py` 必须把所有 ORM 类 import 一遍。漏掉任何一个，
`create_all` 建不出那张表，`autogenerate` 也看不见它——然后 CI 的
「空 diff」测试会绿，上线才 500 `no such table`。

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
  backend: sqlite          # memory | sqlite | postgres   startup-only
  sqlite_dir: .myflow
  url: postgresql+asyncpg://...   # 仅 postgres
  checkpoint_channel_mode: full   # full | delta，启动时冻结
  postgres_schema: ""             # 非空时钉 search_path

run_events:
  backend: memory          # memory | db | jsonl          startup-only
  max_trace_content: 10240 # 仅 db：category=trace 超此字节截断
  track_token_usage: true  # RunJournal 是否把 usage 累到 RunRow
```

| backend | ORM 引擎 | 应用数据 | checkpoint |
|---|---|---|---|
| `memory` | 不初始化，`get_session_factory()` 返回 `None` | 仓库退化成进程内实现 | `InMemorySaver` |
| `sqlite` | `{sqlite_dir}/app.db`，WAL | 同一个文件 | 同一个文件 |
| `postgres` | `database.url` | 同库 | 同库 |

**`memory` 后端不是可有可无的。** 它让整个测试套件能在没有任何外部依赖的情况下跑，
也让 `make dev` 的第一次启动不需要用户配数据库。代价是每个仓库都要显式处理
`session_factory is None` 的分支——写的时候烦，但比测试要起 Postgres 容器划算得多。

引擎和 session 工厂是**模块级单例**：Gateway 启动 `init_engine()`，关闭 `close_engine()`。
`backend="memory"` 时 `init_engine` 直接 return。

一个小而有用的细节：**JSON 列统一用 `ensure_ascii=False` 序列化**。
不这么做的话中文在库里全是 `\uXXXX`，人肉排查数据时极其痛苦。

```python
def _json_serializer(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)
```

`default=str` 不能省：`datetime` / `UUID` 进 JSON 列时没有它会在 worker `finally`
里炸掉——而那正是 M3 嵌套 finally 保护的那条路径。序列化失败表现为「run 终态写不进去」。

#### `init_engine` 的后端分叉

```python
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

**SQLite 的 PRAGMA 必须挂在 `connect` 事件上，不能只在启动时跑一次。**
它们是 per-connection 的；连接池里每条新连接都是默认设置。漏掉 WAL，
并发读写会直接 `database is locked`。`busy_timeout=30000` 是给「两个 Gateway
进程同时 bootstrap」用的：第二个要等第一个跑完 `CREATE TABLE`。

**`makedirs` 必须 `asyncio.to_thread`。** `init_engine` 跑在 FastAPI lifespan
的事件循环上，同步 `os.makedirs` 会卡住启动。M2 的 blocking-IO 套件会抓住这条。

**Postgres 连接池三项现在就设：** `pool_pre_ping=True`（丢掉闲置死连接）、
`pool_recycle=300`（在代理掐掉之前主动回收）、`command_timeout=30`
（单条 SQL 上限，防止一个卡住的查询占死整个池）。这三项看起来像运维调优，
但缺了任何一项，M9 多实例下的症状都是「偶发的 30 秒卡死」，功能测试看不见。

**`asyncpg` 走 extras**（M0 §3.2 同一条原则）。配了 `backend: postgres` 却没装
extra 时，报错必须包含 `uv sync --extra postgres`，不要让用户看到
`No module named 'asyncpg'`。

**`expire_on_commit=False`。** 默认 `True` 会让 commit 之后访问 ORM 对象触发
隐式 IO。仓库把 row 转成 dict 返回之后就不该再碰 session；设成 False 是明确
「我们不走 identity map，返回的是快照」。

引擎是模块级单例。测试隔离靠 `close_engine()` + 重新 `init_engine`，和 M0 的
`reset_app_config` 同一类纪律：`conftest.py` autouse fixture 每个测试后清掉。

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

#### 两条路径就够，第三条留给以后

Greenfield 只需两种库状态：

| 库状态 | 动作 |
|---|---|
| 空（没有任何自己的表） | `create_all` + `alembic stamp head` |
| 已有 `alembic_version` 且 revision 在本地链上 | `alembic upgrade head` |
| 未知 revision / 空 version 表 / 多行 version | **拒绝启动** |

**未知 revision 必须 fail-closed。** 典型场景：运维用新二进制跑过一次，又切回旧分支。
旧代码看不见新 revision，如果「好心」地 `stamp` 回去，新列还在、代码却按旧 schema 读写，
数据会静默写错。正确反应是拒绝启动并告诉运维跑文档里的恢复步骤。

空库判定是「自己的表一张都没有」，不是「没有 `alembic_version`」：

```python
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

### 3.3 仓库层的三个约定

每个仓库（`ThreadMetaStore` / `RunStore` / …）都遵守同一套形状。

#### 工厂函数，调用方不看 backend

```python
def make_thread_meta_store(session_factory) -> ThreadMetaStore:
    if session_factory is None:
        return MemoryThreadMetaStore()
    return SqlThreadMetaStore(session_factory)
```

调用方（worker、router、客户端）永远拿 ABC。测试注入 mock 也走这个工厂。
**禁止在业务代码里 `if get_session_factory() is None`。** 那种分支会复制到每个调用点，
M9 加第三个后端时要改 N 处。

#### `user_id` 的三个取值，默认不是 `None`

```python
async def get(self, thread_id: str, *, user_id: str | None | _AutoSentinel = AUTO) -> dict | None:
    resolved = resolve_user_id(user_id, method_name="ThreadMetaStore.get")
    ...
```

| 传入 | 含义 |
|---|---|
| 省略（`AUTO`） | 从 ContextVar 取当前用户；没有用户上下文 → 失败（fail-closed） |
| 显式 `str` | 按这个用户过滤 |
| 显式 `None` | **全局查询**，仅迁移脚本 / 管理路径使用 |

M3 已经要求读方法带 `user_id` 且不给 `None` 默认值。M4 把默认值定成 `AUTO` 哨兵，
而不是去掉默认值——因为 90% 的调用点就是「用当前用户」，每次都显式传很吵。
关键是 **`None` 和「没传」必须是两个东西**：没传 = 当前用户，显式 `None` = 我知道自己在
做跨用户查询。把默认值设成 `None`，M9 时会有一半调用点在无声地全局扫。

`resolve_user_id(AUTO)` 在没有 ContextVar 时要抛，不要静默返回 `"default"`。
worker 后台 task 常常没有用户上下文，所以 **worker 写路径必须把 `record.user_id`
显式传进去**，不能靠 AUTO。

#### 返回 dict，不返回 ORM 对象

仓库方法返回普通 dict（时间戳已转 ISO、`metadata_json` 已改名为 `metadata`）。
理由：memory 实现没有 ORM 对象可返回；HTTP 层也不该看到 SQLAlchemy。
SQLite 读回的 datetime 是 naive 的（即便列声明了 `timezone=True`），
转 dict 时必须按 UTC 补 tzinfo，否则 JSON 响应里的时间会少一个 `Z`，前端按本地时区解析就偏了。

列名到接口名的对照写死在 `_row_to_dict` 里，不要让 HTTP 层自己 `pop`：

| ORM 列 | 仓库 dict / API |
|---|---|
| `metadata_json` | `metadata` |
| `kwargs_json` | `kwargs` |
| `event_metadata` | `metadata` |
| `created_at` / `updated_at` / `lease_expires_at` | ISO-8601，`Z` 结尾 |

### 3.4 `DatabaseConfig`：一条配置驱动两套连接

用户只配一个 `database.backend`。系统自己拆出 ORM 引擎和 checkpointer 两套连接——
它们**不能共用 SQLAlchemy 引擎**。LangGraph 的 saver 要自己的连接生命周期
（`setup()` / `setup` 期间的 DDL、per-checkpoint 事务），和 ORM 的
`async_sessionmaker` 叠在一起会互相抢事务。

```python
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

| 谁用 | SQLite | Postgres |
|---|---|---|
| ORM 引擎 | `sqlite+aiosqlite:////abs/path/app.db` | `postgresql+asyncpg://...` |
| checkpointer | 同一文件的裸路径（`AsyncSqliteSaver.from_conn_string`） | 用户写的 `postgresql://...`（libpq / psycopg，**去掉** `+asyncpg`） |
| alembic（同步） | `sqlite:////abs/path/app.db` | `postgresql+psycopg://...` |

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
`CREATE DATABASE "name"`，然后**用同一套 `connect_args` 重建引擎**再 bootstrap。
漏掉「同一套 connect_args」，重试会落到 `public`，配置的 schema 是空的。

### 3.5 Session 必须短命

```python
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

M4 的 ORM（不要加 `project_id`——那是 M9）：

```python
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
**worker 的 `try_start` 成功之后**才写 `running`（不是准入时）；收尾时成功回 `idle`，
失败写对应终态。

**`metadata_json` 要剥掉服务端保留键。** 客户端可以塞任意 metadata，但下面这些键
由服务端盖章，请求体里出现就丢掉：

```python
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

部分唯一索引的 `WHERE` 子句在 SQLite 和 Postgres 上语法不同，ORM 里要两个都写：

```python
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
async def create_thread_operation_atomic(self, record: dict, *, kind: str) -> bool:
    try:
        async with self._sf() as session:
            session.add(RunRow(**self._to_row_kwargs(record, kind)))
            await session.commit()
        return True
    except IntegrityError:
        return False    # 活跃冲突或幂等键冲突，上层看 False 再 get_by_idempotency_key
```

**禁止把 `IntegrityError` 抛出仓库。** SQLite 和 Postgres 的异常类型/消息都不同，
路由层不该 import 数据库方言。幂等命中 vs 活跃冲突的区分：`False` 之后
`get_by_idempotency_key`，命中就是 200 原 run，未命中就是 409 busy。

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
| `subagent` | 子代理生命周期（`subagent.start/step/end`），M8 写入；`list_messages` 不返回 |
| `context` | 注入块身份（`context:memory`），M10 写入 |
| `workspace` | 工作区变更（`workspace.changes`），M10 写入 |

**`category` 现在就要有，哪怕 M4 只用到两三个值。** 它是 `list_messages`
和 `list_events` 这两个查询的分区维度，后加意味着所有历史行都没有正确的值。

#### `event_type` / `category` 的权威在 catalog，不在字符串字面量

```python
# runtime/events/catalog.py
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

**`RunJournal` 不实现 `on_llm_new_token`。** token 走 SSE；账本只收完整消息。
把每个 token 写成一行会让 `run_events` 按输出长度线性膨胀，翻历史会慢到不可用。

M4 的 ORM：

```python
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

**`max_trace_content` 只切 `category=trace`。** 消息类事件不能截断——截断后前端
会渲染出半截 JSON，比丢失更糟。超限时把 content 截到上限，metadata 打
`content_truncated: true`。memory / jsonl 后端可以不实施这条，因为它们本来就不是
给生产 trace 用的。

`seq` 的分配是 `max(seq) + 1`，必须串行化。双层保护：应用层的 per-thread 锁负责
正常路径的性能（避免每次都撞唯一索引重试），数据库的唯一约束负责正确性兜底。

**`run_events.backend` 和 `database.backend` 不联动，而且启动时冻结。**
这两点都是刻意的：本地开发常常是 `database: sqlite` 但 `run_events: memory`
（不想让调试事件污染库）；而冻结是因为 store 实例在 lifespan 里构造一次，
热切后端会让已经持有旧引用的组件写进一个没人读的地方。

默认值建议 `memory`，并在文档里写清"要持久化历史必须显式设 `db`"——
让用户显式选择比猜一个默认值安全。

`database.backend=memory` 但 `run_events.backend=db` 时，没有 session factory，
工厂必须退回 `MemoryRunEventStore` 并打 warning，不要等到第一笔 `put` 才 500。

**`GATEWAY_WORKERS > 1` 必须 `run_events.backend=db`。** memory / jsonl 是进程本地的，
跨进程无法共享有序账本。这条门在 lifespan 里检查，配错就拒绝启动。M9 才真的多 worker，
但门现在就写——否则有人先开了 `GATEWAY_WORKERS=2` 做负载测试，账本会静默分叉。

jsonl 的 `list_messages()` 必须扫该 thread 下**所有 run 文件**才能按全局 seq 排；
`list_events()` 只读一个文件。文件 IO 走 `asyncio.to_thread`。这是 jsonl 作为调试后端
能接受、作为生产后端不能接受的原因。

### 5.3 `put` vs `put_batch`

**`put` 是低频路径，正常执行必须用 `put_batch`。**

每条事件一次 `put` 意味着每条都要拿一次 thread 锁 + 一次数据库往返。
M8 的深层子代理一步一个事件，能把 Postgres 的锁打满。
正确做法是在运行时缓冲（比如 20 条一批），在阶段边界和 `finally` 里 flush。

`put_if_absent` 给终态回执用：worker 崩溃后由别的路径补写一条，
重复调用不产生第二行。检查和写入必须跟该 thread 的普通 writer 串行化。
返回 `(record, created)`，调用方靠 `created` 判断是不是自己写的。

#### `RunJournal`：账本的唯一常规生产者

worker 不直接调 `put`。它挂一个 LangChain callback：

```python
class RunJournal(BaseCallbackHandler):
    def __init__(self, store, record, *, progress_flush_interval=5.0, batch_size=20):
        self._buf: list[dict] = []
        ...

    def on_chain_start/end/error  →  run.start / run.end / run.error
    def on_llm_end                →  llm.ai.response（完整消息，不是 token）
    def on_tool_end               →  llm.tool.result
    def on_llm_error              →  llm.error

    async def flush(self) -> None:
        if not self._buf:
            return
        batch, self._buf = self._buf, []
        await self._store.put_batch(batch)
```

缓冲在三个时刻 flush：满 `batch_size` 条、阶段边界（比如工具调用结束）、
worker `finally`。`finally` 那一次必须发生，哪怕前面的 flush 失败过——
漏掉的是用户刷新后看不见的最后几条消息。

token 进度走另一条管子：`update_run_progress`，节流 `progress_flush_interval`
秒，且只在 `status='running'` 的行上生效。**它不动 status，也不写 run_events。**
两条管子不要合成一次写入，失败模式完全不同（进度丢了只是侧栏数字停一下，
事件丢了是历史消息缺一段）。

#### `seq` 分配的双层保护

```python
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

**`_locks` 用 `weakref` 或定期清理。** 长驻进程里 thread_id 无界，
普通 dict 会无限增长。和 M2 的文件锁是同一类问题。

### 5.4 信封回读形状

三个后端 `put` / `put_batch` / `list_*` 返回的 dict 字段必须一致：

```python
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

```python
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

### 6.5 checkpointer 自己的连接池

```python
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

1. **不要把 ORM 的 `AsyncEngine` 传给 saver。** LangGraph Postgres 实现吃的是
   psycopg 连接池（同步协议 + libpq DSN），ORM 吃的是 asyncpg。方言都不同。
2. **`setup()` 在 yield 之前。** 第一次跑的库没有 checkpoint 表，第一次
   `astream` 会 500 `relation checkpoints does not exist`。`setup()` 是幂等的。
3. **SQLite 父目录创建走 `to_thread`。** 和 `init_engine` 同一条 blocking-IO 纪律。
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
# SQLite:  json_type(col, '$.key') / json_extract(col, '$.key')
# Postgres: json_typeof(col -> 'key') / col ->> 'key'
```

**key 要白名单校验 `^[A-Za-z0-9_\-]+$`。** 它会被插进 SQL 路径表达式，
任何更松的字符集都是注入面。value 只接受 `None / bool / int / float / str`，
int 还要在 signed int64 范围内——SQLite 绑定更大的整数会溢出，Postgres 在
`BIGINT` cast 时溢出。

`bool` 不能用 `isinstance(x, int)` 判断（`bool` 是 `int` 子类）。正确写法：

```python
if isinstance(expected, bool):
    return isinstance(actual, bool) and actual is expected
if isinstance(expected, int):
    return isinstance(actual, int) and not isinstance(actual, bool) and actual == expected
```

`JsonMatch` 是 SQLAlchemy `ColumnElement`，`inherit_cache = True`。缓存键必须包含
`value_type = type(value).__name__`——Python 里 `True == 1 == 1.0` 且 hash 相同，
不把类型编进 cache key，bool 谓词会被复用到 int 查询上。

编译结果（开发时对照这条，不要自己写 `json_extract`）：

| expected | SQLite | Postgres |
|---|---|---|
| 缺失 vs null | `json_type(col, '$.k') IS NOT NULL` 才继续比 | `col ? 'k'`（key exists）才继续比 |
| `None` | `json_type(...) = 'null'` | `json_typeof(col -> 'k') = 'null'` |
| `bool` | `json_type = 'true'/'false'`，**不要** `json_extract = 1` | `json_typeof = 'boolean' AND (col ->> 'k')::boolean` |
| `int` | `json_type IN ('integer')` + `CAST(... AS INTEGER)` | `json_typeof = 'number' AND (col ->> 'k') ~ '^-?[0-9]+$'` 再 `::BIGINT` |
| `float` | `json_type IN ('integer','real')` + `CAST AS REAL` | `json_typeof = 'number'` + `::DOUBLE PRECISION` |
| `str` | `json_type = 'text'` | `json_typeof = 'string'` |

Postgres 的 `json_typeof` 对 `1` 和 `1.0` 都返回 `'number'`，所以 int 过滤必须加
「看起来像整数」的正则，否则 `CAST('1.5' AS BIGINT)` 会炸整条查询。

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

### 8.1 作者工作流

```bash
cd backend && make migrate-rev MSG="add foo column to runs"
```

这条命令应该对着**从 migration 链重建的一次性 SQLite** 做 autogenerate，
而不是对着开发者的 `app.db`。对着活库 diff 会把 LangGraph 的表、扩展的表、
甚至开发者手工改过的列都卷进来。推荐做法：

1. 用当前 `versions/` 链在 tmpfs 上 `upgrade head` 建成一个干净库
2. 拿这个库的 schema 和 `Base.metadata` diff
3. 把生成的 revision 放进 `versions/`
4. 把裸 `op.add_column` / `op.drop_column` 换成 `_helpers.py` 的
   `safe_add_column` / `safe_drop_column`（已存在则 no-op）

`env.py` 对 SQLite 开 `render_as_batch=True`。SQLite 不能直接 `ALTER TABLE DROP COLUMN`
（旧版本），batch mode 用「建新表 + 拷数据 + 改名」绕过去。

### 8.2 三条规则

**加新表要同时改两处**：ORM 模型注册进 `models/__init__.py`
（否则 `autogenerate` 根本看不见它），以及生成一条 revision。
空库走 `create_all`，所以 ORM 模型是权威；revision 是给已有库升级用的。
**索引和约束两边都要有**（§3.2），这是同一条规则的推论。

**forward revision 要有恢复预案。** 数据库里 stamp 的版本比代码里的 revision 链更新时
（典型场景：切到旧分支），alembic 会直接报错且信息不友好。
这个场景在多人开发分支切换时每周都会遇到，预案要写进 README 而不是让每个人现查。
Greenfield 的预案可以很简单：备份 → 用新代码 `upgrade head`，不要手改 `alembic_version`。

**扩展表用前缀声明后一起排除。** 和 LangGraph 表同一个 `include_object` 过滤器。
前缀不能是任何宿主表名的前缀（`register_extension_table_prefix("run")` 会把
`runs` / `run_events` 一起藏掉）——注册时就要撞名检查。M4 还没有扩展系统，
但过滤器的接口现在就留：`EXTENSION_TABLE_PREFIXES: set[str] = set()`。

### 8.3 `include_object` 还要排除索引和约束

```python
def include_object(object_, name, type_, reflected, compare_to):
    if type_ == "table" and name in LANGGRAPH_OWNED_TABLES:
        return False
    parent = getattr(object_, "table", None)
    parent_name = getattr(parent, "name", None) if parent is not None else None
    if parent_name in LANGGRAPH_OWNED_TABLES:
        return False
    return True
```

只过滤 table 不够：checkpoint 表上的索引会被当成独立对象出现在 autogenerate 里，
生成一堆 `drop_index`。

`_env_filters.py` 要单独成模块，不要内联在 `env.py`。`env.py` 会在 import 时拉起
alembic 的整套机器，单测过滤器会变得很重。

### 8.4 `0001_baseline` 必须产出什么

baseline 的 `upgrade()` 用 `op.create_table` 建出与当前 ORM **逐列一致**的三张表，
包括：

- `threads_meta` 全部列 + `assistant_id` / `user_id` 索引
- `runs` 全部列 + `ix_runs_thread_status` + `ix_runs_lease` +
  `uq_runs_idempotency_key` + **带 WHERE 的** `uq_runs_thread_active`
- `run_events` 全部列 + `uq_events_thread_seq` + 两个复合索引

SQLite 部分唯一索引在 alembic 里要显式：

```python
op.create_index(
    "uq_runs_thread_active",
    "runs",
    ["thread_id"],
    unique=True,
    sqlite_where=sa.text("status IN ('pending', 'running')"),
    postgresql_where=sa.text("status IN ('pending', 'running')"),
)
```

`downgrade()` 可以 `drop_table` 三张。Greenfield 用不到 downgrade，但 alembic
要求每条 revision 成对，空着会让 `alembic check` 抱怨。

**`env.py` 必须 `import agentkit.persistence.models`。** 漏掉这行，autogenerate
看见的 metadata 是空的，会生成「删掉所有表」的 revision。`target_metadata = Base.metadata`。

### 8.5 extras 依赖（接到 M0 `pyproject.toml`）

```toml
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
新增任何一处 `finally` 里的写入，都必须挂在同一个判断下面：

```python
async def _finalize(...):
    if record.ownership_lost:
        return                              # 一条 return，下面无论加什么都不会跑
    await journal.flush()
    await run_store.update_run_completion(...)
    await thread_meta.update_display_name(...)
    await thread_meta.update_status(...)
```

不要写成「每个写入各自 if」。有人加第四处写入时一定会漏掉那个 if。

#### 关机顺序：先停写入，再关引擎

M3 §6.3 已经要求：abort inflight → 等最后一次 checkpoint 写完 → 关 checkpointer 连接池。
M4 在这后面再加一步：

```
1. 给 inflight task 设 abort、task.cancel()
2. 超时内等它们把 checkpoint + run_events flush + 终态写完
3. dispose checkpointer 连接（LangGraph 的）
4. `await close_engine()`                            ★ M4 新增，必须在 2 之后
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

**不要尝试 resume `astream`。** checkpoint 可能停在 superstep 中间，接着跑会重放
当时正要执行的工具调用。正确的用户体验是：刷新后看到一条错误，可以重新发送。
M9 多 worker 才用租约 + `claim_for_takeover` 做真正的接管；M4 把租约列建好但回收
路径不读它们。**`GATEWAY_WORKERS>1` 时禁止再走本函数**——它会把 peer 正在跑的
inflight 标成 error。替换路径见 [M9 §3.3](2026-09-16-m9-production-design.md)。

`update_run_completion` 的终态守卫在这里是友军：如果关机排空已经把某行写成
`success`，启动回收再写 `error` 会被拒——这正是想要的。

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

### 9.3 `RunRecord` 怎么和水合

M3 的 `RunRecord` 仍然是进程内活体：task、StreamBridge 订阅、`ownership_lost`
这种不能进 SQL 的字段。`RunStore` 只存可序列化的那一组（M3 §5.1 第一组）。

水合方向：

| 时机 | 方向 |
|---|---|
| `create_or_reject` | `RunRecord` → `RunStore.put` |
| 重启后 `GET /runs/{id}` | `RunStore.get` → 临时 dict，**不再造活体**（task 已经没了） |
| inflight 的心跳/进度 | 活体累加 → `update_run_progress` |
| `finally` | 活体终态 → `update_run_completion` |

**不要在启动时把 SQL 行水合成 `RunRecord` 再塞回 `RunManager`。** 没有 task 的
活体是半残的，任何 `await record.task` 都会挂死。重启后 inflight 行的唯一归宿
是 §9.1 的 error。

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

## 11. 测试清单

| 文件 | 覆盖 |
|---|---|
| `test_repositories.py` | **参数化跑 sqlite + postgres 两遍**，所有仓库 CRUD；`user_id=AUTO` 无 ContextVar 时 fail-closed；显式 `None` 才全局查 |
| `test_json_semantics.py` | 字段缺失 vs null；bool 不匹配 int（含 `True` 不匹配 `1`）；float 接受 `1` 和 `1.0`；非法 key 字符被拒；int64 溢出被拒 |
| `test_unique_active_run.py` | 部分唯一索引真的在**新建的空库**上生效（走 `create_all` 那条路）；终态行不占名额，第二条历史 run 能插入 |
| `test_run_event_seq.py` | `seq` per-thread 严格递增；并发 put 不产生重复；双向游标两边都升序；`put_batch` 分配连续 seq；`put_if_absent` 第二次返回 `created=False` |
| `test_run_journal.py` | 不满 batch_size 也在 flush() 时落盘；`on_llm_new_token` 不写账本；token 进度节流期间 `status` 不变 |
| `test_terminal_not_overwritten.py` | `update_run_completion` 拒绝覆盖另一个终态 |
| `test_restart_recovery.py` | 重启后历史会话完整；inflight run 被收成 error；`run_events.backend=memory` 时消息丢失是预期 |
| `test_alembic_autogenerate.py` | 对着当前 ORM 模型跑 `autogenerate`，**断言产生空 diff**（防止有人加了模型忘了 migration） |
| `test_langgraph_tables_excluded.py` | autogenerate 不会试图 drop checkpoint 表或其索引 |
| `test_bootstrap_empty.py` | 空库走 `create_all`+`stamp head` 后 `alembic_version` 等于 head，且 `uq_runs_thread_active` 存在 |
| `test_checkpoint_accessor.py` | 业务代码不直接 `get_tuple`（可 AST 扫）；谱系读取不按时间排序；空账本 + 非空 checkpoint 时 seed 只跑一次 |
| `test_engine_lifecycle.py` | `backend=memory` 时 `get_session_factory()` 是 `None`；`close_engine` 后单例为 `None`；SQLite 新连接带 WAL |
| `test_event_catalog_contract.py` | catalog 定义与 `contracts/run_event_stream_contract.json` 双向一致；超长 event_type / category 构造时炸 |
| `test_put_batch_atomic.py` | `put_batch` 中途失败整批不落；成功则 seq 连续且等长返回 |
| `test_jsonl_seq_restart.py` | jsonl 进程重启后第一次 put 的 seq = 旧 max+1，不从 1 重计 |
| `test_session_not_held.py` | AST 扫描 worker：`astream` 循环里没有 `async with.*session` |
| `test_startup_recovery.py` | 库里留一条 running 行，启动后变成 error，threads_meta.status 同步；已是 success 的行不被改成 error |
| `test_url_dialect.py` | `postgresql://` → ORM 是 `+asyncpg`、checkpointer DSN 没有 `+asyncpg`；`postgres://` 短别名同样处理 |
| `test_delete_thread_order.py` | 有活跃 run 时 409；checkpointer 删除失败时 threads_meta 行仍在 |
| `test_create_idempotent.py` | 同用户重复 `ThreadMetaStore.create` 返回已有行；不同用户抛 `ThreadOwnershipConflictError` |

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

| 字段 | 权威 | 谁写 | 何时写 | 能否删 |
|---|---|---|---|---|
| checkpoint `messages` | checkpoint | LangGraph saver | 每个 superstep | 否（除非删整条 thread） |
| checkpoint `title` | checkpoint | 标题中间件 / 图状态 | 首轮结束后 | 否 |
| 前端历史消息 | `run_events` `category=message` | RunJournal | 完整消息落成时 | M4 不删；删 thread 也留 |
| `runs.status` | `runs` | RunStore CAS / completion | 准入 pending；try_start running；finally 终态 | 行不删；非 run 操作收尾删占位行 |
| `runs.total_*_tokens` | `runs`（内存累加） | RunJournal → progress/completion | 节流 5s + 收尾 | 随行留 |
| `threads_meta.display_name` | **投影**，权威在 checkpoint.title | worker finally | 收尾抄过来 | 随 meta 行 |
| `threads_meta.status` | **投影**，权威在该 thread 最新 run | worker try_start / finally | running / 终态 | 随 meta 行 |
| `threads_meta.incarnation` | `threads_meta` | create | 建行时 `uuid4().hex` | 随 meta 行；重建必换新值 |
| `run_events.seq` | store | store `_next_seq` | 每次 put | 不改、不重编号 |
| StreamBridge 队列 | 易失 | worker | astream 期间 | 进程没了就没了 |
| `RunRecord.task` | 进程内活体 | RunManager | create_task | 重启即丢 |
| 工作区文件 | 磁盘 | 沙箱 / 上传 | M2 | 删 thread 时 rm -rf |

**跨面复制只允许这一条：** 空账本 + 非空 checkpoint → 一次性 seed 成 `message` 事件。
其它方向（账本回写 checkpoint、用事件反算 token、用 meta 当历史）都是 bug。

---

## 14. 刻意不做的事

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

## 15. 一句话总结

M4 的产出物不是"接了个数据库"，而是**一张明确的数据所有权地图**。

检验标准：随便指着一个字段问"它的权威在哪、谁写它、什么时候写、能不能删"，
应该都有一句话的确定答案。答不上来的字段，就是下一个数据一致性 bug 的位置。
