# M10 记忆、护栏、扩展、可观测性 详细设计

> 配套文档：[分阶段开发方案](2026-09-16-greenfield-agent-service-roadmap.md) ·
> [M0 地基](2026-09-16-m0-foundation-design.md) · [M1 最小 Agent](2026-09-16-m1-minimal-agent-design.md) ·
> [M2 工具与沙箱](2026-09-16-m2-tools-sandbox-design.md) · [M3 Gateway 与流式](2026-09-16-m3-gateway-streaming-design.md) ·
> [M4 持久化](2026-09-16-m4-persistence-design.md) · [M5 前端](2026-09-16-m5-frontend-design.md) ·
> [M6 中间件体系](2026-09-16-m6-middleware-design.md) · [M7 MCP 与技能](2026-09-16-m7-mcp-skills-design.md) ·
> [M8 子代理委托](2026-09-16-m8-subagents-design.md) · [M9 生产化](2026-09-16-m9-production-design.md)
>
> 本文细化 M10（3–4 周，四条线内部可并行）。沿用包名：框架层 `agentkit.*`，应用层 `app.*`。
> 读完应能按文件树、接口、陷阱、测试清单直接开工。

---

## 1. M10 是什么性质的一段

前面九个里程碑做的是**功能**：能对话、能用工具、能流式、能持久化、能扩展能力、能委派、能多用户。
M10 做的是**长期正确性**——让系统在跑了三个月、积累了几千条记忆、装了五个插件之后，
仍然是可信的、可解释的、可回滚的。

这四条线的共同特征：**演示时完全看不出差别，上线三个月后决定你能不能继续往前走。**

| 线 | 一句话 | 不确定时怎么做 |
|---|---|---|
| **10A 记忆** | 跨 thread 记住用户的事实 | 标签不全就不写 |
| **10B 护栏与审计** | 约束和记录 agent 做了什么 | 证据被截断就报"无法验证" |
| **10C 扩展插件** | 第三方能贡献中间件 / 路由 / 服务 | 分不清就拒绝那种贡献 |
| **10D 可观测与评测** | 看得见、可复现地度量 | 数据集没钉死就不跑 |

四条线**互不阻塞**，可以并行。但每条线都有**一个装了就很难改**的决定，
下面每节的第一小节就是那个决定。10C 的契约定晚了会让 10A/10B/10D 的扩展点返工，
所以契约周排在最前。

### 1.1 排期

| 周 | 内容 |
|---|---|
| W1 | **10C 扩展契约**（registry / IsolatedMiddleware / 锚点 / `plugins:`）+ **10D 追踪接线**（图根 callbacks、无 import 副作用） |
| W2 | 10A 抽取安全门 + 一条事实一个文件的存储契约 + `MemoryManager` ABC |
| W3 | 10A 检索 / 容量淘汰 / 注入 + **10B** 回执、护栏中间件、沙箱审计、工作区变更 |
| W4 | 10D 离线基准（淘汰策略）+ 四条线收口。陈旧审查 / 合并事实 **有余量再做** |

远程记忆后端、向量检索、重执行校验、插件热加载、Monocle 进程级 OTel，全部不在这四周里。

### 1.2 M10 新增 / 改动的文件

```
packages/extension-api/              ★  # 公共契约包，零框架依赖
└── src/agentkit_extension_api/      # 或 deerflow_extension_api 对位名
    ├── registry.py                  # install(registry, config)
    ├── placement.py                 # Placement 枚举
    └── isolation.py                 # 贡献者看到的投影类型，不是实现

agentkit/
├── config/
│   ├── memory_config.py             ★  # 只放宿主共享字段
│   ├── tracing_config.py            ★  # 从环境变量组装，不进 YAML 热重载
│   └── plugins 段                   # AppConfig extra + STARTUP_ONLY
├── agents/
│   ├── memory/
│   │   ├── manager.py               ★  # MemoryManager ABC + get_memory_manager
│   │   ├── tools.py                 ★  # search/add/update/delete
│   │   ├── summarization_hook.py    ★  # 压缩前 add_nowait；skip_memory_flush
│   │   ├── updater.py               ★  # 抽取 LLM + 三标签门
│   │   └── backends/local/          ★  # 唯一必做后端（对位 DeerMem）
│   │       ├── storage.py           # 一文件一事实 + 用户锁
│   │       ├── journal.py           # 崩溃恢复；与用户锁同目录
│   │       ├── eviction.py          # 唯一淘汰函数
│   │       ├── retrieval.py         # FTS5 或子串
│   │       └── extraction.py        # 提示词模板含分类字段
│   └── middlewares/
│       ├── memory_middleware.py     ★
│       ├── tool_receipt_middleware.py
│       ├── receipt_verification.py
│       ├── sandbox_audit_middleware.py
│       ├── guardrail_middleware.py  # 若 M6 W3 没做完
│       └── safety_finish_reason_middleware.py  # M6 槽；本阶段补测试与文档
├── guardrails/
│   ├── provider.py                  ★  # GuardrailRequest / Decision / Protocol
│   └── builtin.py                   ★  # AllowAll；可选静态 deny 表
├── workspace_changes/
│   ├── scanner.py / diff.py / types.py
│   └── recorder.py                  # worker 调：to_thread 扫，写 catalog 事件
├── extensions/
│   ├── loader.py                    ★  # 读 plugins:，确定性顺序 import
│   ├── stack.py                     ★  # 单一最终组装点
│   ├── ordering.py                  ★  # 锚点；首次调用才 import middlewares
│   ├── isolation.py                 ★  # IsolatedMiddleware
│   └── cli.py                       # W4 可选
├── tracing/
│   ├── factory.py                   ★  # build_tracing_callbacks；无全局副作用
│   └── metadata.py                  ★  # session/user/trace_name
└── runtime/events/catalog.py        # 加 context:memory、workspace.changes

scripts/benchmark/
└── memory_eviction/                 ★  # import 生产 eviction.py，不复制

backend/tests/
├── test_memory_extraction_gate.py
├── test_memory_storage_concurrency.py
├── test_memory_confirmation.py
├── test_memory_identity.py
├── test_memory_retrieval_degrade.py
├── test_receipt_verification.py
├── test_workspace_scan.py
├── test_extension_isolation.py
├── test_extension_ordering.py
├── test_extension_router.py
├── test_tracing_no_import_side_effect.py
└── test_bench_offline.py
```

记忆**不进 SQL**。它是按用户分桶的文件系统（M9 `{base}/users/{uid}/...`）。
不要把 facts 塞进 `run_events`——那是一次 run 的账本，记忆是跨 thread 的。

---

## 2. 10A 持久记忆（W2–W3）

### 2.1 一开始就要定的：抽取安全门

**记忆是整个系统里唯一一个"根据模型输出、不经用户确认就写入持久存储"的模块。**

设计重心不是检索质量，是**什么东西允许被自动写进去**。

抽取出来的每一条提议都必须带三个标签，**三者同时满足**才允许自动写入：

| 标签 | 允许自动写入 | 被挡掉的例子 |
|---|---|---|
| `scope` | `user` | `task`（这次任务的临时结论）、`project` |
| `durability` | `durable` | `ephemeral`（"我现在在开会"） |
| `authority` | `descriptive` | `normative`（"以后都要用中文回答"——这是指令不是事实） |

```python
AUTOMATIC_WRITE_GATE = frozenset({("user", "durable", "descriptive")})

def may_auto_write(proposal: dict) -> bool:
    key = (proposal.get("scope"), proposal.get("durability"), proposal.get("authority"))
    return key in AUTOMATIC_WRITE_GATE
```

三条纪律：

**缺标签就拒绝那一条，不要中断其它条。** 5 条里 1 条缺标签 → 写 4 条并 `rejected += 1`。

**矛盾删除要 fail closed。** 模型可以提议删掉与新事实矛盾的旧条，但：

- `task` / `project` 作用域的删除一律拒绝
- 成对的"删除 + 替换"里，替换项没过写入门则删除也不执行
- 删除提议必须带 `id`、`scope`、`reason`；缺一个就拒该条

**自定义提示词模板必须包含同样的分类字段。** 有人换了模板忘了字段，
抽取 fail closed（全部拒绝）而不是 fail open（全部放行）。
配一个进程内拒绝率计数器：窗口内拒绝率超过阈值打 WARNING。
否则这种情况会静默地让记忆停止增长，三个月后才发现。

摘要散文（共享 `memory.json` 里那一段）同样要求 `scope=user` 且 `authority=descriptive`。

### 2.2 `MemoryManager` 契约

宿主共享配置和后端私有配置必须切开，否则换后端时 YAML 里全是用不上的旋钮：

```python
class MemoryConfig(BaseModel):
    enabled: bool = True
    mode: Literal["middleware", "tool"] = "middleware"
    injection_enabled: bool = True
    shutdown_flush_timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    manager_class: str = "local"          # backends/<name>/ 或 dotted path
    backend_config: dict[str, Any] = Field(default_factory=dict)
```

`manager_class` 解析失败 **ValueError，不许静默换成另一个后端**——记忆是持久状态。
`get_memory_config()` 必须触发和 `get_app_config()` 同一套签名校验重载（`memory.*` 热重载），
否则构图时读到的仍是进程启动时的 `mode`。文件暂时坏了：保留 last-good，打 warning，
不要让进行中的一轮崩掉。`get_app_config()` 从未被调用过则返回内存单例，
避免单元测试因第一次碰记忆配置而偷偷读到磁盘。

契约类用 **pydantic `BaseModel`，不是裸 ABC**（pydantic v2 的 `ModelMetaclass`
派生自 `ABCMeta`，未实现的 `@abstractmethod` 仍在实例化时 `TypeError`）。
后端私有依赖（storage / llm / queue）用 `PrivateAttr`，不进校验 / 序列化。

