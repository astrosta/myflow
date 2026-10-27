# Agent 记忆模块规格说明

> 状态：实施规格（Review Draft）
> 日期：2026-10-26
> 设计依据：[Agent 记忆模块设计](2026-10-26-agent-memory-module-design.md)
> 记忆架构参考：[ai_agent.md](ai_agent.md)
> LangMem 算子参考：[langmem_ai_agent.md](langmem_ai_agent.md)
> 技能管理依据：[技能包 Git 发布、私有创作与审核设计](../superpowers/specs/2026-10-23-skill-package-git-release-design.md)
>
> 本文把设计文档收敛为实现契约。与设计文档冲突时，以本文的固定决策和验收条款为准；未覆盖的细节回看设计文档。

## 1. 范围

实现一个跨 thread 的长期记忆模块，支持：

1. 用户画像 profile：有界 schema、对话直写、手动页面修改、字段墓碑。
2. 语义记忆 semantic：原子事实、Milvus 检索、置信度门控、pending_review。
3. 情景记忆 episodic：session 缓冲、任务级 episode、脱水摘要、纠正、Milvus 检索。
4. 程序性记忆 procedural：技能候选、用户前端确认、复用现有技能系统、公开审核不变。
5. PostgreSQL 权威存储 + Milvus 活跃投影 + outbox。
6. LangMem 无状态算子 A/B/C/D 作为唯一提取实现。
7. 任务分类器：新用户消息到达时在 before_agent 判断任务边界，输出 CONTINUE/NEW_TASK/UNCERTAIN 并生成新任务的 task_goal；task_id 由宿主代码生成。

不在本规格内：

- 团队/组织/跨租户共享记忆。
- 自动创建、自动激活、自动公开 Skill。
- 原始 transcript/工具输出的长期保存。
- 现有技能编辑器、Git 发布、管理员审核台的重新实现。
- MCP、知识库、护栏插件。

## 2. 已固定的决策

| 项 | 决策 |
|---|---|
| 向量库 | Milvus；collection myflow_memory_active |
| 权威存储 | PostgreSQL；Milvus 可重建 |
| LangMem | 最新稳定版；uv.lock 精确锁定；手动升级 |
| 算子模型 | AppConfig.agents.default_model；未设置取 models[0] |
| 单次 run 模型 | 不影响记忆算子模型 |
| 情景 session | 空闲 30 分钟关闭；最多 40 轮 |
| 任务分类器 | before_agent；输出 CONTINUE/NEW_TASK/UNCERTAIN + task_goal；task_goal 为空强制 NEW_TASK |
| task_id | 只由宿主代码生成；模型/客户端不可指定 |
| task_goal | 仅 NEW_TASK 由分类器生成；CONTINUE/UNCERTAIN 不改写；分类器失败可用 fallback |
| UNCERTAIN | 默认继续当前任务，不切 session |
| 长期记忆关闭 | 任务分类器不挂载、不运行、不生成 task_context |
| 画像直写阈值 | confidence >= 0.85 |
| 语义自动 active | confidence >= 0.80 |
| 语义 pending_review | 0.50 <= confidence < 0.80；30 天过期 |
| pending 后续证据 | 参与 A 的 existing 候选；过阈值自动晋升 |
| 画像手动修改 | “我的画像”页；普通写入，不设置 pin；后续高证据可覆盖 |
| 程序性候选 | 默认 3 条成功 episode、至少 2 个 thread；用户显式要求可 1 条 |
| 技能确认 | 用户在前端确认；私有 Skill；公开走现有 admin review |
| 情景存储 | 摘要 + 证据引用；不存原始 transcript |
| 删除 | 墓碑优先；账号删除才物理清除 |

## 3. 不变量

1. 所有读写必须绑定一个 user_id；普通 API 和模型工具不得接受 user_id。
2. PostgreSQL 是唯一权威；Milvus 命中后必须回 PG 验证 user_id、status、valid_to、revision。
3. 只有 active 的 semantic/episodic 进入 Milvus；pending_review、conflicted、dormant、archived、corrected、deleted 不进入。
4. profile 与程序性 Skill 不进入记忆 Milvus；profile 直接读 PG；Skill 由现有技能系统检索。
5. 历史对话不重复提取：同一 message_id 只进入一个 session；已关闭 session 的 episode_key 不重复入队；压缩钩子只补未处理 message_id。
6. 画像自动写只使用当前新的真实用户消息作为证据；历史消息、checkpoint、episodic 摘要不能作为画像证据。
7. 画像没有 pin 字段；页面编辑是普通写入，后续高证据对话可以覆盖；低证据/含糊冲突仍 HOLD。
8. 技能候选不是 Skill；没有用户确认事件，不创建 Skill。
9. 所有提取只由 LangMem 无状态算子完成；LangMem 不持有 store，不直接写 PG/Milvus。
10. 任何多记录状态变更都必须写 memory_mutation_ledger 和 memory_index_outbox。
11. task_id 只由宿主代码生成；model/client 不得指定。task_goal 只允许来自分类器的 NEW_TASK 分支或显式 fallback，CONTINUE/UNCERTAIN 不得改写当前 task_goal。
12. 任务分类器只在 `memory.enabled=true` 且 `task_classifier.enabled=true` 时运行，每轮最多在 lead 的 before_agent 运行一次；只写 ThreadState.task_context 和 task:classified 事件，不写记忆、不直接关闭 session；子代理不运行；长期记忆关闭时不生成 task_context。
13. 分类器返回非法/空 task_goal 时不得静默写库：原 task_goal 为空则 fallback，非空则降级 UNCERTAIN，不切任务。

