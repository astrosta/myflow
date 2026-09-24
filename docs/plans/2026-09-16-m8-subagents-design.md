# M8 子代理委托 详细设计

> 配套文档：[分阶段开发方案](2026-09-16-greenfield-agent-service-roadmap.md) ·
> [M0 地基](2026-09-16-m0-foundation-design.md) · [M1 最小 Agent](2026-09-16-m1-minimal-agent-design.md) ·
> [M2 工具与沙箱](2026-09-16-m2-tools-sandbox-design.md) · [M3 Gateway 与流式](2026-09-16-m3-gateway-streaming-design.md) ·
> [M4 持久化](2026-09-16-m4-persistence-design.md) · [M6 中间件体系](2026-09-16-m6-middleware-design.md) ·
> [M7 MCP 与技能](2026-09-16-m7-mcp-skills-design.md)
>
> 本文细化 M8（2 周，可与 M7 并行）。沿用包名：框架层 `agentkit.*`，应用层 `app.*`。
> 读完应能按文件树、接口、陷阱、测试清单直接开工，不必再翻 DeerFlow 源码拼语义。

---

## 1. M8 到底在解决什么

**目标一句话：lead agent 能把子任务丢给独立上下文的子代理，并在父 run 里安全地等结果。**

子代理的价值只有三条，**其它都是幻觉**：

| 收益 | 什么时候真的成立 |
|---|---|
| **并行延迟** | 3 个互不依赖的调研任务同时跑 |
| **专家能力** | 某个子代理有更强的模型或更专的提示词 / 工具集 |
| **上下文隔离** | 一个会产生几万 token 中间垃圾的任务，只把结论带回来 |

对应的成本也是实打实的：启动开销、重复发现（子代理不知道 lead 已经查过什么）、
结果综合的额外一轮、状态冲突、副作用重复。

**所以委派必须被定位成一种优化，而不是"任务复杂就委派"的默认反应。**
这一条要写进三处，且三处保持一致（测试钉住字符串契约，见 §18）：

1. lead 系统提示词里的 `{subagent_section}`（M1 已留槽）
2. `task` 工具的 docstring
3. 每个内置子代理的 `description` / `system_prompt`

两个**硬否决**（不是"成本"，是"不许并行派发"）：

- **输出依赖**：B 需要 A 的结果 → 不能并行派发；可以串成**一个**子代理
- **可变状态重叠**：两个子代理会写同一个文件 → 不能并行派发

重复发现和"直接做更便宜"是成本项，要权衡，但不是否决项。

### 1.1 M8 明确不做（先读，避免按 DeerFlow 全量抄）

DeerFlow 的 `deerflow/subagents/` 还包含一整套 **durable `batch_task`**、
**回执校验**、**确定性验收清单**、**托管子代理定义表**。那些各自够一个里程碑。
M8 只做**普通 `task` 委派**：同步从 lead 的视角阻塞到子代理终态，进度靠
`task_*` custom SSE，状态靠 `ThreadState.delegations` 台账。

详见 §20。接口上预留 `stop_reason` 和终态 `ToolMessage` 元数据槽，不为它们提前建表。

### 1.2 两周排期

| 天 | 内容 | 主要文件 |
|---|---|---|
| D1 | 配置 + 状态契约 + `DelegationEntry` 填满 M1 的槽 | `subagents_config.py` / `status_contract.py` / `thread_state.py` |
| D2 | 注册表 + 两个内置 + `task` 工具骨架（还不跑图） | `registry.py` / `builtins/` / `task_tool.py` |
| D3–5 | **执行器：隔离循环、ContextVar、checkpoint 血缘** | `executor.py` / `runtime.py` / `capacity.py` |
| D6 | 并发上限 + `SubagentLimitMiddleware` + 总量台账 | `subagent_limit_middleware.py` / `delegation_ledger.py` |
| D7 | 沙箱引用租约 + 取消/超时/清理 | `sandbox/lease.py` 升级 |
| D8 | `task_*` 事件 + namespace 保留 + `put_batch` | `step_events.py` / worker buffer / catalog |
| D9 | 护栏 `stop_reason` + 子代理中间件链 | `build_subagent_runtime_middlewares` |
| D10 | token 归属、步骤捕获收缩、测试收口 | `token_collector.py` + 回归套件 |

**D3–5 是整阶段最难的部分。** 前面两天把契约钉死，后面才能测血缘和取消。

### 1.3 M8 新增 / 改动的文件

```
agentkit/
├── config/
│   ├── subagents_config.py          ★  # SubagentsAppConfig / custom_agents / 上下限
│   └── subagent_runtime_config.py   ★  # STARTUP_ONLY：进程级 max_running
├── subagents/
│   ├── __init__.py
│   ├── AGENTS.md
│   ├── config.py                    ★  # SubagentConfig dataclass（注册表条目）
│   ├── registry.py                  ★  # builtin → custom_agents → agents.<name> 覆盖
│   ├── runtime.py                   ★  # SubagentRuntime：共享容量控制器
│   ├── capacity.py                  ★  # 异步 FIFO 准入
│   ├── executor.py                  ★  # 隔离循环 + 执行 + 注册表
│   ├── status_contract.py           ★  # 跨语言枚举 + ToolMessage kwargs
│   ├── token_collector.py           ★  # 子代理 LLM 用量，不跨循环写父账本
│   ├── step_events.py               ★  # 纯函数：捕获步骤 / 映射持久化事件
│   └── builtins/
│       ├── __init__.py              ★  # BUILTIN_SUBAGENTS
│       ├── general_purpose.py       ★
│       └── bash_agent.py            ★
├── agents/
│   ├── thread_state.py              # 填满 DelegationEntry / merge_delegations
│   └── middlewares/
│       ├── subagent_limit_middleware.py   ★
│       ├── delegation_ledger.py           ★  # 从 messages 抽台账 + 渲染给模型
│       ├── subagent_date_middleware.py    ★  # 子代理专用日期，不走 lead 的 DynamicContext
│       └── tool_error_handling_middleware.py  # 加 build_subagent_runtime_middlewares
├── tools/builtins/
│   └── task_tool.py                 ★
├── sandbox/
│   └── lease.py                     ★  # M2 的 acquire/release 升级为引用租约
└── runtime/
    ├── events/catalog.py            # 加 SUBAGENT_* 三条
    └── runs/worker.py               # _SubagentEventBuffer

contracts/
└── subagent_status_contract.json    ★  # 与 status_contract.py / 前端枚举双向钉

backend/tests/
├── test_subagent_ids.py
├── test_subagent_lineage.py         # ★ 血缘，升 LangGraph 版本必跑
├── test_subagent_loop_boundary.py
├── test_subagent_concurrency.py
├── test_subagent_sandbox.py
├── test_subagent_cancel.py          # ★ 取消父 run 后注册表和租约归零
├── test_subagent_stop_reason.py
├── test_subagent_error_marker.py
├── test_subagent_step_capture.py
├── test_subagent_status_contract.py
├── test_subagent_routing_prompt.py
├── test_delegation_ledger.py
└── test_subagent_event_buffer.py
```

标 ★ 的是本阶段新建。`thread_state.py` / `catalog.py` / `worker.py` 是填 M1/M4/M3 留下的槽。

---

## 2. 两个 ID，绝对不能合并

这是整个 M8 最便宜也最容易被跳过的一个决定。**D1 就要定下来，所有结构体按这两个字段建。**

| ID | 字段名 | 由谁生成 | 用途 |
|---|---|---|---|
| 对外相关键 | `tool_call_id` / `external_task_id` | **provider**（模型返回的） | `ToolMessage.tool_call_id`、`task_*` SSE 的 `task_id`、持久化 `metadata.task_id`、前端卡片、扩展契约的 `scope_id` |
| 注册表主键 | `execution_id` / `SubagentResult.task_id` | **服务端 `uuid4()`** | 进程内注册表、轮询、取消、超时、沙箱租约 owner、清理 |

**为什么不能合并**：provider 的 `tool_call_id` 在不同父 run 之间**不保证全局唯一**。
拿它当注册表所有权键，两个并发的 run 撞上同一个 id 时会互相取消对方的子代理——
而且这个 bug 只在高并发下偶发，事后从日志里几乎看不出来。

推论三条，写进 `executor.py` 模块 docstring：

1. **调度闭包要持有自己的 `SubagentResult` 对象**，不要再通过可变注册表反查所有权。
2. SSE / 持久化 / ToolMessage **对外只暴露 `tool_call_id`**。`execution_id` 不出进程。
3. 沙箱租约 id 用 `f"subagent:{execution_id}"`，不用 `tool_call_id`。