```python
class MemoryManagerError(RuntimeError): ...
class MemoryReadError(MemoryManagerError): ...       # 读失败且配置要求不能降级
class MemoryConflictError(MemoryManagerError): ...   # Gateway → HTTP 409
class MemoryCorruptionError(MemoryManagerError): ... # Gateway → 稳定 500，不带路径

class MemoryCallbacks:
    def on_memory_llm_call(self, invoke_config, *, thread_id, user_id, trace_id, model_name) -> None: ...
    def on_memory_llm_result(self, invoke_config, *, prompt, response, error, duration_ms, model_name) -> None: ...

class MemoryManager(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    backend_config: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["middleware", "tool"] = "middleware"
    callbacks: MemoryCallbacks | None = None
    supports_search: ClassVar[bool] = False
    requires_passive_writes_in_tool_mode: ClassVar[bool] = False  # 本地后端 True

    @classmethod
    def read_failures_are_fatal_for_config(cls, backend_config) -> bool:
        # 只用内存配置，禁止 I/O。未知 fail-closed。
        ...

    # ── Tier 1：每个后端必须实现 ──
    def add(self, thread_id, messages, *, agent_name=None, user_id=None, trace_id=None) -> None: ...
    def get_context(self, user_id, *, agent_name=None, thread_id=None) -> str: ...

    # ── Tier 2：有默认 ──
    def add_nowait(self, ...) -> None:  # 默认转 add
    def search(self, query, top_k=5, *, user_id=None, agent_name=None, category=None) -> list[dict]:
        # category 过滤必须在 top_k 切片之前，否则被其它类目挤掉
    def get_memory(...) -> dict: ...
    def clear_memory(...) -> dict:
        # agent_name=None → 清该用户全部；具名则保留共享摘要
    def cancel_by_agent(self, agent_name=None, *, user_id=None) -> int: ...
    def import_memory(self, memory_data, ...) -> dict: ...
    def shutdown_flush(self, timeout: float) -> bool:  # 无缓冲默认 True
        # 超时返回 False，调用方 warning 后继续关；禁止抛把 lifespan 卡死

    # ── Tier 3：Gateway / 工具探测 ──
    def warm(self) -> bool | None:  # True 成功 / False 失败 / None 无需预热
    def warm_retrieval(self) -> bool | None: ...
    def reload(...) -> dict: ...
    def create_fact / update_fact / delete_fact(...) -> dict: ...
    def close(self) -> None: ...

    @classmethod
    def from_config(cls, backend_config, *, mode="middleware", callbacks=None) -> MemoryManager: ...
```

`model_validator(mode="after")` 钉死：`supports_search` 必须等于 `search()` 是否被覆盖。
`mode="tool"` 且没覆盖 `search()` → 实例化即 `ValueError`，不要等第一次 `memory_search`。

工厂 `get_memory_manager()`：`threading.Lock` 双检单例。扫描 `backends/<name>/`
找 `MANAGER_CLASS`，或把 dotted path `importlib.import_module`。
测试用 `reset_memory_manager()` 同时清实例和 backend 缓存。

M10 **只实现 `backends/local/`**（`MANAGER_CLASS = LocalMemoryManager`，
`supports_search = True`，`requires_passive_writes_in_tool_mode = True`）
和 `backends/noop/`（`add` 空操作，`get_context` 返回 `""`，`warm` 返回 `None`）。
远程适配器见 §10，不进这四周。

### 2.3 存储布局：一条事实一个文件

```
{base}/users/{storage_user_id}/memory.json
{base}/users/{storage_user_id}/agents/{agent_name}/facts/{sha256[:2]}/{fact_id}.md
{base}/users/{storage_user_id}/agents/{agent_name}/.metadata/     # 审计 sidecar，不改 canonical 时间戳
{base}/users/{storage_user_id}/agents/{agent_name}/.retrieval/    # 可重建索引
```

`agent_name` 缺省映射到保留名 `__default__`，禁止自定义 agent 用这个名字。
对外名字一律小写规范化。

**`memory.json` 绝不存事实，也不存事实索引。** 只存共享摘要、revision、时间戳。

为什么不用一个大 JSON：记忆是**高频小改**。加一条要重写整个文件，
并发下要么全局锁（慢）要么丢更新。一条事实一个 markdown（YAML front matter + 正文）
让定向写只碰选中的那几个文件。

front matter 最小集：

```yaml
id: fact_...
scope: user
durability: durable
authority: descriptive
confidence: 0.8
created_at: 2026-09-18T00:00:00Z
updated_at: 2026-09-18T00:00:00Z
revision: 3
confirmed_at: null          # 只由确定性门写
access_count: 0             # 只由 search 命中递增
```

并发契约：

- 写入：**用户级锁** + 共享 revision + 每条事实 revision + 恢复 journal
- **点操作**（改一条）只在所有原始事实的前置条件仍然成立时才能 rebase
- **快照操作**（全量重算）遇到清单冲突必须重新加载并重算
- 用**类型化**冲突异常（`FactRevisionConflict` / `ManifestConflict`），
  **不要靠匹配异常文本**
- POSIX 原子替换之后 **fsync 父目录**（否则崩溃可能丢掉目录项）
- Gateway 把冲突映射成 HTTP 409，把存储损坏映射成稳定的 500（不要把路径泄露出去）

`apply_changes()` 返回增量时必须 `complete: false`，
**绝不能把一份局部缓存标成完整的记忆文档**。
调用方只在响应契约确实需要完整文档时才 `reload()`。

弱锁 cache 不得长期留着不活跃的用户 scope。cache 校验用清单 metadata + 已持久化 revision。
进程外改了 markdown 必须 `reload()`。

绝对 `memory.storage_path` 视为显式退出 per-user 隔离（所有用户共享该路径）——
要在配置注释里写清楚，默认不要设绝对路径。遗留值若以 `.json` 结尾，那是旧的**文件**路径；
本阶段把它当**目录**会 `NotADirectoryError` 或把所有用户记忆孤儿化。加载时丢掉并 warning，
落到 `runtime_home` 默认根。

事实文件形状（实现就按这个写解析器，不要再发明字段名）：

```markdown
---
id: fact_a1b2c3d4e5f6
scope: user
durability: durable
authority: descriptive
category: preference
confidence: 0.82
created_at: 2026-09-18T08:00:00+00:00
updated_at: 2026-09-18T08:00:00+00:00
revision: 1
confirmed_at: null
access_count: 0
schema_version: 1
---

User prefers concise Chinese replies.
```

`id` 生成：`fact_` + sha256(canonical content + created_at)[:12]，冲突再拼 `revision`。
路径用 `sha256(id)[:2]` 做一级分片，避免单目录上万文件。

崩溃 journal（与用户锁同目录，不进事实树）：

```
{agent_dir}/.journal/{op_id}.json
# { "op": "point"|"snapshot", "fact_ids": [...], "expected_revisions": {...}, "payload": ... }
```

写路径：journal fsync → 改 markdown / 改 `memory.json` → fsync 父目录 → 删 journal。
启动时扫描残留 journal：payload 已落盘则删 journal；未落盘则丢弃 journal（等同没发生）。
不要 replay 到一半的 snapshot——那会把半份缓存当完整文档。

### 2.4 两种工作模式

| 模式 | 谁决定写 | 注入什么 |
|---|---|---|
| `middleware`（默认） | 框架按规则排队抽取 | 共享摘要 + 当前 agent 的事实 |
| `tool` | 模型调 `memory_search` / `memory_add` / `memory_update` / `memory_delete` | **只有**共享摘要 |

tool 模式故意不把事实注入提示词——它们藏在 `memory_search` 后面。
省 token，代价是模型可能想不起来去查。

**两种模式不是互斥的写入路径**：tool 模式仍然挂 `MemoryMiddleware` 做被动写入
（本地后端支持）。tool 的 CRUD **不走抽取安全门**——那是用户通过模型显式表达的意图，
和被动抽取的信任级别不同。写进 `tools.py` 和 `updater.py` 的模块 docstring。

`injection_enabled: false` 关掉整块 `<memory>`，抽取仍可在后台跑。

### 2.5 `MemoryMiddleware` 与压缩钩子

挂在 M6 `build_middlewares` 的 `# SLOT: Memory`：压缩之后、标题附近。
它只实现排队，不在 `after_model` 里同步打 LLM。

过滤规则（和标题/压缩共用 `is_real_user_message`，M6 §6.3）：

- 入队：真实 HumanMessage + 该轮最终可见 AIMessage
- 不入队：隐藏注入、tool、子代理步骤、`hide_from_ui`

**入队时捕获 `user_id`**（`resolve_runtime_user_id(runtime)`），
这样 debounce 定时器触发时 ContextVar 已经没了，身份还在。

`summarization_hook`：压缩即将 `RemoveMessage(REMOVE_ALL_MESSAGES)` 之前调
`add_nowait`，把要被扔掉的消息抢救进记忆。
**子代理路径 `skip_memory_flush=True`（M8 §12.1）**——子代理共享父 `thread_id`，
不跳过就会把内部轮次写进父用户记忆。

关机：`shutdown_flush` 预算 1–300 秒，必须能装进进程的 graceful 超时
（通道 + 调度 stop + 这一笔）。drain 跑在 daemon 线程上，超时则丢尾巴往下关——
和"没 flush"同一失败方向，范围仅限尾巴。先关 derived SQLite 检索连接，再关存储。

#### 2.5.1 debounce 队列（本地后端私有，不进 ABC）

```python
@dataclass
class PendingContext:
    thread_id: str
    user_id: str
    agent_name: str
    messages: list          # 入队时已过滤；debounce 触发时 ContextVar 已没了
    trace_id: str | None
    enqueued_at: float
    nowait: bool = False    # add_nowait 走优先

class UpdateQueue:
    # 键：(user_id, agent_name, thread_id)
    # 同键后到的 messages 覆盖（同一轮多条 Human 只抽一次）
    # daemon Timer(debounce_seconds) 触发 _process_queue
    # 已 pop 进 _inflight 的项 cancel_by_agent 不再动
```

