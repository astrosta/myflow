**Agent 评测方案（含知识库 RAG 与 Skills 检索）**

方案组合：Langfuse + DeepEval + 规则 Evaluator

适用对象：基于 LangGraph、Milvus 工具检索、Skills 检索和知识库 RAG 的 B/S 架构 Agent

版本：v1.4（草案，在 v1.3 基础上改为固定测试账号评测、按账号真实数据合成测试集、user_id 只放 metadata 并用于数据过滤；所有 Agent 调用以测试账号身份认证并携带 thread_id，证据从 run_event 读取）

**1. 目标与范围**

本方案用于持续评测 Agent 的工具检索、工具调用、Skills 检索与使用、知识库检索、执行轨迹和最终回答质量。修改 prompt、工具描述、Skill 描述、检索策略、切片策略、embedding 模型或底座模型后，能够快速判断效果变好还是变差，并定位问题出在哪一层。

**评测目标**

- **回归保障**：每次改动前后跑同一套回归集，分数下降时阻止发布。
- **问题定位**：分层出分，区分是工具没召回、选错工具、参数错误、Skill 没召回或选错、知识库没检索到、回答不忠于资料，还是表述问题。
- **持续改进**：线上失败案例回流到评测集，评测集随业务和知识库演进。

不在本方案范围内：前端页面渲染测试、性能压测、底座模型选型（可参考 BFCL 等公开基准）。

**2. 总体架构与分工**

| 组件 | 角色 | 负责内容 |
| --- | --- | --- |
| Langfuse | 评测平台 | 存放评测集 (Dataset)、记录 trace、运行实验、保存分数、网页对比实验、LLM-as-a-Judge、人工标注 |
| DeepEval | 指标库 + 数据合成 | 工具指标 (ToolCorrectness)、RAG 指标 (Faithfulness、AnswerRelevancy、ContextualPrecision/Recall/Relevancy)、GEval；用 Synthesizer 从知识库文档生成问答样本 |
| 规则 Evaluator | 自研代码 | 工具检索 Recall@k、参数比对、禁用工具检查、Skills 检索命中率、知识库检索命中率（按切片或文档 ID） |
| Agent API | 被测对象 | 以认证身份隔离用户；使用 thread/run 两阶段接口；评测侧需补只读的 run_event 证据查询（不提供 debug 模式） |

**数据流**：固定测试账号的真实数据 → 合成 Langfuse Dataset（样本 `metadata.user_id` 存 Langfuse hashed user id）→ 评测脚本按 hashed user id 映射到已登录的固定账号、用独立 thread_id 调用 Agent API（建 thread、起 run）→ 读取 run 终态和 run_event 账本，得到回答、工具检索结果、工具调用、Skills 检索与加载结果、知识库检索切片 → 规则 Evaluator + DeepEval 打分（不评 user_id 相关因素）→ 分数写回 Langfuse → 网页对比实验。

**原则**

- 能用规则判断的不用 LLM。工具检索、工具调用、Skills 检索、知识库检索命中中都优先用名称或 ID 比对。
- 每次调用 Agent 都必须以固定测试账号身份认证（user_id 由 token/会话决定，不放在请求体）；user_id 只用于数据过滤和 trace 归属，不进入 query/expectedOutput/评分；每条样本使用独立 thread_id。
- LLM 评测只用于无法用规则判断的维度：回答是否忠于资料、是否切题、是否满足判定规则。
- 评测集只在 Langfuse 维护一份。

**3. 评测分层与指标**

工具链和知识库链路分开评测，最终回答统一评测。阈值为初始建议值，首轮基线跑完后按实际分布调整。

**3.1 工具链路**

| 层级 | 评测内容 | 指标 | 实现方式 | 初始阈值 |
| --- | --- | --- | --- | --- |
| T1 工具检索 | Milvus 混合检索能否召回正确工具 | Recall@5、MRR | 规则 | Recall@5 ≥ 0.95 |
| T2 工具选择 | 是否调用了正确的工具 | tool_hit、ToolCorrectness | 规则 / DeepEval | ≥ 0.90 |
| T2 参数 | 关键参数是否正确 | args_accuracy | 规则 | ≥ 0.85 |
| T3 轨迹 | 调用顺序、步数、多余调用 | extra_calls_ratio | 规则 | 业务工具调用次数 / 期望调用次数 ≤ 1.5 |

**3.2 Skills 链路**

Skills 检索与工具检索结构类似（Milvus 混合检索 + 按 user_id 过滤），但当前没有自动 SkillRouter：检索由 skill-finder 子代理调用 `search_skills` 完成，之后还要判断“是否加载、加载后是否按 Skill 执行”。

| 层级 | 评测内容 | 指标 | 实现方式 | 初始阈值 |
| --- | --- | --- | --- | --- |
| K0 路由 | 该用 Skill 时是否委派 skill-finder 子代理，不该用时是否没有委派 | skill_routing | 规则 | ≥ 0.95 |
| K1 Skill 检索 | skill-finder 的 search_skills 能否召回正确的 Skill | skill_recall@5、skill_mrr | 规则 | skill_recall@5 ≥ 0.95 |
| K2 Skill 选择 | 最终加载（激活）的是否为期望 Skill，有无多加载 | skill_hit、skill_extra | 规则 | skill_hit ≥ 0.90 |
| K3 Skill 遵循 | 加载后是否按 Skill 规定的步骤、工具和输出格式执行 | skill_adherence | GEval (LLM) | ≥ 0.85 |

说明：K0 只看是否委派 `skill-finder`，不看子代理内部是否调用 `search_skills`；后者属于 K1。K1 与 K2 分开统计。K1 低说明检索问题（Skill 描述、embedding、BM25 权重、元数据过滤）；K1 高而 K2 低说明模型或路由在候选中选错。

**3.3 知识库 RAG 链路**

| 层级 | 评测内容 | 指标 | 实现方式 | 初始阈值 |
| --- | --- | --- | --- | --- |
| R0 路由 | 该查知识库时是否委派 knowledge-researcher 子代理，不查时是否没有委派 | kb_routing | 规则 | ≥ 0.95 |
| R1 检索召回 | 期望的资料是否被检索到 | kb_recall@k（按切片或文档 ID）、kb_mrr | 规则 | kb_recall@5 ≥ 0.85 |
| R1 检索召回（无 ID 标注时） | 检索内容是否覆盖参考答案所需信息 | ContextualRecall | DeepEval (LLM) | ≥ 0.80 |
| R2 检索精度 | 相关切片是否排在前面、噪声多不多 | ContextualPrecision、ContextualRelevancy | DeepEval (LLM) | ≥ 0.70 |
| R3 生成忠实度 | 回答是否忠于检索内容，有无编造 | Faithfulness | DeepEval (LLM) | ≥ 0.90 |
| R3 引用 | 回答引用的来源是否真实存在于检索结果中 | citation_valid | 规则 | ≥ 0.95 |
| R4 拒答 | 知识库没有答案时是否明确说明，而不是编造 | refusal_ok | 规则 + LLM 判定 | ≥ 0.90 |

说明：R0 只看是否委派了 knowledge-researcher，不看子代理内部是否调用 `knowledge_search`；后者属于 R1 的检索证据。所有 `expect_skill` / `expect_kb` 样本都必须走对应子代理；评测环境要保证 `subagent_enabled=true`、两个检索子代理可用、`skills.usage.prompt_top_k=0`，样本不走 slash。若出现静态 `load_skill`、直连 `knowledge_search` 或未委派，属于配置/样本无效，应失败而不是 N/A。R1 只用文档 ID 比对：评测集标注 `expected_doc_ids`。切片 ID 会随切片策略变化，建议优先标注文档 ID 加页码或段落位置，切片策略调整后仍然可用。

**3.4 端到端与安全**

| 层级 | 评测内容 | 指标 | 实现方式 | 初始阈值 |
| --- | --- | --- | --- | --- |
| E1 回答质量 | 最终回答是否满足判定规则 | answer_quality | GEval 或 Langfuse LLM-as-a-Judge | ≥ 0.85 |
| E1 回答正确性 (RAG) | 回答与参考答案是否一致 | answer_correctness | GEval (对照 reference_answer) | ≥ 0.80 |
| E1 相关性 | 回答是否切题 | AnswerRelevancy | DeepEval | ≥ 0.85 |
| S1 安全与边界 | 注入、无工具、缺参数追问 | forbidden_violation、no_tool_ok | 规则 + LLM 判定 | forbidden_violation = 0 |

用户身份、权限和跨用户隔离本轮不作为评分维度：每条样本只使用固定账号自己的可见数据，`metadata.user_id` 只用于选账号和数据过滤，不进入 query/expectedOutput。

优先级：T1、K1、R1 是三条链路的第一道门槛，检索不到的内容后面无法弥补；R3 忠实度是 RAG 最重要的质量指标。

**4. 评测集构建**

**4.1 数据来源**

| 来源 | 做法 | 初始规模 |
| --- | --- | --- |
| 固定测试账号的真实工具 | 测试人员填入固定账号的 username/password；登录后按实际 user_id 调 `GET /api/mcp-servers`，再直接调用每个 server 的 MCP `tools/list` 取 `name`、`description`、`inputSchema`（`GET /api/mcp-servers/{id}/tools` 只给摘要，不作为 schema 来源） | 按实际可见工具动态计算；每工具 2-3 条，典型 200-400 条 |
| 固定测试账号的 Skill 元数据 | 登录后调 `GET /api/skills`，只取 `scope=private` 的技能；内置/builtin 技能不参与测试集生成。取 name、description、git_commit，正文按需从 Skill 仓库读取 | 每个私有 Skill 2-3 条，典型 100-150 条 |
| 固定测试账号的知识库文档 | 登录后调 `GET /api/knowledge/bases`、`/documents`、`/documents/{id}/chunks`，取实际文档和切片；用 DeepEval Synthesizer 或模板生成问答对，并记录 `document_id` / `chunk_id` | 约 150 条 |
| 线上 trace 回流 | 在 Langfuse 按 `userId = metadata.user_id`（hashed）筛失败、低分、用户纠正的 trace，转成 Dataset item；真实 user_id 只在构建时用于访问账号数据，不落样本 | 持续增加 |

**人工 Golden 集暂不做。** 当前评测集完全由固定测试账号的真实工具、Skill 元数据、知识库文档和线上 trace 合成；需要标注的期望工具、参数、文档 ID 和参考答案由合成过程或测试人员审核产生，不单独维护一份人工 Golden 集。

固定账号由测试人员在构建评测集时填入 username/password；构建脚本登录后取得实际 `users.id`，但样本 `metadata.user_id` 写入的是 **Langfuse hashed user id**，不是原始 user_id。哈希用与 Agent tracing 相同的 `user_id_salt` 计算（HMAC-SHA256，取前 32 位十六进制）；**密码不写入 Dataset、trace 或样本**。真实 user_id 只在构建阶段用于访问/过滤该账号的工具、Skill、知识库和计算哈希，不落 Dataset。评测 runner 复用同一组固定账号，登录后计算各自的 hashed id，建立映射。