## 4. 组件

| 组件 | 输入 | 输出 | 禁止 |
|---|---|---|---|
| Capture | after_agent/turn finalizer 的可见轮次 | session 追加 | 调 LLM |
| Task Classifier | 新用户消息 + task_goal + 当前任务 user messages + final AI replies | CONTINUE/NEW_TASK/UNCERTAIN + task_goal | 写记忆、生成 task_id、直接关闭 session |
| Session Closer | 终态/压缩/显式/空闲/轮数/task_boundary | memory_ingest_jobs | 每轮直接抽取 |
| L0/L1 Gate | session 元数据/工具摘要 | 通过/拒绝 | 直接写记忆 |
| LangMem Operator A/B/C/D | 结构化输入 | proposal | 直接写 store |
| Resolver | proposal + existing | ADD/UPDATE/DELETE/NOOP/HOLD | 让模型决定胜负 |
| Repository | proposal/decision | PG transaction + ledger + outbox | 调 Milvus |
| Projector | outbox | Milvus upsert/delete | 决定记忆状态 |
| Retriever | query + scope | 已确认记忆 | 绕过 PG 确认 |
| Skill Bridge | candidate + user decision | 现有技能服务调用 | 新建 Skill 状态机 |

## 5. 数据模型

### 5.1 PostgreSQL 表

以下字段为最小契约；SQLite 开发环境允许等价类型，生产使用 PostgreSQL。

#### memory_profiles

| 列 | 类型 | 约束 |
|---|---|---|
| user_id | text | PK |
| schema_version | int | not null |
| revision | bigint | not null；CAS |
| profile | jsonb | not null；有界字段 |
| tombstones | jsonb | not null；字段级 cleared_at |
| updated_at | timestamptz | not null |

profile 字段结构：

~~~json
{
  "identity": {
    "preferred_name": {
      "value": "小王",
      "source": "conversation_extracted",
      "evidence_tier": "EXPLICIT_STATEMENT",
      "confidence": 0.85,
      "observed_at": "2026-10-26T...",
      "confirmed_at": null,
      "tombstoned_at": null
    }
  }
}
~~~

#### memory_records

| 列 | 类型 | 约束 |
|---|---|---|
| id | uuid | PK |
| user_id | text | not null |
| kind | text | semantic / episodic |
| subtype | text | fact / episode |
| status | text | 见状态机 |
| revision | bigint | not null |
| content | text | 可检索正文 |
| payload | jsonb | 类型 schema |
| topic_key | text | 可空 |
| scope_condition | text | 默认 global |
| source | text | 来源 |
| evidence_tier | text | evidence tier |
| confidence | real | 0..1 |
| consolidation_status | text | episodic: pending/locked/done/error |
| consolidated_at | timestamptz | 可空 |
| salience | real | 默认 0.5 |
| hit_count | int | 默认 0 |
| last_active_at | timestamptz | 默认 now |
| explicit_remember | boolean | 默认 false |
| explicit_remember | boolean | 默认 false；仅语义保护，不是画像 pin |
| parent_memory_id | uuid | 可空；版本链 |
| correction_of | uuid | 可空；情景纠正 |
| valid_from | timestamptz | 可空 |
| valid_to | timestamptz | 可空；NULL 当前 |
| occurred_at | timestamptz | 情景时间 |
| task_id | text | 可空；任务归属 |
| thread_id | text | 可空 |
| agent_name | text | 可空；仅元数据 |
| provenance | jsonb | evidence refs/hash |
| source_episode_ids | uuid[] | 可空 |
| archived_at | timestamptz | 可空 |
| deleted_at | timestamptz | 可空 |
| created_at | timestamptz | not null |
| updated_at | timestamptz | not null |

索引/约束：

- 唯一 (user_id, id)。
- (user_id, kind, status)。
- (user_id, topic_key, status)。
- (user_id, task_id)。
- (user_id, consolidation_status) where kind = 'episodic'。
- partial unique (user_id, correction_of) where status = 'active' and correction_of is not null。
- (user_id, valid_to, kind)。

#### memory_record_versions

| 列 | 类型 | 约束 |
|---|---|---|
| memory_id | uuid | PK part |
| user_id | text | not null |
| revision | bigint | PK part |
| status | text | not null |
| content | text | not null |
| payload | jsonb | not null |
| valid_from/valid_to | timestamptz | 可空 |
| changed_at | timestamptz | not null |

PK (memory_id, revision)。

#### memory_episode_sessions

| 列 | 类型 | 说明 |
|---|---|---|
| id | uuid | PK |
| user_id | text | not null |
| thread_id | text | not null |
| task_id | text | not null；宿主代码生成；同一 thread 内标识任务 |
| task_goal | text | not null；空表示尚未开始任务；分类器生成 |
| task_goal_source | text | classifier/fallback |
| boundary_decision | text | CONTINUE/NEW_TASK/UNCERTAIN；可空 |
| boundary_uncertain | boolean | 默认 false |
| status | text | open/closed/locked/swept |
| open_turn_id | text | 首轮 ID |
| last_turn_id | text | 末轮 ID |
| episode_key | text | 关闭时生成 |
| turn_refs | jsonb | message/tool/run 引用；不含原文 |
| last_activity_at | timestamptz | 空闲关闭依据 |
| closed_at | timestamptz | 可空 |
| close_reason | text | terminal/compression/explicit/idle/max_turns/task_boundary |
| created_at/updated_at | timestamptz | not null |