`debounce_seconds` 默认 30。IM 连发三条会并成一次抽取。
`add_nowait` 把该项插到队列头并立刻 `Timer(0)`——压缩等不及 debounce。
工作线程同步调抽取 LLM（不能被 awaiting 侧取消打断），所以
`on_memory_llm_result` 的后端侧用 `except BaseException` 包 provider，
宿主 hook 包装停在 `except Exception`（观测路径不许吞 `SystemExit` / `KeyboardInterrupt`）。

#### 2.5.2 `skip_memory_flush` 接线（M8 留下、本阶段才致命）

```python
# agentkit/agents/middlewares/summarization_middleware.py
def create_summarization_middleware(..., skip_memory_flush: bool = False):
    if resolved_app_config.memory.enabled and not skip_memory_flush:
        hooks.append(memory_flush_hook)

# agentkit/agents/memory/summarization_hook.py
def memory_flush_hook(event: SummarizationEvent) -> None:
    if not get_memory_config().enabled or not event.thread_id:
        return
    user_id = resolve_runtime_user_id(event.runtime)   # 压缩这一刻身份还在
    get_memory_manager().add_nowait(
        event.thread_id,
        list(event.messages_to_summarize),
        agent_name=event.agent_name,
        user_id=user_id,
    )
```

子代理构建器传 `skip_memory_flush=True`（M8 §12.1）。测试：子代理 3 轮对话之后，
父 `{base}/users/{uid}/agents/__default__/facts/` 不得出现子代理内部 Human/AI。

### 2.6 容量淘汰必须可评测

事实会一直增长。**淘汰策略是这个模块里唯一一个改了之后没法靠直觉判断好坏的部分。**

- 所有路径（自动、手动、工具、导入）**共用** `backends/local/eviction.py`
  里的同一个函数。四个地方各写一遍，评测就失去意义
- 默认策略 `confidence`：按置信度 + 创建时间
- 可选 `hybrid-v1`：confidence + 确认新鲜度 + 访问热度；权重和必须 `1.0`
- **shadow 模式**：记录新旧策略分歧，仍然执行旧策略。W4 基准吃这份日志

`max_facts` 默认建议 200。超了才淘汰，不要每次写入都全量排序除非过水位。

### 2.7 "确认"只能由确定性逻辑产生

淘汰会用到"这条最近被确认过吗"。**确认信号绝不能由模型自己声明。**

模型输出只提供**绑定**（`factsToReinforce: ["fact_..."]`）。
真正的确认门是确定性的：例如"最近 6 条过滤后的消息里有一条人类消息"。
不要求模型把信号和事实对齐——对齐是模型可以作弊的通道。

否则模型可以通过反复声明"我确认了这条"让任意事实永久免疫淘汰。

同理，**访问热度只由 `search` 返回的事实递增**。
提示词注入和 `get_context()` 不递增。不然所有被注入的事实热度一起涨，信号没区分度。

审计 sidecar 写在 `.metadata/`，**必须在 canonical 持久化成功之后**才写。
用户删除 / clear 必须同时删掉对应 sidecar。sidecar 不得改 markdown 的 mtime / revision。

#### 2.7.1 抽取流水线（`updater.py`）

debounce 弹出一条 `PendingContext` 之后：

1. 再滤一遍：只要真实 Human + 最终可见 AI；空批次直接 return
2. 拼抽取 prompt（系统段含三个分类字段的 JSON schema；用户段是对话）
3. `create_chat_model(attach_tracing=True)` —— 图外调用，走默认
4. 调 `callbacks.on_memory_llm_call` 再 invoke；回来调 `on_memory_llm_result`
5. 解析 JSON。缺 `facts` 数组 → 整批拒绝 + 拒绝率计数
6. 对每条 proposal 跑 `may_auto_write`；过门的进 `to_write`
7. 处理 `removals[]`：缺 `id`/`scope`/`reason` 拒该条；`scope in {task, project}` 拒；
   带 `replacementFactIndex` 的，对应 proposal 没过门则删除也不执行
8. `factsToReinforce: ["fact_..."]` **只当绑定**。确认门：
   `any(is_real_user_message(m) for m in filtered[-6:])` 为真才写 `confirmed_at=now`
9. `eviction.py` 一次：若 `len(existing) + len(to_write) - len(to_delete) > max_facts`
10. `storage.apply_changes(writes, deletes, reinforces)` → `{complete: false, ...}`
11. **释放用户锁之后** 发索引更新

抽取模型 JSON 最小形状（字段名钉死，换模板也必须有）：

```json
{
  "facts": [
    {
      "content": "...",
      "scope": "user",
      "durability": "durable",
      "authority": "descriptive",
      "category": "preference",
      "confidence": 0.8
    }
  ],
  "removals": [
    {"id": "fact_...", "scope": "user", "reason": "contradicted", "replacementFactIndex": 0}
  ],
  "factsToReinforce": ["fact_..."],
  "summary": {"scope": "user", "authority": "descriptive", "text": "..."}
}
```

自定义 `prompt_dir` 启动时用正则扫模板：必须同时出现 `scope`、`durability`、`authority`。
缺一个 → 该进程抽取全部短接为拒绝（fail closed），并打 WARNING。
拒绝率：进程内滑动窗 50 次，超过 80% WARNING 一次（不要每次都打）。

### 2.8 检索索引是可重建的派生数据

默认 SQLite FTS5，数据放 `.retrieval/`。`retrieval_adapter: ""` 选子串降级。
中文分词（jieba）作为 extra，没有 extra 就整词/子串。

三条降级：

| 失败 | 行为 |
|---|---|
| 索引更新失败 | 该 scope 标脏；搜索改走 canonical 子串，直到重建成功 |
| 重建致命失败 | 保持惰性重试，记忆模块仍可用 |
| 索引库损坏 | 删掉、重建一次；再坏就停在子串 |

**存储要在释放持久锁之后才发送索引更新。** 锁内更新索引 = 索引 I/O 计入写锁。

畸形 fact 文件重建时 log + skip，不要让一条坏 markdown 毁掉整个 index。

Gateway 启动 `warm_retrieval()` **不阻塞** `/health/ready`。第一次 search 可以重建自己的 scope。
关机等预热最多 1 秒，剩余超时留给 canonical flush。

### 2.9 注入、哈希、token 计数

注入走 M6 `DynamicContextMiddleware` 已经留好的 `<memory>` 槽，
**隐藏 HumanMessage**（M6 §4.5），不要放进 SystemMessage。记忆正文是模型曾经产出的，
属于不可信通道。

每次 run 对生效的隐藏记忆块算一次 SHA-256，记一条 catalog 事件：

```python
MEMORY_CONTEXT_EVENT = RunEventDefinition("context:memory", "context")
# content: { "content_sha256": "...", "agent_name": "...", "chars": N }
# 完整文本留在 checkpoint 的动态上下文里，不进 run_events 行
```

事后能回答"那次回答时模型看到的是哪版记忆"。
Gateway 剥掉客户端伪造的动态上下文标记（M6 provenance + M9 身份剥离同一纪律）。
只有当前 `DynamicContextMiddleware` 的输出能建立 first-run 记忆身份；
checkpoint 复用要求这块在 run 开始前已经存在。

token 预算（`max_injection_tokens`）在 `prompt.py` / 注入路径计数：

- 默认 tiktoken，惰性加载并缓存
- 加载失败 → 字符估算，冷却 600 秒；并发加载期间其它调用方也走字符估算
- `memory.backend_config.token_counting: char` 让离网部署彻底不碰网络
- 注入超时（`asyncio.to_thread` + 5s，M6 已定）：跳过 memory、仍注入日期。
  读失败策略由后端 `read_failures_are_fatal_for_config()` 声明；未知 fail-closed。
  超时处理不得再往 executor 丢活。策略方法只用内存配置，不能在热路径读盘

保证类目（`guaranteed_categories`）有独立 token 预算，从普通注入预算里划出来，
避免"最近的低价值事实把工作偏好挤掉"。

### 2.10 身份

客户端身份键在构图前被 Gateway 删除（M9 §2.1）。
记忆、自定义 agent、用户技能、技能策略、提示词装配都走 `resolve_runtime_user_id`。
IM 内部认证可以代表 connection owner（只有 AuthMiddleware 能授权那对头）。
无认证模式用 `DEFAULT_USER_ID = "default"`。

`cancel_by_agent` 只取消该 user scope 里还在 debounce 队列中的项。
`user_id=None` 只选遗留无用户根。`agent_name=None` 选该用户下所有 agent 桶。
已经从队列取出正在 `_process_queue` 的项不中断。

### 2.11 本地后端配置（`backend_config`）

```yaml
memory:
  enabled: true
  mode: middleware                 # middleware | tool
  injection_enabled: true
  shutdown_flush_timeout_seconds: 30
  manager_class: local
  backend_config:
    debounce_seconds: 30
    max_facts: 200
    fact_confidence_threshold: 0.5
    max_injection_tokens: 2000
    token_counting: tiktoken       # tiktoken | char
    retrieval_adapter: fts5        # fts5 | "" （子串）
    eviction_policy: confidence    # confidence | hybrid-v1
    eviction_shadow: false
```

陈旧审查 / 合并事实的旋钮本阶段可以留在 schema 里但 `enabled: false`，实现推后。

顶层误把 DeerMem 私有字段（`storage_path`、`max_facts`、`debounce_seconds`、`model_name`…）
写在 `memory:` 而不是 `backend_config:` 时：**自动迁进 `backend_config` 并 WARNING**，
不要静默丢自定义值。未知顶层键 WARNING + 忽略。`model_name` 映射到
`backend_config.model.model`。

### 2.12 记忆工具（`mode: tool`）