线上 trace 回流要注意：Langfuse Agent trace 默认 `user_id_mode=hashed`，`userId` 是 HMAC-SHA256 后的 32 位十六进制值。构建脚本用同一个 `user_id_salt` 把实际 user_id 算成 hashed id，再用它过滤 trace；**不要把评测环境改成 `raw`**，否则样本 metadata 与 trace 的 `userId` 不一致。salt 变更会让所有 hashed id 变化，必须重建/重新标注数据集并记录 salt 版本。

**构建流程**

1. 测试人员在固定账号的 secret 配置中填入 username/password；构建脚本登录一次，调 `GET /api/v1/auth/me` 取得实际 `users.id`（只在内存中使用）。
2. 以同一个登录 Client 拉取该账号的数据：`GET /api/mcp-servers`；对每个 server 直接调 MCP `tools/list` 取 schema；`GET /api/skills`（只保留 `scope=private`，排除 builtin）；`GET /api/knowledge/bases` → `/documents` → `/documents/{id}/chunks`；以及 Langfuse 中该账号 hashed `userId` 的失败/低分 trace。
3. 用工具 schema、Skill 描述、知识库切片和 trace 问法合成 query、expectedOutput 和 metadata；`args` 必须来自真实 `inputSchema`，不要猜。
4. 计算 `hmac_sha256(user_id_salt, agent_user_id)[:32]`，把结果写入 `metadata.user_id`；不写原始 user_id、username/password，也不把 user_id 放进 `input`。
5. 合成后按 4.4 做去重、可解性和抽检；当前没有人工 Golden 集。

评测环境的前置条件：`subagent_enabled=true`，`skill-finder` 和 `knowledge-researcher` 都可用；`skills.usage.prompt_top_k=0`，私有 Skill 不进入静态 `<available_skills>`；评测样本不使用 slash 激活。这样所有 `expect_skill` / `expect_kb` 样本都必须经过对应子代理。

注意：代码里在 `subagent_enabled=false` 或检索子代理不可用时，`knowledge_search` 会作为 lead 直连 fallback 重新出现；这不是“知识库直连工具不存在”，而是配置回退。评测必须保证回退不生效；如果出现直连 `knowledge_search` 或静态 `load_skill`，该样本作废或整批评测失败，不能当成通过。

工具 schema 的外部读取还有一个实现约束：`GET /api/mcp-servers/{id}/tools` 只返回摘要，且 MCP server 的鉴权 header 在 API 中是 masked 的。如果目标 server 需要鉴权，构建器直接调 MCP `tools/list` 就需要单独拿到凭据；更好的做法是给这个 GET 加 `include_schema=true`（或新增只读 schema 接口），由 Gateway 用已加密保存的配置代理读取，不把 header 暴露给评测脚本。

**从知识库生成样本的注意**

- Synthesizer 会先切片再生成，生成结果里的 context 是它自己的切片，**不等于你系统的切片**。需要把 context 文本映射回你的文档 ID 和位置（按源文件名，或者用文本匹配找到对应段落）。
- 合成的问题往往直接照抄原文措辞，检索会偏容易。要再做一轮改写（同义替换、口语化、只给部分关键词），才能测出真实召回能力。
- 生成后必须人工抽检，确认参考答案确实能从文档推出。

**4.2 类别配比**

在 metadata.category 中标注类别：

| 大类 | 类别 | 说明 | 建议占比 |
| --- | --- | --- | --- |
| 工具 | single_tool | 单工具调用 | 17% |
| 工具 | multi_tool | 多工具组合或顺序调用 | 8% |
| 工具 | similar_tools | 功能相近的工具并存 (hard negative) | 6% |
| 工具 | missing_params | 缺少必要参数，期望追问 | 4% |
| Skill | skill_single | 应触发单个 Skill | 10% |
| Skill | similar_skills | 描述相近的 Skill 并存，测区分能力 | 4% |
| Skill | skill_with_tools | 需加载 Skill 并按 Skill 指引调用工具 | 4% |
| 知识库 | kb_single_hop | 答案在单个段落内 | 14% |
| 知识库 | kb_multi_hop | 答案需要综合多个段落或多个文档 | 6% |
| 知识库 | kb_unanswerable | 知识库中没有答案，期望明确说明 | 5% |
| 知识库 | kb_conflict | 不同文档或新旧版本内容冲突 | 2% |
| 混合 | hybrid | 同时涉及知识库、Skill 和工具中的两项以上 | 9% |
| 边界 | no_tool | 闲聊或常识，期望不调用工具、不加载 Skill、不查知识库 | 6% |
| 安全 | injection | 工具返回、Skill 正文或知识库文档中夹带指令 | 5% |

本轮是固定账号评测，只用该账号自己的工具、Skill 和知识库数据；不构造跨用户权限样本，也不把 user_id 作为评分维度。

每条种子样本可做改写扰动：口语化、中英混杂、缩写、错别字。

**4.3 Langfuse Dataset 数据格式**

运行时需要的内容放 `input`，评分需要的内容放 `expectedOutput`，分类、来源和账号信息放 `metadata`。工具类、Skill 类和知识库类共用一套结构，不适用的字段留空。

`metadata.user_id` 必填，值是 **Langfuse hashed user id**，不是原始 `users.id`。构建集时测试人员填入固定账号 username/password；构建脚本登录后取得实际 user_id，用与 Agent tracing 相同的 `user_id_salt` 计算 `HMAC-SHA256(user_id_salt, agent_user_id)` 并取前 32 位十六进制，写入 `metadata.user_id`。这里的 `agent_user_id` 是 Agent 认证后使用的 storage-safe ID（`make_safe_user_id(users.id)`）；普通 UUID 测试账号与 `/me.id` 相同，若不是 UUID，必须用同样规则归一后再算哈希。`user_id_salt` 来自与 Agent 相同的 Langfuse 配置/secret，构建器和 runner 必须共用。真实 user_id 只在构建阶段用于访问/过滤该账号的工具、Skill、知识库和计算哈希，不写入样本；密码也不落 Dataset。`input` 只放 query，不放 user_id/user_role；`user_id` 只用于账号映射和数据过滤，不进入 query、expectedOutput 或评分。不要把整份样本 metadata 原样塞进 run 请求：run 侧身份只来自认证，metadata 里的 hashed user_id 也不应被当成运行时身份。

评测脚本还要为每条样本生成/携带唯一的 `thread_id`（例如 `eval-<run_name>-<item_id>`，满足 `^[A-Za-z0-9_-]{1,64}$`），通过 run URL 传递；不要复用历史 thread，否则旧消息会污染本轮评测。`thread_id` 不需要写进 Dataset，运行时生成即可。

工具类样本里的工具名按 MCP 的 `call_name` 标注（`mcp__<server>__<tool>`），不是 `server.tool`；知识库 ID 示例是占位，实际值取 `knowledge_search` 返回的 `document_id` / `chunk_id`。Langfuse 保持 `user_id_mode=hashed` 时，`metadata.user_id` 与 Agent trace 的 `userId` 一致；把它也放进 `Evaluation.metadata`，score 就能按同一标签筛选。experiment trace 本身没有 user_id 字段，详见 6.2.1。

**工具类样本**

```json
{
  "id": "tool_0123",
  "input": {
    "query": "帮我查一下上周新增的项目有哪些"
  },
  "expectedOutput": {
    "expected_tools": ["mcp__project_db__search_projects"],
    "expected_calls": [
      {
        "tool": "mcp__project_db__search_projects",
        "args": {"sector": "solar", "created_after": "<last_week_start>"},
        "check_keys": ["sector", "created_after"]
      }
    ],
    "forbidden_tools": ["mcp__project_db__delete_project"],
    "expect_kb": false,
    "expect_skill": false,
    "judge_rule": "返回列表且全部为 solar 项目"
  },
  "metadata": {
    "user_id": "<langfuse hashed user id>",
    "category": "single_tool",
    "difficulty": "medium",
    "source": "synthetic_from_tools"
  }
}
```

**知识库类样本**

```json
{
  "id": "kb_0045",
  "input": {
    "query": "项目融资中 DSCR 的最低要求一般是多少？"
  },
  "expectedOutput": {
    "expect_kb": true,
    "expected_doc_ids": ["doc_pf_guide_2026"],
    "reference_answer": "一般要求 DSCR 不低于 1.2-1.3，具体以贷款协议为准。",
    "key_points": ["给出最低要求的数值范围", "说明以贷款协议为准"],
    "expect_refusal": false
  },
  "metadata": {
    "user_id": "<langfuse hashed user id>",
    "category": "kb_single_hop",
    "difficulty": "easy",
    "source": "synthetic_from_docs",
    "kb_version": "2026-11"
  }
}
```

**Skill 类样本**

```json
{
  "id": "skill_0017",
  "input": {
    "query": "把这条 Reuters 新闻整理成每日融资简报的格式"
  },
  "expectedOutput": {
    "expect_skill": true,
    "expected_skills": ["daily-finance-brief"],
    "skill_rules": "按 Skill 规定的字段顺序输出",
    "expect_kb": false,
    "expected_tools": []
  },
  "metadata": {
    "user_id": "<langfuse hashed user id>",
    "category": "skill_single",
    "difficulty": "medium",
    "source": "synthetic_from_skill",
    "skill_commit": "alb2c3d"
  }
}
```

skill_rules 用于 K3 遵循度评判，可从 SKILL.md 中的关键步骤和输出格式提炼。

**无答案样本**：expect_refusal: true, expected_doc_ids 为空, reference_answer 写 “知识库中没有相关信息”。

上面的问题和答案只是格式示例，实际数值以你的知识库内容为准。

**4.4 质量控制**

1. 用户检查：每条样本的 `metadata.user_id` 必填，值为固定测试账号对应的 **Langfuse hashed user id**；构建脚本用登录后的实际 user_id 计算哈希，原始 user_id 只留在构建过程内存中，不写进样本。样本的工具、Skill、知识库数据必须来自该账号自己的可见范围。
2. 去重：embedding 相似度超过 0.9 的样本只保留一条。
3. 可解性校验：工具类用该账号实际可见的工具和 schema 判断能否完成；Skill 类确认期望 Skill 存在且该账号可见；知识库类确认参考答案能从 expected_doc_ids 推出。
4. 执行验证：工具类在沙箱中按 expected_calls 实际执行。
5. 人工审核：合成数据至少抽检 20%；当前没有人工 Golden 集，因此不设“Golden 全量审核”。
6. 分布检查：按类别、工具、Skill、文档来源、难度统计，避免集中在少数工具或少数文档。

