# M10 记忆、护栏、扩展、可观测性 详细设计

> 配套文档：[分阶段开发方案](2026-09-16-greenfield-agent-service-roadmap.md) ·
> [M0 地基](2026-09-16-m0-foundation-design.md) · [M1 最小 Agent](2026-09-16-m1-minimal-agent-design.md) ·
> [M2 工具与沙箱](2026-09-16-m2-tools-sandbox-design.md) · [M3 Gateway 与流式](2026-09-16-m3-gateway-streaming-design.md) ·
> [M4 持久化](2026-09-16-m4-persistence-design.md) · [M5 前端](2026-09-16-m5-frontend-design.md) ·
> [M6 中间件体系](2026-09-16-m6-middleware-design.md) · [M7 MCP 与技能](2026-09-16-m7-mcp-skills-design.md) ·
> [M8 子代理委托](2026-09-16-m8-subagents-design.md) · [M9 生产化](2026-09-16-m9-production-design.md) ·
> [路由层详细设计](2026-09-16-router-layer-design.md)
>
> 本文细化 M10（3–4 周，四条线内部可并行）。沿用包名：框架层 `agentkit.*`，应用层 `app.*`。
> 读完应能按文件树、接口、陷阱、测试清单直接开工。

---

## 0. 先用人话讲一遍

> 这一节是导览：**写代码的人可以只看后面的章节，评审 / 联调 / 写产品文档的人先看这里。**

### 0.1 M10 是四件事，不是一个功能

| 部分 | 一句话人话 | 谁受益 |
| --- | --- | --- |
| **10A 记忆** | 让服务"记得住你"：有界用户画像常驻，长尾事实按需检索 | 用户 |
| **10B 护栏与审计** | 工具调用前后留证据：谁在什么身份下调了什么、有没有被策略拦下 | 安全 / 运维 / 合规 |
| **10C 扩展插件** | 允许第三方以插件形式往服务里加中间件、工具、路由，而不用改主仓库 | 集成方 / 管理员 |
| **10D 可观测性** | 把"这一轮到底发生了什么"变成可追溯、可评测的数据 | 运维 / 研发 |

四件事共同的性质：**都不是新功能，而是给已有功能加"长期状态"和"边界"**。
所以 M10 做的判断大多是"这件事归谁管、失败时谁负责"，而不是新算法。

### 0.2 用户会看到什么

- **记忆**：用户上次说"回复别超过三行"，这次没再说，回答就短了；界面上能看、能改、
能删、能导出自己的画像和事实。**记住的内容绝不跨用户**。
- **护栏**：某个工具被策略拒绝时，会话**不会**崩，模型收到一条"这次调用被拒了"的
工具结果，然后换个做法；拒绝原因会写进审计，但**不写**工具参数和正文。
- **扩展**：管理员装了插件之后，可能出现新的工具、新的页面入口、新的中间件行为；
插件自己出错时，用户看到的仍然是正常回答（除非插件被标成必需）。
- **可观测性**：用户看不到；运维能看到完整调用链、每个 run 的账本与基准分数。

### 0.3 四条硬规则

1. **记忆只属于一个人。** 所有读写都带 `user_id`；`None`（全局查询）只允许迁移脚本和 CLI。
2. **"记住"不能由模型说了算。** 模型只能提议；写入门是确定性逻辑（谁说的、什么类型、多长）。
   画像只接受用户显式/持久的陈述；模型推断只进 facts。
3. **护栏拒绝不等于服务出错。** 拒绝要变成一条模型能理解、可恢复的工具结果，不能把整轮打崩。
4. **扩展失败不能拖垮服务。** 插件抛异常 → 发诊断、继续往下（fail-open）；

   只有显式标 `required: true` 的插件才允许让启动失败。

### 0.4 术语对照（后面看到时回来查）

| 文档里的说法 | 人话 |
| --- | --- |
| **抽取（extract）** | 从对话里用模型挑出"值得长期记住的句子"，分成画像候选和长尾事实 |
| **画像（profile）** | 有界的核心 schema，下一次对话开始时全量注入 |
| **长尾事实（facts）** | 画像装不下的偏好/上下文，存在记忆库里，用 `memory_search` 按需查 |
| **安全门（may_auto_write）** | 决定"这条内容能不能不问用户就直接写进去"的确定性规则 |
| **确认（reinforce）** | 用户又提到了同一件事 → 这条事实被"盖章"，不容易被淘汰 |
| **注入（inject）** | 下次对话开始前，把画像核心字段拼进上下文；facts 不默认注入 |
| **debounce 队列** | 攒一小段对话再抽取，而不是每句话都调一次模型 |
| **生命力（vitality）** | 借用遗忘曲线的可解释评分；分越低越该被归档 |
| **语义折叠（folding）** | 把同主题碎片事实合并成一条高密度事实，减少碎片 |
| **tombstone** | 用户删除后留下的"墓碑"，防止旧对话把内容重新抽回来 |
| **冲突（conflict）** | 同一字段/主题出现不同值；未消解保留旧值，候选不进入上下文 |
| **显式记住（explicit_remember）** | 用户要求长期记住的事实；免于生命力淘汰，但新指令可覆盖 |
| **回执（receipt）台账** | 工具说"我做完了"的证据记录，用来核对模型有没有说实话 |
| **护栏（guardrail）** | 工具调用前的策略检查：放行还是拒绝 |
| **贡献点 / 语义锚点** | 扩展往中间件链的**哪个位置**插东西；用名字（"工具可见层"）而不是数组下标 |
| **fail-open / fail-closed** | 出错时是"放过去"还是"拦住" |
| **基准（benchmark）** | 用固定题库量生命力/折叠策略好坏的那套离线评测 |

### 0.5 常见疑问

**Q：为什么模型说"我记住了"不算数？**
A：因为那样模型就能靠反复声明让任意内容永久生效（甚至绕过淘汰）。**只有确定性逻辑
能把一条事实标记为"已确认"**，模型只能提议。

**Q：为什么记忆删了，模型好像还记得？**
A：两件事分开：**存的画像/事实**删了就没了；但**当前这轮对话的历史**里还有它——模型看到的是
对话本身。要让模型"忘掉"，要么开新对话，要么依赖压缩把它挤出上下文。

**Q：画像和 facts 怎么分？**
A：画像只放稳定、跨任务高频、可枚举的字段（称呼、语言、语气、时区、角色等）；
装不下的长尾偏好、项目细节、上下文全部进 facts。画像是常驻的，facts 是按需检索的。

**Q：画像和 facts 冲突怎么办？**
A：画像字段是 canonical；未消解冲突保留旧画像/最高权威 fact，候选进入 `conflicted`，
前端提示用户确认；**不会把两条矛盾正文同时塞进模型上下文**。

**Q：护栏拒绝时为什么不直接报错？**
A：报错会把整轮对话带崩，用户只看到"出错了"。正确形态是：给模型一条**可恢复**的工具结果，
让它换路子完成用户的目标；同时把拒绝写进审计。

**Q：插件挂了为什么服务还正常？**
A：扩展默认 fail-open——"加能力"的东西不该让"已有能力"不可用。要"不装就不能启动"，
就在插件清单里写 `required: true`（**默认必须是 false**）。

## 1. M10 是什么性质的一段

前面九个里程碑做的是**功能**：能对话、能用工具、能流式、能持久化、能扩展能力、能委派、能多用户。
M10 做的是**长期正确性**——让系统在跑了三个月、积累了几千条记忆、装了五个插件之后，
仍然是可信的、可解释的、可回滚的。

这四条线的共同特征：**演示时完全看不出差别，上线三个月后决定你能不能继续往前走。**

| 线 | 一句话 | 不确定时怎么做 |
|---|---|---|
| **10A 记忆** | 跨 thread 记住用户：画像常驻 + 长尾事实按需检索 | 推断不进画像，冲突保留旧值 |
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
| W2 | 10A 分层契约（`MemoryBackend` + services + `MemoryManager`）+ 画像 core schema + LangMem backend + 画像注入 |
| W3 | 10A semantic 抽取 / `memory_search(kinds)` / 冲突消解 / HTTP + **10B** 回执、护栏中间件、沙箱审计、工作区变更 |
| W4 | 10A 定时维护（生命力淘汰 + 语义折叠）+ 10D 离线基准 + 四条线收口 |

episodic / procedural 记忆、插件热加载、Monocle 进程级 OTel，不在这四周里。

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
│   ├── pii_config.py                ★  # PII 边界 / AI 输出开关
│   ├── tracing_config.py            ★  # 从环境变量组装，不进 YAML 热重载
│   └── plugins 段                   # AppConfig extra + STARTUP_ONLY
├── privacy/
│   ├── __init__.py
│   └── redaction.py                 ★  # PIIVault / PIIRedactor / memory view
├── agents/
│   ├── memory/
│   │   ├── manager.py               ★  # MemoryManager 组合根 + get_memory_manager
│   │   ├── backend.py               ★  # MemoryBackend ABC（唯一存储扩展点）
│   │   ├── types.py                 ★  # MemoryRecord / MemoryQuery / MemoryKindSpec
│   │   ├── services/
│   │   │   ├── profile.py           ★  # ProfileService：schema 校验、CAS、tombstone
│   │   │   ├── semantic.py          ★  # SemanticMemoryService：当前 facts
│   │   │   ├── episodic.py          # 预留接口 + 空实现
│   │   │   └── procedural.py        # 预留接口 + 空实现
│   │   ├── ingestors/
│   │   │   ├── base.py              ★  # MemoryIngestor / MemoryProposal
│   │   │   ├── langmem_semantic.py  ★  # LangMem 只做 semantic 抽取
│   │   │   ├── episodic.py          # 后续：复用 summarization hook
│   │   │   └── procedural.py        # 后续：显式/任务后候选 + 审批
│   │   ├── resolver.py              ★  # 按 kind 的 ConflictStrategy
│   │   ├── context.py               ★  # ProfileContextBuilder：唯一 prompt 格式化点
│   │   ├── tools.py                 ★  # 只读 memory_search(kinds)
│   │   ├── summarization_hook.py    ★  # 压缩前 add_nowait；skip_memory_flush
│   │   ├── maintenance/
│   │   │   ├── service.py           ★  # 定时调度、per-user 锁、统计
│   │   │   ├── policies.py          ★  # 按 kind 的淘汰/TTL/版本策略
│   │   │   ├── vitality.py          ★  # Ebbinghaus-inspired 生命力
│   │   │   └── folding.py           ★  # semantic 高密度折叠
│   │   └── backends/
│   │       ├── langmem/             ★  # 主路径：通用 MemoryBackend + BaseStore
│   │       │   ├── backend.py        # MemoryBackend 实现
│   │       │   ├── ingestor.py       # LangMem semantic 抽取
│   │       │   ├── store.py          # namespace / profile record / records
│   │       │   └── config.py
│   │       └── noop/
│   └── middlewares/
│       ├── memory_middleware.py     ★
│       ├── pii_middleware.py        ★  # PII input/output + all-tool handle restore
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

app/gateway/routers/memory.py         # prefix=/memory，§2.12
# MemoryMaintenanceService 在 app lifespan 启动/停止（§2.9）
# goal 三条写在已有的 app/gateway/routers/threads.py，不新建文件

scripts/benchmark/
└── memory_maintenance/              ★  # import 生产 vitality.py / folding.py，不复制