四个 `@tool`，全部走 `MemoryManager`，身份用 `resolve_runtime_user_id(runtime)`，
**禁止**读 ContextVar 当权威。`create_fact` 等抛 `NotImplementedError` 时工具返回
JSON `{"error": "..."}` ，不要把异常冒成 Graph 失败。

| 工具名 | 调什么 | 过抽取门？ |
|---|---|---|
| `memory_search` | `manager.search`；命中加热度 | 否 |
| `memory_add` | `manager.create_fact` | **否**（显式意图） |
| `memory_update` | `manager.update_fact` | 否 |
| `memory_delete` | `manager.delete_fact` | 否 |

注册：`should_use_memory_tools(cfg)` 为真时，M2 工具目录把它四件加进 lead 工具表。
本地后端 `requires_passive_writes_in_tool_mode=True`，所以 tool 模式仍挂 MemoryMiddleware。

### 2.13 Gateway HTTP（`app/gateway/routers/memory.py`）

前缀 `/api`。身份 `_resolve_memory_user_id(request)`：
可信内部头（AuthMiddleware 验过）→ `make_safe_user_id(raw_owner)`，
否则 `get_effective_user_id()`。浏览器调用方永远不是 internal。

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/memory` | `get_memory`；不支持 → 501 |
| POST | `/memory/reload` | 进程外改了 markdown 之后强制重载 |
| DELETE | `/memory` | `clear_memory`；成功后 `cancel_by_agent` |
| POST | `/memory/facts` | `create_fact`；ValueError → 422 |
| PATCH | `/memory/facts/{fact_id}` | `update_fact`；冲突 409 |
| DELETE | `/memory/facts/{fact_id}` | `delete_fact` |
| GET | `/memory/export` | 走 `get_memory`，**不要**调死契约 `export_memory` |
| POST | `/memory/import` | `import_memory`，随后触发淘汰 |

`MemoryConflictError` → 409；`MemoryCorruptionError` → 500（稳定文案，不带路径）。
删除自定义 agent 时必须 `cancel_by_agent(name, user_id=...)`，否则 debounce 晚到会把已删 agent 复活。
所有存储 I/O `asyncio.to_thread`。

---

## 3. 10B 护栏与审计（W3）

### 3.1 一开始就要定的：证据的分层

| 层 | 做什么 | 能证明什么 |
|---|---|---|
| **提示词层** | 声称做了某事时附上回执引用 `[rN tool]` | 什么都不能证明，但让下一层非空转 |
| **确定性层** | 代码校验引用的回执存在、工具确实被调用过 | "这个工具确实被调用了" |
| **重执行层** | 重新跑一遍验证结论 | "结论是对的" |

**M10 只做前两层。** 第三层是独立产品线（M8 已把确定性验收清单列为不做）。

第二层的边界必须写进 `receipt_verification.py` 模块 docstring：
它证明的是**执行发生过**，不是**声称的结论正确**。
Makefile 吞掉失败、测试脚本失败也退出 0——第二层都看不出来。
半年后会有人以为回执校验等于结果正确，这条注释就是为了拦住他们。

M8 的 `task` 结果是 lead 可见的**自我报告**。本阶段把回执台账接到 lead 链上，
让 lead 在综合子代理结果时至少能核"引用的调用发生过"。
不要在 M10 把 M8 明确跳过的 `tests_passed` 文件存在性检查偷运回来。

### 3.2 工具回执台账

`ToolReceiptMiddleware` 挂在 M6 的
`# SLOT: ToolReceipt / Guardrail / SandboxAudit`——必须在 `ToolErrorHandling` **之外**
（更外），因为它要看到原始工具返回；`deerflow_tool_meta` 仍由更内层盖。

每次工具调用盖一条不可变回执到 `ToolMessage.additional_kwargs`：

```python
TOOL_RECEIPT_KEY = "deerflow_tool_receipt"
TOOL_RECEIPT_LEDGER_KEY = "deerflow_tool_receipt_ledger"
RECEIPT_ID_PREFIX = "r"
_MAX_RECEIPT_ID_DIGITS = 10          # 模型输出不可信，int() 之前先限位数
_HASH_LEN = 16
_RENDER_CHAR_BUDGET = 2000

CITATION_RE = re.compile(rf"\[{RECEIPT_ID_PREFIX}(\d+)(?:\s+([A-Za-z_][\w.-]*))?\]")

def receipt_id(position: int) -> str:      # 1-based: r1, r2, ...
    return f"{RECEIPT_ID_PREFIX}{position}"

def format_citation(rid: str, tool_name: str | None = None) -> str:
    return f"[{rid} {tool_name}]" if tool_name else f"[{rid}]"
```

**id 格式、ledger 校验、提示词示例、父侧 verifier 全部从这两个函数 + `CITATION_RE` 派生。**
手写一个示例格式，改了生成器之后提示词会静默地教模型用错格式。

```python
class ToolReceipt(TypedDict):
    id: str                 # 只由 extract_tool_receipts / 引用轮快照赋；盖章时还没有
    tool_call_id: str
    tool_name: str
    status: str             # success | error | partial_success，来自 deerflow_tool_meta
    args_sha256: str
    output_sha256: str      # 盖在 sanitizer / 截断改写 content 之前
    output_bytes: int
    created_at: str         # ISO-8601 UTC

def is_valid_receipt(receipt: object) -> bool:
    # 缺字段 / 错类型 / extra 当损坏：跳过，不当运行时盖的证据
    # output_bytes 必须是 int 且不是 bool（bool 是 int 子类）
```

中间件行为：

1. `wrap_tool_call`：调 handler → 在返回的 `ToolMessage.additional_kwargs` 写
   `make_tool_receipt(tool_call, message)`（**还没有 display id**）
2. 伪造已有回执：覆盖成刚算出的那份，不要信任模型塞进来的
3. `after_model`：对当前消息列表 `extract_tool_receipts` 赋 `r1..rN`，
   `render_tool_receipts_with_snapshot` 生成模型可见台账，
   把**实际渲染进去的那一截**写到本轮 `AIMessage.additional_kwargs[TOOL_RECEIPT_LEDGER_KEY]`

`output_sha256` 是**新鲜度戳**。压缩之后只有消毒过的 content 还在，不能拿它当可再校验的指纹。

**编号陷阱**：压缩会丢掉更早的 `ToolMessage`，按位置分配的 `rN` 会重排。
Layer 2 必须用 `extract_citing_turn_receipts(messages)`——从**最后一条带 ledger 的 AIMessage**
取快照，而不是终端再扫一遍 ToolMessage。子代理路径尤其如此（M8 `max_turns=150` 会压缩）。
快照 id 必须是连续 `r{first}..r{first+n-1}`，否则整份 ledger 当损坏返回 `None`（无法验证，不是通过）。

渲染超预算：从**最旧**的开始省略，保留最近的；省略后仍超 → 截断并把
`retained_receipts = []`（宁可不给 id，也不给错位 id）。台账正文永远带这句边界：

> Execution evidence only — receipts record that a call happened and its status; they do not validate claim correctness or task acceptance.

#### 3.2.1 父侧校验（`receipt_verification.py`，纯函数，无 IO / 无 LLM）

```python
VERDICT_SOURCE = "receipt_citations"
VERDICT_REQUIREMENT = "cited_ids_in_execution_record"

class ReceiptVerdict(TypedDict):
    source: str
    requirement: str
    citation_resolved: bool          # 禁止叫 satisfied / verified / passed
    cited: list[str]
    resolved: list[str]
    failed: list[CitationFailure]    # {id, reason}
    unknown: list[str]
    no_citation_claims: bool
```

`verify_receipt_citations(report_text, receipts)`：

| 情况 | 结果 |
|---|---|
| `[rN]` 不在 ledger | `unknown`，`citation_resolved=False` |
| 在但 `status != success` | `failed` |
| 锚点工具名对不上 | `failed`（`anchor mismatch`） |
| 引用了且全部对上 | `citation_resolved=True` |
| 零引用 + 无动作声称 | 空转通过，`render` 返回 `""` |
| 零引用 + 英文/CJK 动作动词或文件路径 | `no_citation_claims=True` → UNVERIFIED |
| 零引用 + ledger 非空 + 正文 ≥ 240 字 | 同样 UNVERIFIED（跨语言安全网） |

动作动词表和路径正则写在模块常量里，测试钉死中英各一条。
误报代价是多一行 UNVERIFIED，漏报会把假成功放过去——偏敏感。

`render_citation_verdict` 模型可见句永远带
`execution evidence only, does not validate claim correctness`。

`report_contract`（若给子代理加提示词段）的 citation 子句跟随
`verification.receipts_enabled`；关闭时提示词不提 `[rN]`，lead 也不跑 verifier。
不要在 M10 把 M8 明确跳过的 `tests_passed` 文件存在性检查偷运回来。

三条纪律：

- 证据有界，截断就降级为"无法验证"，**不要当成通过**
- 回执在终态化之前收割，和其它 payload 在同一把状态锁下提交
  （M8 `try_set_terminal` 已经要求这个形状；lead 侧 journal 同样）
- 引用词汇用 `citation_resolved` / `supported` 这种中性词，
  **不要**用 `satisfied`——那是 runtime `run.delivery` 硬门的词

### 3.3 `GuardrailMiddleware`

M9 的 `Principal` / `build_principal_from_context` 是身份来源。
护栏是**执行期**检查，不是 RBAC 引擎（M9 内置 AllowAll；本阶段仍可以是 AllowAll，
但中间件和 Protocol 要就位，M9 的 `authz.provider` 将来通过 adapter 插进来）。

