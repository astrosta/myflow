# M6 中间件体系详细设计

> 配套文档：[分阶段开发方案](2026-09-16-greenfield-agent-service-roadmap.md) ·
> [M0 地基](2026-09-16-m0-foundation-design.md) · [M1 最小 Agent](2026-09-16-m1-minimal-agent-design.md) ·
> [M2 工具与沙箱](2026-09-16-m2-tools-sandbox-design.md) · [M3 Gateway 与流式](2026-09-16-m3-gateway-streaming-design.md) ·
> [M4 持久化](2026-09-16-m4-persistence-design.md)
>
> 本文细化 M6（2–3 周）。沿用包名：框架层 `agentkit.*`，应用层 `app.*`。

---

## 1. M6 的范围

M1–M4 做出来的 agent 能跑通一轮对话。M6 解决的是**跑得久、跑得稳**：
上下文会超限、工具会抛异常、用户会中途取消、模型会返回空响应、
上传的文件要让模型知道、会话要有个名字。

这些全都是**横切关注点**——它们不属于任何一个工具，也不属于 agent 主流程，
但每一个都会在真实使用的第一天出现。

> **M6 的核心命题不是"实现 N 个中间件"，而是"建立一套让第 N+1 个中间件能安全加进来的规则"。**

DeerFlow 现在有 47 个中间件文件。它们不是设计出来的，是按痛点一个个长出来的。
能长到 47 个而不崩塌，靠的是四条在早期就立好的规则：
集中组装、顺序即语义、消息来源标记、工具结果元数据标准化。
这四条比任何单个中间件都重要。

### 三周排期

| 周 | 内容 |
|---|---|
| W1 | 中间件框架 + 集中组装 + 横切标准（provenance / 结果元数据）+ 前四个 |
| W2 | 上下文压缩（自动 + 手动 compact）+ 标题生成 |
| W3 | 第二梯队按需挑三四个 + 顺序约束测试 |

---

## 2. 中间件模型：四个 hook，两种顺序

### 2.1 四个 hook

| hook | 时机 | 典型用途 |
|---|---|---|
| `before_agent` | 整个图执行前，一次 | 初始化目录、获取沙箱 |
| `wrap_model_call` | 每次模型调用，洋葱式包裹 | 注入上下文、改写请求、重试 |
| `wrap_tool_call` | 每次工具调用，洋葱式包裹 | 错误处理、预算、审计、门禁 |
| `after_model` | 每次模型返回后 | 检测循环、清理工具调用、终止判定 |

### 2.2 **注册顺序和执行顺序不是一回事**

这是 M6 最容易出错的地方，必须在第一天就搞清楚：

```
注册顺序:  [A, B, C]

wrap_model_call / wrap_tool_call （洋葱）:
    A 外层 → B → C 内层 → 真正的调用 → C → B → A

after_model （LangChain 反序 dispatch）:
    C → B → A
```

**`wrap_*` 是洋葱：先注册的在外层。** 所以"要看到所有其他中间件处理后的最终结果"
的中间件要注册在**最前面**（比如输入清洗，它要保证所有内层中间件——包括 LLM 重试——
看到的都是已清洗的消息）。

**`after_model` 是反序：后注册的先执行。** 所以"要第一个判断模型是不是被安全终止了"
的中间件要注册在**最后面**。

这两条相反的规则会让人反复搞错。**唯一的解法是在组装函数里为每个顺序约束写注释**，
并且写测试把顺序钉死。

---

## 3. 集中组装（W1）

### 3.1 一个函数，不要散落的 append

```python
def build_runtime_middlewares(config, *, runtime_ctx) -> list[AgentMiddleware]:
    """lead 和 subagent 共享的基础层。"""

def build_middlewares(config, *, runtime_ctx, custom=None) -> list[AgentMiddleware]:
    """在基础层之上追加 lead-only 的条目。"""
```

**分两层是有原因的**：M8 的子代理需要绝大部分基础能力（错误处理、输出预算、沙箱），
但不需要标题生成、记忆写入、澄清中断这些只有主对话才有意义的东西。
一开始就分层，比以后从一个大列表里往外摘容易得多。