backend/tests/
├── test_memory_business_rules.py
├── test_memory_backend_contract.py
├── test_memory_kind_registry.py
├── test_memory_type_services.py
├── test_memory_manager_contract.py
├── test_memory_profile_schema.py
├── test_memory_profile_injection.py
├── test_memory_extraction_gate.py
├── test_memory_conflict_resolution.py
├── test_memory_instructions.py
├── test_memory_maintenance.py
├── test_memory_folding.py
├── test_memory_retrieval_filters.py
├── test_memory_identity.py
├── test_memory_routes.py
├── test_memory_token_counting.py
├── test_memory_concurrency.py
├── test_pii_privacy.py               ★  # redaction / handle / tool restore / memory view
├── test_receipt_verification.py
├── test_workspace_scan.py
├── test_extension_isolation.py
├── test_extension_ordering.py
├── test_extension_router.py
├── test_tracing_no_import_side_effect.py
└── test_bench_offline.py
```

记忆**不进 `run_events`**。它按 user 存进 LangGraph `BaseStore`（sqlite/postgres），
profile 和 records 用独立 namespace；不要把记忆记录塞进一次 run 的账本。

---


## 2. 10A 持久记忆（W2–W4）

> 本版把记忆收敛为两层：**用户画像**（有界 core schema，常驻注入）+ **长尾事实**（facts，memory-as-a-tool）。
> 旧的“一条事实一个 markdown 文件 + DeerMem”实现不再作为主路径；LangMem 是第一个后端适配，持久化走 LangGraph `BaseStore`。
> 画像、semantic facts、冲突消解、定时维护共同构成分层契约；换后端不改变宿主语义。

### 2.0 这一版定下的决策（先看这张表）

| 决策 | 定稿 |
|---|---|
| **接口** | `MemoryManager` 是组合根；`MemoryBackend` 是通用存储扩展点；`MemoryTypeService` 是类型扩展点。不返回拼好的 prompt，不暴露 LangMem 类型 |
| **第一个后端** | `LangMemBackend` 实现 `MemoryBackend`；`LangMemSemanticIngestor` 负责 semantic facts 抽取；持久化用 LangGraph `BaseStore`（sqlite/postgres） |
| **画像** | 有界 core schema；`before_agent` 全量注入已填充字段；只有显式/确认值可常驻 |
| **长尾偏好** | 存 facts；`memory_search` 按需检索；不默认注入 |
| **推断内容** | **只进 facts，永不直接写画像** |
| **scope** | user 级；`agent_name` 只作为来源元数据，不作为隔离桶 |
| **检索** | 先元数据过滤（status/validity/category/agent），再向量检索；画像不走向量 |
| **画像自动更新** | 仅来自用户**显式、持久**陈述；前端可手动更新 |
| **未消解冲突** | 画像保留旧值；facts 保留最高权威；冲突候选不进入模型上下文 |
| **显式记住** | `explicit_remember` 免于生命力淘汰，但可被新的用户指令覆盖 |
| **维护** | 定时任务；超过水位后按 Ebbinghaus-inspired 生命力评分归档；随后做高密度语义折叠 |
| **删除** | 默认软删除/归档，30 天可恢复；用户显式 forget 才走删除语义 |

**这不等于四个并列模块。** 代码上是 `MemoryBackend`（存储）+ profile/semantic/episodic/procedural services（类型）+ `MemoryManager`（组合）+ resolver/maintenance（横切能力）；它们共享一套 user 级 namespace。

### 2.1 记忆的业务规则

| 规则 | 具体 | 用户看到什么 |
|---|---|---|
| **只属于一个人** | 所有读写都要求 `user_id`；不接受 `None` 做全局查询；跨用户读返回“没有” | 别人的记忆永远不会出现在我的对话里 |
| **画像有界** | 只放稳定、跨任务高频、可枚举、可安全展示的字段；不设自由文本 notes | 画像不会越用越长 |
| **推断不画像** | 模型推断只写 facts；画像只接受 `user_explicit_durable` / `user_manual` / `user_confirmed` | 不会被模型猜出来的偏好带偏 |
| **长尾进 facts** | 无法映射到 core schema 的偏好、项目细节、上下文，全部进 facts | 需要时能查到，平时不占上下文 |
| **冲突不双写** | 画像和 semantic facts 不能同时成为同一个 core field 的可检索真相；未消解冲突只保留旧值/最高权威 | 模型不会同时看到两条互相矛盾的内容 |
| **删除可恢复** | 淘汰走软删除/归档；用户显式 forget 才删除；tombstone 防止旧对话复活 | 忘掉的东西不会莫名回来 |

三条“什么时候不该记”继续保留：

1. **一次性临时信息**（“这次用 3 行”）——不写长期记忆；
2. **凭据与隐私**（token、密码、身份证号）——安全门直接拒，且不进审计正文；
3. **模型自己的推测**（“他大概喜欢 X”）——只进 facts，且 `source=inferred`，永不直接写画像。

#### 删除语义

| 操作 | 语义 |
|---|---|
| 删一条 fact | 软删除/归档，移出索引和检索；`explicit_remember` 需要用户显式 forget 或新的覆盖指令 |
| 删画像字段 | 写 tombstone；之后只能被 `cleared_at` 之后的真实用户消息重新写入 |
| 清空某个用户 | 删除 profile + 所有 records；保留审计/墓碑到回收期结束 |
| 删除整个账号 | 走 M9 账号删除回收期；期间可导出 |


### 2.2 MemoryManager 分层契约

#### 2.2.0 问题：现在的接口是“万能接口”

当前契约把三件事混在了一个类里：

1. **存储适配**：CRUD、检索、CAS、连接；
2. **类型语义**：画像 schema、facts 去重、冲突、版本；
3. **宿主编排**：注入、抽取、维护、导出。

后果是每加一种记忆（semantic / episodic / procedural）都要往同一个接口加方法，
每个后端都被迫实现自己不需要的能力；这违反接口隔离，也把未来类型锁死在当前实现里。

**修正：拆成三层，扩展点分别放在正确的位置。**

| 层 | 职责 | 变化频率 |
|---|---|---|
| **`MemoryBackend`** | 通用记录存储 / 检索 / CAS / 租户隔离 | 换存储时变 |
| **`MemoryTypeService`** | 每种记忆的 schema、写入策略、检索默认值、冲突规则 | 加记忆类型时变 |
| **`MemoryManager`** | 组合根 / facade：注册 service、路由 query、编排 ingest/maintenance | 稳定 |

一句话：**不要把 `MemoryManager` 做成后端基类；它是组合根。**
后端实现 `MemoryBackend`，类型实现 `MemoryTypeService`，两者互不 import。

#### 2.2.1 Memory kind 与注册表

`kind` 用字符串常量而不是封闭 enum：已知类型有常量，未来类型可以由扩展注册，不需要改核心枚举。

```python
class MemoryKind:
    PROFILE = "profile"
    SEMANTIC = "semantic"       # 当前的 facts；后续可扩展 subtype
    EPISODIC = "episodic"       # 后续：会话/任务情景
    PROCEDURAL = "procedural"   # 后续：可复用流程/技能


class MemoryKindSpec(BaseModel):
    kind: str
    display_name: str
    schema_ref: str                 # 文档/校验引用，不把 Python 类型放进 wire
    searchable: bool = True
    injectable: bool = False        # 只有 profile 为 True
    evictable: bool = True          # 生命力淘汰是否适用
    versioned: bool = False         # procedural 为 True
    default_ttl_days: int | None = None
    description: str = ""
```

| kind | payload | searchable | injectable | evictable | versioned |
|---|---|---:|---:|---:|---:|
| `profile` | `UserProfile` 单条聚合 | 否（直接读） | **是** | 否 | 否 |
| `semantic` | 原子事实/偏好（当前 facts） | 是 | 否 | 是 | 否 |
| `episodic` | 会话/任务摘要、时间、结果 | 是（含时间过滤） | 否 | 是（TTL/生命力） | 否 |
| `procedural` | 流程/技能、版本、审批状态 | 是（按名称/标签/向量） | 否 | 否（版本化替代淘汰） | **是** |

注册表只描述“类型元数据”；行为在 service 里。加类型时同时注册 spec + service，见 §2.2.6。

#### 2.2.2 通用记录：`MemoryRecord`

所有类型共用一个存储信封。类型差异放在 `structured` payload 和 service 校验里，而不是加新表/新方法。

```python
class MemoryRecord(BaseModel):
    id: str
    user_id: str
    kind: str
    subtype: str | None = None
    content: str = ""                         # 可检索正文；profile 可为空
    structured: dict[str, Any] = {}            # 类型 payload
    search_text: str | None = None             # 向量/词法检索文本
    source: str
    confidence: float = 1.0
    status: Literal[
        "active", "superseded", "folded", "archived",
        "deleted", "conflicted", "draft", "deprecated",
    ] = "active"
    revision: int = 0
    created_at: datetime
    updated_at: datetime
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    topic_key: str | None = None
    tags: list[str] = Field(default_factory=list)
    agent_name: str | None = None              # 来源；不是隔离桶
    thread_id: str | None = None               # episodic 用
    provenance: dict[str, Any] = {}            # evidence / source ids / trace
    supersedes: list[str] = Field(default_factory=list)
    superseded_by: str | None = None
    explicit_remember: bool = False
    pinned: bool = False
    archived_at: datetime | None = None
    deleted_at: datetime | None = None
    metadata: dict[str, Any] = {}


class MemoryQuery(BaseModel):
    kinds: list[str] | None = None             # None = 所有 searchable kind
    text: str | None = None
    filters: dict[str, Any] = {}               # status / topic / tags / source
    time_range: tuple[datetime, datetime] | None = None
    limit: int = 8
    cursor: str | None = None
    include_history: bool = False
    sort: Literal["relevance", "recency", "vitality"] = "relevance"


class MemoryHit(BaseModel):
    record: MemoryRecord
    score: float | None = None
    highlights: list[str] = Field(default_factory=list)


class MemorySearchPage(BaseModel):
    items: list[MemoryHit]
    next_cursor: str | None = None


@dataclass(frozen=True)
class MemoryScope:
    user_id: str
    agent_name: str | None = None       # 仅来源元数据，不参与隔离
    thread_id: str | None = None


class MemoryStats(BaseModel):
    user_id: str
    counts_by_kind: dict[str, int] = {}
    last_maintenance_at: datetime | None = None
    last_eviction_at: datetime | None = None
    last_fold_at: datetime | None = None
    dirty: bool = False


class ScopePage(BaseModel):
    items: list[MemoryScope]
    next_cursor: str | None = None


class Resolution(BaseModel):
    action: Literal["accept", "reject", "conflict", "supersede"]
    record: MemoryRecord | None = None
    reason: str = ""


class MemoryInstruction(BaseModel):
    operation: Literal["forget", "supersede", "unprotect", "remember"]
    target_record_id: str | None = None
    topic_key: str | None = None
    evidence: str
    source: Literal["conversation", "ui"]
    created_at: datetime
```

映射关系：

- **profile**：一条记录，`id="profile"`，`structured` 是 `UserProfile`；CAS 用 record revision。
- **semantic**：每条 fact 一条记录，`subtype="fact"`；当前 facts 字段进入 `structured`。
- **episodic**：每条 episode 一条记录，`thread_id` + `time_range`；不存原始 transcript。
- **procedural**：每条流程一条记录，`structured` 带 `version` / `approval` / `tool_refs`；
  `status=draft|active|deprecated`，历史版本保留。

#### 2.2.3 `MemoryBackend`：唯一存储扩展点

```python
class MemoryBackendCapabilities(BaseModel):
    vector_search: bool = True
    metadata_filter: bool = True
    batch_write: bool = True
    cas: bool = True
    list_scopes: bool = True
    ttl: bool = False


class MemoryBackend(ABC):
    capabilities: ClassVar[MemoryBackendCapabilities] = MemoryBackendCapabilities()

    @abstractmethod
    def get_record(self, scope: MemoryScope, kind: str, record_id: str) -> MemoryRecord | None: ...

    @abstractmethod
    def put_record(
        self,
        scope: MemoryScope,
        record: MemoryRecord,
        *,
        expected_revision: int | None = None,
    ) -> MemoryRecord: ...

    def put_records(
        self,
        scope: MemoryScope,
        records: list[MemoryRecord],
        *,
        expected_revisions: dict[str, int] | None = None,
    ) -> list[MemoryRecord]:
        # 默认逐条；有 batch_write 的后端覆盖
        ...

    @abstractmethod
    def search_records(self, scope: MemoryScope, query: MemoryQuery) -> MemorySearchPage: ...

    @abstractmethod
    def delete_records(
        self,
        scope: MemoryScope,
        record_ids: list[str],
        *,
        mode: Literal["soft", "hard"] = "soft",
        reason: str,
    ) -> int: ...

    @abstractmethod
    def get_stats(self, scope: MemoryScope, *, kind: str | None = None) -> MemoryStats: ...

    def list_scopes(self, *, cursor: str | None = None, limit: int = 100) -> ScopePage: ...

    def close(self) -> None:
        return None

    @classmethod
    @abstractmethod
    def from_config(
        cls,
        backend_config: dict[str, Any],
        *,
        store: Any,                      # LangGraph BaseStore，由宿主注入
        callbacks: Any = None,
    ) -> "MemoryBackend": ...
