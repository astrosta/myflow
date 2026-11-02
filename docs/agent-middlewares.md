# Agent 中间件

组装代码在 `backend/packages/harness/agentkit/agents/middlewares/assembly.py`。

lead 图由 `build_middlewares` 组装，子代理图由 `build_subagent_runtime_middlewares` 组装。两者都先经过 `build_runtime_middlewares`。

注册顺序和钩子执行顺序不是同一件事：

- `wrap_model_call` / `wrap_tool_call` 是洋葱。列表里越靠前的在越外层，先进入、后离开。
- `after_model` 是反序。列表里越靠后的越先跑。

条件不满足的中间件不会放进链。注释里的 `SLOT` 是预留插入点，当前没有实现。

## Lead 注册顺序

| 顺序 | 中间件 | 何时进入 |
| --- | --- | --- |
| 1 | `InputSanitizationMiddleware` | 总是 |
| 2 | `ThreadDataMiddleware` | 总是 |
| 3 | `UploadsMiddleware` | 仅 lead |
| 4 | `SandboxMiddleware` | 总是 |
| 5 | `DanglingToolCallMiddleware` | 总是 |
| 6 | `LLMErrorHandlingMiddleware` | 总是 |
| 7 | `ToolErrorHandlingMiddleware` | 总是 |
| 8 | `PIIMiddleware` | 总是 |
| 9 | `DynamicContextMiddleware` | 仅 lead |
| 10 | `SkillActivationMiddleware` | 仅 lead |
| 11 | `SkillToolPolicyMiddleware` | 仅 lead |
| 12 | `DurableContextMiddleware` | 仅 lead |
| 13 | `MyFlowSummarizationMiddleware` | `summarization.enabled` |
| 14 | `TitleMiddleware` | 仅 lead |
| 15 | `OpenVikingMemoryMiddleware` | `memory.openviking.enabled` 且运行时已初始化 |
| 16 | `PIIOutputMiddleware` | 总是 |
| 17 | `SubagentLimitMiddleware` | 本次 run 允许委派 |
| 18 | 调用方传入的 `custom_middlewares` | 有传入时，接在最后 |

`after_model` 因此先跑 `SubagentLimitMiddleware`，再跑 `PIIOutputMiddleware`，然后才是记忆、标题和摘要。这样模型刚写出的文本和工具参数先脱敏，记忆中间件看到的是脱敏后的内容；委派超额也在工具节点执行前被剥掉。

技能三件套必须紧挨着，中间不能插别的：`SkillActivationMiddleware`、`SkillToolPolicyMiddleware`、`DurableContextMiddleware`。它们共用同一次技能目录快照。

## 子代理注册顺序

子代理每次 `task` 新建一套实例，并行任务之间状态不串。

| 顺序 | 中间件 |
| --- | --- |
| 1 | `InputSanitizationMiddleware` |
| 2 | `ThreadDataMiddleware` |
| 3 | `SandboxMiddleware` |
| 4 | `DanglingToolCallMiddleware` |
| 5 | `LLMErrorHandlingMiddleware` |
| 6 | `ToolErrorHandlingMiddleware` |
| 7 | `PIIMiddleware` |
| 8 | `PIIOutputMiddleware` |

子代理链上没有上传清单、标题、技能绑定、动态日期、摘要、长期记忆和委派限额。

## 各中间件的作用

### InputSanitizationMiddleware

包在模型调用最外层。把用户消息里伪造的框架标记转义掉，内层（包括模型重试）看到的都是清洗后的文本。只改本次请求，不写进 checkpoint。

### ThreadDataMiddleware

在 `before_agent` 公布这个会话的工作区、上传目录和输出目录路径，供后续工具使用。

### UploadsMiddleware

仅 lead。把本轮上传文件的清单注入上下文。文件本体留在磁盘上。

### SandboxMiddleware

进入时获取沙箱租约，正常结束时由最后一个持有者释放。工具自己不释放沙箱。lead 是租约所有者，子代理只是持有者，一个子代理结束不能把兄弟还在用的沙箱关掉。取消、超时不会走到这个钩子，由执行器收尾补释放。

### DanglingToolCallMiddleware

模型调用前修补历史：补上没有结果的工具调用，丢掉没有对应调用的工具结果。后面的错误处理看到的是成对的历史。

### LLMErrorHandlingMiddleware

供应商调用失败时，瞬时错误重试；最终失败变成一条可见的 AI 消息，而不是把整轮打崩。

### ToolErrorHandlingMiddleware

包在工具调用内侧。工具抛出的异常收成错误 `ToolMessage`。成功结果打上 `myflow_tool_meta`，外层看到的是盖过章的结果。

### PIIMiddleware

在工具真正执行前，把参数里的 PII 占位符还原成原文。还原不了的占位符交给内层的 `ToolErrorHandlingMiddleware`，变成可恢复的工具错误。金库存于本次 run 的 `runtime.context`，不进 state，也不进 checkpoint。

### DynamicContextMiddleware

仅 lead。在最新用户消息旁注入当前日期。系统提示词保持静态。

### SkillActivationMiddleware

仅 lead。处理用户显式的 `/skill-name`，把对应 `SKILL.md` 作为隐藏的用户消息注入这次模型调用。只改请求，不写入 state。

### SkillToolPolicyMiddleware

仅 lead。按当前激活的技能计算这次模型能看见哪些工具，并过滤工具 schema。这是行为约束，不是安全边界；沙箱和鉴权才是边界。

### DurableContextMiddleware

仅 lead。把摘要、委派台账和技能引用投影到这次模型请求上，不把投影写进 state。模型自己读 `SKILL.md` 的路径也由它捕获。

### MyFlowSummarizationMiddleware

对话过长时，把历史压成 `summary_text`。摘要不塞回 `messages`。和长期记忆无关，压缩内容不进 OpenViking。

### TitleMiddleware

仅 lead。第一轮完整对话结束后，给会话写标题。

### OpenVikingMemoryMiddleware

仅 lead，且 OpenViking 已启用。把这一轮的对话捕获进长期记忆。捕获失败只记日志，不让用户这一轮失败。用户画像不在这里渲染，它在组装 lead 图时写进系统提示词。

### PIIOutputMiddleware

`after_model` 上对模型写出的正文和 `tool_calls` 参数做脱敏，换成 handle。工具返回的 `ToolMessage` 不归它处理。

### SubagentLimitMiddleware

仅 lead，且本次 run 允许委派。模型刚发出 `task` 调用、工具还没执行时，按并发上限和本 run 总量丢掉超额调用。不报错。一个都不允许时，强制结束并补一句可见说明。

## 同目录但不是链上中间件

这些文件给上面的中间件或 `task` 工具用，不会出现在组装列表里：

| 文件 | 作用 |
| --- | --- |
| `delegation_ledger.py` | 从消息里抽出委派记录，渲染成给模型看的台账 |
| `provenance.py` | 标记一段内容的来源类型 |
| `message_utils.py` | 判断是不是真实用户消息等共用谓词 |
| `tool_result_meta.py` | 成功工具结果上的元数据结构 |