当前没有人工 Golden 集，合成集只作为**回归信号**，不用于宣称绝对回答质量；绝对质量结论需要人工抽检或线上标注补充。

**4.5 数据集划分与版本管理**

- 回归集（agent-eval/regression/VN）：固定不变，每次改动都跑。
- 探索集（agent-eval/explore）：持续加入线上失败案例，定期挑选稳定样本并入下一版回归集。
- 工具版本绑定：按固定账号的 MCP server/tool 快照记录 `server_id + call_name + inputSchema hash`；工具 schema 或 server 变更时，标记引用该工具的样本，重新执行验证。
- Skill 版本绑定：Skill 仓库有新的 Git 提交时，按 expected_skills 复查受影响样本重新验证；在 metadata.skill_commit 中记录样本对应的 Skill 版本。
- 知识库版本绑定：文档更新或删除时，标记引用该文档的样本（按 expected_doc_ids 复查），重新确认参考答案。在 metadata.kb_version 中记录样本对应的知识库版本。
- 账号哈希绑定：记录 `langfuse_user_id_salt` 的版本/fingerprint；salt 变更会让 `metadata.user_id` 全部失效，必须重建数据集。
- 运行绑定：实验 metadata 记录 git commit、被测模型、prompt/工具描述版本、评测并发数；否则分数变化无法归因。

**5. Agent 接口改造（按当前实现审计）**

**审计结论**

1. 当前项目没有方案原先假设的“单次 Agent API”：不存在 `POST /agent`，请求体也不是 `{message, user_id, session_id, debug}`。网关是 thread/run 两阶段接口，`RunCreateRequest` 使用 `extra="forbid"`，上述字段会直接返回 422。
2. `user_id` 不能放在请求体里。`start_run()` 会从 `metadata`、`config.configurable`、`config.context`、`context` 中删除所有客户端身份键，服务端只认认证中间件解析出的身份；请求体里的 `user_id` 不会生效，放错位置还会 422。
3. `thread_id` 是 URL 路径参数，也是当前项目的会话隔离单位；没有 `session_id` 字段。评测要带的是“测试账号凭据 + thread_id”，不是“user_id + session_id”。
4. 不建议为评测新增 debug 模式。已有 `subagent.start/step/end` 等 run_event；结构化的 `retrieval.tool/skill/kb` 需要新增，检索结果才能稳定、完整地取到。还需要一个只读的 run events 查询接口，由评测脚本聚合，而不是把评测字段塞进 run 响应。
5. 现有 API 仍不足以完整覆盖原第 5 节的所有字段：`messages` 只返回 `category=message`，看不到 `subagent.step`；`citation` 已经有 `knowledge_citations`，但检索事件和 run events 接口尚未实现；`skill_commit` 没有按 run 记录。下面的取数矩阵逐项标出。

**5.1 实际调用链**

| 步骤 | 接口/状态 | 说明 |
| --- | --- | --- |
| 认证 | `POST /api/v1/auth/login/local`（username + password） | 登录响应只设置 HttpOnly `access_token` 和可读的 `csrf_token` cookie；后续请求复用同一个 cookie jar，并带 `X-CSRF-Token`。PAT 仅作为已预置的备选；其 HTTP 签发路由尚未上线（见 backend/README.md 的“PAT 的现状”）。 |
| 建会话 | `POST /api/threads` | 可带确定性 `thread_id`；owner 由认证身份决定，请求体不传 `user_id`。 |
| 起 run | `POST /api/threads/{thread_id}/runs/wait` | 阻塞到 run 结束，返回该 run 的 checkpoint 快照。 |
| 异步起 run | `POST /api/threads/{thread_id}/runs` | 返回 202 + `run_id`；适合 CI，避免长请求超时或断连取消。 |
| 读 run | `GET /api/threads/{thread_id}/runs/{run_id}` | 取 `status`、`error`、`metadata`；本次 Langfuse trace id 在 `metadata["myflow-trace-id"]`。 |
| 读父消息 | `GET /api/threads/{thread_id}/messages` | 只有 `category=message` 的父代理消息；工具调用/结果嵌在消息内容里。 |
| 读证据 | `GET /api/threads/{thread_id}/runs/{run_id}/events` | **本方案新增，当前代码尚未实现**；这是取得 `subagent.*` 和 `retrieval.*` 的 API 途径。 |
| 清理 | `DELETE /api/threads/{thread_id}` | 可选；至少保证每条样本使用独立 thread。 |

**5.2 请求**

`user_id` 由 Agent 的认证逻辑决定，不能作为业务字段下发。样本 `metadata.user_id` 存的是 Langfuse hashed user id；评测 runner 启动时用 `AGENT_USERS` 登录所有固定账号，对每个账号计算 `HMAC-SHA256(user_id_salt, agent_user_id)[:32]`，建立 `hashed user_id -> 已登录 Client` 映射，运行时按样本 `metadata.user_id` 选 Client。测试人员在构建集时填入固定账号 username/password；构建脚本登录后取得实际 user_id，只用于拉取/过滤该账号的数据、计算 hashed id 和筛选 Langfuse trace，密码和真实 user_id 都不写进 Dataset 或样本。真实 user_id 也不用于 Agent 请求；Agent 仍从 session 解析身份。不要用 `X-MyFlow-Internal-Token` + `X-MyFlow-Owner-User-Id` 作为评测捷径：那是通道/调度器身份，可以代表任意用户，权限面过大。评测环境必须开启 `auth.enabled=true`；关闭时所有请求跑在固定的 `default` 用户上，数据过滤会失效。

每个测试 user 登录一次，后续所有样本复用同一个 `httpx.Client`（cookie jar）；不要每条样本重新登录。登录响应不返回 token 字符串，token 只在 HttpOnly cookie 里，所以评测脚本应依赖 Client 的 cookie 管理，而不是自己拼 `Authorization`。登录后可用 `GET /api/v1/auth/me` 校验账号和 `id`，但 `me.id` 只用于日志/断言，不写回 Agent 请求。

```python
import httpx

client = httpx.Client(base_url=AGENT_API, timeout=180)
resp = client.post(
    "/v1/auth/login/local",
    json={"username": username, "password": password},
)
resp.raise_for_status()
login = resp.json()
if login["needs_setup"]:
    raise RuntimeError("测试账号仍处于 needs_setup，不能用于评测")

# 后续 POST 都要带同一个 CSRF 值；Client 会自动携带 access_token / csrf_token cookie
client.headers["X-CSRF-Token"] = client.cookies["csrf_token"]
actual_user = client.get("/v1/auth/me").json()  # 仅校验，不传给 Agent
```

注意事项：

- `username` 是登录标识，不一定是 `user_id`；实际 `user_id` 以 Agent 解析结果为准，通常可用 `/me.id` 核对。
- 正常注册的账号默认 `needs_setup=false`，可以直接用于评测。`needs_setup` 不是“新账号待初始化”标记，而是**管理员重置密码后的强制改密标记**：为 true 时只能访问 `/me`、改密和登出，业务接口返回 403 `setup_required`。评测前发现 true，就用临时口令改一次密码，或换一个正常账号。注意区分登录响应里的用户级 `needs_setup` 和 `/api/v1/auth/setup-status` 的全局“还没有 admin”含义。
- 测试账号正常注册即可；如果 `auth.allow_registration=false`，则需要在评测前由管理员预建，不能假设评测脚本能自助注册。
- 登录失败按来源 IP 限速（默认 5 次失败锁 300 秒，成功登录会清零）。启动评测前一次性校验所有账号凭据，失败就整批终止，不要在样本循环里反复试密码。
- 每个测试账号使用独立的 `httpx.Client`；不要把不同用户的 cookie 混在一个 Client 里。并发时按用户/线程隔离 Client。
- session 有 TTL（默认 84600 秒）；长任务收到 401 时重新登录该账号，不要复用失效 cookie。
- 本轮不构造跨用户权限样本；固定账号只使用自己可见的工具/Skill/知识库数据，账号之间不互相借数据。

先建 thread。`thread_id` 必须匹配 `^[A-Za-z0-9_-]{1,64}$`，并且每条样本/每次实验唯一；重复使用同一 `thread_id` 会带上旧消息，破坏可复现性。

```http
POST /api/threads
Cookie: access_token=<...>; csrf_token=<...>   # 同一个已登录 Client 会自动携带
X-CSRF-Token: <csrf_token>
Content-Type: application/json

{
  "thread_id": "eval-20261110-tool_0123",
  "assistant_id": "lead_agent",
  "metadata": {
    "eval_run": "baseline",
    "eval_item": "tool_0123"
  }
}
```

再起 run。`assistant_id` 必填，当前只接受 `lead_agent`；`input.messages` 只接受 human 消息。模型、思考开关和子代理开关走 `context`，只能使用服务端公布的装配键（`model_name`、`thinking_enabled`、`reasoning_effort`、`subagent_enabled` 等）。

```http
POST /api/threads/eval-20261110-tool_0123/runs/wait
Cookie: access_token=<...>; csrf_token=<...>   # 同一个已登录 Client 会自动携带
X-CSRF-Token: <csrf_token>
Content-Type: application/json

{
  "assistant_id": "lead_agent",
  "input": {
    "messages": [
      {"type": "human", "content": "帮我查一下上周新增的项目有哪些"}
    ]
  },
  "context": {
    "model_name": "deepseek-v4-flash",
    "thinking_enabled": false
  },
  "metadata": {
    "eval_run": "baseline",
    "eval_item": "tool_0123"
  },
  "on_disconnect": "continue"
}
```

几个容易踩错的点：

- `on_disconnect` 默认是 `cancel`。评测请求超时或客户端断开时，run 会被取消；本地用 `runs/wait` 时要么显式设 `continue`，要么改用 `POST /runs` + 轮询 `GET /runs/{run_id}`。CI 推荐后者。
- `thread_id` 只能在 URL 里；`session_id`、`message`、`debug`、顶层 `user_id` 都不在 `RunCreateRequest` 中，会 422。
- `user_role` 不是 run 参数；账号角色来自固定测试账号。本轮不评分权限/隔离，也不准备跨用户样本。
- 服务端会在四个位置删除客户端身份键。因此 `metadata.user_id` 会被静默丢弃，顶层 `user_id` 会 422；两种都不是“切换用户”的入口。
- 每条样本使用新 `thread`；评测结果**不评估长期记忆的影响**。评测环境应关闭长期记忆，或在每轮实验前统一清空固定账号记忆；否则记忆污染会让同一 query 的结果不可复现。该处理只用于保证可复现，不产生记忆相关指标。

**5.3 响应与证据取数**

`runs/wait` 返回的是 checkpoint 快照，不是第 5 节原先设计的 debug 聚合体：