部分唯一索引：(user_id, thread_id) where status='open'。

约束：task_id 只由宿主代码生成；task_goal 由分类器在 NEW_TASK 生成或 fallback；CONTINUE/UNCERTAIN 不改写 task_goal；task_goal 为空表示新任务。

#### memory_ingest_jobs

| 列 | 类型 | 说明 |
|---|---|---|
| id | uuid | PK |
| user_id | text | not null |
| thread_id | text | not null |
| episode_session_id | uuid | not null |
| episode_key | text | unique |
| job_type | text | episodic_dehydrate |
| status | text | pending/locked/done/error/dead |
| lease_until | timestamptz | 可空 |
| attempts | int | 默认 0 |
| payload | jsonb | 引用和摘要（含 task_id/turn_refs），不允许原始 transcript |
| created_at/updated_at | timestamptz | not null |

#### memory_consolidation_jobs

| 列 | 类型 | 说明 |
|---|---|---|
| id | uuid | PK |
| user_id | text | not null |
| source_episode_ids | uuid[] | not null |
| kind_target | text | semantic/procedural |
| status | text | pending/locked/done/error |
| lease_until | timestamptz | 可空 |
| attempts | int | 默认 0 |
| result_ref | text | 可空 |
| created_at/updated_at | timestamptz | not null |

#### memory_skill_candidates

| 列 | 类型 | 说明 |
|---|---|---|
| id | uuid | PK |
| user_id | text | not null |
| status | text | proposed/validating/pending_confirmation/creating/confirmed_draft/saved/failed/rejected/expired |
| revision | bigint | 确认 CAS |
| private_name | text | 私有名 |
| description | text | 可空 |
| trigger_text | text | 触发条件 |
| package_snapshot | jsonb | SKILL.md + 文件/hash |
| package_sha256 | text | 必填 |
| skill_md_sha256 | text | 必填 |
| base_skill_version_id | text | 可空 |
| diff_patch | text | 可空 |
| validation_report | jsonb | 静态校验结果 |
| source_episode_ids | uuid[] | not null |
| origin_idempotency_key | text | unique |
| skill_id | text | 确认后 |
| skill_version_id | text | 确认并保存后 |
| confirmed_by/at | text/timestamptz | 可空 |
| rejected_reason | text | 可空 |
| created_at/updated_at | timestamptz | not null |

#### memory_conflicts

| 列 | 类型 | 说明 |
|---|---|---|
| id | uuid | PK |
| user_id | text | not null |
| kind | text | profile/semantic |
| field_path | text | 画像字段 |
| topic_key | text | 语义 |
| current_record_id | uuid/text | 可空 |
| candidate_record_id | uuid/text | 可空 |
| current_snapshot | jsonb | 必填 |
| candidate_snapshot | jsonb | 必填 |
| status | text | open/resolved/dismissed |
| evidence_refs | jsonb | 证据 |
| created_at/resolved_at | timestamptz | not null/可空 |

#### memory_mutation_ledger

| 列 | 类型 | 说明 |
|---|---|---|
| tx_id | uuid | PK |
| user_id | text | not null |
| actor_type | text | SYSTEM/USER/ADMIN |
| actor_id | text | not null |
| op_type | text | ADD/UPDATE/DELETE/STAGE_DRAFT/APPROVE_SKILL/REJECT_SKILL/ROLLBACK |
| before_id/after_id | uuid/text | 可空 |
| before_revision/after_revision | bigint | 可空 |
| before_status/after_status | text | 可空 |
| op_json | jsonb | 变更 |
| inverse_json | jsonb | 回滚信息 |
| rollback_of | uuid | 可空 |
| source_episode_ids | uuid[] | 可空 |
| reason | text | 可空 |
| created_at | timestamptz | not null |

#### memory_index_outbox

| 列 | 类型 | 说明 |
|---|---|---|
| id | uuid | PK |
| user_id | text | not null |
| pk | text | user_id:memory_id |
| memory_id | uuid | not null |
| op | text | upsert/delete |
| revision | bigint | not null |
| epoch | bigint | 清除水位 |
| state | text | pending/locked/done/error |
| attempts | int | 默认 0 |
| next_attempt_at | timestamptz | 可空 |
| last_error | text | 可空 |
| created_at/updated_at | timestamptz | not null |

唯一 (memory_id, revision, op)。

#### memory_scope_meta

| 列 | 类型 | 说明 |
|---|---|---|
| user_id | text | PK |
| cleared_at | timestamptz | 可空 |
| epoch | bigint | 默认 0 |
| updated_at | timestamptz | not null |

### 5.2 状态机

| 类型 | 状态 |
|---|---|
| profile 字段 | active / tombstoned |
| semantic | active / pending_review / expired / superseded / folded / dormant / archived / deleted / conflicted / deprecated / rolled_back |
| episodic | active / dormant / archived / corrected / deleted |
| skill_candidate | proposed / validating / pending_confirmation / creating / confirmed_draft / saved / failed / rejected / expired |
| job | pending / locked / done / error / dead |
| 现有 Skill | 沿用现有技能设计 |

语义：