**每一个顺序约束都要在代码里写清楚为什么。** 不是"// 顺序很重要"，
而是具体到"X 必须在 Y 外层，因为 Y 会短路返回自己的 ToolMessage，
内层的 X 会漏掉这条结果"。

### 3.2 可选条目按配置/运行时条件加入

```python
if config.summarization.enabled:
    chain.append(SummarizationMiddleware(...))
if runtime_ctx.is_plan_mode:
    chain.append(TodoListMiddleware())
if model_config.supports_vision:
    chain.append(ViewImageMiddleware())
```

条件判断集中在组装函数里，**不要让中间件自己在 hook 里判断"我该不该生效"**。
后者会让链条长度不可预测，而顺序约束是按位置表达的。

---

## 4. 四条横切标准（W1）

**这一节是 M6 真正的产出物。** 四个中间件谁先谁后是可以调的，但这四条标准定错了，
后面每加一个中间件都要打补丁。

### 4.1 消息来源标记（provenance）

任何中间件注入或改写消息时，都要在 `additional_kwargs` 上盖一个统一的戳：

```python
def provenance_kwargs(content_kind: str, producer_kind: str,
                      producer_entity_id: str | None = None) -> dict: ...
```

**为什么现在就要做：下游无法反推生产者。** 一条隐藏的 `HumanMessage` 出现在历史里，
它可能来自动态上下文注入、来自持久上下文投影、来自图片载荷、来自技能激活——
不盖戳的话，后面想做"压缩时跳过某类注入消息"或者"清理上一次运行遗留的注入"
就只能靠内容匹配，脆弱且必然出错。

这些字段是**服务端所有的**：Gateway 必须剥掉客户端传进来的同名字段。

### 4.2 工具结果元数据标准化

所有工具结果经过一个统一的 `normalize_tool_result()`，盖上结构化元数据：

```python
{
    "status": "success" | "error",
    "error_type": "no_results" | "not_found" | "permission" | "rate_limited"
                | "transient" | "auth" | "config" | "internal",
    "recoverable_by_model": bool,      # 模型换个参数重试有没有意义
    "recommended_next_action": "retry" | "adjust" | "stop",
    "source": str,
}
```

**`recoverable_by_model` 这一个布尔值是整套错误处理的地基。**
"搜索没有结果"和"API key 失效"在模型眼里都是"工具返回了错误"，
但前者应该换个关键词重试，后者重试一万次也没用。

没有这个标记，后面的停滞检测、循环检测、重试策略就只能去猜错误文本里有没有
"rate limit" 这样的字符串——每接一个新工具就要加一条正则。

### 4.3 三个上下文注入通道，职责不重叠

这是我认为最值得从 DeerFlow 抄的一个设计。注入上下文有三种完全不同的需求：

| 通道 | 内容 | 存在哪 | 每轮重建 |
|---|---|---|---|
| **动态上下文** | 当前日期、召回的记忆 | 不进 state | 是 |
| **持久上下文** | 压缩摘要、委派台账、激活的技能引用 | 进 state 的专用通道 | 投影，不重建 |
| **请求级注入** | 图片 base64 载荷 | **绝不进 state** | 是 |

三条对应的纪律：

**动态上下文不要写进系统提示词。** 当前日期每天都变，写进系统提示词会让
整个前缀缓存每天失效一次。正确做法是把它作为 `<system-reminder>`
注入到第一条 `HumanMessage` 里，**保持基础系统提示词完全静态**。

**持久上下文要在压缩之前捕获。** 委派结果、读过的技能引用这些信息，
原本存在于工具调用/结果这对消息里。压缩会把它们合并掉。
所以捕获它们的中间件必须在压缩中间件**之前**运行，
把结构化引用存进 state 的专用通道，然后每轮投影回模型请求。

**请求级载荷绝不进 state。** 见 §6.3。

### 4.4 信任边界：system 通道 vs 隐藏 data 块

注入内容时要区分两类：

- **框架自己的规则**（怎么处理摘要、怎么看待委派结果）→ `SystemMessage`，高权威
- **不可信的值**（摘要正文、委派返回的文本、技能描述）→ 隐藏的 `HumanMessage` 数据块