```json
{
  "values": {"messages": ["..."]},
  "checkpoint_id": "...",
  "checkpoint_ns": "",
  "run_id": "...",
  "status": "success",
  "error": null
}
```

`values.messages` 里的最后一条有文本的 AI 消息就是 `answer` 的可靠来源；不要假设最后一条一定是 AI。`GET /runs/{run_id}` 另行提供 `status`、`error`、`metadata` 和 `myflow-trace-id`。

| 评测字段 | 当前可取得方式 | 来源/格式 | 缺口或注意 |
| --- | --- | --- | --- |
| answer | 最后一条有文本的 AI 消息；或 run_event 的 `run.end` outputs | `values.messages` / `llm.ai.response` | 以 checkpoint 快照为准，不要用 SSE 拼接文本。 |
| retrieved_tools | `retrieval.tool` 的 `tools[].call_name`（fallback：父代理 `tool_search` 的 ToolMessage） | `retrieval.tool` / `llm.tool.result` | 每次 search 最多返回 5 张卡，最终顺序是预算裁剪后的顺序；T1 用 Recall@5。工具名用 `call_name`（`mcp__server__tool`），不是 `server.tool`。 |
| tool_calls | 父/子代理 AI 消息的 `tool_calls`；结果看 ToolMessage | `llm.ai.response`、`llm.tool.result`、`subagent.step` | MCP 工具的业务名在 `tool_execute.args.tool_name`，真实参数在 `tool_execute.args.arguments`；不要统计 `tool_search`/`tool_execute` 本身。子代理步骤里 `args` 是 JSON 字符串，不是对象。 |
| subagent_calls | 父代理 `task` 工具调用的 `subagent_type`；`subagent.start` 事件确认实际启动 | 父 AI 消息的 `task.tool_calls` / run_event 的 `subagent.start` | **K0/R0 只看是否调用对应子代理**：`skill-finder` 对应 K0，`knowledge-researcher` 对应 R0。`task` 调用是路由决策，`subagent.start` 是实际启动；两者都保留，便于区分“没路由”和“路由了但启动失败”。 |
| retrieved_skills | `retrieval.skill` 的 `skills[]`（fallback：skill-finder 的 `subagent.step`） | `retrieval.skill` | 只有委派 skill-finder 并调用 `search_skills` 时才有；本轮所有 Skill 样本都必须如此，缺失即失败而不是 N/A。 |
| loaded_skills | 父代理 `load_skill(name, id)` 调用/结果；checkpoint 的 `values.skill_context` | 父消息、`values.skill_context` | 只评私有 Skill；`load_skill` 的 name/id 来自 skill-finder 返回；`skill_context` 记录 name/path/description。slash 激活不进入评测样本；静态 Skill 索引在评测环境设为 `prompt_top_k=0`。 |
| kb_called | 父/子代理任意一次 `knowledge_search` 调用 | `subagent.step`（knowledge-researcher）或父消息 | 这是 R1 的检索证据和诊断字段，**不是 R0 路由**；R0 看 `knowledge-researcher` 是否被 `task` 调用。 |
| retrieved_chunks | `retrieval.kb` 的 `items[]`（fallback：`subagent.step` 的 `knowledge_search` JSON） | `retrieval.kb` | 字段包含 `document_id`、`chunk_id`、`citation_id`、`filename`、`section`、`page`、`score`、`content`、`rerank_score`、`truncated`；`sources[]` 是本次检索的规范来源清单。 |
| citations | 最终 AI 消息的 `additional_kwargs.knowledge_citations` | 回答正文的 `[kxxxxxxxx]` 标记 | 中间件只在标记对应本次检索来源时写入；伪造/未知 ID 不进入该字段。`citation_valid` 可直接按该字段计算。 |
| trace_id | `GET /runs/{run_id}` 的 `metadata["myflow-trace-id"]` | run metadata | Langfuse 启用时这是 trace id；关闭时只是本地 run trace id。不要用响应头 `X-Trace-Id` 代替（当前中间件生成的请求 trace 与 run context 里的 trace 未绑定）。 |
| user_id | `GET /api/v1/auth/me`（仅会话）或测试账号映射 | 认证结果 | run 响应不返回 `user_id`；PAT 调 `/me` 会 403。跨账号访问该 thread 返回 404 可作为数据过滤的部署校验，但不纳入评分。 |
| skill_commit | `GET /api/skills` 的 `git_commit` 快照 | 实验级元数据 | 当前 run 响应/事件没有 skill 仓库 commit；在实验开始时抓一次并写入 Langfuse experiment metadata。 |

`subagent_calls` 不是 Agent API 的响应字段，而是评测脚本从父代理 `task` tool_calls 归一化出来的证据；不要直接假设 run 响应里有它。

**5.4 需要新增的只读接口**

为了读到 `subagent.start`、`subagent.step`、结构化的 `retrieval.*`（技能/知识库/工具检索）和持久化的 `run.end`/错误事件，需要新增一个受 `runs:read` 保护的只读接口；不要把它做成 `debug=true` 的副作用。

```http
GET /api/threads/{thread_id}/runs/{run_id}/events
  ?after_seq=<int>&limit=200&event_types=subagent.start,subagent.step,retrieval.tool,retrieval.skill,retrieval.kb,llm.tool.result&task_id=<optional>
```

响应仍用现有 `Page` 信封：

```json
{
  "data": [
    {
      "seq": 42,
      "run_id": "...",
      "event_type": "subagent.step",
      "category": "subagent",
      "content": {"message_index": 3, "message": {"type": "tool", "content": "..." }},
      "metadata": {"task_id": "call_...", "subagent_type": "skill-finder"},
      "created_at": "2026-11-10T08:00:00Z"
    }
  ],
  "has_more": true,
  "next_cursor": "42"
}
```

实现约束：

- 复用 `get_run` 的可见性检查：`require_scope("runs:read")`、`require_visible_thread`、`run_manager.get(run_id, user_id=caller.user_id)`，并校验 `record.thread_id == thread_id`；失败一律 404。
- 响应不要带 `user_id`（账本行里有，但 API 不暴露）；`subagent.step` 里可能有子代理上下文，仍然只返回给 thread 所有者。
- `RunEventStore.list_events` 目前没有 `limit` 参数，DB 实现会一次取全量。新增接口时应把 `limit` 加到接口和三个后端，按 `seq` 升序返回；保留现有 `after_seq` / `task_id` 过滤。
- `event_types` 只允许事件目录里的值；新增事件时同步更新 `contracts/run_event_stream_contract.json` 和目录契约测试。
- 事件查询是历史可重放的：run 失败、页面关闭后仍可评分；这正是 debug 响应做不到的。评测环境要求 `run_events.backend=db`（或 `jsonl`），`memory` 后端只适合同进程测试。

**5.4.1 事件可用性与检索事件**

当前 `runtime/events/catalog.py` 已存在：

- `run.start` / `run.end` / `run.error`
- `llm.ai.response` / `llm.tool.result` / `llm.human.input` / `llm.error`
- `subagent.start` / `subagent.step` / `subagent.end`

因此 K0/R0 的子代理路由和子代理步骤已经有账本来源；但 `subagent.step` 有 8192 字符上限，不能作为 RAG 长切片的唯一来源。新增三个结构化检索事件，category 建议为 `retrieval`：

| 事件 | 产生位置 | content（只放最终模型可见结果） |
| --- | --- | --- |
| `retrieval.tool` | `tool_search` 完成召回、过滤、预算裁剪后 | `task_query`、`domain_keywords`、`tools:[{tool_id,call_name,score}]`、`channels`、`warnings`、`truncated` |
| `retrieval.skill` | `search_skills` 完成 Milvus+确认+rerank 后 | `task_query`、`skills:[{skill_id,name,description,score}]`、`channels`、`warnings`、`truncated` |
| `retrieval.kb` | `knowledge_search` 完成 rerank/截断后 | `query`、`keyword_query`、`retrieval_mode`、`reranked`、`items:[{document_id,chunk_id,citation_id,filename,section,page,score,content}]`、`truncated` |

约束：

- 事件按“最终传给模型的那份结果”生成，不做二次排名；rerank 前的候选可以另开 `candidates` 字段，但不作为 R1/R2 的计分输入。
- `retrieval.kb` 的 `content` 必须足够完整，才能算 Faithfulness；超出上限时标 `truncated`，该样本的 R3 直接 N/A，而不是用截断文本硬算。
- 事件通过现有的 run-event 存储落库；新增 category/event_type 时同步更新 `contracts/run_event_stream_contract.json`、目录契约测试和事件查询接口的白名单。
- `subagent.step` 保留用于子代理工具调用和排障；T1/K1/R1 优先读 `retrieval.*`，两者不要重复计分。

如果产品还需要“一次调用直接拿聚合证据”的便利，可以在事件账本之上再加一个只读的 `/runs/{id}/evidence` 投影接口，但它的权威来源仍是 run_event，不能反向要求运行路径为评测额外收集。

**5.5 为什么选 run_event，不选 debug 模式**

- **覆盖范围**：默认架构把知识库和技能检索放在隔离子代理里；debug 响应只从父图收集字段会漏掉 `subagent.step`。补 debug 需要在父循环和隔离循环之间新增收集通道，成本和风险都高于读已有账本。
- **单一事实来源**：事件在 run 结束前由 `RunJournal`/`_SubagentEventBuffer` flush；失败、取消和已结束的 run 都能重放。debug 响应只覆盖当前请求，不能重新评分历史失败样本。
- **安全**：检索结果和子代理上下文可能含用户数据；debug 开关一旦进入常规 API，需要额外的 scope/环境白名单和泄露审查。只读事件接口的授权面更小。
- **运行路径干净**：评测不需要改变 `stream_mode`、响应体或 run 语义；CI 可以用 `runs` + 轮询稳定执行。

**5.6 当前实现的硬缺口**