```python
# ✗ 每次都回注册表查，注册表在此期间可能已被别人改写
result = registry[execution_id]

# ✓ 闭包捕获结果对象本身
def _on_done(fut, result=result):
    result.try_set_terminal(...)
```

`try_set_terminal` 必须是**第一次终态获胜**：后台超时、父 run 取消、worker 正常结束
会竞态写同一个 holder。晚到的终态写不得改 `status` 或 payload。

---

## 3. 子代理不是一次新的 run

**子代理不占用 M3 定义的"每 thread 一条活跃 run"的名额。**

它是 lead 图**内部**的一次委托，不是新的 `/runs`。进度通过 root namespace 上的
`custom` SSE 事件汇报：

| custom `type` | 何时 | 持久化 `event_type` |
|---|---|---|
| `task_started` | 准入成功、即将开跑 | `subagent.start` |
| `task_running` | 每个新捕获的步骤 | `subagent.step` |
| `task_completed` | `status=completed` | `subagent.end` |
| `task_failed` | `status=failed` | `subagent.end` |
| `task_cancelled` | `status=cancelled` | `subagent.end` |
| `task_timed_out` | 执行超时 / 轮询超时 | `subagent.end` |

把子代理做成真正的 run 会立刻撞上 M3 的单活跃操作约束——lead 自己正占着名额，
它派出去的第一个子代理就会被拒。绕过那个约束等于放弃 M3 的并发模型。

`task` 对 lead 是**同步工具**：ToolNode 调它，它把协程提交到隔离循环，然后在
**父循环**上轮询 `SubagentResult` 直到终态，再返回 `Command` 写回 `ToolMessage`。
同一轮 lead `AIMessage` 里的多个 `task` 调用由 LangGraph ToolNode 并行，
这才是"并发子代理"的来源——不是再开 run。

### 3.1 流式透传时必须保留 namespace

如果要把子图的帧透传给客户端，SSE 事件名必须是 `values|<ns>`（LangGraph Platform 风格），
**不能是裸 `values`**。

被委托的子代理继承了父 checkpoint namespace。把它的 `values` 快照发成裸 `values`，
SDK 客户端会用**子代理的局部状态覆盖整个会话视图**——用户会看到自己的对话历史
突然变成子代理那几条消息。

对称的要求：所有只关心根帧的消费者都要**忽略带 namespace 的帧**。至少这几个：

- 文件工具的分块合批（M2）
- 子代理事件持久化（否则子代理事件会被自己二次记录）
- LLM 错误降级检测（M6）
- RunJournal 的消息落账（M4）——子代理步骤走 `category="subagent"`，不进 `list_messages`

M3 的 StreamBridge 已经按 `(mode, ns)` 分流；M8 要加一条回归：
父 `messages` 流里**不准出现**子代理的 `AIMessage` / `ToolMessage`。
只断言子 `RunnableConfig` 里没有四个坐标键是不够的，那只能防住改代码，
防不住升级 LangGraph（见 §7.3）。

---

## 4. 配置（D1）

### 4.1 YAML 形状

```yaml
# config.yaml —— 热重载字段，除非标了 startup-only
subagents:
  timeout_seconds: 1800          # 内置子代理默认墙钟上限（秒）
  max_turns: null                # null = 用 builtin 自己的值（general-purpose=150, bash=60）
  max_total_per_run: 6           # 一次父 run 累计委派上限，范围 1–50
  token_budget:
    enabled: true
    # max_tokens 默认跟 summarization.enabled 耦合：开压缩 1_000_000，关 2_000_000
    # 用户显式写了 token_budget.max_tokens 则永远以用户值为准
    warn_threshold: 0.7
  agents:                        # 按名字覆盖 builtin / custom
    general-purpose:
      timeout_seconds: 1800
      max_turns: 150
  custom_agents:                 # 用户定义的新类型
    researcher:
      description: "…"
      system_prompt: "…"
      tools: null                # null = 继承 lead 当时持有的全部工具
      disallowed_tools: ["task", "ask_clarification"]
      skills: null               # null = 全部启用技能；[] = 禁用技能
      model: inherit
      max_turns: 80
      timeout_seconds: 900

# 进程级容量 —— STARTUP_ONLY，改完必须重启 Gateway
subagent_runtime:
  max_running: 3                 # 同时在跑几个，范围 1–64
  max_queued: 64
  admission_policy: queue        # queue | reject
  queue_timeout_seconds: 300
```

### 4.2 两个独立上限，不要合成一个

| 上限 | 配置 | 约束的是 | 默认 | schema |
|---|---|---|---|---|
| 进程并发 | `subagent_runtime.max_running` | **同时**在跑几个（进程级） | 3 | 1–64，**启动冻结** |
| 单轮并行 | 解析后的 `max_concurrent` | 同一条 `AIMessage` 里保留几条 `task` | = `max_running` | 取 min(请求, 冻结值, 64) |
| 单 run 总量 | `subagents.max_total_per_run` | 一次父 run **累计**派了几个 | 6 | 1–50，可热重载 |

只有并发上限是不够的：一个长 lead run 可以在每个规划检查点都发起一批合法大小的委派，
跑一整天。总量上限堵的就是这个。

总量按**当前 run** 的委派台账计（`DelegationEntry.run_id`），
**同 thread 里历史 run 的委派不消耗新 run 的预算**。

并发数一次解析为三者的最小值，**提示词、Limit 中间件、进程控制器必须读同一个数**：

```python
def effective_subagent_concurrency(requested: int | None, *, max_running: int) -> int:
    upper = min(64, max(1, max_running))
    value = max_running if requested is None else int(requested)
    return max(1, min(upper, value))
```

**热重载不能让任何一层宣告比已创建的进程控制器更多的容量**——
提示词说"你可以并发 8 个"而控制器只有 3 个槽，模型会一直撞墙。
`subagent_runtime.*` 进 M0 的 `STARTUP_ONLY_FIELDS`：

```python
STARTUP_ONLY_FIELDS["subagent_runtime.max_running"] = (
    "进程级准入控制器在 Gateway 启动时构建；热重载改 YAML 不会重建它。"
)
STARTUP_ONLY_FIELDS["subagent_runtime.max_queued"] = (
    "队列容量与控制器一起冻结。"
)
STARTUP_ONLY_FIELDS["subagent_runtime.admission_policy"] = (
    "queue/reject 是控制器构造参数。"
)
STARTUP_ONLY_FIELDS["subagent_runtime.queue_timeout_seconds"] = (
    "排队超时是控制器构造参数。"
)
```

### 4.3 `SubagentConfig` vs YAML

注册表吐出的是 dataclass，和 YAML pydantic 模型分开，避免把校验框架泄漏进执行器：

```python
@dataclass
class SubagentConfig:
    name: str
    description: str
    system_prompt: str | None = None
    tools: list[str] | None = None          # None = 继承
    disallowed_tools: list[str] | None = field(default_factory=lambda: ["task"])
    skills: list[str] | None = None         # None = 全部启用；[] = 禁用
    model: str = "inherit"
    max_turns: int = 50
    timeout_seconds: int = 900
```

解析模型名：

```python
def resolve_subagent_model_name(config: SubagentConfig, parent_model: str | None, *, app_config: AppConfig) -> str:
    if config.model != "inherit":
        return config.model
    if parent_model is not None:
        return parent_model
    if not app_config.models:
        raise ValueError("No chat models are configured.")
    return app_config.models[0].name
```

子代理**不继承请求级密钥**（M7 §6）。`model: inherit` 只继承**名字**，
走和 lead 相同的 `create_chat_model(name=...)`，凭据仍来自服务端配置。

---

## 5. 注册表与内置类型（D2）

### 5.1 解析顺序

```
内置 BUILTIN_SUBAGENTS
  → config.yaml subagents.custom_agents[name]     # 同名覆盖内置
  → config.yaml subagents.agents[name]            # 超时 / 轮次 / 模型 / token_budget 覆盖
```

M8 **不做**管理员托管定义表（DeerFlow 的 `persistence/managed_subagents/`）。
部署级自定义走 `custom_agents` 即可。同名冲突：后写的 YAML 赢，内置被覆盖时打 warning。

```python
def get_subagent_config(name: str, *, app_config: AppConfig) -> SubagentConfig | None:
    builtin = BUILTIN_SUBAGENTS.get(name)
    custom = _from_custom_agents(name, app_config)
    base = custom or builtin
    if base is None:
        return None
    return _apply_overrides(base, app_config.subagents.agents.get(name), app_config.subagents)

def list_subagent_names(*, app_config: AppConfig, allowed: list[str] | None) -> list[str]:
    names = [*BUILTIN_SUBAGENTS, *app_config.subagents.custom_agents]
    if allowed is None:
        return names
    if allowed == []:
        return []
    allowed_set = set(allowed)
    return [n for n in names if n in allowed_set]
```