```
<durable_context_data>
## Conversation summary so far
[摘要正文]
</durable_context_data>
```

**为什么要拆开：摘要正文是模型自己生成的，委派结果来自子代理，技能描述来自文件。**
这些内容里可能包含"忽略之前的指令"之类的文本。把它们放进 `SystemMessage`
等于给了它们系统级权威——一次间接的提示注入。

---

## 5. 第一梯队（W1–W2）

按"撞墙顺序"排，不是按重要性。

### 5.1 `DanglingToolCallMiddleware`——几乎必踩

**症状**：模型发了 `tool_call`，执行被中断（用户取消、进程重启），
checkpoint 里留下一条有 `tool_calls` 但没有对应 `ToolMessage` 的 `AIMessage`。
下一轮请求被 provider **直接拒绝**——严格的 OpenAI 兼容后端要求每个 tool_call
必须有配对的结果。

这条 thread 从此再也发不出消息，只能删掉重开。

**做法**：为缺失响应的 tool_call 注入占位 `ToolMessage`。
顺带处理一个相关问题：**清理畸形的工具调用名和参数**，
否则同样会被严格 provider 拒绝。

这个中间件的价值密度极高——代码几十行，但不做它整个产品在第一次用户点"停止"之后就坏了。

### 5.2 `ToolErrorHandlingMiddleware`

把工具抛出的异常转成错误 `ToolMessage`，让这一轮能继续而不是整个 run 崩掉。
同时它是 §4.2 那套元数据的盖章点。

一个容易忽略的点：**模型需要看到错误内容**。返回一句 "tool failed" 没有用，
要把异常信息（脱敏后）给它，它才可能换个参数重试。

### 5.3 `ThreadDataMiddleware` / `UploadsMiddleware`

前者创建 per-thread 的 `workspace` / `uploads` / `outputs` 目录，
后者把新上传的文件清单注入对话。

唯一的要点：**两者必须用同一个身份解析函数**（`resolve_runtime_user_id(runtime)`）。
一个用 runtime 里的 user_id、另一个 fallback 到 contextvar，
结果就是文件建在 A 用户的桶里、检查存在性时去 B 用户的桶里找。

### 5.4 `TitleMiddleware`

首轮完整交换之后生成会话标题。三个坑：

**用用户的原话，不是被包装过的那条消息。** 上传中间件会往用户消息里加文件上下文，
直接拿来生成标题会得到"用户上传了 3 个文件并询问..."这种东西。
所以需要 §4.1 的 provenance——原始内容要单独保留一份。

**纯附件消息回退到默认标题。** 用户只传了个文件什么也没说，
硬生成标题只会得到一句废话。

**首轮被中断时要有兜底。** 标题还没生成 run 就被取消了，
侧栏会一直显示"新对话"。worker 的收尾阶段要从 checkpoint 或原始输入里
写一个 fallback 标题。

---

## 6. 上下文压缩（W2）

这是第一梯队里唯一真正复杂的一个，值得单独一节。

### 6.1 配置形状

```yaml
summarization:
  enabled: true
  model_name: null            # null = 用这次 run 自己的模型

  trigger:                    # OR 逻辑，任一满足即触发
    - type: tokens
      value: 6000
    - type: messages
      value: 75

  keep:                       # 保留多少近期历史
    type: messages
    value: 25

  trim_tokens_to_summarize: 4000
```

三种度量（`tokens` / `messages` / `fraction`）在 trigger 和 keep 上通用。
`fraction` 是"模型上下文窗口的百分比"，需要模型配置里声明了 `context_window`；
**没声明时要丢掉这条 fraction 子句并告警**，而不是猜一个默认窗口——
猜错会导致压缩在真正需要的时候不触发。

### 6.2 `model_name: null` 应该是"用这次 run 的模型"，不是 `models[0]`

这是个看起来微小但很关键的决定。

如果默认回落到配置里的第一个模型，那么 `models[0]` 的 provider 挂了
（key 过期、配额用尽、服务中断）会**让所有会话的压缩功能失效**——
哪怕用户当前选的模型完全健康。压缩失效意味着长对话直接撞上下文上限，
整个会话报废。