1. **`subagent.step` 有 8192 字符上限**（`SUBAGENT_STEP_MAX_CHARS`）。知识库结果 JSON 很容易超过该上限而被截断，截断后的 JSON 无法解析；R1 的 ID 可能还在头部，R3 的 Faithfulness 不应在截断数据上计算。解决方式是 5.4.1 的结构化 `retrieval.kb/skill/tool` 事件：它们承载最终模型可见的完整结果，`subagent.step` 只作为回退和排障。
2. **引用校验已补齐**（原缺口）：最终 AI 消息带 `additional_kwargs.knowledge_citations`，正文用稳定的 `[kxxxxxxxx]` 标记；中间件已丢弃未知 ID。`citation_valid` 可以直接与本次 `knowledge_search` 的 `sources[]` 求交。
3. **slash / builtin 不纳入本轮**：评测样本全部是私有 Skill，必须走 skill-finder → `search_skills` → `load_skill(name, id)`；slash 激活和 builtin 技能不生成样本，也不要求补激活事件。
4. **Skill 版本没有按 run 固定**：`GET /api/skills` 能拿到当前 `git_commit`，但 run 事件里没有。实验开始时抓取并写入 experiment metadata；否则 Skill 更新后无法解释分数变化。
5. **Langfuse 用户默认是哈希的**：默认 `user_id_mode=hashed`，样本 `metadata.user_id` 存的就是同一 salt 算出的 hashed user id；这样才能与 Agent trace 的 `userId` 一致。**不要为了评测改成 `raw`**；真实 user_id 只在构建期使用。salt 变更会让所有 hashed id 变化，必须重建数据集并记录 salt 版本。`capture_content=redacted` 也会影响 trace 内容，评测证据不要依赖 Langfuse，继续以 run_event 为准。
6. **命名/指标口径要改**：T1 从 Recall@10 改为 Recall@5；工具名用 `call_name`；`expected_calls` 要从 `tool_execute.args.tool_name/arguments` 还原；K1 从“SkillRouter 检索”改为“skill-finder 的 `search_skills`”；R1 用 `document_id/chunk_id/page/section`；`citation_valid` 已可计算（回答带 `knowledge_citations`）。

**5.7 验收清单**

- [ ] 评测环境 `auth.enabled=true`；所有测试账号都能用 `username` + `password` 登录，正常账号 `needs_setup=false`；启动时一次性校验，错误凭据直接终止评测。
- [ ] 登录后 `/api/v1/auth/me` 返回的 `id` 经同一 salt 计算出的 hashed id 与样本 `metadata.user_id` 一致；真实身份由 Agent 解析，不由请求体下发。
- [ ] 请求体带顶层 `user_id` 返回 422；样本 metadata 里的 `user_id` 不会被转发进 run 请求。
- [ ] 同一 thread 用另一个测试账号读取返回 404（仅数据过滤校验，不评分）。
- [ ] 每条样本使用新的 `thread_id`；run 结束后能按 `run_id` 读到 `subagent.start` / `subagent.step`。
- [ ] K0/R0 按 `task.subagent_type` / `subagent.start` 判路由：skill-finder 对应 K0，knowledge-researcher 对应 R0；不把内部 `knowledge_search` 调用当成 R0。
- [ ] 评测环境 `subagent_enabled=true`、两个检索子代理可用、`skills.usage.prompt_top_k=0`；没有直连 `knowledge_search`、静态 `load_skill` 或 slash 样本。
- [ ] `run.metadata["myflow-trace-id"]` 与 Langfuse trace 可关联。
- [ ] 评测代码不依赖 `debug`、`session_id` 或 run 响应里的检索字段。

**6. Evaluator 设计**

**6.1 Score 一览**

| Score 名称 | 层级 | 实现 | 调用 LLM | 计算方式 |
| --- | --- | --- | --- | --- |
| retrieval_recall@5 | T1 | 规则 | 否 | expected_tools 出现在前 5 个检索结果中的比例 |
| retrieval_mrr | T1 | 规则 | 否 | 首个期望工具排名的倒数 |
| tool_hit | T2 | 规则 | 否 | expected_tools 是否全部被调用 |
| tool_correctness | T2 | DeepEval | 默认否 | ToolCorrectnessMetric |
| args_accuracy | T2 | 规则 | 否 | check_keys 中匹配正确的字段比例 |
| extra_calls_ratio | T3 | 规则 | 否 | 业务工具调用次数 / 期望调用次数；不统计 tool_search/tool_execute/task/load_skill |
| skill_routing | K0 | 规则 | 否 | 是否调用 skill-finder 子代理与 expect_skill 一致记 1 |
| skill_recall@5 | K1 | 规则 | 否 | expected_skills 出现在前 5 个检索结果中的比例 |
| skill_mrr | K1 | 规则 | 否 | 首个期望 Skill 排名的倒数 |
| skill_hit | K2 | 规则 | 否 | expected_skills 是否全部被加载 |
| skill_extra | K2 | 规则 | 否 | 加载了期望之外的 Skill 数量 |
| skill_adherence | K3 | GEval | 是 | 按 skill_rules 判断执行是否遵循 Skill |
| kb_routing | R0 | 规则 | 否 | 是否调用 knowledge-researcher 子代理与 expect_kb 一致记 1 |
| kb_recall@5 | R1 | 规则 | 否 | expected_doc_ids 出现在前 5 个切片中的比例 |
| kb_mrr | R1 | 规则 | 否 | 首个期望文档排名的倒数 |
| contextual_recall | R1 | DeepEval | 是 | 参考答案中的信息被检索内容覆盖的比例 |
| contextual_precision | R2 | DeepEval | 是 | 相关切片是否排在不相关切片之前 |
| contextual_relevancy | R2 | DeepEval | 是 | 检索内容中与问题相关的比例 |
| faithfulness | R3 | DeepEval | 是 | 回答中的陈述被检索内容支持的比例 |
| citation_valid | R3 | 规则 | 否 | 最终消息的 `knowledge_citations` 必须全部出现在本次检索来源中 |
| refusal_ok | R4 | 规则 + LLM | 是 | 无答案样本是否明确说明没有相关信息 |
| answer_quality | E1 | GEval / Langfuse Judge | 是 | 按 judge_rule 或 key_points 评判 |
| answer_correctness | E1 | GEval | 是 | 对照 reference_answer 判断是否一致 |
| answer_relevancy | E1 | DeepEval | 是 | 回答是否切题 |
| forbidden_violation | S1 | 规则 | 否 | 调用了 forbidden_tools 记 1 |
| no_tool_ok | S1 | 规则 | 否 | expect_no_tool 样本未调用工具记 1 |

用户身份、权限和跨用户隔离本轮不评分；固定账号的数据过滤在构建期检查。按样本类型选择 evaluator：不适用的指标返回 None，不计入平均分。比如工具类样本不算 RAG 和 Skill 指标，知识库无答案样本不算 faithfulness 和 contextual_recall。

**6.2 LLM 评判的实现**

- **DeepEval RAG 指标**：Faithfulness、ContextualPrecision/Recall/Relevancy、AnswerRelevancy 都是多步拆解再算分，比单次评判稳定，RAG 部分推荐用 DeepEval。retrieval_context 传入 retrieved_chunks 的文本列表；ContextualPrecision 和 ContextualRecall 需要 expected_output，传入 reference_answer。
- **回答质量 (answer_quality)**：可以用 DeepEval GEval（固定写 evaluation_steps），也可以用 Langfuse LLM-as-a-Judge。用 Langfuse 时的变量映射：
  - {query} -> Trace Input $.query
  - {answer} -> Trace Output $.answer
  - {judge_rule} -> Dataset item Expected Output $.judge_rule
  - {reference_answer} -> Dataset item Expected Output $.reference_answer
- 所有 DeepEval 指标设置 async_mode=False，避免与 Langfuse 并发执行冲突。

**6.2.1 Langfuse 里的 user_id 归属**

这里有两个不同的 Langfuse trace，不能混为一谈：

1. **Agent 自己的 run trace**：`ObservabilityRuntime.start_run()` 通过 `propagate_attributes` 写入 `userId`、`sessionId` 和 tags。默认 `user_id_mode=hashed`，Langfuse 里看到的是 HMAC-SHA256 后的 32 位十六进制值，**不是原始 user_id**；样本 `metadata.user_id` 存同一个 hashed 值，因此二者一致。不要改成 `raw`。`sessionId` 是 `thread_id`，不是评测样本 ID。
2. **Langfuse Dataset Experiment trace**：`dataset.run_experiment()` 会为每个 dataset item 另建 `experiment-item-run` trace，并把 item 级 score 挂到这条 trace 上；当前 SDK 的 experiment trace **不设置 user_id**。`create_score` 也没有 user_id 参数，只有 trace_id / observation_id / dataset_run_id / session_id。

因此：**评测分数本身没有第一等的 user_id 字段，也不会自动继承样本 `metadata.user_id` 作为 Langfuse `userId`。** 如果要按测试用户筛选：

- 在 Dataset item 的 `metadata` 里放 `user_id`（Langfuse hashed user id，与 Agent trace 的 `userId` 一致），`run_experiment` 会把它带到 experiment trace metadata；
- 在 `Evaluation(metadata={"user_id": ...})` 里再放一次，score 的 metadata 里也会有；
- 若确实要 score 挂在带 `userId` 的 Agent trace 上，需要绕过 `run_experiment` 的默认 trace，用 `langfuse.create_score(trace_id=agent_trace_id, ...)` 直接写，或自定义 experiment runner；这时才能用 Agent trace 的 hashed `userId` 过滤。

**6.3 评判模型**

评判模型不弱于被测模型，温度设为 0。在 Langfuse LLM Connections 或 DeepEval 自定义模型中配置公司可用的模型接口。

**6.4 成本控制**

RAG 的 LLM 指标每条要调用模型多次（Faithfulness 要先拆解再逐条核对），全部指标跑全量成本较高。

- **CI 中**：规则指标全量；Faithfulness、answer_correctness、skill_adherence 使用固定随机种子的分层抽样（不是每次随机抽），ContextualPrecision/Relevancy 不跑。
- **最小样本量**：每个 LLM 指标至少 N 条（建议 20）才做硬门禁；低于 N 只报告趋势，避免 10 条样本让 0.85 阈值随机抖动。
- **每周定时评测**：全部指标全量；这时才把 LLM 指标作为硬门禁。
- **规则指标优先**：kb_recall@5 用 ID 比对，不调用 LLM，作为 RAG 检索的主要门禁指标。

**7. 执行流程**

| 场景 | 触发方式 | 数据集 | 评测内容 |
| --- | --- | --- | --- |
| 开发自测 | 本地运行 `uv run --project evals myflow-evals run` | 回归集或其子集 | 全部规则指标 + 按需 LLM 指标 |
| 合并前检查 (CI) | 代码、prompt、检索或切片配置变更时自动运行 | 回归集 | 规则指标全量；LLM 指标抽样；不达标则失败 |
| Skill 更新 | Skill 仓库 Git 提交后运行 | Skill 类回归集 | K0-K3 全部指标（K4 暂不做） |
| 知识库更新 | 文档批量入库或切片策略变更后运行 | 知识库类回归集 | R0-R4 全部指标 |
| 定期评测 | 每周定时 | 回归集 + 探索集 | 全部指标，生成趋势 |
| 网页触发 | Langfuse 远程实验（需版本支持，配置 Authorization header） | 任选 | 同开发自测 |
| 线上监控 | Langfuse LLM-as-a-Judge 对线上 trace 采样 | 线上 trace（采样 5%） | answer_quality、faithfulness |

**7.1 运行形态与项目结构（推荐）**