```

约束：

- 后端**不知道** `UserProfile`、`SemanticPayload`、`ProcedurePayload` 的 schema；只存取 `MemoryRecord`。
- 租户隔离在 namespace：`("users", user_id, "profile")` 与 `("users", user_id, "records")`；
  后端内部映射，不暴露给 service。
- 元数据过滤必须在向量 top-k 之前；`capabilities.metadata_filter` 为 False 的后端不能注册 semantic/episodic 的向量检索。
- CAS 不支持时 `capabilities.cas=False`，service 必须降级为 per-user lock + 冲突重试，
  不能假装原子。
- LangMem adapter 只实现这一层；它不需要知道“画像”或“程序记忆”。

#### 2.2.4 `MemoryTypeService`：类型扩展点

```python
class MemoryTypeService(ABC):
    kind: str
    spec: MemoryKindSpec
    backend: MemoryBackend
    resolver: MemoryResolver
    ingestors: list[MemoryIngestor]

    @abstractmethod
    def get(self, scope: MemoryScope, record_id: str) -> MemoryRecord | None: ...

    @abstractmethod
    def search(self, scope: MemoryScope, query: MemoryQuery) -> MemorySearchPage: ...

    @abstractmethod
    def write(
        self,
        scope: MemoryScope,
        proposals: list["MemoryProposal"],
        *,
        expected_revision: int | None = None,
    ) -> list[MemoryRecord]: ...

    @abstractmethod
    def delete(
        self,
        scope: MemoryScope,
        record_id: str,
        *,
        mode: Literal["soft", "hard"] = "soft",
        reason: str,
    ) -> None: ...
```

M10 的四个 service：

| service | 职责 | 状态 |
|---|---|---|
| `ProfileService` | 组合 `UserProfile` 快照、CAS、tombstone、显式优先 | 本阶段实现 |
| `SemanticMemoryService` | 当前 facts：去重、supersede、topic 冲突、生命力淘汰 | 本阶段实现 |
| `EpisodicMemoryService` | 会话/任务摘要、时间过滤、TTL | 预留接口 + 空实现 |
| `ProceduralMemoryService` | 流程注册、版本、审批、激活/废弃 | 预留接口 + 空实现 |

service 只依赖 `MemoryBackend` 和 `MemoryResolver`，不依赖其他 service。跨类型查询由
`MemoryManager` 组合，不把 semantic 的规则塞进 profile。

```python
class MemoryIngestor(ABC):
    kind: str

    @abstractmethod
    def extract(self, scope: MemoryScope, messages: list[Any]) -> list["MemoryProposal"]: ...


class ConflictStrategy(ABC):
    kind: str

    @abstractmethod
    def resolve(
        self,
        scope: MemoryScope,
        current: MemoryRecord | None,
        proposal: "MemoryProposal",
    ) -> "Resolution": ...
```

ingestor 只产出 proposal；conflict strategy 只决定“接受/拒绝/冲突/覆盖”，
两者都不直接写 store。加 episodic/procedural 时各注册一个 ingestor + strategy。

#### 2.2.5 `MemoryManager`：组合根，不是后端基类

```python
class MemoryManager:
    backend: MemoryBackend
    services: dict[str, MemoryTypeService]
    ingestors: list[MemoryIngestor]

    @classmethod
    def from_config(
        cls,
        backend: MemoryBackend,
        services: list[MemoryTypeService],
        ingestors: list[MemoryIngestor],
        *,
        callbacks: Any = None,
    ) -> "MemoryManager": ...

    def register(self, service: MemoryTypeService) -> None: ...

    def service(self, kind: str) -> MemoryTypeService: ...

    @property
    def supported_kinds(self) -> set[str]: ...

    def search(self, scope: MemoryScope, query: MemoryQuery) -> MemorySearchPage:
        """按 kinds 路由；单 kind 走 service，多 kind 由 backend 统一检索。"""

    def ingest(self, scope: MemoryScope, messages: list[Any], *, nowait: bool = False) -> None:
        """一次抽取，按 proposal.kind 路由到对应 service。"""

    def export(self, scope: MemoryScope, *, kinds: list[str] | None = None) -> MemoryExport: ...

    def clear(self, scope: MemoryScope, *, kinds: list[str] | None = None) -> None: ...

    def get_stats(self, scope: MemoryScope) -> MemoryStats: ...

    def list_scopes(self, *, cursor: str | None = None, limit: int = 100) -> ScopePage: ...

    def cancel_pending(self, scope: MemoryScope, *, epoch: int) -> int: ...

    def flush(self, timeout: float) -> bool:
        return True
```

关键点：

- **没有 `get_profile` / `search_facts` / `create_procedure` 这类类型方法。**
  宿主用 `manager.service(MemoryKind.PROFILE).snapshot(...)`、`manager.service(MemoryKind.SEMANTIC).search(...)`。
  这样加类型不需要改 manager 签名。
- `search` 的 `kinds` 是查询参数，不是接口方法；未来加 episodic/procedural 不需要加 `search_episodes`。
- `ingest` 只产出 `MemoryProposal`，由 service 自己决定写不写、怎么冲突消解。
- `MemoryManager` 是普通类，不是 ABC；后端替换通过 `MemoryBackend`，类型扩展通过 `MemoryTypeService`。
- 兼容旧配置：`manager_class` 作为 `backend_class` 的 deprecated alias；新配置用 backend_class。
- 工厂 `get_memory_manager()` 用 `threading.Lock` 双检单例；`backend_class` 解析失败直接 `ValueError`，
  不静默换后端。LangMem 依赖放 optional extra、锁版本、懒加载 import。

#### 2.2.6 加新类型 / 换后端的实际步骤

**加 semantic（如果还没加）**
1. 定义 `SemanticPayload` schema；
2. 实现 `SemanticMemoryService`；
3. 注册 `MemoryKindSpec(kind="semantic", ...)`；
4. 实现 `SemanticIngestor`（LangMem 抽取）；
5. 在 `memory_search` 的默认 kinds 里加 semantic。
不改 `MemoryBackend`、不改 `MemoryManager` 方法签名。

**加 episodic**
1. `EpisodicPayload`（summary / occurred_at / thread_id / outcomes）；
2. `EpisodicMemoryService`（append-only + time filter + TTL）；
3. spec：searchable=true / injectable=false / evictable=true；
4. `EpisodicIngestor`（复用 summarization hook）；
5. 无新后端方法。

**加 procedural**
1. `ProcedurePayload`（name / version / steps / tool_refs / approval / status）；
2. `ProceduralMemoryService`（draft → approved → active → deprecated，版本不可变）；
3. spec：searchable=true / evictable=false / versioned=true；
4. `ProceduralIngestor`（显式创建或任务后候选 + 审批）；
5. 无新后端方法。

**换后端**
1. 实现 `MemoryBackend` 的 6 个泛型方法；
2. 声明 capabilities；
3. 跑 `test_memory_backend_contract.py`；
4. 不改任何 service / kind schema / manager 签名。

#### 2.2.7 错误与兼容

```python
class MemoryManagerError(RuntimeError): ...
class MemoryReadError(MemoryManagerError): ...
class MemoryWriteError(MemoryManagerError): ...
class MemoryConflictError(MemoryManagerError): ...       # profile/record revision 并发 → HTTP 409
class MemoryKindNotSupported(MemoryManagerError): ...    # 501 / kind 未注册
class MemoryCorruptionError(MemoryManagerError): ...     # 500，不带路径
```

- `MemoryKindNotSupported` 不是“后端没有这个方法”，而是“这个 kind 没注册 service”；
  加类型后应变成可用，而不是永远 501。
- 旧接口的 `get_context()`、`search_facts()`、`create_fact()` 删除；
  迁移期可以提供 deprecated 适配器，但新代码只走 service。
- 读失败策略由 `MemoryBackend.read_failures_are_fatal_for_config()` 或 manager 配置声明；
  未知策略 fail-closed。

### 2.3 用户画像核心 schema

#### 2.3.1 设计原则

只放**跨会话稳定、跨任务高频、可枚举、可安全展示**的字段。任何自由文本都会重新变成无界画像，长尾内容必须进 facts。

```python
class ProfileField(BaseModel, Generic[T]):
    value: T
    source: Literal[
        "user_manual",
        "user_explicit_durable",
        "user_confirmed",
        "inferred",
    ]
    confidence: float = 1.0
    observed_at: datetime
    confirmed_at: datetime | None = None
    pinned: bool = False
    tombstoned_at: datetime | None = None


class ProfileSnapshot(BaseModel):
    profile: UserProfile
    schema_version: int
    revision: int
    token_count: int
    truncated: bool = False


class ProfilePatch(BaseModel):
    fields: dict[str, Any]
    source: Literal["user_manual", "user_explicit_durable", "user_confirmed"]
    evidence: str | None = None


class ProfileService(MemoryTypeService):
    kind = MemoryKind.PROFILE
    spec = MemoryKindSpec(
        kind=MemoryKind.PROFILE,
        display_name="User profile",
        schema_ref="UserProfile",
        searchable=False,
        injectable=True,
        evictable=False,
    )

    def snapshot(self, scope: MemoryScope) -> ProfileSnapshot | None: ...
    def update(
        self,
        scope: MemoryScope,
        patch: ProfilePatch,
        *,
        expected_revision: int | None = None,
    ) -> ProfileSnapshot: ...
    def clear(self, scope: MemoryScope, *, field_path: str | None = None) -> None: ...


class Identity(BaseModel):
    preferred_name: ProfileField[str] | None = None       # ≤64 字符，仅显式
    pronouns: ProfileField[str] | None = None             # ≤32 字符，仅显式
    locale: ProfileField[str] | None = None               # BCP-47
    timezone: ProfileField[str] | None = None             # IANA


class Communication(BaseModel):
    language: ProfileField[str] | None = None             # BCP-47
    tone: ProfileField[Literal[
        "neutral", "friendly", "formal", "direct"
    ]] | None = None
    verbosity: ProfileField[Literal[
        "concise", "balanced", "detailed"
    ]] | None = None
    format: ProfileField[list[Literal[
        "bullets", "tables", "steps", "prose", "code_first"
    ]]] | None = None


class WorkContext(BaseModel):
    role: ProfileField[str] | None = None                 # ≤100 字符
    domains: ProfileField[list[str]] | None = None        # ≤10 项
    tools: ProfileField[list[str]] | None = None          # ≤20 项
    expertise: ProfileField[Literal[
        "beginner", "intermediate", "expert"
    ]] | None = None


class Accessibility(BaseModel):
    needs: ProfileField[list[str]] | None = None          # ≤10 项，仅显式


class UserProfile(BaseModel):
    schema_version: Literal[1] = 1
    revision: int = 0
    updated_at: datetime

    identity: Identity = Field(default_factory=Identity)
    communication: Communication = Field(default_factory=Communication)
    work: WorkContext = Field(default_factory=WorkContext)
    accessibility: Accessibility = Field(default_factory=Accessibility)

    boundaries: ProfileField[list[str]] | None = None     # ≤10 条，仅显式