正确的默认是"这次 run 实际执行用的那个模型"。显式配置了独立摘要模型时，
它失败要**回落到 run 自己的模型**，同样的道理。

### 6.3 五条压缩不变量

**① AI/Tool 消息对不能拆开。** 切分点落在工具消息中间时，要整体调整，
把完整的 AI + Tool 序列留在同一侧。拆开的后果和 §5.1 一样：provider 直接拒绝。

**② 摘要存进专用的 `summary_text` 通道，不要塞回 `messages`。**
塞回 messages 会让下一次压缩把摘要本身又摘要一遍，信息逐轮衰减。
存专用通道则每轮投影，原文始终不变。

**③ 必须按 message ID 精确保住最新的真实用户请求。**
把用户刚发的那句话压进摘要里，模型就会开始回答一个被概括过的问题。

**④ 但不要为了保住它而把切分点往回移。**
这条是对 ③ 的重要限制：往回移会连带保住早期的 AI/Tool 轮次，
结果是"首轮长分析"这种场景压缩完全不生效（切分点被推到了最开头）。
正确做法是保住那条消息本身，而不是移动切分点。

**⑤ 触发判断要把 `summary_text` 算进去。** 摘要本身也会投影进下一次请求，
只数 `messages` 的 token 会低估实际用量，在摘要变长之后越来越不准。

### 6.4 手动 compact 复用同一个中间件

`POST /api/threads/{id}/compact` 不要另写一套压缩逻辑。它应该：

1. 走 M3 那条"每 thread 一个活跃操作"的**预约路径**（`operation_kind=checkpoint_write`），
   忙的时候返回 409
2. 复用同一个压缩实现
3. 写一个新 checkpoint

第 1 步是硬要求。手动 compact 和正在跑的对话都会写同一条 thread 的 checkpoint，
不走预约就是并发写，结果是静默的状态损坏。

---

## 7. 视觉载荷绝不进 state（W3，如果做视觉）

单独拎出来讲，因为这个坑的代价特别大。

**做法**：在 `wrap_model_call` 里把 base64 图片数据作为隐藏 `HumanMessage`
追加到 `ModelRequest.messages`，**只活在这一次请求里**，
从不作为 state update 返回。state 里只留轻量元数据（路径、mime、尺寸）。

**不这么做的后果**：每个 checkpoint 都要序列化几 MB 的 base64。
一个看了五张图的会话，checkpoint 表能涨到几十 MB，
而且每次读 state 都要把它们反序列化出来。

**还有一个隐蔽的后续问题**：中间件每次要先把自己上一次注入的消息扫出去，
再重新构建。理由是——被中断的 run 可能在旧 checkpoint 里留下过一条载荷消息
（早期实现用 `before_model` 写进 state、`after_model` 用 `RemoveMessage` 拿出来，
中断就会卡在中间）。不扫的话，那条 base64 会在这条 thread 剩余的生命里
**每次请求都重发一遍**。

扫除的判据要同时要求"保留的 ID 前缀"和"服务端所有的标记"两个条件，
**不能只看 ID 前缀**——否则客户端可以伪造一条消息让它被服务端扫掉。

---

## 8. 第二梯队（W3，按需）

不需要一次做完。按你实际撞到的墙挑：

| 中间件 | 什么时候需要 |
|---|---|
| `ToolOutputBudgetMiddleware` | 第一次有工具返回 100KB 日志时 |
| `LoopDetectionMiddleware` | 第一次看到模型反复调同一个工具时 |
| `TokenBudgetMiddleware` | 第一次收到意外账单时 |
| `SystemMessageCoalescingMiddleware` | 接入 vLLM / Qwen / Anthropic 这类严格后端时 |
| `TerminalResponseMiddleware` | 第一次看到模型返回空的终态回答时 |
| `ClarificationMiddleware` | 要做人机确认时（M3 留的 `command.resume` 字段在这里用上） |
| `TodoListMiddleware` | 做 plan mode 时 |
| `InputSanitizationMiddleware` | 有不可信输入源时（IM 通道、公开部署） |
| `ReadBeforeWriteMiddleware` | 模型开始凭空覆盖没看过的文件时 |