`allowed_subagents` 见 §16：`None` = 全部，`[]` = 硬拒绝，列表 = 白名单。
**提示词发现和 `task` 执行必须用同一份快照。**

### 5.2 两个内置

**`general-purpose`**

- `tools=None`（继承 lead 当时持有的工具，再扣 `disallowed_tools`）
- `disallowed_tools=["task", "ask_clarification"]`——**禁止嵌套委派和向用户提问**
- `max_turns=150`，`model="inherit"`
- 系统提示词必须写明：你没有 `task`；不要试图再派子代理；不要问澄清

**`bash`**

- `tools=["bash", "ls", "read_file", "write_file", "str_replace"]`（有则留，无则跳过）
- 同样禁止 `task` / `ask_clarification`
- `max_turns=60`
- 描述里写明：常规 git/build/test/deploy **不够**成为委派理由

`bash` 类型在 host bash 未允许且当前沙箱不是隔离 shell 时，注册表应把它从
**发现列表**里拿掉（执行时再碰到要返回"可用类型是 …"）。不要让模型看见却调不动。

### 5.3 工具过滤

```python
def filter_tools(all_tools: list[BaseTool], allowed: list[str] | None, disallowed: list[str] | None) -> list[BaseTool]:
    filtered = all_tools
    if allowed is not None:
        allowed_set = set(allowed)
        filtered = [t for t in filtered if t.name in allowed_set]
    if disallowed is not None:
        denied = set(disallowed)
        filtered = [t for t in filtered if t.name not in denied]
    return filtered
```

过滤发生在执行器构造时，基于 **dispatch 那一刻 lead 持有的工具列表**。
不要在子代理循环里再调一次 `get_available_tools()`——那会看到热重载后的另一代 MCP 工具。

M7 的技能策略对子代理同样生效：`task` **不是**框架豁免名（M7 §3.8）。
子代理链上仍然挂 `SkillActivation` + `SkillToolPolicy`。因为内置已经把 `task`
放进 `disallowed_tools`，策略层即使有人声明 `task` 也过滤不到它。

### 5.4 技能可见性跟父身份走

子代理解析技能必须用父 runtime 的 `user_id` 调 `get_or_new_user_skill_storage(user_id)`，
没有身份时才退到 `DEFAULT_USER_ID`。不要读全局-only catalog，否则自定义技能的
shadowing 和 lead 对不齐。

---

## 6. 台账：`ThreadState.delegations`（D1 / D6）

M1 已经为 `delegations` 留了 reducer 槽。M8 把条目形状和"终态不可降级"钉死。

### 6.1 `DelegationEntry`

```python
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out", "polling_timed_out"})
_DELEGATION_LEDGER_MAX_ENTRIES = 50

class DelegationEntry(TypedDict):
    id: str                          # = tool_call_id，对外相关键
    run_id: NotRequired[str]         # 当前父 run；总量上限按这个过滤
    description: str                 # 截断到 200 字
    subagent_type: str
    status: str                      # in_progress | completed | failed | cancelled | timed_out | polling_timed_out
    result_brief: NotRequired[str]   # 确定性头尾截断，最多 2000 字，不是 LLM 摘要
    result_sha256: NotRequired[str]  # 64 位小写 hex；读侧校验形状
    stop_reason: NotRequired[str]    # token_capped | turn_capped | loop_capped
    created_at: str                  # 首次写入的 ISO8601；后续合并保留
```

**state 里不放完整结果。** 完整文本在 `ToolMessage.content` 和 `subagent.end` 事件里。
台账只放 brief + sha256，避免 checkpoint 被 50 次委派撑爆（M1 §2.3）。

M8 **不**往条目里写 `receipt_verdict` / `acceptance_verdict`。TypedDict 预留
`NotRequired` 可以，但执行器不要填——填了前端会当成已校验。

### 6.2 reducer：同 id 最新赢，终态不可降级，有界

```python
def merge_delegations(existing, new):
    """
    - new None/[] → 保留 existing（节点没碰）
    - 按 id 合并，首次出现顺序保留
    - 已是终态的条目，拒绝被非终态覆盖（取消竞态：worker 晚到的 in_progress 不能复活）
    - created_at / run_id 以首次为准
    - 超过 50 条截最老的
    """
    if not new:
        return existing or []
    by_id: dict[str, DelegationEntry] = {}
    order: list[str] = []
    for entry in [*(existing or []), *new]:
        entry_id = entry["id"]
        previous = by_id.get(entry_id)
        if previous is not None and previous["status"] in TERMINAL_STATUSES and entry["status"] not in TERMINAL_STATUSES:
            continue
        if entry_id not in by_id:
            order.append(entry_id)
        elif previous.get("created_at"):
            entry = {**entry, "created_at": previous["created_at"]}
            if previous.get("run_id") and not entry.get("run_id"):
                entry["run_id"] = previous["run_id"]
        by_id[entry_id] = entry
    merged = [by_id[i] for i in order]
    return merged[-_DELEGATION_LEDGER_MAX_ENTRIES:] if len(merged) > _DELEGATION_LEDGER_MAX_ENTRIES else merged
```

M1 的 stub 只写了"按 id 合并 + 截断"。**终态不可降级**是 M8 新加的不变量，
测试必须覆盖：`completed` 之后再来一条同 id 的 `in_progress`，结果仍是 `completed`。

### 6.3 谁写入

两条路径，最终都进 reducer：

1. **`DurableContextMiddleware` 捕获**（M6 §6.2 已留空抽取）：压缩之前扫 `messages`，
   把成对的 `task` AI 调用 + ToolMessage 抽成条目。进行中的调用（还没有 ToolMessage）
   也要记一条 `in_progress`，否则压缩会把"已经派出去"的记忆丢掉，lead 会再派一次。
2. **`task` 工具返回 `Command(update={"messages": [ToolMessage], "delegations": [entry]})`**。
   终态条目与 ToolMessage 同一拍写入。

抽取必须用 `tool_call_id` 配对，不要靠"最后一条 ToolMessage"。一轮里可以有 3 个并行 `task`。

### 6.4 投影给模型看

`delegation_ledger.render_delegation_ledger(entries)` 产出一段有界文本（建议 6000 字预算），
由 DurableContext 放进**隐藏 HumanMessage 数据块**（M6 §4.5），**不要**放进 SystemMessage。
委派结果是模型自己写的，属于不可信通道。

命中护栏时渲染一句面向模型的指引，不能让 lead 把部分结果当成干净完成：

```
hit a guardrail cap with a partial result; reuse it, retry tighter, or raise
the per-agent budget (max_turns / token_budget)
```

`in_progress` 条目渲染：`already delegated; do NOT delegate again; wait for or build on the result`。

---

## 7. 执行器：三个跨边界问题（D3–5）

这是 M8 真正难的地方。子代理运行在一个**独立的持久事件循环**上，
和父 run 的循环之间有三条边界要处理。

### 7.1 为什么要独立循环

子代理是一个完整的 agent 图，会跑几十上百轮。放在父 run 的循环上意味着
它的每一次 `await` 都和父 run 的流式推送、心跳、SSE 消费抢同一个循环。
更麻烦的是父 run 结束时循环会被拆掉，而子代理的清理逻辑还需要跑。

方案：**一个进程级的持久隔离循环**，所有子代理协程提交到它上面。

```python
_isolated_loop: asyncio.AbstractEventLoop | None = None
_isolated_thread: threading.Thread | None = None
_isolated_lock = threading.Lock()

def _run_loop(loop, started: threading.Event) -> None:
    asyncio.set_event_loop(loop)
    loop.call_soon(started.set)
    try:
        loop.run_forever()
    finally:
        started.clear()

def get_isolated_subagent_loop() -> asyncio.AbstractEventLoop:
    """懒启动；daemon 线程；atexit 里 stop + join(1s) + close。"""
    ...

def run_on_isolated_subagent_loop(coro) -> Future:
    """把清理任务钉在隔离循环上。父循环的 create_task 会随 asyncio.run() 一起被取消。"""
    return asyncio.run_coroutine_threadsafe(coro, get_isolated_subagent_loop())
```