- pending_review：低置信事实；不进 Milvus/上下文；参与 A 的 existing；可手动 promote 或后续证据过阈值自动 active；30 天过期。
- expired：pending 过期，终态；不可直接 active。
- conflicted：有冲突候选，当前 active 不变；必须用户消解。
- corrected：情景被新情景纠正；不默认注入。
- rolled_back：历史 revision 回滚后的旧版本；不是当前 active。
- 历史态不直接回 active；rollback/restore 产生新 revision。

### 5.3 Milvus collection

collection：myflow_memory_active。

| 字段 | 类型 | 说明 |
|---|---|---|
| pk | VARCHAR | user_id:memory_id |
| user_id | VARCHAR | 标量索引 |
| memory_id | VARCHAR | 回 PG 确认 |
| kind | VARCHAR | semantic/episodic |
| status | VARCHAR | 只写 active |
| topic_key | VARCHAR | 可空 |
| scope_condition | VARCHAR | 可空 |
| valid_from/valid_to | INT64 | valid_to=0 表示 NULL |
| occurred_at | INT64 | 情景时间 |
| confidence | FLOAT | 证据置信度 |
| salience | FLOAT | 排序 |
| last_active_at | INT64 | 衰减 |
| explicit_remember | BOOL | 语义保护；画像不使用 pin |
| indexed_revision | INT64 | PG revision |
| embedding | FLOAT_VECTOR | 维度由配置 |
| embedding_model/embedding_dimensions | VARCHAR/INT | 漂移 |
| updated_at | INT64 | 投影时间 |

规则：

- 只写 active；其他状态不写。
- 过滤必须在搜索前：user_id == ... and kind in ... and status == active and valid_to == 0。
- 命中后回 PG 验证 user_id/status/valid_to/revision。
- 维度固定；换模型新建 collection 并重建。
- profile、procedural candidate、Skill 正文不写此 collection。

### 5.4 Outbox 与投影

- PG 事务写记录 + ledger + outbox；事务内不调用 Milvus。
- Projector 领取 pending outbox，重读 PG 当前 revision/status/epoch；旧事件丢弃。
- upsert 仅 active；delete 清 pk。
- 失败重试；dead-letter；reindex 全量重建。
- 投影是派生数据，不是权威。

## 6. LangMem 无状态算子

### 6.1 版本与模型

- 安装最新稳定版 LangMem；精确版本写 uv.lock；不在设计/代码中写开放式版本。
- 升级单独 PR，跑 A/B/C/D contract tests。
- 模型解析：

~~~python
config = app_config or get_app_config()
name = config.agents.default_model or config.models[0].name
model = create_chat_model(
    name=name,
    thinking_enabled=False,
    attach_tracing=False,
    app_config=config,
)
~~~

- 四类算子共用该模型；不跟随单次 run 模型。
- 模型不支持 structured output/tool calling 时，四类提取关闭并告警。
- 禁止 create_memory_store_manager；禁止 LangMem 读写 BaseStore/PG/Milvus。

### 6.2 算子 A：语义事实

输入：

~~~json
{
  "messages": ["当前 episode 的可见对话"],
  "existing": [
    {"id": "uuid", "content": "...", "status": "active|pending_review", "scope_condition": "..."}
  ]
}
~~~

- create_memory_manager：inserts/updates/deletes 开启。
- 输出：ExtractedMemory 列表；可能含 RemoveDoc。
- 宿主解析：
  - id 不在 existing → ADD
  - id 在 existing 且内容改变 → UPDATE
  - id 在 existing 且内容相同 → NOOP
  - RemoveDoc → DELETE
  - 不在候选集的 id → 拒绝。
- 宿主计算 confidence，不使用模型浮点。

### 6.3 算子 B：画像

输入：当前新的真实用户消息 + 当前 profile（宿主先确保空 profile 记录存在）。
- create_memory_manager：inserts=False，updates=True。
- 输出：ProfilePatch。
- 限制：
  - 只处理当前消息；evidence_quote 必须命中当前消息。
  - 字段白名单。
  - confidence >= 0.85 才直写。
  - 高证据（>=0.85）可覆盖页面值；低证据/含糊冲突 → HOLD。
  - 不扫描历史 transcript。

### 6.4 算子 C：情景脱水

输入：closed session 的 turn_refs + 工具/结果摘要。
- create_memory_manager：inserts=True，updates=False。
- 输出：DehydratedEpisode：
  - task_intent
  - environment preconditions
  - trajectory_digest
  - final_outcome
  - key_takeaway
  - feedback_signal
  - salience
  - is_skill_candidate
  - evidence refs
- 不存原始 transcript/完整工具输出。

### 6.5 算子 D：技能优化

输入：现行 active Skill + 至少 3 条成功 episode（或用户显式要求 1 条）。
- create_prompt_optimizer(kind=gradient)。
- 输出：Skill Markdown/包草案 + diff。
- 宿主静态校验后写 skill_candidate；不执行 scripts；不创建 Skill。

### 6.6 EvidenceTier 与 confidence

| tier | confidence |
|---|---|
| EXPLICIT_MANDATE | 0.95 |
| EXPLICIT_STATEMENT | 0.85 |
| BEHAVIORAL_OBSERVED | 0.70 |
| INFERRED_HYPOTHESIS | 0.50 |

- semantic >= 0.80 active；0.50-0.79 pending_review；<0.50 丢弃。
- profile >= 0.85 直写；低于阈值不写画像。
- pending 参与 A existing；新证据过 0.80 自动晋升。
- pending 30 天过期。