**同仓库，独立 `evals` 包、独立进程；不要放进 Gateway 进程，也不要做成 FastAPI 路由。** 评测代码需要 Langfuse、DeepEval、LLM key 和长耗时批量执行；放进生产网关会把依赖、凭据和资源消耗耦合到主服务。

推荐目录：

```text
evals/
  pyproject.toml          # myflow-evals，和 backend/harness 同一个 uv workspace
  src/myflow_evals/
    cli.py                # build / run / ingest 子命令
    accounts.py           # 固定账号登录、hashed user id、Client 映射
    api.py                # thread/run/events 的 HTTP 客户端
    evidence.py           # run_event -> 评测证据
    build_dataset.py      # 从账号数据合成 Langfuse Dataset
    run_eval.py           # 读取 Dataset、跑实验、写分数
    evaluators.py         # 规则 + DeepEval 指标
  tests/                  # 证据解析、路由判定、阈值门禁的单测
```

- 根 `pyproject.toml` 的 uv workspace 增加 `evals`；评测依赖只放 `evals/pyproject.toml`，不要污染 `backend` 生产镜像。DeepEval/合成器可放 `evals` 的可选 extra，CI 规则模式只装基础依赖。
- 评测只通过 HTTP 调 Gateway，**不 import `backend.app` 或直接读 Gateway 数据库**；run_event 通过 5.4 的只读接口读取。唯一可以共享的是 `contracts/` 的 wire 契约和少量纯 schema 常量。
- `build` 是低频、半人工的构建命令：测试人员交互输入固定账号凭据，登录后拉工具/Skill/知识库/trace，生成并校验 Dataset；`run` 是批量实验命令：只读已冻结的 Dataset，不重新生成样本。`ingest` 把线上失败 trace 写入探索集，单独运行。

目标命令形态：

```bash
# 构建/更新探索集，交互输入固定账号凭据；--dry-run 只打印统计不写 Langfuse
uv run --project evals myflow-evals build \
  --dataset agent-eval/explore \
  --from-accounts --limit 100 --dry-run

# CI 冒烟：固定回归集、规则指标、低并发
uv run --project evals myflow-evals run \
  --dataset agent-eval/regression/v1 \
  --run-name ci-$(date +%Y%m%d-%H%M) \
  --mode ci --max-items 30 --max-concurrency 2

# 夜间全量：全部指标，独立 job
uv run --project evals myflow-evals run \
  --dataset agent-eval/regression/v1 \
  --run-name nightly-$(date +%Y%m%d) --mode full
```

运行纪律：

- **构建和评测分离**：一次实验只读一个冻结 Dataset；Dataset 变了就是新版本/新实验，不在 run 过程中重新合成。
- **完整性检查**：runner 启动时保存本次 Dataset 的完整 item ID 集合；结束后与 `result.item_results` 的 item ID 对比，任何缺失、task 异常或未评分项都让整次实验失败，不能只看剩余样本的均值。
- **黑盒调用**：runner 只走公开 API；gateway 必须是已部署的测试/预发实例，不要为了评测在生产实例上启动第二个服务。
- **并发从 2 开始**：默认 `run_experiment(max_concurrency=50)` 对 Agent 太重；按 `subagent_runtime.max_running` 和网关容量设 2-4，单用户样本串行或小并发。
- **每条样本独立 thread**，失败重试也换新 thread；不要用 `runs/wait` 的默认 `on_disconnect=cancel`，用 `on_disconnect=continue` 或 `POST /runs` + 轮询。
- **凭据只在 secret store**：`AGENT_USERS` 给构建和 runner 共用；密码、原始 user_id 不写 Dataset/日志；Dataset 只存 hashed `metadata.user_id`。
- **实验 metadata 固定**：`git_sha`、model、dataset_version、`salt_version`、tool/skill/kb 版本、评测模式、并发数；否则分数无法解释。
- **CI 两层**：PR 跑小回归集 + 规则指标 + 抽样 LLM 指标，10 分钟级；夜间跑全量 + DeepEval 全指标。评测失败用非零退出码阻断发布。
- **先 API 后 UI**：当前用 CLI + Langfuse 网页看结果；不要先做评测管理页面，等数据集和指标稳定后再考虑。

实验命名规范：{改动类型}-{简述}-{日期}，例如 tooldesc-v2-20261110、chunk-512-overlap64-20261112、rerank-bge-20261115、skilledesc-v3-20261118。

控制变量：每次实验只改一类因素（prompt、工具描述、Skill 描述、工具或 Skill 检索参数、切片策略、embedding 模型、rerank 模型、底座模型之一），并在实验 metadata 中记录相关配置。

**8. 结果分析与迭代**

1. **按链路看分层指标**：工具链路先看 T1 再看 T2、T3；Skill 链路先看 K0、K1 再看 K2、K3；知识库链路先看 R0、R1，再看 R2、R3。上一层不达标时优先修上一层。
2. **按类别和用户拆分**：在 Langfuse 中按 metadata.category 和样本 `metadata.user_id` 筛选，找出薄弱类别（如 similar_tools、kb_multi_hop）。这要求评测环境保持 `user_id_mode=hashed`，且样本 metadata 与 Agent trace 使用同一个 salt 算出的 hashed user id。
3. **对比实验**：在 Dataset 页面选择两次实验，查看分数变化和单条样本差异。
4. **失败回流**：线上低分或用户纠正的 trace 加入探索集，补充标注后进入下一版回归集。
5. **评判校准**：每月从 LLM 评判结果中抽 50 条进入 Langfuse Annotation Queue 人工标注，计算人工与 LLM 评判的一致性；低于 85% 时修改评判 prompt 或更换评判模型。

**8.1 常见问题定位**

| 现象 | 可能原因 | 改进方向 |
| --- | --- | --- |
| T1 Recall 低 | 工具描述不清、向量与 BM25 权重不当 | 优化工具描述、调整 RRF 参数 |
| T1 高、T2 低 | 相似工具难区分 | 描述中写明适用和不适用场景、增加示例 |
| 参数准确率低 | schema 描述不清、日期等需推理 | 完善参数说明和格式约束 |
| K0 低 | 该委派 skill-finder 时没委派，或不该委派时委派 | 调整子代理描述/路由提示，检查 skill-finder 是否可用 |
| K1 低 | Skill description 不清、与其他 Skill 重叠、元数据过滤条件错误 | 改写 description（写明触发场景和不适用场景）、调整混合检索权重、检查元数据过滤 |
| K1 高、K2 低 | 候选 Skill 太多或太像，模型选错 | 减少 top_k、合并或拆分相似 Skill、在路由 prompt 中增加区分说明 |
| skill_extra 高 | 一次加载多个 Skill，上下文混乱 | 限制单次加载数量、提高加载阈值 |
| K3 低 | Skill 正文太长或步骤不明确 | 精简 SKILL.md、把关键步骤和输出格式放在前面 |
| R0 低 | 该委派 knowledge-researcher 时没委派，或不该委派时委派 | 调整路由提示、子代理描述，检查 knowledge-researcher 是否可用 |
| R1 低 | 切片不合适、embedding 不匹配领域、问法与原文差异大 | 调整切片大小和重叠、混合检索加 BM25、换 embedding 模型、查询改写 |
| R1 高、R2 低 | 噪声切片多、排序差 | 加或换 rerank 模型、减少 top_k |
| R1、R2 高，R3 低 | 模型不遵循资料、自行补充 | 强化“只依据资料回答”的 prompt、要求引用来源 |
| citation_valid 低 | 模型编造引用 | 引用格式改为从检索结果编号中选择 |
| refusal_ok 低 | 无答案时编造 | prompt 中明确无答案时的回复方式 |

| 现象 | 可能原因 | 改进方向 |
| --- | --- | --- |
| answer_quality 低但前面各层都高 | 结果整理或表述问题 | 调整回答生成 prompt |

**9. 实施计划**

| 阶段 | 时间 | 主要工作 | 交付物 |
| --- | --- | --- | --- |
| 阶段 1：基础 | 第 1-2 周 | 按第 5 节接入 thread/run 两阶段 API；新增只读 run events 查询接口（含 subagent.start / subagent.step）；配置固定测试账号（测试人员填 username/password；实际 user_id 只在构建期过滤数据，metadata 写 hashed id）；用账号实际工具/Skill/知识库/线上 trace 合成首版评测集（**人工 Golden 暂不做**）；实现 T1、T2、K0-K2、R0、R1 规则 evaluator；跑通 `myflow-evals run` | 首个基线实验 |
| 阶段 2：扩充 | 第 3-4 周 | 按账号实际可见工具 schema 合成 200-400 条；按私有 Skill 元数据合成 100-150 条并质检；从知识库文档合成约 150 条并完成 ID 映射和质检；补充线上 trace 回流样本；接入 DeepEval RAG 指标；配置 LLM 评判 | 回归集 v1、完整指标 |
| 阶段 3：自动化 | 第 5-6 周 | 接入 CI 门禁；配置 Skill 仓库提交和知识库更新触发评测；配置线上采样评判；建立失败回流流程 | CI 流水线、线上监控 |
| 阶段 4：持续优化 | 长期 | 人工标注校准；评测集和知识库版本同步迭代；按需引入 Promptfoo 做安全测试 | 月度评测报告 |

**10. 风险与注意事项**

- **合成数据偏差**：LLM 生成的问法过于规整，知识库问题容易照抄原文，需改写并结合线上真实问法。
- **测试用户状态污染**：会话历史会让结果不可复现，每条样本独立 thread。
- **user_id 伪造风险**：服务端必须以 token/会话身份为准；评测中尝试在顶层请求体和 metadata 注入 user_id，确认前者 422、后者被删除，并用另一个账号读取同一 thread/run 得到 404。该检查只验证数据/身份边界，不纳入评分。
- **Skill 变更导致样本失效**：Skill 改名、合并或拆分后，按 expected_skills 复查并更新样本。
- **知识库变更导致样本失效**：文档更新后参考答案可能过期，必须按文 ID 复查并重新确认。
- **LLM 评判成本与波动**：RAG 指标调用次数多；CI 中抽样，规则指标作为主要门禁。
- **工具副作用**：写入、删除类工具在评测环境中必须指向沙箱或使用 mock。
- **评测集过拟合**：回归集只用于验证，不用于调优示例。
- **数据安全**：评测样本、trace 和检索切片中不包含真实客户敏感信息，必要时脱敏；token、username/password 只放 secret store，密码不写入 Dataset 或 Langfuse。
- **版本差异**：Langfuse 的 run_experiment、Code Evaluator、远程实验鉴权，以及 DeepEval 指标参数都依赖版本，使用前先确认。

**附录 A：评测脚本 run_eval.py（伪代码，不可直接运行）**

> 注意：以下代码是**伪代码，仅参考逻辑**，不可直接运行；仍包含旧单接口、语法错误和未实现的证据归一化。认证、账号映射、thread 创建、run 轮询、run_event 取数和失败完整性检查都按第 5/7 节重新实现。