提交协程的顺序必须是：**先拿到 loop，再创建 coro**。写成
`run_coroutine_threadsafe(factory(), loop)` 时 Python 先求值 `factory()`，
loop 启动失败会留下一个从未 await 的协程（`RuntimeWarning` + 捕获对象泄漏）。
`run_coroutine_threadsafe` 自己失败时（loop 在 lookup 和 schedule 之间关掉），
要对已创建、未启动的 coro 调 `close()`。

`atexit` 关循环：`call_soon_threadsafe(loop.stop)`，join 1 秒，thread 停了且
loop 不再 running 才 `close()`。否则打 warning，不要在仍 running 的 loop 上 close。

### 7.2 边界一：ContextVar 要拷贝，但要摘掉绑定循环的回调

提交前把环境 ContextVar 拷贝进隔离循环，这样 checkpoint 血缘、用户身份、
追踪上下文、tags/metadata、以及 LangGraph 自己的 namespace 消息流处理器都能存活。

**但要摘掉标记为"绑定父循环"的回调处理器。** 典型的就是 RunJournal——
它持有父循环上的任务和一个 SQL 连接池。让它跨循环会导致：

- 重复计账（LangGraph 会把继承的回调和子 run 自己的 `SubagentTokenCollector` 合并）
- `Future attached to a different loop` 崩溃

**但也不能图省事传一个空白 `Context`**：继承的 checkpoint namespace 和框架的
流处理回调是 §3.1 那个契约的前提，丢了它们子代理的帧就发不出去了。

做法是给需要摘除的处理器打一个显式标记 `deerflow_loop_bound = True`
（包名仍用这个标记字符串，兼容以后对照 DeerFlow；不要维护类名黑名单）：

```python
def copy_isolated_subagent_context() -> Context:
    context = copy_context()
    inherited = context.get(var_child_runnable_config)
    if inherited is None or "callbacks" not in inherited:
        return context
    callbacks = inherited.get("callbacks")
    if isinstance(callbacks, BaseCallbackManager):
        isolated = callbacks.copy()
        isolated.handlers = [h for h in callbacks.handlers if not getattr(h, "deerflow_loop_bound", False)]
        isolated.inheritable_handlers = [h for h in callbacks.inheritable_handlers if not getattr(h, "deerflow_loop_bound", False)]
    elif isinstance(callbacks, (list, tuple)):
        isolated = [h for h in callbacks if not getattr(h, "deerflow_loop_bound", False)]
    elif getattr(callbacks, "deerflow_loop_bound", False):
        isolated = None
    else:
        isolated = callbacks
    cfg = inherited.copy()
    if isolated:
        cfg["callbacks"] = isolated
    else:
        cfg.pop("callbacks", None)
    context.run(var_child_runnable_config.set, cfg)
    return context
```

M4 的 `RunJournal` 构造时设置 `self.deerflow_loop_bound = True`。
`SubagentTokenCollector` **不要**打这个标记——它就是要在隔离循环上跑的。

M8 **不做**把 loop-detection / tool-promotion 审计跨回父 journal 的 proxy。
子代理的护栏 `consume_stop_reason` 在隔离循环本地读即可。父 run 的 middleware
审计事件只记录 lead 自己的决策。需要跨循环审计时再加一条窄接口
（只暴露 `record_middleware`，禁止把 `RunJournal` 本身传进隔离循环）。

### 7.3 边界二：checkpoint 血缘——**不要**在子配置里重新指定坐标

这是一个纯粹靠踩坑才知道的点：

```python
# ✗ 即使传的是同一个 parent thread_id，也会开启一条新的根血缘
child_config = {"configurable": {"thread_id": parent_thread_id, ...}}

# ✓ 完全省略 checkpoint 坐标键，让 LangGraph 从拷贝来的 ContextVar 继承
CHECKPOINT_COORDINATE_KEYS = ("thread_id", "checkpoint_ns", "checkpoint_id", "checkpoint_map")

child_config = {
    "configurable": {k: v for k, v in parent_configurable.items() if k not in CHECKPOINT_COORDINATE_KEYS},
    "recursion_limit": max_turns,
}
```

显式重新提供它们（**哪怕值完全一样**）会在较新的 LangGraph（1.2.6+）上开启一条新的根血缘，
子代理就失去了非根的子图 namespace——然后它的 AI/tool 帧会被路由进**父 run 的
`messages` 流**，回到 §3.1 那个"覆盖整个会话视图"的故障。

业务组件（沙箱、中间件、归属统计）仍然需要父 `thread_id`，
但它们应该从 `runtime.context` 拿，不是从 `RunnableConfig`。

这条要写一个**两层**回归测试：

1. 调用契约：子 `astream(...)` 收到的 config 不含四个键（防自己改代码）。
2. 生产形状：驱动一个真实的父图，断言子帧**没有**出现在父 `messages` 流里
   （防升级 LangGraph）。第 2 层对 LangGraph 1.2.6+ 启用。

### 7.4 边界三：子代理图用 `checkpointer=False` 编译

子代理是一次性的，永远不会 resume。继承父 run 的 checkpointer 只会把
子代理的中间状态写进父 thread 的 checkpoint 表，污染历史又浪费存储。

```python
agent = create_agent(
    model=model,
    tools=bound_tools,
    middleware=middlewares,
    system_prompt=None,          # 系统提示放进 initial state，避免两条 SystemMessage
    state_schema=ThreadState,
    checkpointer=False,
)
```

`system_prompt=None` 是故意的：initial state 已经有一条框架 SystemMessage，
再让 `create_agent` 插一条，严格后端会 400。日期提醒由
`SubagentDateContextMiddleware` 注入后再交给 `SystemMessageCoalescingMiddleware`
合成一条（§12.3）。

### 7.5 反向边界：延迟清理钉隔离循环，最终用量交回父循环

轮询器意外退出时，注册表清理要提交到**隔离循环**执行，
因为 `asyncio.run()` 退出时会取消调用方循环上的任务——
在那里 `create_task` 的清理逻辑会在跑起来之前就被取消。

而最终的 token 用量报告要走**相反方向**：在展开时捕获父 run 的循环，
用 `call_soon_threadsafe` 交回去。原因是账本的累加器是无锁的读-改-写字段，
从隔离循环或工作线程写它会静默丢失 token 更新。

```python
def _deliver_final_usage_report(records, *, report_loop, recorder) -> None:
    if report_loop is None or report_loop.is_closed():
        logger.info("drop subagent usage: parent loop closed (execution_id=%s, n=%s)", ...)
        return
    report_loop.call_soon_threadsafe(recorder.record_external_llm_usage_records, records)
```

清理闭包只捕获"解析好的用量记录器 + 几个 id + 捕获的循环"，
**不要捕获整个 runtime**——清理任务可能存活到完整的轮询预算结束，
通过 runtime 它会把父 run 的账本和事件存储钉住那么久。

父循环已经关掉（同步 `asyncio.run` 拆掉）时，报告故意丢弃并打 info：
run 已经把 completion data 持久化了，没有人会再读这些计数器；
注册表条目紧接着删除，这些 records 无处可去。

---

## 8. `SubagentResult` 与进程内注册表

```python
class SubagentStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED, self.TIMED_OUT}


@dataclass
class SubagentResult:
    task_id: str                          # execution_id，注册表主键
    trace_id: str
    status: SubagentStatus
    external_task_id: str | None = None   # provider tool_call_id
    result: str | None = None
    error: str | None = None
    stop_reason: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    token_usage_records: list[dict] = field(default_factory=list)
    admission_failure: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _state_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def try_set_terminal(self, status, **payload) -> bool:
        """第一次终态获胜；非终态参数直接 ValueError。"""
        ...

    def update_token_usage_records(self, records) -> None:
        """仍在跑时发布累计快照，供下一条 task_running 携带。终态后忽略。"""
        ...
```

对外契约的 `polling_timed_out` 是 **ToolMessage 上的 status**，不是
`SubagentStatus` 的成员——执行侧仍是 `TIMED_OUT`，由 `task` 工具区分
"子代理自己墙钟到了"还是"父侧轮询预算到了"。

注册表是进程内 dict，锁用 `threading.Lock`（取消回调可能从别的线程进来）：

```python
_registry_lock = threading.Lock()
_registry: dict[str, SubagentResult] = {}

def register(result: SubagentResult) -> None: ...
def get(execution_id: str) -> SubagentResult | None: ...
def cleanup(execution_id: str) -> None: ...          # 终态后摘掉
def request_cancel(execution_id: str) -> None: ...   # 设 cancel_event；Future.cancel 在释放锁之后
def cancel_all_for_run(run_id: str) -> None: ...     # 父 run 取消时扇出
```