## 7. 工作流

### 7.1 捕获与 session

任务分类器在 `before_agent` 先运行；task_goal 为空强制 NEW_TASK，NEW_TASK 先关闭旧 session（task_boundary）再捕获，UNCERTAIN 默认不切。细节见 §7.1.1。

1. after_agent/turn finalizer 找 open session；无则创建。
2. 追加 message/tool/run 引用；按 message_id 去重。
3. 不调用 LLM。
4. 关闭条件：
   - terminal；
   - compression；
   - explicit remember；
   - idle 30 分钟；
   - max_turns 40；
   - semantic drift（可选）。
5. 关闭时写 episode_key，入队 memory_ingest_jobs；一个 session 一个 job。
6. 压缩钩子只补未处理 message_id；不重放旧 checkpoint。
7. 子代理不写父用户 session。
8. 每个 run 的 after_agent 捕获后，把 `{run_id, human_message, final_ai_message}` 追加到 `task_context.completed_runs`；只保留最近 `max_history_runs` 个；NEW_TASK 清空，CONTINUE/UNCERTAIN 追加。

### 7.1.1 任务分类器

- 位置：lead 主循环 `before_agent`，位于 `MemoryMiddleware` 之前；只在 `memory.enabled=true` 且 `task_classifier.enabled=true` 时挂载；每轮最多一次；子代理不运行；长期记忆关闭时完全不运行。
- 输入按 run 对齐：当前新 run 的 `human_message`、当前 `task_goal`、`task_context.completed_runs` 中已完成 run 的 `RunPair` 列表。每个 `RunPair` 只含 `run_id/human_message/final_ai_message`；`final_ai_message` 只取该 run 的最终可见 AI 回复（非 tool-call、非隐藏/画像/子代理）。只传 PII 脱敏文本，不传中间 AI tool-call、工具输出、完整 transcript、SystemMessage、子代理消息；按 `max_history_runs/max_input_tokens` 截断。
- 每个 run 的 `after_agent` 捕获后，把 `{run_id, human_message, final_ai_message}` 追加到 `task_context.completed_runs`，只保留最近 `max_history_runs` 个；NEW_TASK 清空，CONTINUE/UNCERTAIN 追加。当前新 run 还没有最终 AI 回复，不能进入本轮分类输入。
- `TaskContext` 的 `completed_runs` 只存 `RunPair`，不存 `ToolMessage`、中间 AI tool-call 或完整工具结果。
- 输出：

```python
class RunPair(TypedDict):
    run_id: str
    human_message: str
    final_ai_message: str

class TaskBoundaryResult(BaseModel):
    decision: Literal["CONTINUE", "NEW_TASK", "UNCERTAIN"]
    task_goal: str | None  # 仅 NEW_TASK 必填，max_length=500
```

- 空 `task_goal`：强制 NEW_TASK，但分类器仍生成新 `task_goal`；分类器不可用时用 fallback（首条脱敏消息，纯多模态用固定占位符）。
- 非空 `task_goal` + NEW_TASK：必须返回合法非空 task_goal；否则降级 UNCERTAIN，不切任务。
- CONTINUE/UNCERTAIN：忽略模型返回的 task_goal，保持当前目标，防止漂移。
- 超时/异常/非法输出：原 task_goal 为空 -> NEW_TASK + fallback；非空 -> UNCERTAIN。
- task_id 只由宿主代码生成；`task_goal_source` 为 `classifier` 或 `fallback`，fallback 不得伪装为模型生成。
- 分类器只写 `ThreadState.task_context` 和 `task:classified` 事件，不写记忆、不写 `memory_episode_sessions`；Capture 层消费 `boundary_pending` 后关闭/创建 session。
- NEW_TASK 关闭旧 session 时 `close_reason=task_boundary`，旧 session 只入队一个 job；新任务的消息历史只包含新 task_id。

### 7.2 L0/L1

- L0 规则拒绝：寒暄/临时参数/凭据/问题/提议/无价值。
- L1 在 session 关闭时算 surprise/feedback/complexity；通过才调用 C。

### 7.3 画像

1. 规则层在当前新用户消息中检测画像信号。
2. 调用 B；只传当前消息 + 当前 profile。
3. 校验 evidence_tier/quote/字段/阈值。
4. 高证据：CAS 覆盖当前值并保留历史；低证据/含糊冲突：写 conflict/HOLD。
5. 页面编辑是普通写入，不设置 pin；expected_revision。
6. 历史消息/旧 checkpoint 不重放。
7. 页面路径：/workspace/profile；导航“我的画像”。

### 7.4 情景

1. session 关闭（terminal/compression/explicit/idle/max_turns/task_boundary）→ job。
2. L1 通过 → C 脱水。
3. 校验 outcome/时间/证据/无 transcript。
4. 写 memory_records + versions + ledger + outbox。
5. 纠正：新 episode active + correction_of；旧 corrected。
6. L2 只领取 consolidation_status=pending。

### 7.5 语义

1. L1/L2 选 source episodes。
2. A 读取 active + 同 topic pending_review。
3. Resolver 输出 ADD/UPDATE/DELETE/NOOP/HOLD。
4. confidence 阈值：
   - >=0.80 active；
   - 0.50-0.79 pending_review；
   - <0.50 丢弃；
   - 冲突 HOLD。
5. pending 可被后续 A 更新/晋升。
6. 写 PG + ledger + outbox。
7. memory_search 不返回 pending。