```python
@dataclass
class GuardrailRequest:
    tool_name: str
    tool_input: dict
    agent_id: str | None = None
    thread_id: str | None = None
    is_subagent: bool = False
    user_id: str | None = None
    user_role: str | None = None
    run_id: str | None = None
    tool_call_id: str | None = None
    channel_user_id: str | None = None
    is_internal: bool = False
    authz_attributes: dict = field(default_factory=dict)

@dataclass
class GuardrailReason:
    code: str
    message: str = ""

@dataclass
class GuardrailDecision:
    allow: bool
    reasons: list[GuardrailReason] = field(default_factory=list)
    policy_id: str | None = None
    metadata: dict = field(default_factory=dict)

class GuardrailProvider(Protocol):
    name: str
    def evaluate(self, request: GuardrailRequest) -> GuardrailDecision: ...
    async def aevaluate(self, request: GuardrailRequest) -> GuardrailDecision: ...
```

内置 `AllowAllProvider`（`evaluate` 永远 `allow=True`）。加载走 M0 的 `resolve_variable()`，
和模型 / 工具 / 沙箱同一条 dotted path。YAML：

```yaml
guardrails:
  enabled: true
  fail_closed: true
  provider: agentkit.guardrails.builtin:AllowAllProvider
```

中间件 `wrap_tool_call`：deny → 错误 `ToolMessage`（`recoverable_by_model=True`，
盖 `deerflow_tool_meta`），不调 handler。provider 抛异常时 `fail_closed=True`（默认）拒，
`False` 放行并 warning。

**`GraphBubbleUp` 必须原样再抛**（M6 §2.4）。护栏包装器漏了这条，中断/澄清会变成普通工具错误。

审计事件：`middleware:guardrail`（M4 catalog 的 `MIDDLEWARE_EVENT_PATTERN`），
**不要**带工具参数、消息正文、工具结果。身份用 `is_subagent` + 可选 `agent_id`。

`release_policy_parameters()` 给扩展 descriptor 用。热路径只取 `policy_id` /
`policy_version` 两个字符串，不要每次排序 allow/deny 列表。

### 3.4 沙箱审计

`SandboxAuditMiddleware` 记录每一次沙箱工具调用：命令/路径、结果概要、时间。
这是事后取证的唯一来源——checkpoint 里的 ToolMessage 可能被压缩丢掉。

事件 `middleware:sandbox_audit`，content 有界。路径要经过 M2 的虚拟路径规范化后再记，
不要记宿主机绝对路径（会泄露用户分桶）。

同样必须在 ToolErrorHandling 之外，同样要放行 `GraphBubbleUp`。

### 3.5 工作区变更

回答"这次 run 改了哪些文件"。worker 在 run 前后各拍一张快照（`asyncio.to_thread`），
有变更才写：

```python
WORKSPACE_CHANGES_EVENT = RunEventDefinition("workspace.changes", "workspace")
```

根：thread 的 `workspace` + `outputs`。**uploads 故意不扫**（那是用户放进来的，不是 agent 产物）。

排除目录按**任意深度的目录名**匹配，不要只匹配根下那一层——
MCP 服务器可能从别的 cwd 创建相对路径的内部目录：

```python
EXCLUDED_DIR_NAMES = {
    ".git", ".hg", ".svn", ".cache", ".next", ".venv",
    "__pycache__", "build", "dist", "node_modules",
    BROWSER_FRAMES_DIRNAME,      # 与 browser 工具同一常量
    TOOL_RESULTS_DIRNAME,        # 与 ToolOutputBudget 的 storage_subdir 同一常量
    MCP_INTERNAL_DIRNAME,
}
```

自定义 `tool_output.storage_subdir` 通过 `extra_excluded_dir_names` 传入，
否则外置的大工具输出会变成"未投递的产物"把 run 打成 error（若你们接了 delivery 硬门）。

diff 有大小上限；超限、二进制、敏感路径（`.env`、`id_rsa`、`*.pem`）只报元数据 +
`DiffUnavailableReason`，不把内容写进事件。

```python
class DiffUnavailableReason(StrEnum):
    BINARY = "binary"
    TOO_LARGE = "too_large"
    SENSITIVE = "sensitive"
    DECODE_ERROR = "decode_error"
```

worker 接线（`runtime/runs/worker.py`，终态收据**之前**）：

```python
pre = await asyncio.to_thread(capture_workspace_snapshot, thread_id, user_id, extra_excluded)
# ... 跑图 ...
try:
    await record_workspace_changes(
        event_store, thread_id, run_id, pre,
        user_id=workspace_changes_user_id,
        extra_excluded_dir_names=workspace_excluded_dir_names,
    )
except Exception:
    logger.warning("Failed to record workspace changes for run %s", run_id, exc_info=True)
```

扫描失败**不得**把 run 打成 error。`ownership_lost` 跳过这条（M9：丢租约进程不再写账本）。
`list_events(category=workspace)` 给前端；`GET .../workspace-changes` 可返回 summary-only。
uploads 目录不在 `WorkspaceRoot` 里。

### 3.6 安全性终止 ≠ 普通终止

M6 W3 已为 `SafetyFinishReasonMiddleware` / `ModelLengthFinishReasonMiddleware` 留槽。
本阶段补齐并钉测试：

| provider 信号 | `stop_reason` | 用户看见 |
|---|---|---|
| `content_filter` / safety | `safety_capped` | 明确的安全终止说明，**不是**空回复 |
| `length` / `MAX_TOKENS` | `model_length_capped` | "没说完"，保留已有正文，不重解析像工具调用的信封 |

`after_model` 反序：Safety 必须**最后注册**才能最先跑，先剥掉 safety 终止的 `tool_calls`，
后面的 LoopDetection 才不会把它们算进循环。M6 §3.2 已写。

---

## 4. 10C 扩展插件系统（W1）

### 4.1 一开始就要定的：插件列表放在哪

**插件列表放 `config.yaml` 的顶层 `plugins:`，不放 API 可写的 `extensions_config.json`。**

这个列表会导致 **Python 入口点被 import**，也就是**代码执行**。
M7 的 JSON 是 Gateway API 可写的，放进去等于给了 API 调用方任意代码执行。

```yaml
plugins:
  - name: example
    use: agentkit_extension_example:install
    enabled: true
    required: false          # 托管安装默认 false
    config: { ... }          # 该扩展私有，enable/disable 必须保留
```

`plugins:` 是 **STARTUP_ONLY**。任何变更都需要重启 Gateway。
不要做热加载——Python 模块卸载不可靠，会留下半个旧版本的类对象。

`AppConfig` 允许 extra 顶层键。重写 `plugins:` 块时，块边界必须来自 YAML parser，
**不要**用"下一个像 key 的行"这种正则——邻居段可以叫 `my.key`、`2fa`、`$schema`，
认不出下一个 key 时会把邻居整棵子树吃掉。文件末尾块下面的 trailing comments 要保留。

重复的顶层 `plugins:` 键直接拒绝，不要对着其中一个块管理、Gateway 读另一个。

### 4.2 `required: true` 默认必须是 false

`required: true` 的插件加载失败 → Gateway **拒绝启动**。
坏 wheel、缺本地库、被删的源码快照，都会变成只能通过 shell 才能恢复的启动失败。

托管 `install` 写入的记录一律 `required: false`。
`required: true` 只能 `--required` 显式选择。接管手写记录时保留运维已经选的值。

可选插件加载失败：打带归因的诊断、fail open、继续启动。

加载顺序：YAML 列表顺序，确定性。同一个 `name` 出现两次 → 启动失败。

### 4.3 公共契约包不许 import 框架

```
packages/extension-api/     # PEP 621，不依赖 agentkit / fastapi / langchain
packages/harness/           # 框架；extensions/ 实现隔离包装
```

扩展自己声明 FastAPI / LangChain / LangGraph。契约包一旦依赖框架，
扩展作者就被锁死在框架版本区间里。

入口点组：`agentkit.extensions`（或对位 `deerflow.extensions`），
值为 `module:install`。`install(registry, config) -> None`。
契约包给每个 Protocol 方法**默认空实现**，给每个 dataclass 字段**默认值**——
以后加方法对已发布扩展保持 additive。

```python
# packages/extension-api/.../contracts.py
class ExtensionRegistry(Protocol):
    def middlewares(self, contributor: MiddlewareContributor) -> None: ...
    def task_lifecycle(self, contributor: TaskLifecycleContributor) -> None: ...
    def system_model_observer(self, observer: SystemModelCallObserver) -> None: ...
    def service(self, service: ExtensionService) -> None: ...
    def routers(self, routers: Sequence[Any]) -> None: ...  # Any：契约包不许依赖 FastAPI

ExtensionInstall = Callable[[ExtensionRegistry, Mapping[str, Any]], None]

class TaskOutcome(StrEnum):
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"

class SystemOperationKind(StrEnum):
    GOAL = "goal"
    MEMORY = "memory"
    TITLE = "title"
    SUMMARIZATION = "summarization"
```

`SystemModelRequest.messages` 在 `__post_init__` 里规范成 tuple：
goal / memory 传 list，title / summarization 传单字符串；`str` 已经是 `Sequence`，
不规范的话观察者迭代会走到字符。复制 list 才让"冻结快照"名副其实。

记忆抽取**不**直接 import 扩展 API（本地后端要能独立 vendor）：
走 `MemoryCallbacks.on_memory_llm_result`，宿主再翻译成 `SystemModelCallObserver`，
**submit 不 await**（debounce 工作线程上，取消 awaiting 侧打不断正在跑的线程）。

M10 实现五种贡献（路线图那五个）。另外两种（assembly observer、compaction observer）
有余量再加，加的时候契约包和宿主必须**同一 PR**：

| 种类 | 生命周期 | 失败 |
|---|---|---|
| 中间件 | 每张图组装 | IsolatedMiddleware fail open |
| 任务生命周期 `on_task_start/stop` | 每个 lead run / 带 parent 的子代理 | 有界预算，fail open |
| 系统模型观察者 | 图外 LLM（记忆抽取、标题、摘要） | fail open |
| Gateway 服务 | 进程 lifespan | start 失败按 required 决定 |
| FastAPI 路由 | 挂载后一直活 | 遮蔽则整份 router 拒 |