**取消必须在释放注册表锁之后才调 `Future.cancel()`。** 取消可能同步触发
完成回调，回调会再拿同一把锁 → 死锁。

`task` 工具在发出 `task_started` 失败时也要走这条清理：那是真正开跑前的 await 点，
漏了会留下一个永远 RUNNING 的注册表条目和租约。

---

## 9. `task` 工具（D2 骨架，D3 接执行器）

```python
@tool("task", parse_docstring=True)
async def task_tool(
    runtime: Runtime,
    prompt: str,
    subagent_type: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
    *,
    description: str = "",
) -> Command:
    """Delegate a bounded task to a specialized subagent in its own context.
    ... 与 lead 提示词、内置 description 同一套收益/否决/成本语言 ...
    """
```

参数刻意精简。M8 **不**收 `acceptance_criteria`——那条通道一旦出现，lead 会以为
有代码在做确定性验收。

### 9.1 调度步骤（父循环）

1. 从 `runtime.context` 读 `run_id` / `thread_id` / `user_id` / `allowed_subagents` 快照 /
   父模型名 / 工具列表 / `uploaded_files`。缺 `run_id` 打 warning：Limit 中间件会把
   整个 thread 的历史委派都算进本 run（偏严，不偏松）。
2. `get_subagent_config(subagent_type)`；未知类型返回错误 ToolMessage，列出允许的名字。
3. 白名单拒绝 → 同样错误 ToolMessage，不启动执行器。
4. `deepcopy(uploaded_files)`。`[]` 是合法快照（"本 run 的上传全是历史"），必须保留。
   缺失或畸形 → 子代理禁用 `list_uploaded_files`（fail closed）。
5. 构造 `SubagentResult(task_id=uuid4(), external_task_id=tool_call_id, ...)`，
   闭包捕获这个对象，登记进注册表。
6. `copy_isolated_subagent_context()` + 提交 `executor._aexecute(prompt, result_holder=result)`。
7. `aemit_custom_event({"type": "task_started", "task_id": tool_call_id, "description": ..., "model_name": ...})`。
8. 轮询：`asyncio.sleep(poll_interval)` 看 `result.status`，新步骤发 `task_running`
   （`task_id` 仍是 `tool_call_id`）。轮询预算 = `config.timeout_seconds + 余量`。
9. 终态 → 发对应 `task_*` → `_report_subagent_usage`（在父循环）→ `cleanup` →
   返回 `Command`。

轮询间隔建议 0.2–0.5s。不要用 `Future.result(timeout=)` 堵父循环——那样发不出
`task_running`，前端卡片会一直转圈。

### 9.2 返回 `Command`，不是纯字符串

```python
def _task_result_command(*, tool_call_id, status, result=None, error=None, stop_reason=None, model_name=None, usage=None) -> Command:
    content = format_subagent_result_message(status, result=result, error=error, stop_reason=stop_reason)
    kwargs = make_subagent_additional_kwargs(
        status=status,
        error=error,
        result=result,
        stop_reason=stop_reason,
        model_name=model_name,
        usage=usage,
    )
    entry = make_delegation_entry(...)
    return Command(update={
        "messages": [ToolMessage(content=content, tool_call_id=tool_call_id, additional_kwargs=kwargs)],
        "delegations": [entry],
    })
```

`format_subagent_result_message` 是模型可见散文，例如
`Task Succeeded (capped: token budget). Result: ...`。
运行时消费者**只读** `additional_kwargs`，禁止解析散文当状态协议。

### 9.3 跨语言状态契约

`contracts/subagent_status_contract.json`：

```json
{
  "version": 2,
  "valid_status_values": ["completed", "failed", "cancelled", "timed_out", "polling_timed_out"],
  "valid_stop_reason_values": ["token_capped", "turn_capped", "loop_capped"]
}
```

`status_contract.py` 的元组必须和这份 JSON 双向 assert。前端 TypeScript 枚举同样钉住。
**不要**再加 `max_turns_reached` 这种状态值——那是 DeerFlow 已经删掉的 Phase 1 错误。
读侧可以把历史 checkpoint 里的 `max_turns_reached` 映射成 `failed` + `turn_capped`，
写侧永不产出。

`additional_kwargs` 键名一次性定死：

| 键 | 谁读 |
|---|---|
| `subagent_status` | 前端卡片、台账、Limit 中间件不读这个（读 state.delegations） |
| `subagent_stop_reason` | 可选 |
| `subagent_error` | 失败类 |
| `subagent_result_brief` / `subagent_result_sha256` | 仅 `completed` |
| `subagent_model_name` | 卡片；刷新后仍在 |
| `subagent_token_usage` | `{input_tokens, output_tokens, total_tokens}`；provider 没给就**缺省**，不要写 0 |

Gateway 从 checkpoint 回放时必须剥掉调用方伪造的这些键（和 M4 剥伪造字段同一纪律）：
只接受本进程 `task` 工具盖过的值。

---

## 10. 并发控制（D6）

### 10.1 `SubagentRuntime`：直接调用也要共享控制器

Gateway 启动时建一个进程级 `SubagentRuntime.from_app_config(app_config)`。
`create_agentkit_agent(..., subagent_runtime=runtime)` 必须把**同一个实例**
传给 `task` 闭包、Limit 中间件、Executor。直接调 factory 的测试如果每次 `new`
一个 Runtime，就会有 N 套各 3 槽的控制器，上限形同虚设。

```python
class SubagentRuntime:
    def __init__(self, config: SubagentRuntimeConfig, *, max_total_per_run: int = 6, app_config: AppConfig | None = None):
        self.config = config.model_copy(deep=True)
        self.max_total_per_run = max_total_per_run
        self.app_config = app_config
        self.execution_capacity = SubagentExecutionCapacity(self.config)
```

### 10.2 异步 FIFO 准入

```python
class SubagentExecutionCapacity:
    def __init__(self, config: SubagentRuntimeConfig) -> None:
        self._config = config
        self._lock = asyncio.Lock()
        self._running = 0
        self._waiters: deque[asyncio.Future[None]] = deque()

    @asynccontextmanager
    async def slot(self):
        await self._acquire()
        try:
            yield
        finally:
            await self._release()
```

两条硬要求：

- **等待者不占调度线程**（纯 async Future，不是 `threading.Semaphore` 堵隔离循环）
- **取消和超时必须释放队列位置和槽位所有权**

`snapshot()` 可能从非 loop 线程读（配置热路径）。不要迭代 `_waiters`；
用 `len(self._waiters)`——deque 的长度读取是原子的。超时后尚未把自己从队列摘掉
的 waiter 会让 snapshot 偏忙，这比偏闲安全。

准入失败（队列满 / 排队超时）发生在**跑模型之前**：
`try_set_terminal(FAILED, error=..., admission_failure=True)`。
`task` 工具把它映射为普通失败 ToolMessage，不消耗"成功委派"语义。
总量台账仍记一条 `failed`，避免模型原地重试把队列打满。

### 10.3 槽位耗尽时不要报错——剥调用

这是 **lead 图**上 `SubagentLimitMiddleware.after_model` 的职责，不是执行器的：

1. 数本轮 `tool_calls` 里 `name=="task"` 的下标
2. `prior = count(delegations where run_id == current_run_id)`
3. `allowed = min(max_concurrent, max(0, max_total - prior))`
4. 超出的 task 调用丢掉；用 `clone_ai_message_with_tool_calls` 换掉最后一条 AIMessage
   （同一 `id` 触发 `add_messages` 替换，见 M6 dangling 的同一技巧）
5. **同步 provider 的原始 tool_call 元数据**（否则留下悬空调用，M6 §5.1）
6. `allowed == 0` 时强制 `finish_reason="stop"`，追加可见说明：

```
[SUBAGENT LIMIT REACHED] The subagent delegation limit for this run has been reached.
Continue using the subagent results already collected, execute remaining simple work
directly, or summarize the remaining work instead of launching more subagents.
```

这样模型会拿已经收到的结果去综合，而不是看到一个错误然后重试。

`max_concurrent` 在组装 lead 链时就 clamp 到进程 `max_running`。中间件构造函数
不要自己再读 YAML。

缺 `run_id` 时把 **整个 thread** 的 delegations 都算进 prior（偏严）。
Gateway `run_agent()` 和嵌入 `stream()` 都必须把 `run_id` 放进 runtime context。
**Resume 路径可能不追加新的 HumanMessage**，所以 worker 还要把"本 run 开始前的
checkpoint 消息 id 集合"放进 context，DurableContext 用它当当前请求边界，
绝不把更老的 task 调用重新打成当前 run。

---