```

| 字段组 | 放什么 | 不放什么 |
|---|---|---|
| `identity` | 称呼、代词、locale、时区 | 法定姓名、邮箱、身份证 |
| `communication` | 语言、语气、长度、格式 | 某次任务的临时格式要求 |
| `work` | 角色、领域、常用工具、熟练度 | 具体项目细节、一次性任务 |
| `accessibility` | 用户显式声明的无障碍需求 | 模型推测 |
| `boundaries` | 用户显式说“不要做/不要提”的边界 | 模型推断的禁忌 |

**存储映射**：`ProfileService` 只读写一条 `MemoryRecord`：
`kind=profile`、`id="profile"`、`structured=UserProfile`、`revision=N`。
画像字段的 CAS 是整条 record 的 CAS；不需要后端提供 `update_profile_field` 之类的方法。

#### 2.3.2 注入规则

`before_agent` 只注入**已填充且非 tombstone** 的字段；不注入 `None`、不注入 `source/confidence/revision/updated_at`。示例：

```text
<user_profile>
preferred_name: 小王
language: zh-CN
tone: direct
verbosity: concise
format: bullets, code_first
role: backend engineer
domains: distributed systems, Python
tools: pytest, FastAPI
</user_profile>
```

- 沿用 M6 的 `<memory>` 槽；正文用 `<user_profile>` 包裹。走 **hidden HumanMessage**，不要放进 SystemMessage；画像正文是用户数据，不是系统指令。
- 只注入 `source in {user_manual, user_explicit_durable, user_confirmed}` 的字段；`inferred` 不常驻。
- 消息 id 固定为 `__user_profile__`；revision 变化时替换，不追加第二份。
- 每次注入记录 `profile_revision`、`content_sha256`、`token_count`、`truncated`；正文不进 `run_events`。
- `profile.max_tokens` 默认 800；超预算按字段优先级裁剪（身份/语言/安全边界 > 沟通偏好 > 工作上下文），记录 `profile_truncated`，不要截断半个字段。
- 读取失败：空画像 + warning，继续对话；不注入半份画像。

#### 2.3.3 权威与更新规则

| 来源 | 例子 | 能否常驻画像 | 优先级 |
|---|---|---|---|
| `user_manual` | 前端设置“回答语言：中文” | 是，`pinned=true` | 持久最高；自动推断/确认不能覆盖 |
| `user_explicit_durable` | “以后都用中文” | 是 | 新的明确指令可覆盖 manual |
| `user_explicit_ephemeral` | “这次用英文” | 否，只影响当前任务 | 不写画像 |
| `user_confirmed` | 多次提到、用户确认 | 是，只填空/低权威 | 低于 manual/explicit |
| `inferred` | 模型猜测 | **否**，只进 facts | 最低 |
| assistant/tool | 模型自述、工具结果 | 否 | 不参与画像 |

更新算法：

```text
validate path / type / length / sensitivity
→ 如果 proposal.source == inferred:
    写 fact，结束
→ 如果 current 为空:
    写入，source 按 proposal
→ 如果 current 与 proposal 同值:
    刷新 confirmed_at / confidence，结束
→ 如果 current.source == user_manual 且 proposal 不是 user_explicit_durable:
    拒绝，写 conflict candidate
→ 如果 proposal 是 user_explicit_durable 且更新、语义明确:
    覆盖，旧值进 profile_history
→ 如果两条都是 user_explicit 且时间很近/语义含糊:
    标 conflicted，保留旧值，前端提示
→ 写入时带 expected_revision；冲突则重读重算
```

**“新指令覆盖”规则**：

- 用户明确说“忘掉/改了/不用记了” → 走 `forget` / `supersede` / `unprotect`；
- 任务级指令（“这次用英文”）不覆盖长期画像；
- 新指令也必须是真实用户消息或前端操作；模型推断不能覆盖；
- 覆盖是软删除/归档 + 审计，不是静默改值。

删除字段时写 tombstone：`field_path`、`cleared_at`、`source=user_manual`。只有 `cleared_at` 之后的真实用户消息能重新写入。

### 2.4 语义记忆（当前 facts）

长尾 facts 是画像之外的“可检索记忆”，也是 `semantic` kind 的第一个 subtype。每条 fact 必须短、原子、可引用。

```python
class SemanticPayload(BaseModel):
    content: str
    category: str
    topic_key: str | None = None
    confidence: float
    source: Literal["user_explicit", "user_confirmed", "inferred", "consolidated", "import"]
    status: Literal["active", "superseded", "folded", "archived", "deleted", "conflicted"]
    explicit_remember: bool = False
    profile_linked: str | None = None
    access_count: int = 0
    confirmation_count: int = 0
    last_accessed_at: datetime | None = None
    last_confirmed_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    source_fact_ids: list[str] = Field(default_factory=list)
    fold_depth: int = 0


class SemanticMemoryService(MemoryTypeService):
    kind = MemoryKind.SEMANTIC
    spec = MemoryKindSpec(
        kind=MemoryKind.SEMANTIC,
        display_name="Semantic facts",
        schema_ref="SemanticPayload",
        searchable=True,
        injectable=False,
        evictable=True,
    )

    def search(self, scope: MemoryScope, query: MemoryQuery) -> MemorySearchPage: ...
    def write(self, scope: MemoryScope, proposals: list["MemoryProposal"], *, expected_revision: int | None = None) -> list[MemoryRecord]: ...
    def forget(self, scope: MemoryScope, record_id: str, *, mode: Literal["soft", "hard"] = "soft") -> None: ...
```

**映射**：一条 fact 对应一条 `MemoryRecord`（`kind="semantic"`、`subtype="fact"`）；
`SemanticPayload` 放在 `structured`，`content` 与 `search_text` 供检索；`topic_key`、`status`、
`valid_from/valid_to`、`explicit_remember` 等公共字段在 record 上。

后续真正的“语义记忆扩展”（实体、关系、知识图谱）可以：

- 复用 `kind="semantic"` + 新 `subtype`，由同一个 service 路由；或
- 注册新的 kind + service，但仍复用 `MemoryBackend`。
两条路都不需要改 `MemoryBackend` 或 `MemoryManager` 签名。

#### 2.4.1 抽取输出

一次抽取（不按类型拆多次 LLM 调用）返回：

```json
{
  "profile_patches": [
    {
      "field": "communication.language",
      "value": "zh-CN",
      "source": "user_explicit_durable",
      "evidence": "以后都用中文回答",
      "confidence": 0.95
    }
  ],
  "facts": [
    {
      "content": "用户偏好用 pytest 做 Python 测试",
      "topic_key": "python.test_framework",
      "category": "preference",
      "source": "inferred",
      "confidence": 0.8,
      "evidence": "我们一般用 pytest"
    }
  ],
  "instructions": [
    {
      "operation": "forget",
      "target_fact_id": "fact_...",
      "evidence": "忘掉这个"
    }
  ]
}
```

规则：

- `profile_patches` 只能映射到 §2.3 的字段路径；路径/类型/长度不合法 → 拒绝该条；
- `facts` 是无法映射到 core field 的长尾内容；转成 `MemoryProposal(kind="semantic", subtype="fact")`，
  `inferred` 只能出现在这里；
- `instructions` 只接受 `forget` / `supersede` / `unprotect`；缺证据/目标 → 拒绝该条；
- 任何字段缺失、模板漏分类字段 → 整批 fail closed，并计拒绝率；
- 抽取模型不能声明“用户确认了”；确认只来自确定性信号。

#### 2.4.2 写入门

自动写画像必须同时满足：

```text
scope == user
durability == durable
authority in {user_explicit_durable, user_confirmed}
evidence 是真实 HumanMessage 的逐字引用
字段在 core schema 内，且通过类型/长度/敏感字段校验
```

否则降级为 fact，或丢弃。当前轮真实用户消息是唯一可引用的证据源；assistant、tool result、注入记忆、system 都不能当证据。

#### 2.4.3 画像与 facts 的边界

- 能映射到 core field → 尝试更新 profile；
- 无法映射、或不确定 → 写 fact；
- 已提升为画像的 fact 标记 `profile_linked=field_path`，`memory_search` 默认不返回；
- 画像字段删除 → 指向它的 fact 一起失效或降级，不能画像没了 fact 又冒出来；
- `explicit_remember` 的 fact 不参与生命力淘汰，但可被新指令覆盖；
- facts 仍然需要容量/淘汰，不能无限增长。

### 2.5 LangMem 适配与存储

#### 2.5.1 LangMem 是什么

LangMem 不是存储服务，它是 LangChain 的抽取/记忆管理层；持久化落在 LangGraph `BaseStore`。因此“LangMem 后端”实际是：

```text
MemoryManager (facade)
  ├── ProfileService         -> MemoryBackend (profile record)
  ├── SemanticMemoryService  -> LangMemSemanticIngestor -> MemoryBackend
  ├── EpisodicMemoryService  -> future ingestor          -> MemoryBackend
  ├── ProceduralMemoryService-> future ingestor          -> MemoryBackend
  └── MemoryResolver / Maintenance

MemoryBackend (LangMemBackend)
  └── LangGraph BaseStore / vector index
```

不要直接把 LangMem 的 `Item` / `SearchItem` / 内部 schema 暴露到宿主。M10 只用 LangMem 做 facts 抽取；不使用它的 episodic / procedural / prompt optimization 路径。

#### 2.5.2 Namespace 与 scope

| 数据 | Namespace | Key |
|---|---|---|
| 画像 | `("users", user_id, "profile")` | `current` |
| 所有类型记录 | `("users", user_id, "records")` | `kind:id` |
| 维护统计 | `("users", user_id, "stats")` | `current` |

`agent_name` 写进 record metadata，不放进 namespace；`kind` 是记录字段，不是后端方法。
这样同一用户的 semantic/episodic/procedural 记录跨 agent 可见，符合 user 级 scope。

#### 2.5.3 Store 选择

复用现有 `get_store()`：

- `memory`：仅单测/本地临时，重启丢数据；
- `sqlite`：单机、小规模持久；
- `postgres`：生产、多进程。

不要把记忆写进 `run_events`；记忆是跨 thread 数据，run_events 是一次 run 的账本。使用独立 namespace/schema 即可。

#### 2.5.4 画像不交给 LangMem 通用 manager

LangMem 的通用 manager 适合 semantic facts 抽取/更新，不适合直接管理有严格 schema、显式优先级、tombstone 的用户画像。画像写入必须走宿主 `ProfileService`；LangMem 最多提供 store 和 semantic 抽取建议。

semantic facts 也建议两阶段：

```text
LangMem 抽取 -> proposal
MemoryResolver -> 冲突/去重/保护规则
canonical store -> 写入
vector index -> 异步更新
```

如果 LangMem ingestor 直接写 canonical store，会绕过冲突消解；这时要用 staging namespace 或 backend wrapper，确保 resolver 是唯一写入口。

#### 2.5.5 检索：元数据过滤 + 向量

```text
namespace = ("users", user_id, "records")
filter:
  kind in {semantic, episodic, ...}   # 由 query.kinds 决定
  status == active
  valid_to is null
  profile_linked is null
  conflict_group is null
  category == ...          # 可选
  agent_name == ...        # 可选，仅过滤
→ vector search(query.text)
→ rerank: similarity * confidence_weight * recency_weight
```

- `user_id` 必须在 namespace，不能只做 metadata filter；
- 过滤必须下推到 top-k 之前；不能先向量取 top-k 再过滤，否则可能被其他用户/失效事实占满；
- 画像不走向量；直接 `manager.service(MemoryKind.PROFILE).snapshot()`；
- embedding 模型、维度、版本写进配置；换模型必须重建索引；
- 索引是可重建派生数据；失败时降级为词法/子串，不能阻塞对话。

#### 2.5.6 并发与一致性

- 画像写带 `expected_revision`；单进程 per-user lock，多进程用 Postgres advisory lock 或等价 CAS；
- semantic record id 尽量由 `user_id + topic_key + normalized_content` 决定，保证幂等；
- 汇总/折叠后的写也要先过 resolver，再更新索引；
- 读缓存按 `user_id + revision` 失效；多进程缓存用短 TTL 兜底；
- LangMem/BaseStore 通常是 last-write-wins，不能假设它自带 CAS；CAS 由 `MemoryBackend` capability 声明。

### 2.6 写入管线

#### 2.6.1 Middleware 与队列

挂在 M6 `build_middlewares` 的 `# SLOT: Memory`，压缩之后、标题附近。它只负责入队，不在 `after_model` 同步打 LLM。

- 入队：真实 HumanMessage + 该轮最终可见 AIMessage；
- 不入队：隐藏注入、tool、子代理步骤、`hide_from_ui`；
- 入队时捕获 `user_id`，debounce 线程里 ContextVar 已消失；
- `skip_memory_flush=True` 用于子代理，避免内部轮次写父用户记忆；
- 压缩前 `summarization_hook` 调 `add_nowait`，把即将被扔掉的消息抢救进队列；
- 队列键：`(user_id, thread_id)`；user 级 scope 下不再按 agent 分桶，但保留来源 agent 元数据；
- 清空/删除时增加 `scope_epoch`；旧 epoch 的 inflight 结果必须丢弃，防止清空后又被写回；
- 关机：`flush(shutdown_flush_timeout_seconds)` 有硬预算，超时丢尾巴、不阻塞关闭；队列必须幂等。

#### 2.6.2 处理步骤

```python
class MemoryProposal(BaseModel):
    id: str
    kind: str
    subtype: str | None = None
    operation: Literal["upsert", "forget", "supersede", "unprotect", "remember"]
    payload: dict[str, Any] = {}
    target_record_id: str | None = None
    evidence: str
    source: Literal["conversation", "ui", "import"]
    confidence: float = 1.0
    expected_revision: int | None = None
```