整合工具链路规则、知识库 RAG 规则和 DeepEval 指标。SDK 接口以实际安装版本为准。

```python
import os, sys, json, random, httpx
from langfuse import get_client, Evaluation
from deepeval.test_case import LLMTestCase, ToolCall, LLMTestCaseParams
from deepeval.metrics import (ToolCorrectnessMetric, GEval, FaithfulnessMetric,
                AnswerRelevancyMetric, ContextualRecallMetric,
                ContextualPrecisionMetric, ContextualRelevancyMetric)

langfuse = get_client()
API = os.environ["AGENT_API"]
# 旧示例，不要直接使用：按第 5 节从 AGENT_USERS 取 username/password 登录。
# 每个测试 user_id 对应一个 token, 例如:
# AGENT_TOKENS = {"eval_analyst_01": "xxx", "eval_viewer_01": "yyy"}
TOKENS = json.loads(os.environ["AGENT_TOKENS"])
DATASET = os.getenv("DATASET_NAME", "agent-eval/regression/v1")
RUN_NAME = os.getenv("RUN_NAME", "baseline")
MODE = os.getenv("EVAL_MODE", "full") # full: 全部指标; ci: LLM 指标抽样
LLM_SAMPLE = float(os.getenv("LLM_SAMPLE", "0.2"))

# ---------- 调用 Agent ----------
def task(*, item, **kwargs):
    user_id = item.input.get("user_id")
    if not user_id:
        raise ValueError(f"dataset item {item.id} 缺少 user_id")
    token = TOKENS.get(user_id)
    if not token:
        raise ValueError(f"未配置 user_id={user_id} 的测试 token")
    r = httpx.post(API, timeout=180,
        headers={"Authorization": f"Bearer {token}"},
        json={"message": item.input["query"],
           "user_id": user_id,
           "session_id": f"eval-{RUN_NAME}-{item.id}",
           "debug": True})
    r.raise_for_status()
    out = r.json()
    out["_req_user_id"] = user_id # 供隔离检查使用
    return out

def called(o):    return [c["tool"] for c in o.get("tool_calls", [])]
def chunks(o):    return o.get("retrieved_chunks", [])
def chunk_ids(o):   return [f'{c["doc_id"]}'#'{c["loc"]}' if c.get("loc") else c["doc_id"] for c in chunks(o)]
def ctx_text(o):   return [c["text"] for c in chunks(o)]
def is_kb(exp):    return bool(exp.get("expect_kb"))
def run_llm():    return MODE == "full" or random.random() < LLM_SAMPLE

def hit(expected, retrieved):
    ""expected 可以是 doc_id 或 doc_id#loc; 只写 doc_id 时匹配该文档任意位置""
    return any(r == expected or r.split("#")[0] == expected for r in retrieved)

# ================= 工具链路 =================
def retrieval_recall(*, output, expected_output, **kw):
    exp = expected_output.get("expected_tools", [])
    if not exp: return Evaluation(name="retrieval_recall@5", value=None)
    top = output.get("retrieved_tools", [])[0:5]
    return Evaluation(name="retrieval_recall@5", value=sum(t in top for t in exp) / len(exp))

def retrieval_mrr(*, output, expected_output, **kw):
    exp = set(expected_output.get("expected_tools", []))
    if not exp: return Evaluation(name="retrieval_mrr", value=None)
    for i, t in enumerate(output.get("retrieved_tools", []), 1):
        if t in exp: return Evaluation(name="retrieval_mrr", value=1 / i)
    return Evaluation(name="retrieval_mrr", value=0.0)

def tool_hit(*, output, expected_output, **kw):
    exp = set(expected_output.get("expected_tools", []))
    if not exp: return Evaluation(name="tool_hit", value=None)
    return Evaluation(name="tool_hit", value=float(exp <= set(called(output))))

def args_accuracy(*, output, expected_output, **kw):
    total = ok = 0
    actual = {c["tool"]: c.get("args", {}) for c in output.get("tool_calls", [])}
    for ec in expected_output.get("expected_calls", []):
        for k in ec.get("check_keys") or list(ec.get("args", {})):
            v = ec["args"].get(k)
            if isinstance(v, str) and v.startswith("<"): continue # 占位符另行校验
            total += 1
            ok += actual.get(ec["tool"], {}).get(k) == v
    return Evaluation(name="args_accuracy", value=(ok / total) if total else None)

def tool_correctness(*, input, output, expected_output, **kw):
    exp = expected_output.get("expected_tools", [])
    if not exp: return Evaluation(name="tool_correctness", value=None)
    m = ToolCorrectnessMetric(async_mode=False)
    m.measure(LLMTestCase(input=input["query"], actual_output=output.get("answer", ""),
        tools_called=[ToolCall(name=t) for t in called(output)],
        expected_tools=[ToolCall(name=t) for t in exp]))
    return Evaluation(name="tool_correctness", value=m.score, comment=m.reason)

def extra_calls_ratio(*, output, expected_output, **kw):
    expected_calls = expected_output.get("expected_calls")
    if not expected_calls: return Evaluation(name="extra_calls_ratio", value=None)
    # business_tool_calls 已排除 tool_search/tool_execute/task/load_skill 等基础设施调用
    actual = len(output.get("business_tool_calls", []))
    return Evaluation(name="extra_calls_ratio", value=float(actual / len(expected_calls)))

# ================= Skills 链路 =================
def r_skills(o):   return o.get("retrieved_skills", [])
def l_skills(o):   return o.get("loaded_skills", [])
def subagent_types(o): return [c.get("subagent_type") for c in o.get("subagent_calls", [])]

def skill_routing(*, output, expected_output, **kw):
    if "expect_skill" not in expected_output: return Evaluation(name="skill_routing", value=None)
    return Evaluation(name="skill_routing",
        value=float(("skill-finder" in subagent_types(output)) == bool(expected_output["expect_skill"])))

def skill_recall(*, output, expected_output, **kw):
    exp = expected_output.get("expected_skills", [])
    if not exp: return Evaluation(name="skill_recall@5", value=None)
    top = r_skills(output)[:5]
    return Evaluation(name="skill_recall@5", value=sum(s in top for s in exp) / len(exp))

def skill_mrr(*, output, expected_output, **kw):
    exp = set(expected_output.get("expected_skills", []))
    if not exp: return Evaluation(name="skill_mrr", value=None)
    for i, sname in enumerate(r_skills(output), 1):
        if sname in exp: return Evaluation(name="skill_mrr", value=1 / i)
    return Evaluation(name="skill_mrr", value=0.0)

def skill_hit(*, output, expected_output, **kw):
    exp = set(expected_output.get="expected_skills", []))
    if not exp: return Evaluation(name="skill_hit", value=None)
    return Evaluation(name="skill_hit", value=float(exp <= set(l_skills(output))))

def skill_extra(*, output, expected_output, **kw):
    if "expect_skill" not in expected_output: return Evaluation(name="skill_extra", value=None)
    extra = set(l_skills(output)) - set(expected_output.get("expected_skills", []))
    return Evaluation(name="skill_extra", value=float(len(extra)), comment="".join(extra) or None)


def skill_adherence(*, input, output, expected_output, **kw):
    rules = expected_output.get("skill_rules")
    if not rules or not l_skills(output) or not run_llm():
        return Evaluation(name="skill_adherence", value=None)
    calls = json.dumps(output.get("tool_calls", []), ensure_ascii=False)
    m = GEval(name="skill_adherence", async_mode=False,
        evaluation_steps=[f"Skill 要求: {{r}}" for r in rules] +
                ["结合实际输出和工具调用记录, 逐项判断是否满足 Skill 要求",
                "未满足的要求越多分数越低"],
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT])
        m.measure(LLMTestCase(input=input["query"],
               actual_output=f"{{output.get('answer', '')}}\n\n[工具调用记录]\n{calls}")
    return Evaluation(name="skill_adherence", value=m.score, comment=m.reason)

# ================= 知识库 RAG 链路 =================
def kb_routing(*, output, expected_output, **kw):
    if "expect_kb" not in expected_output: return Evaluation(name="kb_routing", value=None)
    routed = any(c.get("subagent_type") == "knowledge-researcher"
                 for c in output.get("subagent_calls", []))
    return Evaluation(name="kb_routing", value=float(routed == is_kb(expected_output)))

def kb_recall(*, output, expected_output, **kw):
    exp = expected_output.get("expected_doc_ids", [])
    if not exp: return Evaluation(name="kb_recall@5", value=None)
    top = chunk_ids(output)[:5]
    return Evaluation(name="kb_recall@5", value=sum(hit(e, top) for e in exp) / len(exp))

def kb_mrr(*, output, expected_output, **kw):
    exp = expected_output.get("expected_doc_ids", [])
    if not exp: return Evaluation(name="kb_mrr", value=None)
    for i, r in enumerate(chunk_ids(output), 1):
        if any(hit(e, [r]) for e in exp): return Evaluation(name="kb_mrr", value=1 / i)
    return Evaluation(name="kb_mrr", value=0.0)

def citation_valid(*, output, expected_output, **kw):
    cites = [c.get("chunk_id") for c in output.get("knowledge_citations", [])
             if isinstance(c, dict) and c.get("chunk_id")]
    if not is_kb(expected_output) or not cites: return Evaluation(name="citation_valid", value=None)
    ids = chunk_ids(output)
    bad = [c for c in cites if not hit(c, ids)]
    return Evaluation(name="citation_valid", value=float(not bad), comment="".join(bad) or None)


def _rag_case(input, output, expected_output):
    return LLMTestCase(input=input["query"], actual_output=output.get("answer", ""),
        expected_output=expected_output.get("reference_answer"),
        retrieval_context=ctx_text(output))
```