## 11. 共享沙箱的生命周期（D7）

M2 的模型是"中间件 acquire、中间件 release"。子代理和 lead **共用同一个 thread 沙箱**，
这个模型立刻破功：**一个子代理跑完了，不能把沙箱关掉——兄弟还在跑。**

### 11.1 引用租约

每个准入的子代理携带稳定的、从 `execution_id` 派生的：

```python
sandbox_lease_owner_id = f"subagent:{result.task_id}"
sandbox_command_scope_id = sandbox_lease_owner_id   # M8 两者相同即可
```

放进子代理 `runtime.context`。`SandboxLeaseManager` 按这个 id 对 **lead 当前的
provider 客户端**登记一次持有。**最后一个持有者**执行真正的 `provider.release`。

```python
class SandboxLeaseManager:
    async def retain(self, owner_id: str, *, client, releasing: bool = True) -> None: ...
    async def release_async(self, owner_id: str) -> None: ...   # 幂等
```

Lead 的 `SandboxMiddleware` 是 **owner**（`releasing=True`）。
子代理是 **non-owner 持有者**：挡住父清理、拥有自己的 command scope，
但自己不发起 park。父的 park 请求等到最后一个子代理释放。

M2 文档里"release 不能由单个工具触发"升格为："release 不能由单个**执行**触发"。
工具级 `Command` 包装器永远不 release——兄弟 handler 可能还在跑。

### 11.2 命令作用域

每个子代理有自己的 `command_scope_id`。在支持持久 shell 会话的 provider 上，
这意味着每个子代理挑一个独立的 shell 会话——独立作用域可以并发跑，
而单个子代理内部的 shell 状态仍然保序。

### 11.3 `to_thread` 里的同步调用要 shield 并排空

否则一个被取消的 worker 可能在子代理已经终态化之后才真正执行完，
去释放一个已经被释放的持有者（再 admit 一个已释放的 owner，或在终态后跑沙箱调用）。

```python
# 外层执行在进入 finally 释放租约之前：
await _drain_shielded_sandbox_work()
```

### 11.4 释放要幂等，且在 `finally` 里重复一次

中间件正常路径会释放，但异常、协作式取消、超时展开这三条路径都可能绕过它。
执行器在 `finally` 里再 `release_async(owner_id)` 一次（幂等），才能保证不泄漏租约。

---

## 12. 子代理中间件链（D9）

M6 已经把组装分成 `build_runtime_middlewares`（共享）和 `build_middlewares`（lead-only）。
M8 加第三条：

```python
def build_subagent_runtime_middlewares(
    *,
    app_config: AppConfig,
    model_name: str | None,
    lazy_init: bool = True,
    deferred_setup: DeferredToolSetup | None = None,
    agent_name: str | None = None,
    available_skills: set[str] | None = None,
    user_id: str | None = None,
    skip_memory_flush: bool = True,          # 子代理路径必须 True
) -> list[AgentMiddleware]:
```

### 12.1 和 lead 链的差异（对照着抄，不要"减几条"靠感觉）

| 条目 | lead | 子代理 |
|---|---|---|
| UploadsMiddleware | 有 | **无**（`include_uploads=False`）；靠 dispatch 快照 |
| TitleMiddleware | 有 | 无 |
| ClarificationMiddleware | 最后一条 | 无（工具集已禁 `ask_clarification`） |
| SubagentLimitMiddleware | 有 | 无（没有 `task`） |
| DynamicContextMiddleware | 有（日期 + 将来的 memory） | **无**；改挂 `SubagentDateContextMiddleware` |
| Memory flush hook | 压缩时按 `thread_id` 刷 | **`skip_memory_flush=True`** |
| SkillActivation / Policy | 有 | 有，技能集 = 该子代理配置的白名单 |
| DurableContext + Summarization | 有 | 有，**同一** `summarization.enabled` |
| SystemMessageCoalescing | 有 | 有，且日期中间件紧挨它之前 |
| LoopDetection / TokenBudget | 有 | 有；token 默认跟压缩开关耦合 |
| SafetyFinishReason | 有 | 有 |
| SandboxMiddleware | owner | non-owner + 租约 id |

`skip_memory_flush=True` 的原因：lead 的压缩钩子按 `thread_id` 刷长期记忆。
子代理共享父 `thread_id`，不跳过的话**子代理内部轮次会污染父 thread 的记忆**。
M10 才真正接 memory，但 factory 参数 M8 就要有，默认 True。

### 12.2 压缩在子代理链上的位置

lead：压缩在护栏 trio **之前**。
子代理：压缩在护栏 trio **之后**。

这是 benign 的：Summarization 只实现 `before_model`，没有 `after_model` /
`consume_stop_reason`，不会干扰 D9 的 stop_reason 通道。不要为了"两边长得一样"
去改 lead 顺序。

DurableContext 必须紧挨压缩之前（M6 §6.2）：压缩后 `summary_text` 要投影回去，
否则 message-count keep 可能留下 assistant/tool 尾巴，严格后端 assistant-first 400。
DurableContext 会再插一条权威 SystemMessage，所以 Coalescing 必须无条件在最内层，
否则 #4039 的 400 会变成重复 system 的 400。

### 12.3 子代理日期中间件

不要复用 lead 的 `DynamicContextMiddleware`。那条会读 `AppConfig.memory`、
改 HumanMessage、处理午夜生命周期。子代理只要一行 `<current_date>`。

`SubagentDateContextMiddleware`：一次性 `before_agent`，插入一条隐藏的、
框架所有的 `SystemMessage`（只含日期）。不读 memory，不改任务 HumanMessage。
时区与 lead 相同：环境变量 `DEER_FLOW_DATE_TIMEZONE`（M6 §6.1），不要做成 YAML 字段。

挂在 `SystemMessageCoalescingMiddleware` **紧前面**，让 coalescer 把静态角色提示
和日期合成一条前导 SystemMessage。

### 12.4 延迟 MCP 工具：每次 task 一份新鲜 `ThreadState`

`SubagentExecutor._build_initial_state` 在子代理名字的 allow/deny 和组装期授权之后，
调共享的 `assemble_deferred_tools`，挂上 `tool_search`，把
`<available-deferred-tools>` 写进子代理 SystemMessage。

每次 task 都是一份**新鲜** `ThreadState`，`promoted` 从空开始。
子代理提升的 MCP 工具不会漏回父会话；父提升的也不会自动出现在子代理里
（子代理若需要，自己 `tool_search`）。

技能策略仍然在提升之后生效：`tool_search` 可以披露/提升 catalog 元数据，
但 `SkillToolPolicyMiddleware` 仍会拆掉当前技能没声明的业务工具（M7 §3.8）。

### 12.5 护栏 duck typing

```python
self._stop_reason_middlewares = [m for m in middlewares if hasattr(m, "consume_stop_reason")]
# 跑完之后：
stop_reason = next((m.consume_stop_reason(run_id) for m in self._stop_reason_middlewares if m.consume_stop_reason(run_id)), None)
```

执行器不 import `TokenBudgetMiddleware` / `LoopDetectionMiddleware`。
以后加一条轴，执行器一行都不用改。

Token 预算默认值：

- `summarization.enabled=True` → `max_tokens=1_000_000`
- 否则 → `2_000_000`
- 用户在 YAML 里显式写了 `subagents.token_budget` → **永远用用户值**

`TokenBudgetMiddleware` / `LoopDetectionMiddleware` 的硬停**不抛异常**：
剥掉本轮 `tool_calls`、强制 `finish_reason="stop"`，让本轮自然产出最终答复。
状态键是 `run_id`；每次 `_create_agent` 都 new 一套中间件实例，
并行子代理即使 context 里带着同一个父 `run_id` 也不会串号——实例不共享。

---

## 13. 护栏上限与 `stop_reason`（D9）

三条独立的轴都能让子代理提前结束。

| 轴 | 机制 | 抛异常吗 |
|---|---|---|
| **轮次** | `recursion_limit = max_turns`，耗尽时 LangGraph 抛 `GraphRecursionError` | **会**，要专门 catch（在通用 `except` 之前） |
| **token** | token 预算中间件，到硬上限时剥工具调用、强制 `stop` | 不会 |
| **循环** | 循环检测中间件，识别重复的工具调用集合 | 不会 |

后两者**故意不抛异常**——让本轮自然产出一个最终答复，比丢一个异常给用户有用得多。

### 13.1 用附加字段，不要加状态枚举

三条轴触发时，状态仍然是正常的 `completed` / `failed`，
**另外**带一个 `stop_reason`（`token_capped` / `turn_capped` / `loop_capped`）。