### 4.4 中间件贡献用语义锚点，不用列表下标

```python
class Placement(Enum):
    MODEL_LOGICAL = "model_logical"     # 清洗、预算：看见逻辑消息
    MODEL_PHYSICAL = "model_physical"   # 压缩之后、provider 之前
    TOOL_VISIBLE = "tool_visible"       # 工具前：护栏、回执、审计
    TOOL_RAW = "tool_raw"               # 最内：错误包装
    STANDARD = "standard"               # 普通 before_agent / after_model

registry.add_middleware(
    MyMiddleware(),
    scope="lead",          # lead | subagent | both
    placement=Placement.TOOL_VISIBLE,
    order=100,             # 同锚点内稳定序号
)
```

四条纪律：

**单一最终组装点 `extensions/stack.py`。** 注入发生在组装**最后一步**，
不能注在 `build_runtime_middlewares` 里——lead 构建器在那之后还会追加。
M6 的 `# SLOT: custom / extensions` 就是这个点，在 Loop/Token 之后、Safety 之前。

**依赖方向不能反。** `extensions/` 在 `agents/` 下面，模块作用域
**不许** `import agentkit.agents.middlewares`。锚点表、顺序约束表延迟到
`assert_ordering()` / compose **首次调用**时解析。
延迟的是**调用**，不要用惰性容器假装已解析——那种东西迭代时报一个答案、
`len()` 时报另一个。

**IsolatedMiddleware 包装，fail open。** 扩展失败发诊断、继续往下，
**不能重复**下游已经发生的模型/工具副作用（只调一次 `handler`，异常在之后）。
包装器镜像内层的所有生命周期钩子、tools、transformers、state schema。
descriptor 必须 unwrap 到 `.inner` 并记录 `.source`，否则所有贡献者的指纹塌成同一个类名。

**同步/异步成对。** LangChain 把每一对 sync/async 包装器当作**一个**能力，
只实现一边会收到直通的另一边。要观察两条路径就两边都实现。

### 4.5 fail-open 按失败的**来源**判断，不按异常基类

`CancelledError` 到达贡献者的 `except` 有两个完全无关的原因：

1. 宿主任务真的被取消了 → **必须传播**
2. 扩展自己用取消实现内部超时，泄漏出来 → **必须包住**

按基类 `except CancelledError: raise` 会让第 2 种跳过后续贡献者，
并且走进 worker 的延迟中断路径，把一次本来成功的 run 标成已取消。

正确做法：`asyncio.current_task().cancelling() > 0` 才传播。
没有 running loop（同步构图）视为宿主未取消。
`KeyboardInterrupt` / `SystemExit` 照常传播。

```python
def _host_is_cancelling() -> bool:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return False
    return task is not None and task.cancelling() > 0
```

`IsolatedMiddleware` 不是简单 `try/except`：

- 按 inner 实际覆盖的 hook 集缓存子类（LangChain 用**类级**身份检查发现能力）
- 只实现 sync 或 async 一边时，另一边补静默 passthrough，否则基类 `NotImplementedError` 发生在隔离之前
- `tracked_handler`：**只许调一次**，第二次 `RuntimeError`；没调也是 `RuntimeError`
- 扩展在 handler **之前**失败 → 诊断 + 调一次 handler
- 扩展在 handler **之后**失败 → 诊断 + 返回已捕获的结果（禁止再调）
- handler 自己失败 → 归图的错误策略，隔离层不吞
- `GraphBubbleUp` 原样再抛（和护栏同一条）
- `name` 规范化成 LangGraph 节点安全字符：`extension:{source}:{InnerClass}`
- `describe_middleware()` unwrap `.inner` 并记 `.source`；**duck-type 这两个属性**，
  不要从 `agents/` import `extensions/isolation.py`（反向依赖）

任务生命周期：lead worker 在 run 已启动后 await `on_task_start`，
在完成持久化之后、清 finalizing barrier / `publish_end` **之前** await `on_task_stop`。
子代理有 `parent run_id` 时同样包一对。outcome 只允许 `completed` / `aborted` / `failed`。
贡献者按注册顺序，同一有界预算。

系统模型观察：成功 / 失败 inline await；**取消**改 submit 到通知 loop（再 await 会被第二次 cancel 打断，观察者一次都跑不到）。没有注册 loop 的部署丢掉取消观察——和同步记忆桥同一失败方向。

扩展快照不可变：每个 run 解析一次，绑进 task store。
`task_tool` 从 runtime context 的宿主保留键读快照（类型检查——context 可被调用方合并），
**调用方塞的值从来不是权威**。缺省才 `get_loaded_extensions()`（嵌入客户端）。
M8 执行器已经要求把这份快照绑在构造期，不要在隔离循环里再读单例。
只在注册了中间件 / 生命周期 / 系统观察时才分配 `ExtensionData` task store；
服务和路由是 app 作用域，不分配。

### 4.6 贡献的 HTTP 路由

- **最后挂载，宿主永远赢**
- 能证明会被已有路由遮蔽 → 整份 router 原子拒绝；需要一般正则包含关系才能判断的一律放行，不要猜
- **强制会话认证，不能 opt out**。扩展只拿到投影：`user_id`、`is_admin`、`is_internal`、`role`，
  **拿不到**宿主 AuthContext / JWT
- **禁止** router lifespan / startup / shutdown 钩子。进程级资源走 Gateway 服务贡献
- **不接受 WebSocket 路由**。宿主还没法给贡献的 WS 做认证和 Origin 校验。
  宁可拒绝一种贡献，也不要接受一种宿主会静默忽略的注册方式

`create_app()` 顺序（钉测试，不要调换）：

1. 加载 `plugins:` → 不可变 registry 放 `app.state` + 进程单例
2. 加 `AuthMiddleware`
3. 把 `resolve_principal` 装到 `app.state[EXTENSION_PRINCIPAL_RESOLVER_KEY]`
   （扩展 `install()` 时 router 对象已存在，身份只能请求时再读）
4. 挂**宿主**路由
5. 挂贡献路由（可证明遮蔽 → 整份原子拒绝；失败不阻止后续扩展）
6. lifespan：持久化引擎就绪 → 按注册序 `service.start`；cleanup 先于
   store / checkpointer / engine teardown，反向停，每条独立超时

投影同步读 `request.state.user`（AuthMiddleware 盖的同一字段），不要走异步
exception-based accessor——贡献路由既有 sync 也有 async。
`get_request_route_path(request)` 必须委托 Starlette 自己的
`starlette._utils.get_route_path`（返回路由器正在匹配的那条路径，不是
"去掉 root_path"的本地仿写）。`starlette` 因此是契约包**之外**、Gateway 的直接依赖。

YAML 插件记录：

```python
class PluginRecord(BaseModel):
    name: str
    use: str                      # "module:install"
    enabled: bool = True
    required: bool = False
    package: str | None = None    # CLI 托管才有
    config: dict[str, Any] = Field(default_factory=dict)
```

### 4.7 安装事务（W4 可选；做了就必须完整）

CLI：`install SOURCE` / `list` / `enable NAME` / `disable NAME` / `remove NAME`。
W1 可以只支持手写 `plugins:` + `use: module:install`。一旦做 CLI，这三条不做会出事：

**跑包管理器之前先校验配置文件能写。** `uv add` 会执行构建后端（任意代码）。
一个永远写不进去的配置必须在那段代码跑起来**之前**失败。

**本地目录安装是快照，不是可编辑链接。** 复制到 `backend/extensions/sources/<dist>/`，
忽略 git 元数据、venv、字节码；拒绝符号链接、路径逃逸的包名、看起来像凭据的文件。
SSH Git / 本地 wheel / `file://` 拒绝（Docker 构建复现不了）。HTTPS Git 可以。

**安装后审计锁文件。** `UV_FIND_LINKS` 能把一个普通依赖变成本地 wheel。
生产镜像构建复现不了的本地引用 → 整笔事务回滚。
loopback URL 警告不回滚（操作员故意写的，但镜像里 `127.0.0.1` 是另一台机器）。

丢弃会重定向项目/解释器/TLS 的 UV 环境变量（`UV_PYTHON`、`UV_INSECURE_HOST`）。
index/proxy/cache/凭据可以留。

失败回滚 `pyproject.toml` + `uv.lock` 并再 sync；recovery sync 自己失败时仍报告**原始**错误。
取消则跳过 recovery sync（声明已恢复，下次 locked startup 会对齐）。
并发外部编辑检测到了就 raise，不要覆盖。

**这些检查防的是打包事故，不是恶意代码。** 构建钩子和扩展代码都以 Gateway 权限执行。
真正的边界是"只接受可信的运维来源"。CLI 要确认或 `--yes`。URL 里嵌凭据拒绝。

MVP **不做原地升级**：留下私有 config → remove 旧包 → install 新 pin → 恢复 config。

生产镜像启动 `uv run --no-sync`，**永不**在容器启动时从网络解析扩展。

---

## 5. 10D 可观测性与评测（W1 接线，W4 基准）

### 5.1 追踪回调挂在图的调用根上，不是模型上

M1 §3.6 已经定了。M10 落实：

- `create_chat_model(..., attach_tracing=False)` 是硬规则。图内所有调模型的位置登记在工厂 docstring
- `build_tracing_callbacks()` 返回当前启用的 CallbackHandler 列表，挂到
  `config["callbacks"]`：**Gateway `run_agent`、嵌入 `stream`、子代理 `_aexecute`**
- Langfuse 一类 provider 只在 `on_chain_start(parent_run_id=None)` 时把保留属性提升到根 trace。
  挂在模型层 = 属性永远不生效，一次 run 变成一堆互不相关的 trace

**图外模型调用是例外**：记忆抽取、标题、摘要、目标评估走
`create_chat_model(attach_tracing=True)` 的默认，因为它们不经过图。