```text
debounce 弹出一条 PendingContext
→ 再滤真实 Human + 最终可见 AI；空批次 return
→ MemoryIngestor 一次抽取，返回 MemoryProposal[]
→ 校验 proposal schema；缺字段整批拒绝 + 拒绝率计数
→ manager 按 proposal.kind 路由到已注册 service
→ ProfileService / SemanticMemoryService / ...
→ service 调 MemoryResolver（权威/冲突/保护）→ MemoryBackend
→ 释放用户锁后更新向量索引
```

**加类型时只加 proposal kind + service**：`MemoryProposal.kind` 是路由键；
`MemoryIngestor` 只负责产出 proposal，不直接写 store。episodic/procedural 后续
注册自己的 ingestor/service，队列和 manager 不变。

#### 2.6.3 显式记住与覆盖

- 用户说“记住这个” → `explicit_remember=true`；不参与生命力淘汰；
- 新指令 `forget` → 立即移出 active/索引，写 tombstone；
- 新指令 `supersede` → 旧 fact `superseded`，新 fact active；
- 新指令 `unprotect` → 清 `explicit_remember`，之后按普通生命力规则；
- 任务级指令不覆盖长期记忆；
- 所有覆盖写 `MemoryInstruction` 审计，保留来源与证据。

#### 2.6.4 PII / 敏感信息边界（补充）

M10 的文本记忆边界采用**入口脱敏 + run 级可还原 handle**：

```text
start_run（及其他 graph 入口）
  -> 只对 HumanMessage 文本做 PII 分类/替换
  -> state / graph_input / run record 只保存 handle
  -> 原值只存在当前 run 的 SensitiveVault（runtime.context，不序列化）
  -> 进程重启后 handle 不可解析，工具必须 fail closed
```

规则：

- **handle 作用域是当前 run**：不得写进长期记忆、日志或 run 之外的持久账本；
  handle 形如 `[[PII:EMAIL:<vault_id>:1]]`，同一 handle 在另一个 run 不保证
  指向同一原值。memory view 把 handle 和字面 PII 都变成 `[REDACTED]`。
- **memory 只看 memory view**：handle 和字面 PII 都替换成 `[REDACTED]`；memory
  proposal / evidence 中出现 handle、`[REDACTED]` 或已知凭据时整条丢弃。
  memory 不得保存 handle，也不得把 masked 值当长期事实。
- **用户画像白名单**：`UserProfile` 的字段视为允许持久化的低敏感画像数据，不参与
  通用 PII 替换；否则 `preferred_name` 等字段无法写入。其他 PII 不进入 facts。
- **工具原值还原**：所有工具在 `wrap_tool_call` 前由 `PIIMiddleware` 还原 handle；
  handle 解析失败必须作为可恢复工具错误，不能把 handle 当普通字符串发给外部服务。
  工具内部如何使用原值仍由工具自己负责；tool result 不在本策略保证范围内。
- **工具结果不在本策略保证范围内**：`ToolMessage` 中的敏感信息由工具自身负责。
  本策略不承诺对 tool result 做识别、脱敏或阻止其进入 checkpoint / run_events /
  memory；需要该保证的 tool 必须自己实现并测试。
- **输出侧**：模型写回 state / checkpoint 的 AI 文本和 tool_calls 参数先脱敏；
  面向用户的 display 是否把 handle 还原为原值，是独立的展示层决定，展示值不得落库。
- **失败策略**：脱敏器异常时使用不可解析的 failsafe handle；memory 丢弃该条；
  工具不得把 failsafe handle 发给外部服务。
- **范围**：只覆盖文本 `HumanMessage` 和模型文本输出。多模态输入、上传文件、
  artifact 内容、工具结果不在本策略范围内，必须由对应工具/上传层自行处理。


### 2.7 冲突消解

冲突不是接口错误，是数据状态。只有 `expected_revision` 并发冲突才是 HTTP 409。

#### 2.7.0 冲突策略按 kind 注册

| kind | 冲突策略 |
|---|---|
| `profile` | 字段级权威/时间/tombstone；见 2.7.2 |
| `semantic` | topic_key + 权威/时间 + valid time；见 2.7.3 |
| `episodic` | append-only，不覆盖；同一事件重复写入只做幂等去重 |
| `procedural` | 版本不可变；新版本走 draft → approved → active，旧版本 deprecated |

`MemoryResolver` 不写死所有规则，而是按 kind 取 `ConflictStrategy`；加类型时注册策略，不改 resolver 主体。

#### 2.7.1 权威顺序

```text
新的用户显式、持久指令
  > 用户手动设置（pinned）
  > 用户已确认
  > 模型推断
  > assistant/tool
```

同一权威下：新的 `observed_at` 优先；无法判断时保留旧值，进入 `conflicted`。

#### 2.7.2 画像字段

- 标量：同值只刷新确认；不同值按权威/时间；同权威且时间接近 → conflicted；
- 列表：item-level 合并/删除，不整体覆盖；有上限，按来源优先级裁剪；
- 敏感字段（pronouns/accessibility/boundaries）：只接受显式/手动；
- 手动字段 pinned：推断/确认不能覆盖；新的显式持久指令可以覆盖；
- 删除：写 tombstone，阻止旧对话复活。

#### 2.7.3 semantic facts

- 相同内容：去重/刷新；
- 同 `topic_key` 不同值：按权威/时间 supersede；
- 没有 topic_key：向量相似 + LLM 只做“是否矛盾”的判断，不决定谁赢；
- 时间变化不是冲突：用 `valid_from/valid_to` 表达；
- 未消解：保留最高权威 active；同权威保留已有 active，新候选 `conflicted`；不把两条都返回。

#### 2.7.4 画像 vs semantic facts

- 画像字段是 canonical；semantic fact 与之冲突时，fact 标 `superseded` 或 `profile_linked`，不进入 search；
- 画像字段删除后，指向它的 fact 一起失效；
- semantic 折叠结果也要先过画像冲突检查，再激活。
- episodic/procedural 不入画像冲突：episodic 是历史；procedural 走自己的版本/审批。

#### 2.7.5 冲突对象与前端

```python
class MemoryConflict(BaseModel):
    id: str
    user_id: str
    kind: str                         # profile / semantic / episodic / procedural / future
    subtype: str | None = None
    field_path: str | None
    topic_key: str | None
    current_record_id: str | None
    candidate_record_id: str | None
    current_source: str
    candidate_source: str
    evidence: list[str]
    status: Literal["open", "resolved", "dismissed"]
    created_at: datetime
```

前端提供：冲突列表、二选一/合并/忽略、字段来源与最后更新时间、records 的 active/superseded/conflicted 视图。未消解时不把两条正文同时注入模型。

### 2.8 注入与工具

#### 2.8.1 画像注入

- `before_agent` 通过 `manager.service(MemoryKind.PROFILE).snapshot(scope)` 读取 `ProfileSnapshot`；没有画像就只注入日期；
- 用 hidden HumanMessage，稳定 id `__user_profile__`；
- 只注入已填充字段；不注入 semantic/episodic/procedural 记录；
- token 预算独立于工具结果；默认 800；
- 会话首轮快照默认冻结；如需会话中生效，检测 `profile_revision` 变化后注入 compact delta，不重发整份；
- 读取超时 5s：跳过画像、保留日期、继续 run；读失败不 fail-closed，除非配置显式要求；
- 子代理不要每个都全量注入；lead 注入，子代理按需读取。

#### 2.8.2 `memory_search`

只读工具：

```python
@tool
def memory_search(
    runtime: Runtime,
    query: str,
    kinds: list[str] | None = None,
    time_range: tuple[str, str] | None = None,
    limit: int = 8,
) -> str:
    """Search long-term records not already in the user profile."""
```

- 身份从 `resolve_runtime_user_id(runtime)` 取，禁止接受模型传 `user_id`；
- `kinds` 默认 `["semantic"]`；后续加 episodic/procedural 只扩默认集合，不加新工具；
- 只返回 active/searchable 记录；不返回 profile_linked/superseded/archived/conflicted；
- 结果含 `kind/subtype/id/content/topic_key/confidence/updated_at`，可选 `conflict` 标记，不返回完整冲突正文；
- 出错返回 JSON `{"error": "..."}`，不把 Graph 打崩；
- v1 不提供 `memory_add/update/delete` 给模型；写入走后台抽取 + 前端/API；
- 静态系统提示里加一句：画像已加载，其他长期记录用 `memory_search`。

#### 2.8.3 上下文事件

```python
MEMORY_CONTEXT_EVENT = RunEventDefinition("context:memory", "context")
# content: {
#   "profile_revision": 3,
#   "content_sha256": "...",
#   "tokens": 412,
#   "truncated": false,
#   "agent_name": "..."
# }
```

完整文本留在 checkpoint 的动态上下文里，不进 `run_events`。客户端伪造的 profile/reminder 标记必须在 Gateway 剥掉。

### 2.9 定时维护：生命力淘汰 + 语义折叠

#### 2.9.1 独立维护服务

新增 `MemoryMaintenanceService`，在 Gateway lifespan 启动，不要复用用户定时任务服务（后者会创建 thread/run）。

- 默认每 24h 跑一次，低峰 + jitter；
- 按 user 分页，单次有 `max_users` / `max_duration`；
- per-user 锁，避免和写入/折叠并发；
- 多实例用 Postgres advisory lock / maintenance lease，不能每个 worker 都跑；
- 支持 `dry_run/shadow`；默认先 shadow；
- 优雅关闭：处理完当前 user 就停；归档/折叠操作幂等；
- 维护统计存 `("users", uid, "stats")`：`counts_by_kind`、`last_maintenance_at`、`last_eviction_at`、`last_fold_at`、`dirty`；
- 写入时增减计数，定时任务只扫超过水位的 user；定期对账修正漂移；
- 维护策略按 kind 注册：`profile` 不淘汰；`semantic` 走生命力；`episodic` 走 TTL/生命力；`procedural` 不自动淘汰（版本化替代）。

#### 2.9.2 semantic 生命力评分

```text
last_touch_at = max(last_accessed_at, last_confirmed_at, created_at)
elapsed_days  = now - last_touch_at

S0 = 60d (user_explicit)
   = 45d (user_confirmed)
   = 30d (inferred)
   = 60d (consolidated)

stability_days = S0
                 * (1 + 0.6 * log1p(access_count))
                 * (1 + 0.3 * confirmation_count)

retention  = exp(-elapsed_days / stability_days)
importance = confidence * source_weight * category_weight
vitality   = retention * importance
```

| 参数 | 建议 |
|---|---|
| `source_weight` | user_explicit 1.0 / confirmed 0.9 / inferred 0.6 / consolidated 0.9 |
| `category_weight` | preference 1.0 / project 0.7 / context 0.5 |
| `min_vitality` | 0.15 |
| `min_age_days` | 7（普通）/ 30（显式） |
| `max_evictions_per_run` | 50 |
| `max_eviction_ratio` | 0.10 |
| `archive_retention_days` | 30 |
| `high_watermark` / `target_count` | 200 / 160 |

规则：

- `explicit_remember=true` **不参与生命力淘汰**；但新的 `forget` / `supersede` / `unprotect` 用户指令可以覆盖；
- 但计入 `counts_by_kind["semantic"]`；如果受保护事实导致超容量，不自动删除，发 `memory_capacity_protected` 告警；
- 普通 `user_explicit` 不免疫：长期无命中、超过最小年龄、连续 2 个周期低分才归档；
- `access_count` 只由 `memory_search` 命中递增；画像注入不算；
- 如果最近 `search_activity_window_days` 天全局 `memory_search` 调用量低于 `min_search_calls_per_window`，维护任务只 shadow/告警，不执行基于访问次数的归档，避免“工具没人用 → 全库饿死”；
- 排序：`vitality` 升序，`record.id` 第二键；低于阈值才归档；不足目标数不强删健康记忆。

默认软删除：

```text
status = archived
archived_at = now
archive_reason = "low_vitality"
vitality_at_archive = ...
```

检索/索引/工具都排除 archived；30 天内可恢复/导出，之后由 retention 任务物理删除。

#### 2.9.3 semantic 语义折叠：高密度化