理由是兼容性：新增一个状态枚举值会让所有已有的消费者（前端、台账读取器、
跨语言契约）遇到未知值；一个可选字段会被老消费者直接忽略。

- 有可用的最终答复（哪怕是从最后一个流式块里恢复的部分结果）→ `completed` + `stop_reason`
- 什么都没剩下 → `failed` + `stop_reason`

`_extract_final_result` 用共享的 `message_content_to_text`。没有任何文本时返回
哨兵 `"No response Generated"`——哨兵算"没可用结果"，走 failed。

`GraphRecursionError` 的 catch 必须在 `except Exception` 之前。漏了会变成普通 failed，
lead 看不到 `turn_capped`。

### 13.2 图干净地结束 ≠ 子代理成功

M6 的 LLM 错误处理中间件会**故意**把 provider 异常转成一条 `AIMessage`，
让图能干净地结束，并盖 `additional_kwargs.deerflow_error_fallback=true`
（M6 已定的标记名）。

执行器终态化时检查最后一条 assistant 消息上的这个标记，有就映射为 `failed`。

**只有标记是权威的。** 看起来像错误的 assistant 散文没有标记就是正常完成——
执行器和前端都不许把展示文本当状态协议解析。

---

## 14. 事件流与步骤捕获（D8）

### 14.1 步骤捕获走新增的尾部，不是 `messages[-1]`

一轮里模型可能调了 3 个工具，LangGraph 的 ToolNode 会在**一个超级步**里
追加 3 条 `ToolMessage`。只看 `messages[-1]` 会丢掉前两条。

`step_events.py` 是纯函数，不启动图，单元测试不需要 Gateway：

```python
def capture_new_step_messages(messages, captured, seen_ids, processed_count) -> int:
    total = len(messages)
    if total < processed_count:          # 压缩收缩
        processed_count = total
    if total > processed_count:
        for message in messages[processed_count:total]:
            capture_step_message(message, captured, seen_ids)
        return total
    if messages:
        capture_step_message(messages[-1], captured, seen_ids)  # 等长原地替换
    return max(processed_count, total)
```

只捕获 `AIMessage` 和 `ToolMessage`。去重：有 id 按 id，没 id 按整 dict。

**收缩问题**：摘要压缩用 `RemoveMessage(REMOVE_ALL_MESSAGES)` 重写整个消息通道，
`len(messages)` 会突然**小于**捕获游标。检测到收缩就把游标重置到新的尾部，
靠 id/内容去重防止重发压缩前的步骤。不这么做的话，压缩之后新产生的步骤会一直被丢弃，
直到 `total` 重新超过那个陈旧的游标。

不变量（写进 docstring，测试钉住）：重置之后"等长"分支只重看 `messages[-1]`，
所以压缩**不得**在游标以下插入一条新的可捕获消息。今天 Summarization 把摘要放进
`summary_text`，messages 通道只留已经见过的尾巴。将来若有中间件违反这条，
重置分支要改成全量重扫。

### 14.2 步骤 payload 有界

`SUBAGENT_STEP_MAX_CHARS = 8192`。`build_subagent_step` 截 `text`，
工具参数 JSON 超长时改成截断预览并标 `args_truncated`。
一次 `write_file` 的全文不能变成无界的 `subagent.step` 行。

### 14.3 持久化批量写

catalog 增加（M4 § 已预告 `subagent` category）：

```python
SUBAGENT_START_EVENT = RunEventDefinition("subagent.start", "subagent")
SUBAGENT_STEP_EVENT  = RunEventDefinition("subagent.step",  "subagent")
SUBAGENT_END_EVENT   = RunEventDefinition("subagent.end",   "subagent")
```

`category="subagent"` 最长 8 字符，在 16 的上限内。`list_messages` 不返回它们；
`list_events(task_id=..., after_seq=...)` 给前端展开卡片分页。
过滤骑在已有的 `(thread_id, run_id, seq)` 索引上，**不改 schema**。
`DbRunEventStore` 用 `event_metadata["task_id"].as_string()`；JSONL/memory 在内存滤。

worker 侧 `_SubagentEventBuffer`：

- `FLUSH_THRESHOLD = 25`
- flush 时机：`subagent.end`、达到阈值、worker `finally`
- 不要每步 `put()`——`put()` 是 per-thread 咨询锁的低频路径，
  `max_turns=150` 的子代理会在热流循环上打几百次

`subagent_run_event(chunk)` 拒掉：

- 没有非空 `task_id` 的块
- `task_running` 缺少非负 `message_index` 或 message 对象

`task_id` 在持久化 metadata 里是 **provider 的 tool_call_id**（对外相关键），
不是 `execution_id`。

只处理 root namespace 上的 `custom` 帧。带 namespace 的 custom 丢掉，避免子代理
自己的事件被自己再记一次（§3.1）。

终态 `subagent.end` 的 content 带 `status` / `model_name` / `usage`（有则带，无则缺）。
结果正文截断到与 step 相同的上限，完整结果以 ToolMessage 为准。

---

## 15. token 归属（D10）

子代理的 token 要归到**父 run** 上，通过两条通道，**不要用进程级全局缓存**：

1. 运行中：`SubagentTokenCollector` 每完成一次 LLM 响应，把累计快照写进
   `SubagentResult.token_usage_records`（持 holder 锁）。下一条 `task_running`
   带上这份 snapshot，前端折叠卡片可以更新，而不重算父 run 总计。
2. 终态：`ToolMessage.additional_kwargs["subagent_token_usage"]` +
   `subagent_model_name`。刷新页面后卡片仍能显示。
3. 父账本：`task` 工具在**父循环**调 `RunJournal.record_external_llm_usage_records`。
   永远不要从隔离循环或 `to_thread` 里调（§7.5）。

Collector 按 `run_id` 去重（一次 LLM 回调可能打多次 `on_llm_end`）。
`total_tokens <= 0` 的记录直接跳过——**不要估算成 0**。0 和"未知"是不同的信息。

`caller` 字段用 `subagent:{name}`，父 journal 按这个分桶（M3 已定
`lead_agent` / `subagent` / `middleware` 三个维度）。

M6 的 `TokenUsageMiddleware`（若启用）从当前 run 的终态 ToolMessage 读用量，
合并回发出该 tool_call 的 AIMessage，并给 ToolMessage 盖
`subagent_token_usage_attributed=true`，避免 checkpoint 回放加两次。
缺/畸形用量或找不到配对的 dispatch → 不盖戳，下次可重试。

---

## 16. 调用方策略要快照进 run 元数据

一个自定义 lead agent 可以声明 `allowed_subagents`：

- `None` = 全部允许
- `[]` = 硬拒绝（提示词里不出现委派段，`task` 工具可以从 lead 工具集拿掉）
- 列表 = 白名单

**这个策略要在 run 开始时快照进 run 元数据 / `runtime.context`，`task` 工具内部
绝不重新读可变的 agent 配置。** 否则运行中途有人改了配置，同一次 run 的前后
两次委派会用不同的策略。

策略要同时过滤**提示词里的发现**和 **`task` 的执行**——只过滤一边，
要么模型看得见却调不动（浪费轮次），要么看不见但能调（策略形同虚设）。

`apply_prompt_template(..., subagent_enabled=True)`（M1 已留）渲染的
`{subagent_section}` 读这份快照。当解析后的 `max_concurrent == 1` 时，
**删掉**并行和多批次收益指引，只保留"专家能力 / 上下文隔离"两条——
提示词还在教模型并行 3 个，中间件却只放行 1 个，模型会空转。

---

## 17. 取消、超时、父 run 收尾

| 场景 | 执行侧 | 对外 status |
|---|---|---|
| 父 run 取消（M3 `runs:cancel`） | `cancel_all_for_run` → 每个 holder 的 `cancel_event`；隔离循环在 chunk 边界检查并 `try_set_terminal(CANCELLED)` | `cancelled` |
| 子代理墙钟 (`timeout_seconds`) | 隔离循环 `wait_for(astream, timeout=...)` | `timed_out` |
| 父侧轮询预算耗尽 | 父循环停止等待，`request_cancel`，ToolMessage 标明轮询超时 | `polling_timed_out` |
| 协作式 `cancel_event` 在 stream 中途 | 保留**已经捕获的**最新步骤和用量，再终态化 | `cancelled` |

执行器主循环：

```python
async for chunk in agent.astream(initial_state, config=child_config, stream_mode="values"):
    if result.cancel_event.is_set():
        result.try_set_terminal(CANCELLED, result=_extract_final_result(last_state), ...)
        break
    last_state = chunk
    # 捕获新步骤、发布 receipts/usage 快照 —— 在检查取消之前先发布
    ...
```