### 7.6 程序性

1. 触发：显式用户要求（1 条），或 >=3 条成功 episode（同 task_class/environment、至少 2 个 thread、成功率 >=0.80、trigger 相似度 >=0.82、至少 2 个共同关键步骤）。成功 = SUCCESS 或用户接受的 PARTIAL，且没有未解决 blocker；同一 episode 只计一次。
2. 同一种 episode 的判定由宿主完成：episode 身份、成功、task_class、environment、trigger 相似度、共同步骤都是宿主 signals；LangMem D 只能提供 same_sop/mergeable 建议，不能单独放行。宿主 criteria 不满足时，即使 D 认为相同也不生成候选。
3. D 生成 package/diff。
4. 静态校验；写 skill_candidate pending_confirmation。
5. 前端“我的技能/记忆建议”显示；owner 确认。
6. 确认校验 expected_revision/package_sha256/owner/idempotency key。
7. 现有技能服务创建/保存私有 Skill；skill_versions.metadata_json 写 origin_candidate_id。
8. 公开仍走现有 submit/admin review；无自动公开。
9. 拒绝/失败不创建 Skill；候选可重试/过期。

trigger 灰区：

- >= 0.92 强相似，但仍要过 task_class/environment/共同步骤/成功/跨 thread。
- 0.82–0.92 灰区，必须有共同关键步骤，并由 LangMem D 给出可校验的 same_sop；否则拆成独立候选或 HOLD。
- < 0.82 不归入同一 pattern。
- episode 永不因相似而合并；聚类只是候选选材视图。环境不同、步骤不足时拆分支，不做模糊平均。

### 7.7 删除/回滚

- forget：墓碑 + 移出 active/Milvus + 取消 job。
- clear：epoch + cleared_at + 墓碑；保留墓碑/审计；不接受内部 kind。
- 账号删除：回收期后物理删除。
- rollback：target_revision → 当前 rolled_back，新 active revision；deleted 不可回滚。
- reindex：从 PG active 重建 Milvus。

## 8. 检索

### 8.1 画像注入

- before_agent 读 profile；同 thread 首轮注入；固定 ID；隐藏消息；数据块。
- 800 tokens；字段优先级；不截断半个字段。
- 读失败空画像继续。

### 8.2 memory_search

- 只读；只 semantic/episodic active。
- 不接受模型 user_id。
- 不返回 pending/conflict/archived/skill/transcript。
- 查询 text/kinds/time_range/limit。

### 8.3 混合检索

1. query embedding；失败直接 PG 词法。
2. Milvus dense top candidate_k；filter 前置。
3. PG 词法 top candidate_k。
4. RRF 合并。
5. PG 逐条确认。
6. per-kind quota；衰减重排；截断。
7. 命中后更新 hit_count/last_active_at + outbox。

## 9. API

| 能力 | 方法/路径 | 权限 | 说明 |
|---|---|---|---|
| 画像 | GET/PATCH /api/memory/profile | memory:read/write | PATCH expected_revision；普通写入，不设置 pin |
| 字段删除 | DELETE /api/memory/profile/{field_path} | memory:write | 墓碑 |
| 检索 | GET /api/memory/search | memory:read | active semantic/episodic |
| 待确认记忆 | GET /api/memory/pending | memory:read | owner-only；semantic pending_review |
| 晋升 | POST /api/memory/items/semantic/{id}/promote | memory:write | owner-only；expected_revision |
| 删除 | DELETE /api/memory/items/{kind}/{id} | memory:write | 墓碑；pending 也可删 |
| 情景 | GET /api/memory/episodes；POST /{id}/correct | memory:read/write | 纠正是新事件 |
| 技能候选 | GET /api/memory/skill-candidates | memory:read | owner |
| 确认候选 | POST /api/memory/skill-candidates/{id}/confirm | skills:write | owner；expected_revision/package_sha256/idempotency_key |
| 拒绝候选 | POST /api/memory/skill-candidates/{id}/reject | skills:write | owner；expected_revision |
| 冲突 | GET /api/memory/conflicts；POST /{id}/resolve | memory:read/write | expected_revision；快照 |
| 导出/清空 | GET /api/memory/export；DELETE /api/memory | memory:read/write | 墓碑保留 |
| 重建 | POST /api/memory/reindex | admin | 全量投影 |
| 维护 | POST /api/memory/maintenance/run | admin | dry-run |

错误：

- 409 memory_conflict：revision 冲突/候选过期。
- 422 invalid_memory：字段/合并值/参数非法。
- 404：记录/冲突/候选不存在。
- 403/404：跨用户/非 owner。
- 501：kind 未启用。
- 500 memory_corrupted：固定 detail，不返回路径。

## 10. 前端

| 路由 | 组件 | 内容 |
|---|---|---|
| /workspace/profile | components/profile/my-profile-page.tsx | 我的画像；字段/证据/时间；编辑/删除 |
| /workspace/memory/pending | components/memory/pending-memory-page.tsx | 待确认语义；confidence/evidence/source；promote/delete；badge |
| /workspace/skills | 现有 MySkillsPage | 增加“记忆建议”候选区 |
| /workspace/admin/skills | 现有审核台 | 公开 Skill 审核，不变 |

- 导航在 sidebar-container.tsx/Sidebar 增加“我的画像”“待确认记忆”。
- pending 页 owner-only；管理员只见聚合计数。
- 不展示原始 transcript/工具输出/Skill 脚本内容。
- 确认 skill candidate 进入现有 skill editor。