只作用于 `semantic` kind。profile 常驻、episodic 是历史、procedural 是版本化资产，都不走这条折叠线。

目标：**减少碎片化，把同一主题的多条小事实合并成一条高密度主题事实**，不是压成模糊短句。

触发：

- 遗忘阶段之后；
- 折叠只看 active；已归档/已折叠/conflicted 不参与；
- active semantic records > `fold_min_facts`（50），或某 `topic_key` 碎片 ≥ 3；
- 距上次折叠 > `cooldown_days`（7）；
- 有候选 cluster，未超预算。

候选条件：

| 条件 | 建议 |
|---|---|
| 同一 user | 必须 |
| 同 `topic_key` / 实体 | 优先 |
| 无 topic 时向量相似度 | cosine ≥ 0.82 起步 |
| cluster 大小 | 3–20 |
| 状态 | 仅 active |
| 年龄 | > 7 天 |
| 保护项 | 不折叠 pinned / explicit_remember / profile_linked / conflicted |
| 跨 agent | 默认不折叠，除非 topic_key 明确相同 |
| 高特异性 | 日期/数字/ID/金额/代码必须被摘要覆盖，否则不折 |
| 显式事实 | 只做近重复合并；不同细节的显式事实不改写，作为锚点 |

合并条件：

- LLM 判定 `mergeable=true`，并给出理由；
- 摘要必须覆盖源 semantic records 的关键点：实体、属性、值、约束、时间、否定；
- 不能新增源 records 没有的事实；
- 事实数明显下降（如 5 → 1）；token 数 ≤ 源总和 80%；
- `source_fact_ids` 完整；`fold_depth` ≤ 2；
- 不通过就跳过整个 cluster，不做部分合并。

写入：

```text
new_fact:
  source = consolidated
  source_fact_ids = [...]
  fold_depth = parent_depth + 1
  confidence = min(source confidence)
  status = active
  created_at = now
  valid_from = min(source valid_from)

originals:
  status = folded
  superseded_by = new_fact_id
```

折叠结果先过 `MemoryResolver` 和画像冲突检查，再激活；原始 facts 保留审计/恢复，向量索引只保留新事实。默认 shadow 1–2 周，用离线 QA/检索基准比较前后。

#### 2.9.4 维护顺序与失败

```text
archive low-vitality
→ fold active
→ update index/stats
→ emit audit/metrics
```

- 归档失败：跳过折叠，下一轮重试；
- 折叠失败：保留原 facts，不能留半合并；
- 单 user 失败不阻塞其它 user；
- 所有操作有 run_id、指标、审计；维护事件不进 `run_events`，走独立审计/指标。

### 2.10 身份与多租户

- 所有读写带 `user_id`；Gateway 用 `resolve_runtime_user_id` / `caller.user_id`，客户端伪造头在构图前剥掉；
- namespace 必须包含 `user_id`；`agent_name` 只写 metadata；
- IM/无认证模式用 M9 的 principal 规则；`DEFAULT_USER_ID` 只能用于无认证模式；
- 子代理共享父 user，但 `skip_memory_flush=True`，不写内部轮次；
- 账号删除走 M9 回收期；期间可导出；到期硬删 profile、records、conflicts、maintenance stats。

### 2.11 配置

```yaml
memory:
  enabled: true
  backend_class: langmem
  profile:
    injection_enabled: true
    max_tokens: 800
    refresh: thread_start        # thread_start | revision_delta
  ingest:
    debounce_seconds: 30
    shutdown_flush_timeout_seconds: 30
  maintenance:
    enabled: true
    interval_hours: 24
    jitter_minutes: 60
    max_users: 500
    max_duration_seconds: 900
    shadow: true
    high_watermark: 200
    target_count: 160
    min_vitality: 0.15
    min_age_days: 7
    explicit_min_age_days: 30
    explicit_consecutive_low_cycles: 2
    max_evictions_per_run: 50
    max_eviction_ratio: 0.10
    archive_retention_days: 30
    search_activity_window_days: 30
    min_search_calls_per_window: 20
    folding:
      enabled: true
      min_facts: 50
      min_topic_fragments: 3
      min_cluster_size: 3
      max_cluster_size: 20
      similarity_threshold: 0.82
      max_summary_token_ratio: 0.80
      max_fold_depth: 2
      max_clusters_per_run: 10
      cooldown_days: 7
  backend_config:
    extraction_model: ...
    embedding_model: ...
    embedding_dimensions: ...
    embedding_version: ...
    retrieval_adapter: vector     # vector | lexical
    token_counting: tiktoken      # tiktoken | char
```

宿主共享字段：`enabled` / `backend_class` / `profile` / `ingest` / `maintenance`；LangMem 私有旋钮放 `backend_config`。删掉旧的全局 `mode: middleware / tool`：现在固定是“画像常驻 + 其他 kind 按需检索”。

PII 边界是宿主级配置，不属于 `memory.backend_config`：

```yaml
pii:
  enabled: true
  redact_ai_output: true
```

`pii` 只覆盖文本 HumanMessage、模型文本输出和本策略声明的工具参数还原。
**tool result 由工具自身负责，不在本策略保证范围内**；多模态、上传文件和
artifact 内容不在本策略范围内。

热重载：`profile` / `maintenance` 每轮读取新配置；`backend_class` 变更要求 `reset_memory_manager()`，不要静默换后端。旧的 `manager_class` 作为 `backend_class` 的 deprecated alias；`memory.mode` / 顶层 DeerMem 字段在加载时迁移或 warning，不静默丢。

### 2.12 路由

`prefix="/memory"`。静态段写在参数段前面。

| Method | Path | 能力 | 成功 | 说明 |
|---|---|---|---|---|
| GET | `/api/memory` | `memory:read` | `MemoryResponse` | 聚合视图：profile + 首页 records + conflicts |
| GET | `/api/memory/profile` | `memory:read` | `ProfileResponse` | 当前画像 + revision |
| PATCH | `/api/memory/profile` | `memory:write` | `ProfileResponse` | 前端手动更新，带 expected_revision |
| DELETE | `/api/memory/profile/{field_path}` | `memory:write` | `{"cleared": field_path}` | 写 tombstone |
| GET | `/api/memory/search?q=&kinds=` | `memory:read` | `MemorySearchPage` | 跨 kind 检索；profile 不走这里 |
| GET | `/api/memory/items?kind=...` | `memory:read` | `RecordPage` | 通用列表；kind 必填 |
| GET | `/api/memory/items/{kind}/{record_id}` | `memory:read` | `MemoryRecordResponse` | 单条记录 |
| DELETE | `/api/memory/items/{kind}/{record_id}` | `memory:write` | `{"deleted": record_id}` | 软删除/归档 |
| POST | `/api/memory/items/{kind}/{record_id}/remember` | `memory:write` | `MemoryRecordResponse` | 设置/取消 explicit_remember |
| GET | `/api/memory/facts` | `memory:read` | `FactPage` | **deprecated alias**：kind=semantic |
| GET | `/api/memory/conflicts` | `memory:read` | `ConflictPage` | 未消解冲突 |
| POST | `/api/memory/conflicts/{conflict_id}/resolve` | `memory:write` | `ConflictResponse` | 用户选择 current/candidate/merge |
| GET | `/api/memory/export` | `memory:read` | `MemoryExport` | profile + records + conflicts，带 schema_version |
| DELETE | `/api/memory` | `memory:write` | `{"cleared": true}` | 清空 user 级记忆，先增 epoch 再删 |
| POST | `/api/memory/maintenance/run` | `memory:admin` | `MaintenanceRun` | 管理员触发，支持 dry_run |

```python
class ProfilePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: dict[str, Any]
    expected_revision: int


class MemoryRecordResponse(BaseModel):
    id: str
    kind: str
    subtype: str | None
    content: str
    structured: dict[str, Any] = {}
    status: str
    revision: int
    explicit_remember: bool
    created_at: str
    updated_at: str


class FactResponse(BaseModel):
    id: str
    content: str
    category: str
    topic_key: str | None
    confidence: float
    source: str
    status: str
    explicit_remember: bool
    created_at: str
    updated_at: str


class ProfileResponse(BaseModel):
    schema_version: int
    revision: int
    fields: dict[str, Any]


class ConflictResponse(BaseModel):
    id: str
    kind: str
    field_path: str | None
    status: str


class RecordPage(BaseModel):
    items: list[MemoryRecordResponse]
    next_cursor: str | None = None


class FactPage(BaseModel):
    items: list[FactResponse]
    next_cursor: str | None = None


class ConflictPage(BaseModel):
    items: list[ConflictResponse]
    next_cursor: str | None = None


class MemoryExport(BaseModel):
    schema_version: int
    exported_at: str
    profile: ProfileResponse | None
    records: list[MemoryRecordResponse]
    conflicts: list[ConflictResponse]
    maintenance: dict[str, Any] = {}


class MemoryResponse(BaseModel):
    schema_version: int
    profile: ProfileResponse | None
    records: list[MemoryRecordResponse]
    conflicts: list[ConflictResponse]


class MaintenanceRun(BaseModel):
    run_id: str
    dry_run: bool
    archived: int
    folded: int
    errors: list[str] = []
```

- 存储 I/O 在 `asyncio.to_thread`；路由不直接打开 store；
- wire 用 snake_case；不返回 store 路径、不返回别的 user_id；
- 错误：`MemoryConflictError` 409 `memory_conflict`；字段非法 422 `invalid_memory_profile`；record/kind 不存在 404；kind 未注册 501；损坏 500 `memory_corrupted`，detail 固定；
- `DELETE /memory` 先增 `scope_epoch` 再清 profile/records，最后 `cancel_pending(epoch=old)`；顺序反了 debounce 会把刚删的写回来；
- `/maintenance/run` 只给 admin，不接受普通 `memory:write`；`remember` 只对可淘汰 kind（semantic/episodic）开放，profile/procedural 不走它。

### 2.13 删除、导出、保留

- 归档/软删除：默认 30 天，可恢复/导出；
- 显式 forget：立即从 active/索引/上下文移除，写 tombstone；内容按隐私策略保留审计或硬删；
- tombstone 保留：防止旧对话/旧 facts 重新抽取；至少保留到账号删除；
- 导出：`schema_version` + profile + records（含 status）+ conflicts + maintenance 元数据；不导出 embedding；
- 账号删除：M9 回收期结束后物理删除 profile、records、conflicts、maintenance stats、tombstones 和向量索引；
- 维护审计：`archive` / `fold` / `conflict` / `instruction` 单独存审计，不塞进 `run_events`。

### 2.14 与旧设计的差异

| 旧设计（DeerMem 文件事实） | 新设计 |
|---|---|
| 一条事实一个 markdown + journal | LangGraph `BaseStore` namespace；profile 记录 + 通用 records |
| 全局 `mode: middleware / tool` | 画像常驻 + 按 kind 的 `memory_search` |
| `get_context() -> str` | service 返回 typed payload；宿主格式化 |
| 只按 agent 桶存 | user 级 scope；agent 仅 metadata |
| 写路径同步淘汰 | 定时 `MemoryMaintenanceService`；软删除 |
| confidence + recency 淘汰 | Ebbinghaus-inspired 生命力 + 语义折叠 |
| 只有 facts 冲突删除 | profile/semantic/instruction 三层冲突消解 |
| FTS5 检索 | 元数据过滤 + 向量检索；词法降级 |

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

**id 格式、ledger 校验、提示词示例、父侧 verifier 全部从这两个函数 +** `CITATION_RE` **派生。**
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

`GraphBubbleUp` **必须原样再抛**（M6 §2.4）。护栏包装器漏了这条，中断/澄清会变成普通工具错误。

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

### 3.7 护栏与审计的业务语义

机制在 §3.1–§3.6，落到业务上只有下面这张表。**写前端、写支持文档、做验收都对着它。**