```python
def _rag_metric(name, metric_cls, need_ref=False):
    def ev(*, input, output, expected_output, **kw):
        if (not is_kb(expected_output) or expected_output.get("expect_refusal")
            or not chunks(output) or (need_ref and not expected_output.get("reference_answer"))
            or not run_llm():
            return Evaluation(name=name, value=None)
        m = metric_cls(async_mode=False)
        m.measure(_rag_case(input, output, expected_output))
        return Evaluation(name=name, value=m.score, comment=m.reason)
    ev.__name__ = name
    return ev

faithfulness = _rag_metric("faithfulness", FaithfulnessMetric)
contextual_recall = _rag_metric("contextual_recall", ContextualRecallMetric, need_ref=True)
contextual_precision = _rag_metric("contextual_precision", ContextualPrecisionMetric, need_ref=True)
contextual_relevancy = _rag_metric("contextual_relevancy", ContextualRelevancyMetric)

def refusal_ok(*, input, output, expected_output, **kw):
    if not expected_output.get("expect_refusal"): return Evaluation(name="refusal_ok", value=None)
    m = GEval(name="refusal_ok", async_mode=False,
        evaluation_steps=["知识库中没有该问题的答案",
            "判断实际输出是否明确说明没有找到相关信息",
            "实际输出给出了具体但无依据的答案时判为不通过"],
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT])
    m.measure(LLMTestCase(input=input["query"], actual_output=output.get("answer", ""))
    return Evaluation(name="refusal_ok", value=m.score, comment=m.reason)

# ========== 端到端 ==========
def answer_quality(*, input, output, expected_output, **kw):
    rule = expected_output.get("judge_rule")
    points = expected_output.get="key_points")
    if not (rule or points) or not run_llm(): return Evaluation(name="answer_quality", value=None)
    steps = [f"判定标准: {rule}" if rule else [f"回答需包含要点: {p}" for p in points]
    steps += ["逐项检查实际输出是否满足", "存在编造或与问题无关的内容时扣分"]
    m = GEval(name="answer_quality", evaluation_steps=steps, async_mode=False,
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT])
    m.measure(LLMTestCase(input=input["query"], actual_output=output.get("answer", ""))
    return Evaluation(name="answer_quality", value=m.score, comment=m.reason)

def answer_correctness(*, input, output, expected_output, **kw):
    ref = expected_output.get("reference_answer")
    if not ref or expected_output.get("expect_refusal") or not run_llm():
        return Evaluation(name="answer_correctness", value=None)
    m = GEval(name="answer_correctness", async_mode=False,
        evaluation_steps=["对比实际输出与参考答案中的关键事实和数值",
            "关键事实不一致或数值错误时大幅扣分",
            "表述方式不同但含义一致不扣分"],
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT,
            LLMTestCaseParams.EXPECTED_OUTPUT])
    m.measure(LLMTestCase(input=input["query"], actual_output=output.get("answer", ""),
            expected_output=ref))
    return Evaluation(name="answer_correctness", value=m.score, comment=m.reason)

def answer_relevancy(*, input, output, expected_output, **kw):
    if MODE != "full": return Evaluation(name="answer_relevancy", value=None)
    m = AnswerRelevancyMetric(async_mode=False)
    m.measure(LLMTestCase(input=input["query"], actual_output=output.get("answer", ""))
    return Evaluation(name="answer_relevancy", value=m.score, comment=m.reason)

# ========== 安全与边界 ==========
def forbidden_violation(*, output, expected_output, **kw):
    forbid = expected_output.get("forbidden_tools", [])
    if not forbid: return Evaluation(name="forbidden_violation", value=None)
    bad = set(forbid) & set(called(output))
    return Evaluation(name="forbidden_violation", value=float(bool(bad)), comment=".".join(bad) or N
one)

def no_tool_ok(*, output, expected_output, **kw):
    if not expected_output.get("expect_no_tool"): return Evaluation(name="no_tool_ok", value=None)
    return Evaluation(name="no_tool_ok", value=float(not called(output))


RULE_EVALUATORS = [retrieval_recall, retrieval_mrr, tool_hit, args_accuracy, extra_calls_ratio,
        skill_routing, skill_recall, skill_mrr, skill_hit, skill_extra,
        kb_routing, kb_recall, kb_mrr, citation_valid,
        forbidden_violation, no_tool_ok]
LLM_EVALUATORS = [tool_correctness, skill_adherence, faithfulness, contextual_recall, refusal_ok,
        answer_quality, answer_correctness]
FULL_ONLY = [contextual_precision, contextual_relevancy, answer_relevancy]

THRESHOLDS = {"retrieval_recall@5": 0.95, "tool_hit": 0.90, "args_accuracy": 0.85,

        "extra_calls_ratio": 1.5,
        "skill_routing": 0.95, "skill_recall@5": 0.95, "skill_hit": 0.90,
        "skill_adherence": 0.85,
        "kb_routing": 0.95, "kb_recall@5": 0.85, "faithfulness": 0.90,
        "citation_valid": 0.95, "refusal_ok": 0.90, "answer_quality": 0.85,
        "answer_correctness": 0.80}
MUST_BE_ZERO = ["forbidden_violation"]

if __name__ == "__main__":
    evaluators = RULE_EVALUATORS + LLM_EVALUATORS + (FULL_ONLY if MODE == "full" else [])
    dataset = langfuse.get_dataset(DATASET)
    result = dataset.run_experiment(name=RUN_NAME, task=task, evaluators=evaluators,
        max_concurrency=4,
        metadata={"mode": MODE})
    langfuse.flush()
    
    agg = {}
    for r in result.item_results:
        for e in r.evaluations:
            if e.value is not None:
                agg.setdefault(e.name, []).append(e.value)
    failed = False
    for name, vals in sorted(agg.items()):
        avg = sum(vals) / len(vals)
        th = THRESHOLDS.get(name)
        flag = "" if th is None else ("PASS" if avg >= th else "FAIL")
        if name in MUST_BE_ZERO:
            flag = "PASS" if sum(vals) == 0 else "FAIL"
        failed |= flag == "FAIL"
        print(f"{name:26s} {len(vals):4d} avg={avg:.3f} {flag}")
    sys.exit(1 if failed else 0)
```

**运行方式**
```bash
# 本地全量
python run_eval.py

# CI: LLM 指标抽样 20%，不跑 ContextualPrecision/Relevancy, AnswerRelevancy
EVAL_MODE=ci RUN_NAME=ci-$(date +%Y%m%d) python run_eval.py
```

****附录 B：从知识库文档生成样本（伪代码，不可直接运行）**

> 只参考生成流程，不可直接复制执行；实际实现应放在 `evals/` 并补测试。

```python
from deepeval.synthesizer import Synthesizer
from langfuse import get_client
```

```python
langfuse = get_client()
synth = Synthesizer() # 可传入自定义评判模型；无 OpenAI key 时还需配置自定义 embedding 模型
goldens = synth.generate_goldens_from_docs(
    document_paths=["kb/pf_guide_2026.pdf", "kb/policy.docx"],
    include_expected_output=True,
    max_goldens_per_context=2,
)

def map_doc_id(golden):
    # 根据 golden 的来源文件和 context 文本，映射回你系统中的 doc_id#loc
    # 例如：用 context 文本在你的切片库中做精确或模糊匹配
    ...
EVAL_USER_ID = "<langfuse hashed user id>"  # 构建脚本登录固定账号后从 /me.id 算 HMAC，写入 metadata
for i, g in enumerate(goldens):
    langfuse.create_dataset_item(
        dataset_name="agent-eval/explore",
        id=f"kb_syn_{i:04d}",
        input={"query": g.input},
        expected_output={"expect_kb": True,
            "expected_doc_ids": map_doc_id(g),
            "reference_answer": g.expected_output},
        metadata={"user_id": EVAL_USER_ID,
            "category": "kb_single_hop", "source": "synthetic_from_docs",
            "review_status": "pending"},
        )

```
生成后先放进探索集，review_status 为 pending。人工审核通过后，再挑选并入回归集。

******附录 C：从 Skill 仓库生成样本（伪代码，不可直接运行）**

> 只参考生成流程，不可直接复制执行；实际实现应放在 `evals/` 并补测试。

```python
import glob, json, re, yaml
from openai import OpenAI # 或公司内部兼容 OpenAI 协议的模型接口
from langfuse import get_client

langfuse = get_client()
llm = OpenAI()

def load_skills(repo="skills-repo"):
    for path in glob.glob(f"{repo}/**/SKILL.md", recursive=True):
        text = open(path, encoding="utf-8").read()
        m = re.match(r"^-+\n(\.*?)\n---\n(\.*?)$", text, re.S)
        meta, body = (yaml.safe_load(m.group(1)), m.group(2)) if m else ({}, text)
        yield {"name": meta.get("name"), "description": meta.get("description", ""), "body": body}

PROMPT = """你是评测数据构造员。下面是一个 Agent Skill:

名称: {name}
描述: {description}
正文 (节选): {body}

请输出 JSON:
{{="queries": [5 条会触发该 Skill 的中文用户请求，口语化、风格各异],
   "skill_rules": [3-5 条从正文提炼的、可检查的执行要求]}]}"""

for sk in load_skills():
    resp = llm.chat.completions.create(
        model="gpt-4.1", temperature=0.7, response_format={"type": "json_object"},
        messages=[{"role": "user", "content": PROMPT.format(
            name=sk["name"], description=sk["description"], body=sk["body"][:3000])}]
    data = json.loads(resp.choices[0].message.content)
EVAL_USER_ID = "<langfuse hashed user id>"  # 构建脚本登录固定账号后从 /me.id 算 HMAC，写入 metadata
for i, q in enumerate(data["queries"]):
    langfuse.create_dataset_item(
        dataset_name="agent-eval/explore",
        id=f"skill_syn_{sk['name']}_{i}",
        input={"query": q},
        expected_output={"expect_skill": True,
            "expected_skills": [sk["name"]],
            "skill_rules": data["skill_rules"]},
        metadata={"user_id": EVAL_USER_ID,
            "category": "skill_single", "source": "synthetic_from_skill",
            "review_status": "pending"},
        )
```

• 生成后同样先放进探索集，人工审核后再并入回归集。
• **相似 Skill 区分样本**：先用 embedding 找出 description 相似度高的 Skill 对，再让 LLM 针对每一对生成“只适合其中一个”的问法，标注为 similar_skills。
• **权限样本**：本轮不做；固定账号评测只用该账号自己的 Skill 数据。

****附录 D：环境变量**

| 变量 | 说明 |
| --- | --- |
| LANGFUSE_PUBLIC_KEY /LANGFUSE_SECRET_KEY / LANGFUSE_HOST | Langfuse 连接信息 |
| AGENT_API | 网关根地址（包含 `/api`，例如 `http://127.0.0.1:8001/api`） |
| AGENT_USERS | 固定测试账号的登录凭据映射 (JSON)，例如 `{"analyst_a": {"username": "...", "password": "..."}}`；构建脚本登录后把实际 `users.id` 算成 Langfuse hashed id 写入样本 `metadata.user_id`，原始 user_id/密码只放 secret store，不进数据集 |
| AGENT_TOKENS | 可选：已预置 PAT 到测试 user 的映射。当前没有 PAT 的 HTTP 签发路由，优先用 `AGENT_USERS` 登录 |
| OPENAI_API_KEY 或自定义模型配置 | DeepEval 评判模型和 Synthesizer 使用 |
| DATASET_NAME / RUN_NAME | 本次评测的数据集和实验名称 |

| 变量 | 说明 |
| --- | --- |
| EVAL_MODE | full 或 ci |
| LLM_SAMPLE | CI 模式下 LLM 指标的抽样比例，默认 0.2 |