**先发布最新 chunk 的证据，再看取消。** 否则取消刚好卡在 stream 边界时，
lead 拿不到已经发生的工具输出。

父 run worker 的 `finally`（M3 已有 `publish_end` 嵌套 finally）再加一步：
`cancel_all_for_run(run_id)` + 等隔离循环上的清理 Future（有超时）。
不要假设 `task` 工具的轮询循环一定还活着——客户端断开时 ToolNode 可能已经取消。

---

## 18. 测试清单

| 文件 | 覆盖 |
|---|---|
| `test_subagent_status_contract.py` | Python 元组 ↔ JSON fixture 双向；读侧把历史 `max_turns_reached` 映射到 `failed`+`turn_capped`；写侧永不产出 |
| `test_subagent_ids.py` | 两个 run 用相同的 `tool_call_id` 并发委派，互不干扰；闭包不回查注册表 |
| `test_subagent_lineage.py` | **子配置不含四个坐标键**；子代理帧不进父 `messages` 流；`checkpointer=False` |
| `test_subagent_loop_boundary.py` | ContextVar 拷贝保留流回调；`deerflow_loop_bound` 处理器被摘除；无重复计账；空白 Context 会丢 namespace（负例） |
| `test_subagent_concurrency.py` | 并发上限；总量上限；历史 run 不消耗新预算；槽位耗尽时剥调用而非报错；提示词并发数 = 控制器槽数；热重载不能抬高已建控制器 |
| `test_delegation_ledger.py` | 同 id 合并；终态不被 `in_progress` 降级；`[]` 保留；截断 50；`run_id` 首次为准；brief 截断；护栏指引出现在渲染里 |
| `test_subagent_sandbox.py` | 一个子代理结束不关闭沙箱；最后一个持有者释放；取消/超时不泄漏租约；`finally` 二次释放幂等 |
| `test_subagent_cancel.py` | **取消父 run，所有子代理被清理**；取消释放队列位置；`Future.cancel` 在锁外；`task_started` emit 失败也清理 |
| `test_subagent_stop_reason.py` | 三条轴各自的 `stop_reason`；有部分结果 → completed，无 → failed；`GraphRecursionError` 不落入泛型 except |
| `test_subagent_error_marker.py` | 带 `deerflow_error_fallback` → failed；看起来像错误的散文 → completed |
| `test_subagent_step_capture.py` | 一轮多工具全部捕获；压缩收缩后游标重置且不重发；等长原地替换仍捕获；args 截断 |
| `test_subagent_event_buffer.py` | 阈值 25 flush；`subagent.end` flush；`finally` flush；无 `task_id` 的块丢弃；带 ns 的 custom 忽略 |
| `test_subagent_routing_prompt.py` | lead 段 / task docstring / 两个 builtin description 含同一套收益与硬否决；`max_concurrent==1` 时无并行指引 |
| `test_subagent_uploads.py` | `[]` 快照保留；缺失/畸形禁用 `list_uploaded_files`；deepcopy 跨循环后父列表改动不影响子 |
| `test_startup_only_subagent_runtime.py` | `subagent_runtime.*` 在 `STARTUP_ONLY_FIELDS` 且 description 带前缀 |

`test_subagent_lineage.py` 和 `test_subagent_cancel.py` 是这一阶段最值得写的两个。
血缘那个要驱动一个真实的父图。取消那个建议做一次压力形状：起 3 个长任务，
在第 2 秒取消，然后断言沙箱 provider 侧的会话数和注册表条目数都归零。

TDD：D1 先写契约测试，D3 先写血缘负例（显式传 `thread_id` 必须失败的那种断言），
再写执行器。

---

## 19. 验收清单

- [ ] 一个需要 3 个并发子任务的请求能正确完成，结果被 lead 综合
- [ ] 中途取消父 run，**所有子代理被清理**，没有残留的沙箱租约或注册表条目
- [ ] 前端会话视图在子代理运行期间不被子代理的局部状态覆盖（父 `messages` 流无子帧）
- [ ] 父 run 的 token 统计包含子代理的消耗，且刷新页面后卡片仍显示用量和模型名
- [ ] 一个必定超轮次的子代理，返回 `completed` + `turn_capped` 和部分结果
- [ ] 一个 provider 报错（带降级标记）的子代理，返回 `failed`，而不是一条看起来正常的完成
- [ ] 两个子代理并发写沙箱，先结束的那个不影响后一个
- [ ] 总量用尽时 lead 看到可见说明并综合已有结果，而不是工具错误
- [ ] `list_messages` 不含 `subagent.*`；展开卡片能按 `task_id` + `after_seq` 拉回步骤
- [ ] 技能 `allowed-tools` 未声明 `task` 时 lead 不能靠委派绕过（M7 回归仍绿）

第二条建议专门做一次压力验证：起 3 个长任务，在第 2 秒取消，
然后检查沙箱 provider 侧的会话数和注册表条目数都归零。

---

## 20. 刻意不做的事

| 不做 | 理由 |
|---|---|
| **持久化批量委派**（`batch_task` + 租约服务 + 重试预算 + 独立表） | 一整套独立的租约状态机 + 恢复语义，够单独一个里程碑。不要从 prompt 长度推断 batch |
| **验收标准的确定性校验**（`file exists` / `tests_passed`） | 真要把"文件存在""测试通过"做到不可伪造，需要 shell 结构解析、执行证据溯源、符号链接规范化——研究级工作量 |
| **工具回执与引用校验** | 同上，属于"可验证性"产品线。M8 的 ToolMessage 不要填 `subagent_tool_receipts` |
| 子代理嵌套委派 | 内置 `general-purpose` / `bash` 显式**不**带 `task`。递归委派的成本模型和取消语义都要重做 |
| 子代理 resume | 一次性语义是 `checkpointer=False` 的前提 |
| 子代理继承请求级密钥 | M7 §6 明确了不继承 |
| 管理员托管子代理定义表 | `custom_agents` 够用；托管存储跟 Custom Agent 同一套 backend，放到生产化 |
| MCP durable background task 工具 | DeerFlow 的 `list_background_tasks` 管的是 MCP 长时任务，不是子代理。M8 不要借这个名字 |
| 把 `RunJournal` 传进隔离循环 | 用标记摘除。跨循环审计以后用窄 proxy，不在 M8 做 |
| 新的 run 状态枚举值 | `stop_reason` 是附加字段 |

前几条要在代码里留出接口位置（`stop_reason`、终态 ToolMessage 的元数据槽、
`skip_memory_flush` 参数），但不要为它们提前建表或实现检查器。

---

## 21. 与已有里程碑的接口对照

| 已有槽 | M8 填什么 |
|---|---|
| M0 `STARTUP_ONLY_FIELDS` | `subagent_runtime.max_running` 等四条 |
| M1 `ThreadState.delegations` / `merge_delegations` | 终态不可降级 + `run_id` + `stop_reason` |
| M1 `{subagent_section}` | 收益 / 硬否决 / 成本；`max_concurrent==1` 时收紧 |
| M2 `SandboxMiddleware` acquire/release | 升级为 `SandboxLeaseManager`；lead owner，子代理 non-owner |
| M3 custom SSE / "子代理的 custom 事件" | `task_*` → StreamBridge `custom` |
| M3 `subagent_tokens` 字段 | 由 journal `record_external_llm_usage_records` 填 |
| M4 catalog `subagent` category | `subagent.start/step/end` |
| M4 `list_events` | 加 `task_id` + `after_seq` 过滤 |
| M6 `build_runtime_middlewares` 分层 | `build_subagent_runtime_middlewares` |
| M6 DurableContext 捕获 delegations | 真正抽取 `task` 对；进行中也记 |
| M6 `deerflow_error_fallback` | 执行器映射为 failed |
| M7 `task` 不豁免 | 子代理 `disallowed_tools` 含 `task`；技能策略仍可再挡 lead 侧 |
| M7 `ThreadState.promoted` | 子代理每次 fresh state，提升隔离 |

---

## 22. 一句话总结

M8 的实现难度集中在**一条边界**上：子代理跑在独立的事件循环里，
而它需要继承父 run 的 checkpoint 血缘、流回调和身份，
同时又不能继承父 run 的账本、checkpointer 和消息通道。

检验标准只有一个：**在子代理运行期间，用户的会话视图不能被它的局部状态覆盖；
取消父 run 之后，沙箱会话数和注册表条目数都归零。**
这两条过了，剩下的都是可以增量补的。