| 场景 | 判定 | 用户看到 | 审计里记什么 |
| --- | --- | --- | --- |
| 管理员没配 provider（内置 `AllowAllProvider`） | 放行 | 无感 | 只有 sandbox / 工具回执那几类事件，没有 `guardrail` 拒绝 |
| 策略拒绝这次工具调用 | 拒绝 | 会话**继续**：模型收到一条可恢复的工具结果，换条路完成目标 | `middleware:guardrail`：`policy_id`、拒绝码、`is_subagent`、可选 `agent_id`——**不记工具参数、消息正文、工具结果** |
| provider 自己抛异常，`fail_closed=true`（默认） | 拒绝 | 同上（但原因文案不同） | 同上 + 诊断字段标明"provider 异常" |
| provider 抛异常，`fail_closed=false` | 放行 | 无感（可用性优先） | 一条 WARNING 级别的诊断 |
| 子代理里被拒 | 拒绝（同一套策略） | lead 收到可恢复的工具结果，继续综合 | 同上，用 `is_subagent=true` 区分 |
| 工具触发中断 / 澄清（`GraphBubbleUp`） | **原样上抛** | 正常的"需要用户确认"交互 | 不产生拒绝事件 |

三条隐私规则（它们决定了这张表能写什么）：

1. **审计只记"发生了什么"，不记"内容是什么"**——参数、正文、结果都不进事件；
2. **身份只用** `is_subagent` **/** `agent_id`，不把 `channel_user_id`、`user_id` 之外的

   外部标识塞进事件；
3. **路径先过 M2 的虚拟路径规范化**再记（§3.4）——记宿主机绝对路径等于泄露用户分桶布局。

**为什么拒绝必须是"可恢复的工具结果"而不是错误**：护栏的目标是挡住某个动作，
不是让用户的任务失败。给模型一条可恢复的结果，它往往会换个工具、换个参数把事办成；
抛异常则会让整轮对话以"出错了"结束，用户既没得到结果，也不知道是被策略挡的。

**为什么** `fail_closed` **默认是 true**：护栏属于"安全边界"这一侧，
默认放行意味着"provider 挂了的时候规则全部失效"。要换成可用性优先，
必须在配置里显式写 `fail_closed: false`，并在审计里能看到这是有意为之。

## 4. 10C 扩展插件系统（W1）

### 4.1 一开始就要定的：插件列表放在哪

**插件列表放** `config.yaml` **的顶层** `plugins:`**，不放 API 可写的** `extensions_config.json`**。**

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

记忆抽取**不**直接 import 扩展 API（memory backend 要能独立 vendor）：
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

**单一最终组装点** `extensions/stack.py`**。** 注入发生在组装**最后一步**，
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

#### 代码：语义锚点的组装

```python
# agentkit/extensions/stack.py
ANCHOR_MARKERS: tuple[str, ...] = (
    "model_logical",    # 清洗、预算：看见逻辑消息
    "model_physical",   # 压缩之后、provider 之前
    "tool_visible",     # 工具前：护栏、回执、审计
    "tool_raw",         # 最内：错误包装
    "standard",         # 普通 before_agent / after_model
)


def compose(
    base: Sequence[tuple[str, Any]],                    # M6 组装出的链：(锚点名, 中间件)
    contributions: Sequence[MiddlewareContribution],    # 扩展贡献
) -> list[Any]:
    """**唯一的最终组装点**（§4.4）。注入只发生在这里，而且只发生一次。

    为什么不用下标：`base.insert(3, mw)` 在基础链顺序一变就插错位置，
    而且错得很安静——中间件顺序错了通常不报错，只表现为"某个行为偶尔不对"。
    """
    buckets: dict[str, list[MiddlewareContribution]] = {marker: [] for marker in ANCHOR_MARKERS}
    for item in contributions:
        buckets[item.placement.value].append(item)      # 未知锚点在这一步就该报错
    out: list[Any] = []
    for marker, middleware in base:
        out.append(middleware)
        for item in sorted(buckets[marker], key=lambda c: (c.order, c.source)):   # 同锚点内稳定
            out.append(IsolatedMiddleware(item.middleware, source=item.source))
    return out


def assert_ordering(chain: Sequence[Any]) -> None:
    """锚点之间的先后约束在这里**首次调用时**解析（延迟的是调用，不是解析结果）。

    例：护栏必须在错误包装之外、压缩必须在模型调用之前。
    """
    index = {_marker_of(mw): i for i, mw in enumerate(chain) if _marker_of(mw)}
    for earlier, later in ORDER_CONSTRAINTS:            # 表在扩展包外，见 §4.4
        if earlier in index and later in index and index[earlier] > index[later]:
            raise ExtensionOrderError(f"{earlier} must come before {later}")
```

- `sorted(..., key=(order, source))`：`order` 相同时按来源名排序，
保证两个扩展的插入顺序在任何一次运行里都一样（否则测试会偶发红灯）。
- `IsolatedMiddleware(item.middleware, source=...)` **在这里包**：包装点只有一处，
descriptor 的 `.inner` / `.source` 就有唯一来源，指纹不会塌成同一个类名（§4.4）。
- `assert_ordering` **只在首次组装后调用**：中间件类可能还没 import 完，
锚点表在模块导入期解析会撞循环依赖；延迟到"链已经拼好"这一刻最稳。

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
`config["callbacks"]`：**Gateway** `run_agent`**、嵌入** `stream`**、子代理** `_aexecute`
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

基准脚本 **import 生产函数**（`agents/memory/maintenance/vitality.py` 和 `folding.py`），
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

W4 必做的基准是**生命力参数 + 折叠策略对比**（合成事实集，shadow 日志格式）。
LongMemEval 作为可选脚本，文档写明"自备数据路径"。

并发争用基准（多**进程**打同一 user namespace / profile revision）是加分项：
worker 直连 store 跳过迁移引导；READY/GO 屏障对齐后再计时。

---

## 6. catalog 补丁（M4 预告的 category）

```python
MEMORY_CONTEXT_EVENT   = RunEventDefinition("context:memory", "context")
WORKSPACE_CHANGES_EVENT = RunEventDefinition("workspace.changes", "workspace")
# middleware:{tag} 已有：guardrail / sandbox_audit / loop_detection / skill_activation
# context:memory content: profile_revision / content_sha256 / tokens / truncated / agent_name
```

`context` 8 字符、`workspace` 9 字符，都在 category 16 上限内。
`list_messages` 不返回它们。`event_type` `context:memory` 14 字符，在 32 上限内。
生命力维护、折叠、冲突变更走独立审计/指标，不塞进 `run_events`。

---

## 7. 测试清单

| 文件 | 覆盖 |
|---|---|
| `test_memory_business_rules.py` | 六条业务规则逐条：不跨用户；画像有界；推断只进 facts；删除/导出语义一致；后端读失败 → 当轮不注入、打 warning、对话照常完成 |
| `test_memory_manager_contract.py` | 组合根契约：`service(kind)` 路由、`search(kinds)`、`ingest` 按 proposal.kind 分发；不返回 prompt；未注册 kind 抛 `MemoryKindNotSupported` |
| `test_memory_backend_contract.py` | `MemoryBackend` 泛型契约：get/put/search/delete/stats/CAS；LangMem 与 noop 共用；capability 缺失时不假装支持 |
| `test_memory_kind_registry.py` | 注册 spec/service；新增 fake kind 不改 backend/manager 签名；search 默认 kinds；未注册 kind 返回 501 |
| `test_memory_type_services.py` | Profile/Semantic 服务；Episodic/Procedural 空实现可注册；每类 service 只依赖 backend/resolver |
| `test_memory_concurrency.py` | 画像 record revision CAS；同一 user 的 profile/records 不串 namespace；归档/折叠与写入互斥 |
| `test_memory_profile_schema.py` | 字段路径/类型/长度/敏感字段校验；显式优先；tombstone；schema_version 迁移 |
| `test_memory_profile_injection.py` | hidden HumanMessage、稳定 id、只注入已填充字段、token 裁剪、读取失败不注入半份、revision delta |
| `test_guardrail_business.py` | 拒绝 → 可恢复工具结果且会话继续；`fail_closed` 两态行为；`GraphBubbleUp` 原样上抛不被吞；审计事件不含工具参数 / 正文 / 结果；审计里的路径已虚拟化 |
| `test_extension_compose.py` | 贡献插到正确锚点；同 `order` 按 `source` 稳定；未知锚点报错；`assert_ordering` 在首次组装后抛出；`IsolatedMiddleware` 包装只发生在一处（指纹不塌） |
| `test_memory_extraction_gate.py` | 显式/持久/证据门；`inferred` 只进 facts；任务级指令不写画像；缺字段模板 fail closed；拒绝率计数 |
| `test_memory_conflict_resolution.py` | 画像/fact/instruction 三层冲突；权威顺序；未消解保留旧值/最高权威；两条矛盾不进入上下文；`explicit_remember` 可被新指令覆盖 |
| `test_memory_instructions.py` | forget / supersede / unprotect；tombstone 防止旧对话复活；任务级指令不覆盖长期记忆 |
| `test_memory_maintenance.py` | 按 kind 的 policy：profile 不淘汰、semantic 生命力、episodic TTL、procedural 版本化不淘汰；普通 `user_explicit` 低命中归档；`explicit_remember` 不参与淘汰；软删除可恢复；每轮上限；search 活跃度不足时跳过 |
| `test_memory_folding.py` | 同主题聚类；5→1 高密度；关键点/数字/否定不丢；`fold_depth<=2`；显式事实只做近重复；折叠后过 resolver/画像冲突 |
| `test_memory_identity.py` | 客户端伪造身份被剥；user 级 namespace；`agent_name` 仅 metadata；子代理 `skip_memory_flush` 不写父记忆；入队捕获 user_id 越过 debounce |
| `test_memory_retrieval_filters.py` | 元数据过滤先于向量 top-k；跨用户/已归档/已折叠/冲突候选不进结果；向量索引失败降级词法；换 embedding 后重建 |
| `test_memory_token_counting.py` | tiktoken 失败走字符估算 + 冷却；`char` 模式零网络；profile token 预算独立 |
| `test_receipt_verification.py` | 引用不存在的回执被拒；截断 → 无法验证而非通过；citation 格式单一来源；压缩后 rN 不对新 ledger 错位误判为通过 |
| `test_guardrail_middleware.py` | deny 变 ToolMessage；`GraphBubbleUp` 传播；provider 抛 + fail_closed |
| `test_workspace_scan.py` | 任意深度排除 `.git`/`node_modules`/`TOOL_RESULTS`；uploads 不扫；敏感路径无 diff 正文 |
| `test_extension_isolation.py` | 贡献者抛异常 fail open 且 handler 只调一次；**真实取消传播、内部 CancelledError 被包住** |
| `test_extension_ordering.py` | 语义锚点；组装在最后一步；AST 检查 `extensions/` 无模块级反向 import |
| `test_extension_router.py` | 宿主优先；可证明的遮蔽原子拒绝；认证不可 opt out；WS 贡献被拒 |
| `test_memory_routes.py` | profile/items/search/conflicts/export/maintenance 路由；`kind` 参数；snake_case 且无 store 路径；revision 不一致 409；非法字段 422；`DELETE /memory` 先增 epoch 再清；maintenance 仅 admin |
| `test_goal_routes.py` | 无 goal 的 GET 是 `{"goal": null}`；`max_continuations` 大于 8 → 422；活跃 run 时 PUT 409 `thread_busy`；别人的 thread id 409 `thread_ownership_conflict` 或 404，不写成自己的 goal；路由不调用 `start_run` |
| `test_plugins_startup_only.py` | `plugins` 在 `STARTUP_ONLY_FIELDS`；API 写 JSON 不能改 plugins |
| `test_tracing_no_import_side_effect.py` | **import 框架不启动追踪** |
| `test_tracing_graph_root.py` | 图内 `attach_tracing=False`；子代理 metadata 带父 thread_id |
| `test_bench_offline.py` | 离线套件不碰网络/凭据/真实数据集；benchmark 模块 import 的是生产 vitality/folding |

`test_memory_conflict_resolution.py` 的未消解冲突路径，和 `test_extension_isolation.py` 的取消来源，
是这一阶段最值得写的两个——纯靠推理很难发现。

必须先写、形状如下（TDD，M0 已定）：

```python
def test_inferred_fact_never_writes_profile(fake_manager):
    """模型推断只进 facts；画像保持空，不能被猜测带偏。"""
    fake_manager.ingest(
        scope,
        [HumanMessage("我们一般用 pytest"), AIMessage("好的")],
    )
    assert fake_manager.service(MemoryKind.PROFILE).snapshot(scope) is None
    hits = fake_manager.service(MemoryKind.SEMANTIC).search(
        scope, MemoryQuery(kinds=[MemoryKind.SEMANTIC])
    ).items
    assert hits[0].record.source == "inferred"


def test_explicit_remember_survives_vitality_but_instruction_overrides(fake_manager):
    fact = seed_semantic(
        fake_manager, scope,
        content="用户要求记住：项目代号是 falcon",
        explicit_remember=True,
    )
    run_maintenance(fake_manager, scope, now=now + timedelta(days=365))
    assert fake_manager.service(MemoryKind.SEMANTIC).get(scope, fact.id).status == "active"

    fake_manager.ingest(scope, [HumanMessage("忘掉项目代号")])
    assert fake_manager.service(MemoryKind.SEMANTIC).get(scope, fact.id).status == "deleted"


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

- [ ] 用户说过"回复别超过三行" → 画像 `communication.verbosity=concise`；下一轮没再说，回答依然是短的；前端改掉后生效
- [ ] 用户说"记住这个" → fact `explicit_remember=true`；把它放到 1 年后的维护周期，不因低命中被归档；用户说"忘掉"后从 context/索引消失
- [ ] 普通 `user_explicit` fact 长期零命中 → 维护归档；`explicit_remember` 不受影响；归档可恢复
- [ ] 模型推断"用户大概喜欢 X" → 只进 facts，画像保持空；不会因为一条推断改变常驻行为
- [ ] 长尾项目细节在 A 会话写入，B 会话通过 `memory_search` 查到；画像里不会重复出现
- [ ] 用户手动把语言设为中文，后台推断英文 → 画像保持中文，英文作为 fact/冲突候选；不静默覆盖
- [ ] 用户说"这次用英文" → 当前回答英文；画像语言不变
- [ ] 画像与 fact 冲突且未消解 → 前端提示，模型上下文只看到旧画像/最高权威 fact，看不到两条矛盾正文
- [ ] 事实数超过高水位 → 低生命力 fact 软删除/归档；可在 30 天内恢复；shadow 日志能看见候选
- [ ] 5 条同主题碎片折叠成 1 条高密度事实；数字/日期/否定不丢；原始 facts 可追溯
- [ ] 同一用户两个 agent 共享画像与 records；`agent_name` 只出现在来源/审计里
- [ ] 注册一个 fake kind + service → `memory_search(kinds=[fake])` 可用；`MemoryBackend` / `MemoryManager` 签名不变
- [ ] 换 `backend_class=noop` → 画像为空、semantic 为空、对话继续；不抛未处理异常
- [ ] 跨用户：A 的 `memory_search` 查不到 B 的任何 profile/fact；伪造身份头被剥
- [ ] 一次被护栏拒绝的工具调用：会话继续，模型换条路把事办成；审计里查得到拒绝，**看不到参数与正文**
- [ ] 装一个有 bug 的扩展：服务照常启动并正常回答，诊断里能看到它的报错；把它标成 `required: true` 之后启动失败
- [ ] lead 声称"写了 report.md"但引用了不存在的 `[r99]` → 模型可见 UNVERIFIED，而不是当成功
- [ ] 一次 run 改了 workspace 文件，`list_events(category=workspace)` 能看到；`node_modules` 里的变化没有
- [ ] 一个故意抛异常的扩展中间件，run 正常完成，诊断可见，下游工具只执行一次
- [ ] 贡献路由试图遮蔽 `/health` → 整份 router 被拒，其它扩展照常挂载
- [ ] 一次 run 在追踪后台呈现为**一棵**树，子代理归在同一 session（建议肉眼看一眼）
- [ ] `import agentkit.agents` 不创建 TracerProvider、不写 span 文件

---

## 9. 与已有里程碑的接口对照

| 已有槽 | M10 填什么 |
|---|---|
| M0 `STARTUP_ONLY_FIELDS` | `plugins`（整段） |
| M1 图根 callbacks / `attach_tracing=False` | `build_tracing_callbacks` 真正挂上 |
| M1 `{soul}` 等提示词段 | 画像进 hidden HumanMessage；其他 kind 不进静态提示词，走 `memory_search` |
| M2 虚拟路径 / 沙箱工具 | 审计记虚拟路径；workspace 扫描 workspace+outputs |
| M4 catalog `context` / `workspace` | `context:memory`（带 `profile_revision`）、`workspace.changes`；维护/折叠走独立审计 |
| M4 `BaseStore` / `get_store()` | LangMem `MemoryBackend` 的 profile/records 持久化；user namespace |
| M4 `middleware:{tag}` | `guardrail`、`sandbox_audit` |
| M6 `# SLOT: Memory` | `MemoryMiddleware` |
| M6 `# SLOT: ToolReceipt / Guardrail / SandboxAudit` | 三条中间件，均在 ToolErrorHandling 外 |
| M6 `# SLOT: custom / extensions` | `stack.compose` 最终注入 |
| M6 DynamicContext `<memory>` | 槽内注入 `<user_profile>`；其他 kind 不注入；哈希/revision 事件 |
| M6 SafetyFinishReason | 补测试；与回执/审计一起验收 |
| M7 `extensions_config.json` | **仍然不能**出现 `plugins` |
| M8 `skip_memory_flush=True` | 子代理不写父记忆；清空时用 `scope_epoch` 丢弃旧 inflight |
| M8 回执槽位空着 | lead 链开始盖回执；不实现 M8 跳过的确定性验收清单 |
| M9 `resolve_runtime_user_id` / Principal | user 级 namespace、GuardrailRequest 身份；`agent_name` 仅 metadata |
| M9 通道出站白名单 | 画像块带 provenance；`<memory>` 泄漏仍靠 9D |
| M7 技能系统 | 本阶段不实现 procedural 记忆；后续复用 skill，不另造并行文本记忆 |

---

## 10. 刻意不做的事

| 不做 | 理由 |
|---|---|
| 重执行式结论校验 | §3.1 第三层。独立产品线 |
| M8 的 `file exists` / `tests_passed` 验收清单 | 已证明是研究级工作量；回执层只证明调用发生过 |
| 插件热加载 / 原地升级 | 模块卸载不可靠；升级走 remove + install |
| episodic / procedural 记忆 | 本期只实现 profile + semantic；episodic/procedural 只留 service 接口，不实现完整闭环；procedural 后续复用 M7 skill |
| 推断直接写画像 | 推断只进 facts；避免错误画像全局生效 |
| 自动改写 `explicit_remember` | 它免于生命力淘汰；只能被新用户指令覆盖或用户手动整理 |
| 未消解冲突同时注入两条 | 保留旧画像/最高权威 fact；候选只在 UI/审计里 |
| 默认硬删除 | 先软删除/归档 30 天；用户显式 forget 才走删除语义 |
| 多实例维护不加锁 | 必须 Postgres advisory lock / lease；否则每个 worker 都跑 |
| 记忆进 `run_events` | 跨 thread 数据，不是一次 run 的账本；审计单独存 |
| Monocle 式进程级 OTel | 全局 TracerProvider 只能放 lifespan；默认不做，免得从 import 溜走 |
| CI 里跑上游 LongMemEval 原文 | 离线套件只用合成 fixture |
| 贡献 WebSocket | 宿主还没有认证/Origin 能力 |
| 把 `plugins` 放进 API 可写 JSON | 任意代码执行 |

最后一条通用原则：**宁可拒绝一种贡献，也不要接受一种宿主会静默忽略的注册方式。**
任何新增贡献种类，公共契约和宿主运行时必须在同一个改动里都实现。

---

## 11. 一句话总结

M10 四条线的共同主题是**让系统在长期运行后仍然可信**，
而每条线的核心都是同一种取舍：**在不确定的时候拒绝，而不是放行。**

- 记忆：画像有界常驻，长尾事实按需检索；推断不进画像，冲突保留旧值，维护先归档后折叠
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

### W2：分层契约 + 画像服务 + LangMem backend

**测试** `test_memory_backend_contract.py` / `test_memory_kind_registry.py` /
`test_memory_manager_contract.py` / `test_memory_type_services.py` /
`test_memory_profile_schema.py` / `test_memory_profile_injection.py` /
`test_memory_concurrency.py`（§7 表）。

**实现**

- `config/memory_config.py`：`profile` / `ingest` / `maintenance` 宿主字段；`backend_class`
- `agents/memory/{manager,backend,types,context}.py`
- `agents/memory/services/profile.py` + semantic/episodic/procedural service 空壳
- `backends/langmem/{backend,store,config}.py` + `backends/noop/`
- 注册 profile spec/service；画像 one-record CAS、tombstone、hidden HumanMessage 注入
- backend contract tests 先覆盖 get/put/search/delete/stats；semantic 抽取 W3 接

### W3 上午：semantic 服务 / 工具 / 冲突 / HTTP

**测试** `test_memory_business_rules.py` / `test_memory_extraction_gate.py` /
`test_memory_conflict_resolution.py` / `test_memory_instructions.py` / `test_memory_retrieval_filters.py` /
`test_memory_identity.py` / `test_memory_routes.py` / `test_memory_token_counting.py`；
Gateway 路由用 TestClient + 伪造 identity 头必须被剥。

**实现**

- `ingestors/base.py` + `ingestors/langmem_semantic.py`：MemoryProposal(kind=semantic)
- `resolver.py`：按 kind 的 ConflictStrategy；semantic 去重/supersede/profile-linked
- `services/semantic.py`：semantic 搜索/写入/forget；facts 迁入 `subtype=fact`
- `tools.py`：只读 `memory_search(kinds=...)`（元数据过滤 → 向量）
- `memory_middleware.py` + `summarization_hook.py`；`scope_epoch` 丢弃旧 inflight
- `app/gateway/routers/memory.py`：profile/items/search/conflicts/export；
lifespan 启动 store schema/索引预热不挡 `/health/ready`；
shutdown：`shutdown_flush` → 关 store → `close`

### W3 下午：10B 回执 / 护栏 / 审计 / 工作区

**测试** `test_receipt_verification.py`（压缩后错位不得判通过；截断 retained=[]）、
`test_tool_receipt_middleware.py`（伪造回执被覆盖）、
`test_guardrail_middleware.py`（`GraphBubbleUp`）、
`test_workspace_scan.py`（任意深度 `node_modules`；uploads 不扫）。

**实现** 按 §3；worker 在终态收据前 `record_workspace_changes`。
SafetyFinishReason 补 M6 留下的测试。

### W4：维护 + 语义折叠 + 收口

**测试** `test_memory_maintenance.py` / `test_memory_folding.py` /
`test_bench_offline.py`：`scripts/benchmark/memory_maintenance/` import 的
`vitality.py` / `folding.py` 与生产是同一 `id()`；套件无网络、无真实数据集。

**实现**

- `maintenance/service.py`：lifespan 启动/停止、per-user 锁、分页、shadow
- `maintenance/policies.py`：profile/semantic/episodic/procedural 的 policy 注册
- `maintenance/vitality.py` + `folding.py`（只作用于 semantic）
- `/api/memory/maintenance/run`（admin，支持 dry_run）
- 归档 retention job；软删除 30 天
- shadow 日志格式与指标；可选 CLI 事务（§4.7，做了就必须完整）

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

加记忆类型：

1. 定义 payload schema（如 `SemanticPayload` / `EpisodicPayload` / `ProcedurePayload`）
2. 实现 `MemoryTypeService`（get/search/write/delete）
3. 注册 `MemoryKindSpec`（searchable/injectable/evictable/versioned）
4. 注册 ingestor 和 `ConflictStrategy`
5. 需要注入/维护时注册对应的 strategy/policy
6. 不改 `MemoryBackend`、不改 `MemoryManager` 方法签名

换记忆后端：

1. `backends/<name>/` 暴露 `BACKEND_CLASS`
2. 实现 `MemoryBackend` 的泛型方法：`get_record` / `put_record` / `search_records` / `delete_records` / `get_stats` / `from_config`
3. 返回 `MemoryRecord` / `MemorySearchPage`；**不要返回拼好的 prompt**，不要暴露后端自己的 Item 类型
4. 私有旋钮只进 `backend_config`，不要改 `MemoryConfig`
5. namespace 必须带 `user_id`；`agent_name` 只写 metadata
6. 用 `test_memory_backend_contract.py` 跑一遍；再加一个 fake kind 证明 service 层不需要后端改动
7. LangMem 这类快速演进的库：锁版本、optional extra、懒加载 import；缺包时构造失败并给安装提示

---

*文档版本：M10 开发参考。实现以本文 + DeerFlow 对位源码为准；冲突时先改文档再改代码。*