## 11. 配置

~~~yaml
memory:
  enabled: false
  canonical_store: postgres
  langmem:
    model: agent_default
    operator_timeout_seconds: 60
    schema_version: 1
  vector_store:
    provider: milvus
    uri: MILVUS_URI
    token: MILVUS_TOKEN
    collection: myflow_memory_active
    metric_type: COSINE
    index_type: AUTOINDEX
    consistency_level: Strong
  embedding:
    base_url: EMBEDDING_BASE_URL
    api_key: EMBEDDING_API_KEY
    model: Qwen/Qwen3-Embedding-8B
    dimensions: 4096
  profile:
    inject: true
    max_tokens: 800
    auto_write_threshold: 0.85
  semantic:
    auto_activate_threshold: 0.80
    pending_review_ttl_days: 30
    include_pending_in_extraction: true
  episodic:
    session_idle_close_minutes: 30
    max_turns_per_session: 40
    gate_threshold: 0.55
    max_summary_tokens: 1200
    ttl_days: 180
  task_classifier:
    enabled: true
    model: agent_default
    timeout_seconds: 8
    max_history_runs: 10
    max_input_tokens: 4000
    uncertain_policy: continue
  consolidation:
    l2_unconsolidated_threshold: 10
    l3_cron: "0 3 * * *"
    shadow: true
  skill_candidates:
    enabled: true
    min_source_episodes: 3
    min_distinct_threads: 2
    min_success_rate: 0.80
    trigger_similarity_threshold: 0.82
    strong_trigger_similarity: 0.92   # 0.82-0.92 灰区，需共同步骤 + D 建议
    min_common_steps: 2
    expire_days: 30
    require_user_confirmation: true
  retrieval:
    candidate_k: 50
    lexical_fallback: true
    per_kind_quota:
      semantic: 6
      episodic: 2
~~~

## 12. 迁移

1. 新建 PG 表/索引/outbox/session；0006 建基础表，0007 加 task 列；旧 BaseStore 兼容读。
2. 迁移 profile/records/versions/tombstones；保持 memory_id。
3. 回填 active semantic/episodic 到 Milvus；建立 projection state。
4. 切换检索到 Milvus + PG 确认；保留词法降级。
5. 停写 BaseStore；保留只读回滚窗口。
6. 现有 skills 不迁移；只使用 skill_versions.metadata_json.origin_candidate_id。
7. 真 Milvus smoke：建表/写入/过滤/删除/重建。

## 13. 观测

指标：

- session open/closed/swept/平均轮数；
- 任务分类：CONTINUE/NEW_TASK/UNCERTAIN 分布、分类延迟/token、task_goal_source 分布、UNCERTAIN 率、重复 task_id、旧任务消息进入新 session 的比率；
- ingest/consolidation job 深度、lease 过期、dead-letter；
- LangMem A/B/C/D 调用/失败/schema 拒绝/超时；
- profile 直写/低证据拒绝/高证据覆盖页面值；
- semantic active/pending/auto-promote/expired/conflict；
- Milvus p95/词法降级/PG 确认丢弃/跨用户拒绝；
- outbox 滞后/reindex/孤儿；
- skill candidate pending/confirm/reject/fail/auto create（必须 0）。

日志/trace 不写正文、证据原文、Skill 正文、PII handle。

## 14. 测试矩阵