子代理 trace 归到父 thread 会话下：

| 字段 | 来源 |
|---|---|
| `session_id` | 父 `thread_id` |
| `user_id` | `task` dispatch 时 `resolve_runtime_user_id` |
| `trace_name` | `subagent:<normalized-name>`（小写，`_` → `-`） |
| `deerflow_trace_id` | 与 Gateway 返回的 `X-Trace-Id` 相同，**不**被 tracing 开关关掉 |

调用方 `setdefault` 已有的 `session_id` 不被覆盖。
Langfuse 未启用时 `build_langfuse_trace_metadata()` 返回 `{}`，不影响只开 LangSmith 的部署。

配置**只从环境变量组装**，不进 YAML 热重载（换 tracer 等于换进程级客户端）：

| 变量 | 作用 |
|---|---|
| `LANGSMITH_TRACING`（兼 `LANGCHAIN_TRACING_V2`） | 开 LangSmith |
| `LANGSMITH_API_KEY` / `LANGCHAIN_API_KEY` | 开了但没 key → 启动 `ValueError` |
| `LANGFUSE_TRACING` | 开 Langfuse |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | 缺 key 同上 |
| `DEER_FLOW_ENV`（兼 `ENVIRONMENT`） | `langfuse_tags` 里的 `env:` |
| `MONOCLE_TRACING` | **默认不做**；接口留空函数 |

`get_tracing_config()` 线程锁缓存。`build_tracing_callbacks()` 对每个启用的 provider
失败要 `RuntimeError` 带归因，不要默默少一个 handler 让"以为在追"的部署空转。

`deerflow_trace_id` 始终写入，等于该次 Gateway 请求的 `X-Trace-Id`，
**不被 tracing 开关关掉**（排障用，不是产品分析用）。

### 5.2 进程级全局的 provider 只能从 lifespan 初始化

某些库会装进程级 `TracerProvider`、打补丁、自动 instrument。
这类一次性全局副作用**只能从 Gateway lifespan 调用**，
绝不能从 `build_tracing_callbacks()` 或 `agentkit.agents` 的 import 期触发。

测试钉死：

```python
def test_import_agents_does_not_start_tracing():
    # 在干净子进程里 import agentkit.agents，断言没有 TracerProvider / 没有 span 文件
    ...
```

代价：这种 provider 只覆盖 Gateway 路径。嵌入客户端和 CLI 要自己调 `setup_*_if_enabled()`。
M10 **默认不做**这类 provider（DeerFlow 的 Monocle）；接口留一个
`setup_process_telemetry_if_enabled()` 空实现，免得以后再从 `__init__.py` 往里塞。

### 5.3 离线基准的六条纪律

基准脚本 **import 生产函数**（`backends/local/eviction.py`），
**禁止复制一份平行实现**。复制的那一刻基准度量的就不是生产行为。

| 纪律 | 要求 |
|---|---|
| 数据集钉死 | 不可变 revision + SHA-256；路径由调用方提供；**评测命令不许静默下载** |
| 不提交敏感内容 | 不提交上游数据集文本、凭据、完整 provider 请求。清单只有稳定 ID 和 locator |
| 配置版本化 | 模型 ID、推理参数、提示词、重试、时钟、种子全部进评测配置 |
| 结果分级 | 公开结果：case ID、策略决策、评分、非机密元数据。问题/答案/记忆原文 gitignore |
| 确定性 | 固定时钟 + 确定性排序；结果记录 git revision 和配置哈希 |
| 离线套件独立 | 单元测试不许依赖网络、凭据、真实数据集。fixture 必须合成，并自我标注 |

最后一条最容易被违反：有人把 LongMemEval 的几条样本贴进测试文件，仓库就永远带着不该有的数据。

W4 必做的基准是**淘汰策略对比**（合成事实集，shadow 日志格式）。
LongMemEval 作为可选脚本，文档写明"自备数据路径"。

并发争用基准（多**进程**打同一用户记忆目录 / 同一张 SQL 表）是加分项：
worker 直连存储跳过迁移引导；READY/GO 屏障对齐后再计时。

---

## 6. catalog 补丁（M4 预告的 category）

```python
MEMORY_CONTEXT_EVENT   = RunEventDefinition("context:memory", "context")
WORKSPACE_CHANGES_EVENT = RunEventDefinition("workspace.changes", "workspace")
# middleware:{tag} 已有：guardrail / sandbox_audit / loop_detection / skill_activation
```

`context` 8 字符、`workspace` 9 字符，都在 category 16 上限内。
`list_messages` 不返回它们。`event_type` `context:memory` 14 字符，在 32 上限内。

---

## 7. 测试清单

| 文件 | 覆盖 |
|---|---|
| `test_memory_extraction_gate.py` | 三标签门；缺标签只拒该条；`task`/`project` 删除 fail closed；成对删除依赖替换项过门；缺分类字段的模板 → 全拒 + 拒绝率计数 |
| `test_memory_storage_concurrency.py` | 点操作 rebase 前置条件；快照冲突后重算；原子替换 sync 父目录；崩溃 journal 恢复；`complete: false` |
| `test_memory_confirmation.py` | **模型不能自己声明确认**；注入 / `get_context` 不加热度；只有 search 命中加热度 |
| `test_memory_identity.py` | 客户端伪造身份被剥；子代理 `skip_memory_flush` 不写父记忆；入队捕获的 user_id 越过 debounce |
| `test_memory_retrieval_degrade.py` | 索引失败 → 子串；损坏 → 删一次重建；畸形 markdown skip；更新在锁外 |
| `test_memory_token_counting.py` | tiktoken 失败走字符估算 + 冷却；`char` 模式零网络 |
| `test_receipt_verification.py` | 引用不存在的回执被拒；截断 → 无法验证而非通过；citation 格式单一来源；压缩后 rN 不对新 ledger 错位误判为通过 |
| `test_guardrail_middleware.py` | deny 变 ToolMessage；`GraphBubbleUp` 传播；provider 抛 + fail_closed |
| `test_workspace_scan.py` | 任意深度排除 `.git`/`node_modules`/`TOOL_RESULTS`；uploads 不扫；敏感路径无 diff 正文 |
| `test_extension_isolation.py` | 贡献者抛异常 fail open 且 handler 只调一次；**真实取消传播、内部 CancelledError 被包住** |
| `test_extension_ordering.py` | 语义锚点；组装在最后一步；AST 检查 `extensions/` 无模块级反向 import |
| `test_extension_router.py` | 宿主优先；可证明的遮蔽原子拒绝；认证不可 opt out；WS 贡献被拒 |
| `test_plugins_startup_only.py` | `plugins` 在 `STARTUP_ONLY_FIELDS`；API 写 JSON 不能改 plugins |
| `test_tracing_no_import_side_effect.py` | **import 框架不启动追踪** |
| `test_tracing_graph_root.py` | 图内 `attach_tracing=False`；子代理 metadata 带父 thread_id |
| `test_bench_offline.py` | 离线套件不碰网络/凭据/真实数据集；benchmark 模块 import 的是生产 eviction |

`test_memory_confirmation.py` 和 `test_extension_isolation.py` 的取消来源，
是这一阶段最值得写的两个——纯靠推理很难发现。

必须先写、形状如下（TDD，M0 已定）：

```python
def test_model_cannot_self_confirm(tmp_path):
    """factsToReinforce 只是绑定；没有 Human 消息就不得写 confirmed_at。"""
    storage = FileMemoryStorage(tmp_path)
    fact = storage.create_fact(user_id="u", content="likes tea", ...)
    updater.apply_llm_output(
        {"facts": [], "factsToReinforce": [fact.id], "removals": []},
        messages=[AIMessage("I confirm this.")],   # 没有 Human
        user_id="u",
    )
    reloaded = storage.get_fact(fact.id, user_id="u")
    assert reloaded.confirmed_at is None


def test_injection_does_not_increment_heat(tmp_path):
    mgr = LocalMemoryManager.from_config({"storage_path": str(tmp_path)})
    mgr.create_fact(user_id="u", content="x")
    before = mgr.get_memory(user_id="u")["facts"][0]["access_count"]
    mgr.get_context(user_id="u")
    after = mgr.get_memory(user_id="u")["facts"][0]["access_count"]
    assert after == before
    hits = mgr.search("x", top_k=1, user_id="u")
    assert hits[0]["access_count"] == before + 1


@pytest.mark.asyncio
async def test_isolated_middleware_contains_internal_cancel_propagates_host():
    class Inner(AgentMiddleware):
        async def awrap_tool_call(self, request, handler):
            raise asyncio.CancelledError()   # 扩展自己的超时
            # 注意：不要在 raise 之前调 handler
    wrapped = IsolatedMiddleware(Inner(), source="ex", on_error=lambda d: None)

    async def host():
        # 宿主任务没被取消
        with pytest.raises(RuntimeError, match="did not call the downstream"):
            # 或：隔离层调一次 handler 并返回——按 4.5 的 "handler 之前失败 → 调一次"
            await wrapped.awrap_tool_call(req, real_handler)

    await host()
    # 另测：cancel 宿主任务后 CancelledError 必须冒到 worker
```

`test_extension_ordering.py` 用 AST 扫 `agentkit/extensions/`：模块级
`import agentkit.agents.middlewares` 直接 fail。允许函数内延迟 import。

---

## 8. 验收清单