两个值得提前说明的设计点：

**`ToolOutputBudgetMiddleware` 的截断要保头保尾并注明。**
直接截断尾部会丢掉最关键的错误信息（通常在末尾）。
更好的做法是超限时**外置到文件**，上下文里只留一个结构化摘要 + `read_file` 引用，
让模型需要时自己去读。

**停滞检测和循环检测是两件事，不要合并。**
前者是结果质量守卫——工具执行完了但没产生新信息（同样的搜索结果、同样的报错），
按工具粒度阻断。后者是调用模式守卫——模型反复发出完全相同的 tool_call，
硬停整轮。两者可以在同一次模型调用里都注入提示而不冲突，
但它们读的信号完全不同。

---

## 9. 测试清单

| 文件 | 覆盖 |
|---|---|
| `test_middleware_order.py` | **对编译后的图断言中间件顺序**；每个顺序约束一个用例 |
| `test_dangling_tool_call.py` | 中断后留下的孤儿 tool_call 被补上占位结果；畸形名字被清洗 |
| `test_tool_error_meta.py` | 每类错误映射到正确的 `recoverable_by_model` / `recommended_next_action` |
| `test_summarization.py` | AI/Tool 对不被拆；最新用户请求按 ID 保住；**首轮长分析不会变成 no-op**；`summary_text` 计入触发 |
| `test_compact_reservation.py` | 手动 compact 在有活跃 run 时 409 |
| `test_provenance.py` | 每个注入型中间件都盖了戳；客户端伪造的戳被剥掉 |
| `test_view_image_payload.py` | 载荷不出现在任何 checkpoint；旧 checkpoint 里的遗留载荷被扫除；客户端伪造的消息扫不掉 |
| `test_title.py` | 用原话不用包装后的消息；纯附件回退默认；首轮中断有 fallback |

第一个是这一阶段最重要的。**顺序约束必须对编译后的图断言**，
而不是对组装函数的返回值——因为真正决定行为的是 LangChain 实际的 dispatch 顺序，
包括 `after_model` 的反序。

---

## 10. 验收清单

- [ ] 长对话自动压缩后能继续，摘要不会被反复二次压缩
- [ ] 故意让工具抛异常，对话不中断，模型能看到错误内容并换参数重试
- [ ] 对话跑到一半点停止，下一轮能正常发送（不被 provider 拒绝）
- [ ] 手动 `POST /compact` 在有活跃 run 时返回 409，空闲时写出新 checkpoint
- [ ] 首轮生成的标题是用户的原话概括，不含"用户上传了文件"之类的包装
- [ ] 首轮被中断时侧栏仍有一个合理的标题，不是"新对话"
- [ ] 看过图片的会话，其 checkpoint 大小是 KB 级不是 MB 级
- [ ] 基础系统提示词逐字不变（前缀缓存可复用），日期通过注入通道给

倒数第二条和最后一条建议做成断言而不是人工检查。

---

## 11. 刻意不做的事

| 不做 | 理由 |
|---|---|
| 47 个中间件 | 它们是按痛点长出来的。没撞到墙就做，是在猜 |
| 鉴权 / 护栏门禁 | M9。但 `wrap_tool_call` 的位置和 fail-closed 语义现在就要想清楚 |
| 技能相关中间件 | M7 |
| 子代理限额中间件 | M8 |
| 委派台账 | M8。但"持久上下文通道"这个机制 M6 就要有（压缩摘要要用） |
| 中间件自描述 / 发布策略 | 规模上来之后才有意义 |
| 扩展加载的中间件 | 需要先有扩展系统 |

---

## 12. 一句话总结

M6 交付的四个中间件会被后面的几十个稀释掉，但四条横切标准不会。

检验标准：**让另一个人加一个新中间件，他需要问你几个问题？**
如果他能从组装函数的注释里看出该插在哪、从 provenance 规范里知道该盖什么戳、
从结果元数据规范里知道该怎么报错——那 M6 就做对了。
如果他得来问你"这个应该放在压缩前面还是后面"，说明约束还留在你脑子里，没写进代码。