| ID | Given | When | Then |
|---|---|---|---|
| T1 | task_goal 为空 | 新用户消息到达 | 强制 NEW_TASK；分类器生成 task_goal；task_id 由代码生成 |
| T2 | 当前 task_goal 非空 | 分类器 CONTINUE | task_id/task_goal 不变；本轮进入当前 session |
| T3 | 当前 task_goal 非空，新消息是无关任务 | 分类器 NEW_TASK | 旧 session close_reason=task_boundary，只入队一次；新 task_id；旧消息不进入新 session |
| T4 | 分类器 UNCERTAIN | 当前任务非空 | 不关闭；boundary_uncertain=true；本轮仍被捕获 |
| T5 | 分类器超时/非法 schema | 当前任务非空 | 降级 UNCERTAIN；不切任务 |
| T6 | 分类器失败且 task_goal 为空 | 新消息到达 | NEW_TASK + fallback task_goal；task_id 由代码生成 |
| T7 | 分类器 CONTINUE 返回新 goal | 当前任务非空 | host 忽略 goal；task_goal 不漂移 |
| T8 | 模型/用户尝试指定 task_id | 任意 | 不采信；task_id 只由宿主代码生成 |
| T9 | 长期记忆关闭 | 新用户消息到达 | 任务分类器不挂载、不运行、不生成 task_context；session 保持旧行为 |
| T10 | 当前任务有 2 个已完成 run，每个 run 有中间 tool-call AI | 构造分类输入 / after_agent 追加 | 只取每个 run 的 `human_message + final_ai_message` 对；中间 AI/tool 输出不进入输入；`completed_runs` 最多保留 `max_history_runs` 个 |
| S1 | thread 多轮任务 | 终态关闭 session | 一个 episode；一个 job；不每轮抽 |
| S2 | open session | 30 分钟 idle | close_reason=idle；入队一次 |
| S3 | 40 轮 | 第 41 轮 | 旧 session max_turns 关闭；新 session |
| S4 | 压缩钩子重复提交 | 已处理 message_ids | 不重复入队；不重放 profile |
| P1 | 当前用户消息高证据画像 | B 运行 | profile 直写；revision+1 |
| P2 | 页面编辑字段 | 后续新用户消息高证据 | 自动覆盖；保留历史；页面可再改 |
| P3 | 历史 checkpoint | 再次压缩 | 不重抽 profile；无新用户消息时页面值不变 |
| P4 | 页面 PATCH | revision 过期 | 409；提示重读 |
| E1 | 高价值工具失败/修复 | session 关闭 | 情景摘要；无 transcript |
| E2 | 同事件重复 | job 重复 | 幂等；一个 episode |
| E3 | 纠正旧 episode | 新 event | 旧 corrected；默认不注入 |
| M1 | 显式陈述事实 | confidence 0.85 | active；Milvus upsert |
| M2 | 行为观察 | 0.70 | pending_review；不进 Milvus/context |
| M3 | pending 同 topic | 新证据 0.85 | 自动晋升 active；审计 |
| M4 | pending 30 天 | L3 | expired；不可直接 active |
| M5 | active 冲突 | A 返回 HOLD | conflicted；旧 active 不变 |
| K1 | 3 条成功 episode，同 task_class/environment/trigger，跨 2 个 thread | L2 | skill candidate；不创建 Skill |
| K0 | 3 条同会话重复 episode / 有未解决 blocker / 用户纠正 | L2 | 不计入成功样本；不生成候选 |
| K0b | LangMem D 说 same_sop，但宿主 task_class/environment/trigger/common-step 任一不满足 | L2 | 不生成候选；D 建议不能单独放行 |
| K0c | 宿主 criteria 满足，但 D 无法生成可校验 package | L2 | 不生成候选；记录失败原因 |
| K0d | trigger 0.82–0.92 灰区，共同步骤不足 | L2 | 拆成独立候选或 HOLD；不平均成模糊 Skill |
| K2 | 用户显式 1 条 | 确认 | 可生成 candidate |
| K3 | candidate 被修改 | confirm 带旧 hash | 409 |
| K4 | 并发确认 | 两个请求 | 一个 Skill/version |
| K5 | 创建成功/回写失败 | 重试 | metadata origin 命中；不重复 |
| K6 | 公开未提交 | 尝试公开 | 无公共指针；admin review |
| V1 | Milvus down | search | PG 词法；对话完成 |
| V2 | delete 后旧 outbox | 延迟 upsert | fencing 丢弃；reindex 清 |
| V3 | collection 删除 | reindex | 从 PG active 重建 |
| D1 | clear/forget | 旧 job/旧对话 | 不复活 |
| D2 | rollback target | 回滚 | 新 active revision；deleted 拒绝 |
| A1 | 跨用户 | 读/搜/confirm | 403/404；无内容 |
| A2 | admin 访问 pending | 直接请求 | 只聚合计数；不读正文 |
| L1 | 四类提取 | 静态检查 | 仅 LangMem A/B/C/D；无 store 写库 |
| L2 | LangMem 不可用 | 提取 | 关闭/告警；旧记忆可读 |

## 15. 实施顺序

1. W0：PG schema/repository/outbox/session（0006 基础表，0007 task 列）；LangMem A/B adapter；profile/semantic 基础；fake operators。
2. W1：任务分类器 + task_context；session capture/close；LangMem C；Milvus semantic/episodic；画像直写 + 我的画像页；检索/降级。
3. W2：L2/L3；A/D；pending_review 页/自动晋升；conflict/冷热/折叠。
4. W3：skill candidates；现有技能桥接；用户确认；公开审核回归。
5. W4：真实 Milvus smoke；迁移；灰度；验收。

## 16. 验收条件

- 四类提取只走 LangMem A/B/C/D；代码中没有 create_memory_store_manager 写库路径。
- 同一 thread 同时最多一个 open session；多轮任务只生成一个 episode；历史不重放。
- 任务分类器：task_goal 为空 100% 强制 NEW_TASK；task_id 只由代码生成；NEW_TASK 先关闭旧 session（task_boundary）且只入队一次；CONTINUE/UNCERTAIN 不改写 task_goal；UNCERTAIN 默认不切；NEW_TASK 后旧任务消息进入新 session = 0；长期记忆关闭时分类器运行 = 0；输入只含 `completed_runs` 的 human/final AI 对和当前 human，中间 tool-call AI/工具输出进入输入 = 0；`completed_runs` 最多保留 `max_history_runs` 个。
- 画像只从当前新用户消息取证据；历史 transcript/compaction 不重放；高证据可覆盖页面值；无新用户消息时页面值不变。
- semantic >=0.80 active；0.50–0.79 pending_review；pending 不进 Milvus/context；可手动/后续证据晋升；30 天过期。
- 待确认记忆页 owner-only；admin 只聚合计数。
- 技能候选默认 3 条成功 episode（至少 2 个 thread，同 task_class/environment/trigger，至少 2 个共同关键步骤）；同一种 episode 由宿主判定，LangMem D 仅提供建议；显式 1 条；用户确认后创建私有 Skill；公开走现有 admin review；自动创建/公开 = 0。
- Milvus 只存 active semantic/episodic；filter 前置；PG 确认；outbox 可重建。
- cross-user = 0；delete resurrection = 0；raw transcript in memory = 0。
- Milvus 故障时普通对话完成；PG 写入不被拖垮。