- [ ] 跨 thread：在 A 会话说的一个用户级、durable、descriptive 事实，在 B 会话被引用
- [ ] 一批抽取混入 `task` 作用域和 `normative` 条目，它们被拒绝，其它正常写入
- [ ] 换掉抽取模板但漏掉分类字段 → 抽取 fail closed，拒绝率 WARNING
- [ ] 两个线程（或进程）同时写同一用户记忆，没有丢更新；冲突是 409 不是静默后者赢
- [ ] 子代理跑完后，父用户记忆里没有子代理内部轮次
- [ ] lead 声称"写了 report.md"但引用了不存在的 `[r99]` → 模型可见 UNVERIFIED，而不是当成功
- [ ] 一次 run 改了 workspace 文件，`list_events(category=workspace)` 能看到；`node_modules` 里的变化没有
- [ ] 一个故意抛异常的扩展中间件，run 正常完成，诊断可见，下游工具只执行一次
- [ ] 贡献路由试图遮蔽 `/health` → 整份 router 被拒，其它扩展照常挂载
- [ ] 一次 run 在追踪后台呈现为**一棵**树，子代理归在同一 session（建议肉眼看一眼）
- [ ] `import agentkit.agents` 不创建 TracerProvider、不写 span 文件
- [ ] 淘汰 shadow 模式能输出新旧策略分歧（合成数据）

---

## 9. 与已有里程碑的接口对照

| 已有槽 | M10 填什么 |
|---|---|
| M0 `STARTUP_ONLY_FIELDS` | `plugins`（整段） |
| M1 图根 callbacks / `attach_tracing=False` | `build_tracing_callbacks` 真正挂上 |
| M1 `{soul}` 等提示词段 | 记忆不进静态系统提示词，进 DynamicContext `<memory>` |
| M2 虚拟路径 / 沙箱工具 | 审计记虚拟路径；workspace 扫描 workspace+outputs |
| M4 catalog `context` / `workspace` | `context:memory`、`workspace.changes` |
| M4 `middleware:{tag}` | `guardrail`、`sandbox_audit` |
| M6 `# SLOT: Memory` | `MemoryMiddleware` |
| M6 `# SLOT: ToolReceipt / Guardrail / SandboxAudit` | 三条中间件，均在 ToolErrorHandling 外 |
| M6 `# SLOT: custom / extensions` | `stack.compose` 最终注入 |
| M6 DynamicContext `<memory>` | 真正召回；哈希事件 |
| M6 SafetyFinishReason | 补测试；与回执/审计一起验收 |
| M7 `extensions_config.json` | **仍然不能**出现 `plugins` |
| M8 `skip_memory_flush=True` | 本阶段钩子真的会写盘，这条才变得致命 |
| M8 回执槽位空着 | lead 链开始盖回执；不实现 M8 跳过的确定性验收清单 |
| M9 `resolve_runtime_user_id` / Principal | 记忆分桶、GuardrailRequest 身份 |
| M9 通道出站白名单 | `<memory>` 泄漏仍然靠 9D；本阶段注入块带 provenance 让白名单可识别 |

---

## 10. 刻意不做的事

| 不做 | 理由 |
|---|---|
| 重执行式结论校验 | §3.1 第三层。独立产品线 |
| M8 的 `file exists` / `tests_passed` 验收清单 | 已证明是研究级工作量；回执层只证明调用发生过 |
| 插件热加载 / 原地升级 | 模块卸载不可靠；升级走 remove + install |
| 向量检索记忆 | 先 FTS5。向量引入嵌入模型、重建成本和一个新质量维度 |
| 远程记忆后端（mem0 / 托管服务） | 先把本地文件契约跑扎实 |
| 陈旧审查 / 事实合并 | schema 可留，实现推后；先把写入门和淘汰做硬 |
| Monocle 式进程级 OTel | 全局 TracerProvider 只能放 lifespan；默认不做，免得从 import 溜走 |
| CI 里跑上游 LongMemEval 原文 | 离线套件只用合成 fixture |
| 贡献 WebSocket | 宿主还没有认证/Origin 能力 |
| 把 `plugins` 放进 API 可写 JSON | 任意代码执行 |
| 记忆进 SQL / run_events | 跨 thread 数据，不是一次 run 的账本 |

最后一条通用原则：**宁可拒绝一种贡献，也不要接受一种宿主会静默忽略的注册方式。**
任何新增贡献种类，公共契约和宿主运行时必须在同一个改动里都实现。

---

## 11. 一句话总结

M10 四条线的共同主题是**让系统在长期运行后仍然可信**，
而每条线的核心都是同一种取舍：**在不确定的时候拒绝，而不是放行。**

- 记忆：标签不全就不写；模型不能给自己发确认
- 护栏：证据被截断就报"无法验证"；回执证明的是调用发生过，不是结论正确
- 扩展：列表不进 API 可写配置；分不清是不是真取消就按能证明的那条走
- 评测：数据集没钉死就不跑；`import agentkit.agents` 不许启动追踪

这四个 fail-closed 的选择，是这个阶段唯一真正需要坚持的东西。

---

## 12. 按周实施任务（对着抄）

每条都是"先写测试、再写最小实现"。文件路径相对 `backend/`。

### W1 上午：扩展契约包

**测试** `tests/test_extension_api_no_framework_import.py`：对
`packages/extension-api` 跑 importlinter / AST，断言不出现 `fastapi`、`langchain`、
`langgraph`、`agentkit`。

**实现**

- 新建 `packages/extension-api/` PEP 621，包名 `agentkit_extension_api`
- `contracts.py` / `placement.py` / `auth.py`（`resolve_principal` / `require_admin`）
- 根 `pyproject.toml` workspace members 加上这个包（M0 两个 member 变三个）
- `examples/agentkit-extension-example/`：`install(registry, config)` 注册一条
  `STANDARD` 中间件（`after_model` 记计数）、一条空 service、一条
  `GET /api/extension-example/ping`（强制会话认证）

### W1 下午：loader + IsolatedMiddleware + 图根 tracing

**测试**

- `test_plugins_startup_only.py`：`plugins` ∈ `STARTUP_ONLY_FIELDS`；PATCH config.yaml
  热重载后 `app.state` 里的 registry 不变
- `test_extension_isolation.py`：§7 那两条
- `test_tracing_no_import_side_effect.py`：子进程 `import agentkit.agents`

**实现**

- `AppConfig` extra + `PluginRecord`；`STARTUP_ONLY_FIELDS["plugins"]`
- `agentkit/extensions/{loader,registry,isolation,stack,ordering,notify}.py`
- `create_app()` 按 §4.6 顺序
- `build_tracing_callbacks()` + 三个挂点（worker / client / `_aexecute`）
- `create_chat_model(..., attach_tracing=False)` 图内全部调用点在工厂 docstring 列清单，
  测试用 AST 钉死没漏

### W2：抽取门 + 存储契约

**测试** `test_memory_extraction_gate.py` / `test_memory_storage_concurrency.py`
（§7 表）。并发测试至少：两个线程点操作同一事实不同 revision → 一个 冲突异常；
journal 在 markdown 写完、父目录 fsync 前 kill 的模拟（留下 journal → 启动恢复）。

**实现**

- `config/memory_config.py` 宿主字段
- `agents/memory/manager.py` + `backends/noop/` + `backends/local/{storage,journal}`
- `updater.py` 的 `may_auto_write` + 模板字段扫描（先不接真 LLM，测试塞 dict）

### W3 上午：检索 / 注入 / 中间件 / 工具 / HTTP

**测试** `test_memory_retrieval_degrade.py` / `test_memory_identity.py` /
`test_memory_token_counting.py`；Gateway 路由用 TestClient + 伪造 identity 头必须被剥。

**实现**

- `backends/local/{retrieval,eviction}.py`
- `memory_middleware.py` + `summarization_hook.py`
- `tools.py` 四件；`should_use_memory_tools` 接到 M2 目录
- `app/gateway/routers/memory.py`；lifespan `warm_retrieval` 不挡 `/health/ready`；
  shutdown：等预热 ≤1s → `shutdown_flush` → 关 SQLite → `close`

### W3 下午：10B 回执 / 护栏 / 审计 / 工作区

**测试** `test_receipt_verification.py`（压缩后错位不得判通过；截断 retained=[]）、
`test_tool_receipt_middleware.py`（伪造回执被覆盖）、
`test_guardrail_middleware.py`（`GraphBubbleUp`）、
`test_workspace_scan.py`（任意深度 `node_modules`；uploads 不扫）。

**实现** 按 §3；worker 在终态收据前 `record_workspace_changes`。
SafetyFinishReason 补 M6 留下的测试。

### W4：淘汰基准 + 收口

**测试** `test_bench_offline.py`：`scripts/benchmark/memory_eviction/` import 的
`eviction.py` 与生产是同一 `id()`；套件无网络、无真实数据集。

**实现** shadow 日志格式；可选 CLI 事务（§4.7，做了就必须完整）。
陈旧审查 / 合并事实保持 `enabled: false`。

W4 结束时跑 §8 验收清单。缺一条就还没完。

### 参考扩展最小 `install`

```python
# examples/agentkit-extension-example/src/agentkit_extension_example/__init__.py
from agentkit_extension_api import Placement

def install(registry, config):
    registry.middlewares(_CountingMwContributor())
    registry.service(_NoopService())
    registry.routers([_ping_router()])   # APIRouter；依赖 resolve_principal
```

---

## 13. 加第 N 种贡献 / 换记忆后端的清单

加贡献种类：

1. 契约包 Protocol + 默认空实现（additive）
2. 宿主 registry 方法 + loader 调用
3. 失败策略写进本文件（fail open 还是 required）
4. 同一 PR 的测试：静默忽略的注册路径必须不存在
5. 更新本文 §4.3 表

换记忆后端：

1. `backends/<name>/` 暴露 `MANAGER_CLASS`
2. 覆盖 `add` / `get_context`；要 tool 模式再覆盖 `search` 并 `supports_search=True`
3. 私有旋钮只进 `backend_config`，不要改 `MemoryConfig`
4. `read_failures_are_fatal_for_config` 只用内存
5. 不要让宿主 import 你的存储文件；走 `MemoryManager` 边界

---

*文档版本：M10 开发参考。实现以本文 + DeerFlow 对位源码为准；冲突时先改文档再改代码。*
