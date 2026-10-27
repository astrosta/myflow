# Agent 长期记忆模块实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 MyFlow 中落地一套以 PostgreSQL 为权威、Milvus 为可重建投影、LangMem 无状态算子为唯一提取实现的四类长期记忆系统，并完成画像页、待确认记忆页与技能候选确认闭环。

**Architecture:** `before_agent` 先由任务分类器判定 CONTINUE/NEW_TASK/UNCERTAIN，并在 NEW_TASK 时生成 `task_goal`；`task_id` 只由宿主代码生成。热路径只把可见轮次追加到 `memory_episode_sessions`，关闭 session 时生成一个 `episode_key` 和一个 ingest job；后台 worker 再调用 LangMem A/B/C/D 产出结构化 proposal，由宿主做门控、冲突仲裁和 CAS。所有记忆状态变更在同一个 PostgreSQL 事务里写 `memory_records/memory_profiles`、`memory_record_versions`、`memory_mutation_ledger` 和 `memory_index_outbox`；Projector 异步把 active semantic/episodic 投影到 Milvus，命中后必须回 PG 确认。Skill 候选只生成 package 草案，用户确认后才通过现有技能服务创建私有 Skill，公开仍走现有 admin review。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2.0 async、Alembic、PostgreSQL（SQLite 开发等价）、LangMem `0.0.30`、LangChain/LangGraph、pymilvus、Next.js 16、React 19、TanStack Query、Vitest。

**Spec:** `docs/plans/2026-10-26-agent-memory-module-spec.md`

## Global Constraints

- `langmem` 精确锁定为 `0.0.30` 并写入 `uv.lock`；升级必须单独 PR，并跑完 A/B/C/D contract tests。
- 四类提取只允许走 LangMem 无状态算子 A/B/C/D；禁止 `create_memory_store_manager`，禁止 LangMem 持有 store 或直接写 PG/Milvus。
- 算子模型解析固定为：`AppConfig.agents.default_model`，为空取 `config.models[0].name`；调用 `create_chat_model(name=..., thinking_enabled=False, attach_tracing=False, app_config=config)`；不跟随单次 run 模型。
- PostgreSQL 是唯一权威；Milvus 是可重建投影。Milvus 命中必须回 PG 确认 `user_id/status/valid_to/revision`。
- Milvus collection 固定为 `myflow_memory_active`；只写 active semantic/episodic；filter 必须在搜索前：`user_id == ... and kind in ... and status == active and valid_to == 0`。
- profile 与程序性 Skill 不进入记忆 Milvus；Skill 正文继续由现有 Git/技能系统管理。
- 画像自动写入阈值 `0.85`；语义自动 active `0.80`；语义 pending_review 区间 `0.50–0.79`，低于 `0.50` 丢弃；pending TTL `30 天`。
- 画像没有 `pinned` 字段，也没有 pin/unpin UI；页面编辑是普通写入，后续高证据对话可以覆盖，历史通过 ledger 保留。
- session 空闲关闭 `30 分钟`，最多 `40 轮`；同一 `(user_id, thread_id)` 同时最多一个 open session；一个 session 只生成一个 episode/job。
- 任务分类器只在 `memory.enabled && task_classifier.enabled` 时、lead 的 `before_agent` 运行一次；长期记忆关闭时不挂载、不生成 `task_context`；输出 `CONTINUE/NEW_TASK/UNCERTAIN` + `task_goal`；`task_goal` 为空强制 NEW_TASK，但仍由分类器生成新目标；`task_id` 只由宿主代码生成；`CONTINUE/UNCERTAIN` 不改写 `task_goal`；UNCERTAIN 默认继续当前任务。
- 分类器输入按 run 对齐：每个已完成 run 只取 `human_message + final_ai_message` 对，当前新 run 只取 `human_message`；不传中间 tool-call AI、工具输出或完整 transcript。
- 分类器超时 `8 秒`；默认历史 `10` 个已完成 run、输入预算 `4000` token；原 `task_goal` 非空时超时/非法输出降级 UNCERTAIN，原为空时 NEW_TASK + fallback goal；fallback 必须标记 `task_goal_source=fallback`。
- 默认技能候选需要 `3` 条成功 episode、至少 `2` 个 thread、成功率 `>=0.80`、trigger 相似度 `>=0.82`、至少 `2` 个共同关键步骤；用户显式要求可用 `1` 条。
- trigger 相似度 `>=0.92` 为强相似但仍需全部硬条件；`0.82–0.92` 为灰区，必须有共同步骤并由 LangMem D 给出可校验的 `same_sop`，否则拆分或 HOLD；`<0.82` 不归入同一模式。
- 情景 TTL `180 天`；技能候选过期 `30 天`；L1 gate 阈值 `0.55`；L2 未整合阈值 `10`；L3 cron `"0 3 * * *"`。
- 检索 `candidate_k=50`，per-kind quota `semantic=6 / episodic=2`；query embedding 模型 `Qwen/Qwen3-Embedding-8B`，维度 `4096`，metric `COSINE`，index `AUTOINDEX`，consistency `Strong`。
- 任何时刻不得把原始 transcript、完整工具输出、Skill 脚本正文作为长期记忆写入；只允许摘要、结构化信号和 `message_id/tool_call_id/run_event_id` 证据引用。
- 所有 API 和模型工具从认证上下文取 `user_id`，禁止接受客户端传入的 `user_id`；跨用户一律按 403/404 处理且不泄露内容。
- 新表/新列只允许新增 Alembic revision `0006_agent_memory` 和 `0007_task_classifier`；不得修改 0001–0005；0006 建基础表，0007 加 task 列；SQLite 开发环境允许等价类型，但生产列语义必须匹配 spec。
- 前端固定中文文案；不展示原始 transcript、工具输出或 Skill 脚本；不新增技能审核台。
- 后端测试统一从仓库根目录运行：`PYTHONPATH=. uv run --project backend --group dev --extra memory pytest <path> -q`；前端：`cd frontend && pnpm test:node <path>` / `pnpm test:dom <path>` / `pnpm typecheck`。
- 每个 task 必须 TDD、独立可验收、频繁提交；提交信息使用 conventional commits，不把多个 task 混在一个提交里。

## Review Focus

- **任务边界误判**：空 `task_goal`、续写、新任务、UNCERTAIN、分类器超时、CONTINUE 返回新 goal 都必须不丢消息、不漂移、不把旧任务消息塞进新 session；测试落在 Task 5A、Task 5C 和 Task 14。
- **同一 thread 的并发捕获/压缩**：两个标签页、重试或压缩钩子同时到达时，用户期望仍然只有一个 open session、一个 episode、每个 `message_id` 只处理一次；测试落在 Task 5、Task 5C 和 Task 14。
- **Milvus 或 embedding 故障、维度漂移、collection 被删**：普通对话和检索不能失败，用户期望自动降级到 PG 词法，恢复后 reindex 能重建；测试落在 Task 9、Task 10 和 Task 15。
- **删除/清空/回滚后的旧 outbox，或伪造的跨用户 id**：用户期望旧事件不能复活记录、跨用户读不到任何正文；测试落在 Task 9 和 Task 12。
- **恶意或畸形的 LangMem 输出**：未知 id、evidence_quote 不在当前消息、凭据/PII、脚本路径穿越、超大 package 都必须 fail closed，不能写 PG/Milvus 或创建 Skill；测试落在 Task 4、Task 6、Task 7 和 Task 11。
- **并发确认技能候选或“创建成功但回写失败”后重试**：用户期望只产生一个 Skill/version，且 `origin_candidate_id` 可追踪；测试落在 Task 11。

---

## File Structure

### 新增后端 canonical 包

- `backend/packages/harness/agentkit/agents/memory/canonical/__init__.py`：只导出稳定入口，避免上层 import 具体实现。
- `.../canonical/models.py`：12 张表的 SQLAlchemy 行模型；task 列由 `0007_task_classifier` 追加；不包含业务方法。
- `.../canonical/domain.py`：枚举、`MutationContext`、`RecordDraft`、`ProfileView`、`TurnRef` 等 canonical dataclass/Pydantic 模型；无存储 import。
- `.../canonical/repository.py`：所有 PG 事务、CAS、ledger、outbox、session/job 领取；唯一允许直接操作 `AsyncSession` 的模块。
- `.../canonical/evidence.py`：EvidenceTier → confidence 的唯一映射和阈值判断。
- `.../canonical/proposals.py`：A/B/C/D 的输入/输出 Pydantic schema；不依赖 LangMem。
- `.../canonical/operators.py`：LangMem 无状态适配器，构造 A/B/C/D，调用后立即返回 proposal。
- `.../canonical/fakes.py`：确定性 fake operators 与 in-memory embedder，供测试和本地开发。
- `.../canonical/capture.py`：从中间件消息构建 `TurnRef/TurnDigest`；只做结构化和脱敏，不调 LLM。
- `.../canonical/sessions.py`：open session、追加、关闭、idle/max-turn sweeper、episode key、ingest job。
- `.../canonical/episodic.py`：L1 gate、C 脱水、情景落库、纠正、consolidation 状态。
- `.../canonical/profile.py`：画像读取/手动写入/字段墓碑/B 直写/CAS/冲突 HOLD。
- `.../canonical/semantic.py`：A 输入装配、resolver、pending_review、自动晋升、TTL 过期。
- `.../canonical/projection.py`：Milvus collection schema、embedding、Projector、outbox 领取、reindex。
- `.../canonical/retrieval.py`：Milvus dense + PG lexical + RRF + PG 确认 + quota/衰减重排。
- `.../canonical/skill_candidates.py`：宿主聚类/成功判定/共同步骤、D 调用、package 静态校验、确认/拒绝幂等。
- `.../canonical/service.py`：异步 `MemoryService` 组合根，供 middleware、tool、API 使用。
- `.../canonical/legacy.py`：只读迁移 BaseStore；不进入新运行路径。
- `.../canonical/metrics.py`：无正文的计数器与快照；日志只记录类型/计数。

### 新增任务分类器文件

- `backend/packages/harness/agentkit/agents/middlewares/task_classifier.py`：`TaskClassifierMiddleware`、`TaskBoundaryDecision/TaskBoundaryResult`、有界输入装配、`task_goal` 校验和 fallback；不写记忆、不生成 task_id。
- `backend/packages/harness/agentkit/persistence/migrations/versions/0007_task_classifier.py`：给 `memory_episode_sessions` 加 task 列，给 `memory_records` 加 `task_id`。

### 修改的现有后端文件

- `backend/packages/harness/pyproject.toml`：新增 `memory` extra（`langmem==0.0.30`、`pymilvus>=2.5`）。
- `backend/pyproject.toml`：新增 `memory` extra 和 dev 依赖；不改已有 extra 语义。
- `uv.lock`：由 `uv lock` 生成，不手改。
- `backend/packages/harness/agentkit/config/memory_config.py`：加入 spec 配置树；保留旧字段的迁移别名。
- `backend/packages/harness/agentkit/persistence/schema.py`：显式 import/注册新行模型。
- `backend/packages/harness/agentkit/persistence/migrations/versions/0006_agent_memory.py`：基础表/索引/部分唯一索引；task 列不在这里加。
- `backend/packages/harness/agentkit/agents/thread_state.py`：新增 `TaskContext` 和 `task_context` channel。
- `backend/packages/harness/agentkit/agents/middlewares/assembly.py`：把 `TaskClassifierMiddleware` 放在 `MemoryMiddleware` 之前，只挂 lead 主循环。
- `backend/packages/harness/agentkit/agents/middlewares/memory_middleware.py`：`after_agent` 只捕获；去掉 `after_model` 抽取；消费 `task_context`，NEW_TASK 先关旧 session；画像注入改走 `MemoryService`。
- `backend/packages/harness/agentkit/agents/memory/summarization_hook.py`：只补未处理 `message_id`，异步调度 capture。
- `backend/packages/harness/agentkit/agents/memory/tools.py`：`memory_search` 改为 await 新检索器；只读 semantic/episodic active。
- `backend/packages/harness/agentkit/agents/leader_agent/agent.py`：绑定新 `MemoryService` 的 search tool。
- `backend/app/gateway/app.py`：lifespan 构建/启动/关闭 `MemoryService`、Projector、维护任务；memory disabled 时零 worker。
- `backend/app/gateway/deps.py`：新增 `get_memory_service`；保留旧 `get_memory_manager` 仅供迁移测试。
- `backend/app/gateway/routers/memory.py`：按 spec API 扩展；保留旧 `/items`、`/facts`、`/export`、`DELETE /memory` 的 wire 兼容。
- `backend/app/gateway/errors.py`：新增 `invalid_memory` 422 和 `memory_not_found` 404 的统一映射；旧 code 保留兼容。
- `backend/packages/harness/agentkit/skills/author.py`、`skills/store/sql.py`：`save/record_saved_version` 支持合并 `origin_candidate_id` 的 `extra_metadata`；不新增 skill 表。
- `backend/tests/test_memory_routes.py`、`test_memory_core.py`：迁移到新 service/新语义；删除 pin 断言。
- `config.example.yaml`：补 `memory` 新配置块，所有 secret 用空值或环境变量占位。

### 新增前端文件

- `frontend/src/core/api/memory.ts`：唯一 memory API 客户端；错误体转 `MemoryApiError`。
- `frontend/src/core/api/memory.test.ts`：wire shape、错误 code、expected_revision。
- `frontend/src/app/workspace/profile/page.tsx`：`/workspace/profile` route。
- `frontend/src/components/profile/my-profile-page.tsx`：画像字段/证据/时间/编辑/删除；无 pin UI。
- `frontend/src/components/profile/my-profile-page.dom.test.tsx`：409 重读、字段编辑、删除。
- `frontend/src/app/workspace/memory/pending/page.tsx`：`/workspace/memory/pending` route。
- `frontend/src/components/memory/pending-memory-page.tsx`：待确认语义；promote/delete；空态/错误。
- `frontend/src/components/memory/pending-memory-page.dom.test.tsx`：owner-only 文案、promote 409、删除。
- `frontend/src/components/memory/skill-suggestions.tsx`：现有技能页的“记忆建议”候选区。
- `frontend/src/components/memory/skill-suggestions.dom.test.tsx`：确认/拒绝/package hash/跳转 editor。

### 修改的现有前端文件

- `frontend/src/components/workspace/sidebar.tsx`：新增“我的画像”“待确认记忆”导航项和高亮 props。
- `frontend/src/app/workspace/sidebar-container.tsx`：路径前缀判断并传入新 props。
- `frontend/src/components/skills/my-skills-page.tsx`：嵌入 `SkillSuggestions`；不新增审核 UI。
- `frontend/src/components/workspace/sidebar.dom.test.tsx`：导航渲染与 active 态。

### 测试文件

- `backend/tests/test_task_classifier.py`
- `backend/tests/test_memory_config_v2.py`
- `backend/tests/test_memory_schema_v2.py`
- `backend/tests/test_memory_repository.py`
- `backend/tests/test_memory_operators.py`
- `backend/tests/test_memory_sessions.py`
- `backend/tests/test_memory_episodic.py`
- `backend/tests/test_memory_profile.py`
- `backend/tests/test_memory_semantic.py`
- `backend/tests/test_memory_projection.py`
- `backend/tests/test_memory_retrieval.py`
- `backend/tests/test_memory_skill_candidates.py`
- `backend/tests/test_memory_api_v2.py`
- `backend/tests/test_memory_runtime_v2.py`
- `backend/tests/test_memory_legacy_migration.py`
- `backend/tests/test_memory_acceptance.py`
- `backend/tests/integration/test_memory_milvus_smoke.py`（`MILVUS_URI` 未设置时 skip）

> 旧 `manager.py`、`services/*`、`ingestors/*` 在 Task 14 前保持可导入但不再被新运行时调用；Task 15 验证迁移后才允许删除。不要一边迁移一边改旧 store 的写路径。

## Task 1: 配置、依赖与算子模型解析

**Files:**
- Modify: `backend/packages/harness/pyproject.toml`
- Modify: `backend/pyproject.toml`
- Modify: `uv.lock`
- Modify: `backend/packages/harness/agentkit/config/memory_config.py`
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/__init__.py`
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/operator_model.py`
- Test: `backend/tests/test_memory_config_v2.py`

**Interfaces:**
- Consumes: `AppConfig.agents.default_model`、`AppConfig.models[0].name`、`agentkit.models.create_chat_model`。
- Produces: `MemoryConfig` 的 spec 配置树；`MemoryProfileConfig.inject`、`MemoryProfileConfig.injection_enabled`（只读兼容属性）；`MemoryLangMemConfig`、`MemoryVectorStoreConfig`、`MemoryEmbeddingConfig`、`MemorySemanticConfig`、`MemoryEpisodicConfig`、`MemoryTaskClassifierConfig`、`MemoryConsolidationConfig`、`MemorySkillCandidatesConfig`、`MemoryRetrievalConfig`；`operator_model_name(app_config: AppConfig) -> str`；`resolve_operator_model(app_config: AppConfig) -> BaseChatModel`；`MemoryConfigError`。

- [ ] **Step 1: 写失败测试**

~~~python
from types import SimpleNamespace

import pytest

from agentkit.config.memory_config import MemoryConfig
from agentkit.agents.memory.canonical.operator_model import (
    operator_model_name,
    resolve_operator_model,
)


def test_spec_defaults_are_exact():
    config = MemoryConfig()
    assert config.canonical_store == "postgres"
    assert config.vector_store.collection == "myflow_memory_active"
    assert config.embedding.model == "Qwen/Qwen3-Embedding-8B"
    assert config.embedding.dimensions == 4096
    assert config.vector_store.consistency_level == "Strong"
    assert config.profile.auto_write_threshold == 0.85
    assert config.semantic.auto_activate_threshold == 0.80
    assert config.semantic.pending_review_ttl_days == 30
    assert config.episodic.session_idle_close_minutes == 30
    assert config.episodic.max_turns_per_session == 40
    assert config.episodic.gate_threshold == 0.55
    assert config.episodic.max_summary_tokens == 1200
    assert config.episodic.ttl_days == 180
    assert config.task_classifier.enabled is True
    assert config.task_classifier.timeout_seconds == 8
    assert config.task_classifier.max_history_runs == 10
    assert config.task_classifier.max_input_tokens == 4000
    assert config.task_classifier.uncertain_policy == "continue"
    assert config.skill_candidates.min_source_episodes == 3
    assert config.skill_candidates.min_distinct_threads == 2
    assert config.skill_candidates.min_success_rate == 0.80
    assert config.skill_candidates.trigger_similarity_threshold == 0.82
    assert config.skill_candidates.strong_trigger_similarity == 0.92
    assert config.skill_candidates.min_common_steps == 2
    assert config.skill_candidates.expire_days == 30
    assert config.retrieval.per_kind_quota == {"semantic": 6, "episodic": 2}


def test_legacy_profile_alias_still_parses():
    config = MemoryConfig.model_validate({"profile": {"injection_enabled": False}})
    assert config.profile.inject is False
    assert config.profile.injection_enabled is False


def test_operator_model_uses_agent_default(monkeypatch):
    calls = {}

    def fake_create_chat_model(**kwargs):
        calls.update(kwargs)
        return object()

    monkeypatch.setattr(
        "agentkit.agents.memory.canonical.operator_model.create_chat_model",
        fake_create_chat_model,
    )
    app = SimpleNamespace(
        agents=SimpleNamespace(default_model="agent-default"),
        models=[SimpleNamespace(name="fallback")],
    )
    assert resolve_operator_model(app) is not None
    assert calls == {
        "name": "agent-default",
        "thinking_enabled": False,
        "attach_tracing": False,
        "app_config": app,
    }


def test_operator_model_falls_back_to_first_model():
    app = SimpleNamespace(
        agents=SimpleNamespace(default_model=None),
        models=[SimpleNamespace(name="first"), SimpleNamespace(name="second")],
    )
    assert operator_model_name(app) == "first"


def test_operator_model_requires_a_model():
    app = SimpleNamespace(agents=SimpleNamespace(default_model=None), models=[])
    with pytest.raises(Exception, match="model"):
        operator_model_name(app)
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_config_v2.py -q`
Expected: FAIL，`canonical.operator_model` 不存在或 `MemoryConfig` 没有 `canonical_store`。

- [ ] **Step 3: 实现配置和模型解析**

在 `memory_config.py` 增加嵌套配置类（含 `MemoryTaskClassifierConfig`），字段名和默认值按测试；`uncertain_policy` 用 `Literal["continue","new_task"]`，默认 `continue`；保留旧 `profile.injection_enabled`、`backend_class`、`ingest`、`maintenance`、`backend_config` 的解析兼容。`MemoryProfileConfig` 用 `model_validator(mode="before")` 把旧 `injection_enabled` 映射到 `inject`，并提供只读属性 `injection_enabled`。`operator_model.py` 只做两件事：解析名字、调用 `create_chat_model`；不缓存模型实例。

在 `backend/packages/harness/pyproject.toml` 的 optional-dependencies 增加：
~~~toml
memory = ["langmem==0.0.30", "pymilvus>=2.5"]
~~~
在 `backend/pyproject.toml` 增加 `memory = ["agentkit[memory,postgres]"]`，并把 `langmem==0.0.30` 加入 `dependency-groups.dev`，保证测试环境可 import。

- [ ] **Step 4: 锁依赖并验证版本**

Run: `uv lock`
Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory python -c "import importlib.metadata as m; print(m.version('langmem'))"`
Expected: 输出 `0.0.30`。

- [ ] **Step 5: 跑测试确认通过**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_config_v2.py -q`
Expected: PASS（本 task 的测试全部通过）。

- [ ] **Step 6: 提交**

~~~bash
git add backend/packages/harness/pyproject.toml backend/pyproject.toml uv.lock \
  backend/packages/harness/agentkit/config/memory_config.py \
  backend/packages/harness/agentkit/agents/memory/canonical/__init__.py \
  backend/packages/harness/agentkit/agents/memory/canonical/operator_model.py \
  backend/tests/test_memory_config_v2.py
git commit -m "feat(memory): add canonical memory configuration"
~~~

## Task 2: PostgreSQL 行模型与 0006 迁移

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/models.py`
- Modify: `backend/packages/harness/agentkit/persistence/schema.py`
- Create: `backend/packages/harness/agentkit/persistence/migrations/versions/0006_agent_memory.py`
- Test: `backend/tests/test_memory_schema_v2.py`

**Interfaces:**
- Consumes: `agentkit.persistence.base.Base`。
- Produces: 行模型 `MemoryProfileRow`、`MemoryRecordRow`、`MemoryRecordVersionRow`、`MemoryEpisodeSessionRow`、`MemoryIngestJobRow`、`MemoryConsolidationJobRow`、`MemorySkillCandidateRow`、`MemoryConflictRow`、`MemoryMutationLedgerRow`、`MemoryIndexOutboxRow`、`MemoryScopeMetaRow`；表名与 spec 5.1 完全一致。

**表结构固定值：**
- 主键 id 用 `String(36)`（UUID 文本），时间用 `DateTime(timezone=True)`，JSON 用 `JSON().with_variant(postgresql.JSONB, "postgresql")`，数组用 `JSON`（生产可等价映射，读取时由 repository 统一转 tuple）。
- `0006` 只建基础表/列；`memory_records.task_id` 和 `memory_episode_sessions.task_id/task_goal/task_goal_source/boundary_decision/boundary_uncertain` 由 Task 5B 的 `0007_task_classifier` 追加，不改 0006。
- `memory_records` 基础列必须匹配 spec；`provenance` 只存引用和 hash，不存正文。
- `memory_episode_sessions` 建部分唯一索引 `uq_memory_episode_open(user_id, thread_id) WHERE status='open'`。
- `memory_ingest_jobs.episode_key` 唯一；`memory_index_outbox(memory_id, revision, op)` 唯一；`memory_skill_candidates.origin_idempotency_key` 唯一。
- `memory_records` 建 `(user_id, kind, status)`、`(user_id, topic_key, status)`、`(user_id, consolidation_status) WHERE kind='episodic'`、`(user_id, valid_to, kind)`、active correction 部分唯一索引。

- [ ] **Step 1: 写失败测试**

~~~python
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from agentkit.persistence.schema import Base

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
EXPECTED = {
    "memory_profiles", "memory_records", "memory_record_versions",
    "memory_episode_sessions", "memory_ingest_jobs", "memory_consolidation_jobs",
    "memory_skill_candidates", "memory_conflicts", "memory_mutation_ledger",
    "memory_index_outbox", "memory_scope_meta", "memory_projection_state",
}


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_all_memory_tables_are_registered():
    assert EXPECTED <= set(Base.metadata.tables)


def test_only_one_open_session_per_user_thread(engine):
    insert = Base.metadata.tables["memory_episode_sessions"].insert()
    with engine.begin() as connection:
        connection.execute(insert.values(
            id="s1", user_id="u1", thread_id="t1", status="open",
            open_turn_id="u1", last_turn_id="u1", turn_refs=[],
            last_activity_at=NOW, created_at=NOW, updated_at=NOW,
        ))
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(insert.values(
                id="s2", user_id="u1", thread_id="t1", status="open",
                open_turn_id="u1", last_turn_id="u1", turn_refs=[],
                last_activity_at=NOW, created_at=NOW, updated_at=NOW,
            ))


def test_outbox_revision_is_unique(engine):
    table = Base.metadata.tables["memory_index_outbox"]
    values = dict(
        id="e1", user_id="u1", pk="u1:m1", memory_id="m1", op="upsert",
        revision=1, epoch=0, state="pending", attempts=0,
        next_attempt_at=NOW, created_at=NOW, updated_at=NOW,
    )
    with engine.begin() as connection:
        connection.execute(table.insert().values(values))
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(table.insert().values({**values, "id": "e2"}))
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_schema_v2.py -q`
Expected: FAIL，表不存在。

- [ ] **Step 3: 实现行模型、注册和迁移**

按 spec 5.1 建 12 张表；在 `schema.py` 显式 import 和 `__all__` 注册。迁移文件头：
~~~python
revision = "0006_agent_memory"
down_revision = "0005_skill_catalog"
~~~
`upgrade()` 先检查表/索引是否存在再创建（与 0005 一致），`downgrade()` 逆序 drop；不要调用 `Base.metadata.create_all` 代替迁移。

- [ ] **Step 4: 跑测试与迁移冒烟**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_schema_v2.py -q`
Expected: PASS（本 task 的测试全部通过）。
Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_bootstrap_schema.py -q`
Expected: 通过，head 仍可启动。

- [ ] **Step 5: 提交**

~~~bash
git add backend/packages/harness/agentkit/agents/memory/canonical/models.py \
  backend/packages/harness/agentkit/persistence/schema.py \
  backend/packages/harness/agentkit/persistence/migrations/versions/0006_agent_memory.py \
  backend/tests/test_memory_schema_v2.py
git commit -m "feat(memory): add canonical postgres schema"
~~~

## Task 3: Repository 事务、CAS、ledger 与 outbox

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/domain.py`
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/repository.py`
- Test: `backend/tests/test_memory_repository.py`

**Interfaces:**
- Consumes: `models.*Row`、`async_sessionmaker[AsyncSession]`。
- Produces:
  - `@dataclass(frozen=True) MutationContext(actor_type, actor_id, op_type, reason, source_episode_ids)`
  - `@dataclass(frozen=True) RecordDraft(id, kind, subtype, status, content, payload, topic_key, scope_condition, source, evidence_tier, confidence, salience, explicit_remember, parent_memory_id, correction_of, valid_from, valid_to, occurred_at, thread_id, agent_name, provenance, source_episode_ids)`
  - `MemoryRepository(session_factory)`
  - `async get_profile(user_id) -> MemoryProfileView | None`
  - `async ensure_profile(user_id) -> MemoryProfileView`
  - `async cas_profile(user_id, *, expected_revision, profile, tombstones, mutation) -> MemoryProfileView`
  - `async create_record(draft: RecordDraft, *, mutation) -> MemoryRecordView`
  - `async update_record(user_id, record_id, *, expected_revision, changes: Mapping[str, Any], mutation) -> MemoryRecordView`
  - `async get_record(user_id, record_id) -> MemoryRecordView | None`
  - `async find_semantic_existing(user_id, *, topic_key: str | None, include_pending: bool = True) -> list[MemoryRecordView]`
  - `async list_records(user_id, *, kind, statuses, limit=100) -> list[MemoryRecordView]`
  - `async list_pending_semantic(user_id, *, limit, cursor=None) -> PendingPage`
  - `async list_outbox(user_id, *, limit=100) -> list[MemoryOutboxView]`
  - `async soft_delete(user_id, record_id, *, reason, mutation) -> MemoryRecordView`
  - `async rollback(user_id, record_id, *, target_revision, mutation) -> MemoryRecordView`
  - `async clear_user(user_id, *, kinds: Sequence[str] | None, mutation) -> None`
  - `async touch_hits(user_id, record_ids, *, now) -> None`
  - `async last_ledger(user_id, *, kind=None) -> MemoryLedgerView | None`
  - `async open_session(user_id, thread_id, *, run_id, turn_id, now) -> MemorySessionView`
  - `async append_session_refs(session_id, refs, *, now) -> MemorySessionView`
  - `async close_session(session_id, *, reason, episode_key, now) -> MemorySessionView`
  - `async enqueue_ingest_job(session_id, *, episode_key, payload) -> MemoryIngestJobView`
  - `async claim_ingest_job(*, worker_id, lease_seconds) -> MemoryIngestJobView | None`
  - `async finish_ingest_job(job_id, *, status, error=None) -> None`
  - `async claim_outbox(limit=50) -> list[MemoryOutboxView]` / `finish_outbox` / `retry_outbox`
  - `async write_conflict(...) -> MemoryConflictView` / `async resolve_conflict(...)`
- 约束：`create_record`、`update_record`、`cas_profile` 必须在同一个 `AsyncSession` 事务中完成业务行 + `memory_record_versions` + `memory_mutation_ledger` +（仅 semantic/episodic）`memory_index_outbox`；任一步失败全部回滚。
- `create_record` 的 `id` 由调用方传入稳定 UUID；`update_record` 必须 `expected_revision` 匹配，否则抛 `MemoryConflictError`。
- outbox `pk = f"{user_id}:{memory_id}"`，`epoch` 从 `memory_scope_meta` 读，`revision` 用变更后的 revision。
- `claim_ingest_job` 在 PostgreSQL 使用 `FOR UPDATE SKIP LOCKED`，SQLite 使用条件更新；过期 lease 可重新领取。
- `rollback` 只接受未删除记录：把当前 revision 标为 `rolled_back`，用 target revision 的内容生成**新的** active revision（revision+1），写 ledger/outbox；deleted 记录拒绝回滚。
- `clear_user` 先递增 `memory_scope_meta.epoch` 并取消/置死未完成 job，再按 kind 软删/墓碑；旧 epoch 的 outbox/ingest 结果必须被丢弃，不能复活。

- [ ] **Step 1: 写失败测试**

~~~python
import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.canonical.domain import MutationContext, RecordDraft
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.persistence.schema import Base


def run(coro):
    return asyncio.run(coro)


async def _repo(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "m.db").as_posix())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return MemoryRepository(async_sessionmaker(engine, expire_on_commit=False)), engine


def semantic_draft(content: str, *, record_id: str = "m1") -> RecordDraft:
    return RecordDraft(
        id=record_id, kind="semantic", subtype="fact", status="active",
        content=content, payload={"content": content}, topic_key="t",
        scope_condition="global", source="conversation",
        evidence_tier="EXPLICIT_STATEMENT", confidence=0.85, salience=0.5,
        explicit_remember=False, parent_memory_id=None, correction_of=None,
        valid_from=None, valid_to=None, occurred_at=None, thread_id="t1",
        agent_name=None, provenance={}, source_episode_ids=(),
    )


def test_record_write_is_one_atomic_unit(tmp_path):
    async def scenario():
        repo, engine = await _repo(tmp_path)
        try:
            draft = RecordDraft(
                id="m1", kind="semantic", subtype="fact", status="active",
                content="用户偏好 pytest", payload={"content": "用户偏好 pytest"},
                topic_key="python.test", scope_condition="global",
                source="conversation", evidence_tier="EXPLICIT_STATEMENT",
                confidence=0.85, salience=0.5, explicit_remember=False,
                parent_memory_id=None, correction_of=None, valid_from=None,
                valid_to=None, occurred_at=None, thread_id="t1", agent_name=None,
                provenance={"message_id": "m1"}, source_episode_ids=("e1",),
            )
            record = await repo.create_record(
                draft,
                mutation=MutationContext("SYSTEM", "memory-worker", "ADD", None, ("e1",)),
            )
            async with repo._sf() as session:
                assert await session.get(__import__(
                    "agentkit.agents.memory.canonical.models", fromlist=["MemoryRecordRow"]
                ).MemoryRecordRow, record.id) is not None
                assert await session.scalar(
                    __import__("sqlalchemy").text("select count(*) from memory_mutation_ledger")
                ) == 1
                assert await session.scalar(
                    __import__("sqlalchemy").text("select count(*) from memory_index_outbox")
                ) == 1
        finally:
            await engine.dispose()
    run(scenario())


def test_profile_revision_conflict(tmp_path):
    async def scenario():
        repo, engine = await _repo(tmp_path)
        try:
            first = await repo.ensure_profile("u1")
            await repo.cas_profile(
                "u1", expected_revision=first.revision,
                profile={"identity": {}}, tombstones={},
                mutation=MutationContext("USER", "u1", "UPDATE", "manual", ()),
            )
            with pytest.raises(Exception, match="revision"):
                await repo.cas_profile(
                    "u1", expected_revision=first.revision,
                    profile={"identity": {}}, tombstones={},
                    mutation=MutationContext("USER", "u1", "UPDATE", "manual", ()),
                )
        finally:
            await engine.dispose()
    run(scenario())


def test_rollback_creates_a_new_active_revision(tmp_path):
    async def scenario():
        repo, engine = await _repo(tmp_path)
        try:
            first = await repo.create_record(
                semantic_draft("v1"), mutation=MutationContext("SYSTEM", "w", "ADD", None, ())
            )
            second = await repo.update_record(
                "u1", first.id, expected_revision=first.revision,
                changes={"content": "v2", "payload": {"content": "v2"}},
                mutation=MutationContext("SYSTEM", "w", "UPDATE", None, ()),
            )
            rolled = await repo.rollback(
                "u1", first.id, target_revision=first.revision,
                mutation=MutationContext("ADMIN", "admin", "ROLLBACK", "manual", ()),
            )
            assert rolled.revision == second.revision + 1
            assert rolled.status == "active"
            assert rolled.content == "v1"
        finally:
            await engine.dispose()
    run(scenario())
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_repository.py -q`
Expected: FAIL，`canonical.repository` 不存在。

- [ ] **Step 3: 实现 domain 和 repository**

`domain.py` 只放纯数据结构和 `MemoryConflictError`/`MemoryNotFoundError`/`MemoryUnavailableError`。`repository.py` 集中所有 SQL；使用 `session.begin()` 包住多行写入。写入 outbox 时不要调用 embedding/Milvus。`cas_profile` 的 revision 单调递增，并在 `memory_mutation_ledger.inverse_json` 记录旧 profile/tombstones。`claim_outbox` 采用与 `skills/store/sql.py` 相同的 PostgreSQL `SKIP LOCKED` + SQLite 条件更新模式。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_repository.py -q`
Expected: PASS（本 task 的测试全部通过）。

- [ ] **Step 5: 提交**

~~~bash
git add backend/packages/harness/agentkit/agents/memory/canonical/domain.py \
  backend/packages/harness/agentkit/agents/memory/canonical/repository.py \
  backend/tests/test_memory_repository.py
git commit -m "feat(memory): add transactional memory repository"
~~~

## Task 4: Evidence、proposal schema 与 LangMem A/B/C/D 适配器

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/evidence.py`
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/proposals.py`
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/operators.py`
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/fakes.py`
- Test: `backend/tests/test_memory_operators.py`

**Interfaces:**
- Consumes: `resolve_operator_model`、`langmem.create_memory_manager`、`langmem.create_prompt_optimizer`、`MemoryConfig`。
- Produces:
  - `EvidenceTier = Literal["EXPLICIT_MANDATE","EXPLICIT_STATEMENT","BEHAVIORAL_OBSERVED","INFERRED_HYPOTHESIS"]`
  - `confidence_for_tier(tier) -> float`、`tier_for_confidence(float) -> EvidenceTier | None`、`is_active_confidence(float)`、`is_pending_confidence(float)`、`is_profile_confidence(float)`
  - `SemanticDocument`、`ProfileDocument`、`DehydratedEpisode`、`SkillDraft`、`SemanticExtraction`、`ProfileProposal`、`EpisodeProposal`、`SkillProposal`
  - `LangMemOperators`，方法：`async extract_semantic(messages, existing) -> list[SemanticExtraction]`、`async extract_profile(current_message, profile) -> ProfileProposal`、`async dehydrate(turns, tool_summaries) -> EpisodeProposal`、`async propose_skill(current_skill_md, episodes) -> SkillProposal`
  - `FakeOperators`，同样四个方法，固定返回测试数据并记录 `calls`；不写任何存储。
- `create_memory_manager` 固定参数：A `enable_inserts=True, enable_updates=True, enable_deletes=True`；B `enable_inserts=False, enable_updates=True, enable_deletes=False`；C `enable_inserts=True, enable_updates=False, enable_deletes=False`；D `create_prompt_optimizer(model, kind="gradient")`。
- 所有同步 LangMem 调用通过 `asyncio.to_thread` 执行，并用 `asyncio.wait_for(..., timeout=config.langmem.operator_timeout_seconds)`；超时/异常转 `OperatorUnavailable`，不重试。
- 适配器只返回 proposal；不得 import repository、projection 或 Milvus。
- `FakeOperators` 必须能构造所有后续测试的边界值：低置信、pending、RemoveDoc、quote 不匹配、D 失败、超长 package。

- [ ] **Step 1: 写失败测试**

~~~python
import asyncio
from types import SimpleNamespace

import pytest

from agentkit.agents.memory.canonical import evidence
from agentkit.agents.memory.canonical.fakes import FakeOperators
from agentkit.agents.memory.canonical.operators import LangMemOperators


def test_evidence_tier_confidence_is_fixed():
    assert evidence.confidence_for_tier("EXPLICIT_MANDATE") == 0.95
    assert evidence.confidence_for_tier("EXPLICIT_STATEMENT") == 0.85
    assert evidence.confidence_for_tier("BEHAVIORAL_OBSERVED") == 0.70
    assert evidence.confidence_for_tier("INFERRED_HYPOTHESIS") == 0.50
    assert evidence.is_active_confidence(0.80) is True
    assert evidence.is_pending_confidence(0.79) is True
    assert evidence.is_pending_confidence(0.49) is False
    assert evidence.is_profile_confidence(0.85) is True
    assert evidence.is_profile_confidence(0.84) is False


def test_fake_operators_are_stateless():
    ops = FakeOperators()
    result = asyncio.run(ops.extract_semantic([], []))
    assert result == []
    assert ops.calls == ["extract_semantic"]


def test_operator_rejects_unsupported_model():
    class NoTools:
        pass

    with pytest.raises(Exception, match="structured output|tool"):
        LangMemOperators(NoTools(), timeout_seconds=1)
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_operators.py -q`
Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现 evidence/proposals/operators/fakes**

`evidence.py` 只允许四个固定映射，不接受模型给的浮点 confidence。`proposals.py` 用 `extra="forbid"`，字段长度/枚举严格；`SemanticDocument` 至少含 `content/topic_key/scope_condition/evidence_quote/evidence_tier`；`ProfileDocument` 用 bounded field keys；`DehydratedEpisode` 至少含 `task_intent/environment/trajectory_digest/final_outcome/key_takeaway/feedback_signal/salience/is_skill_candidate/evidence_refs/task_class/key_steps/trigger_text/outcome/blocker`。

`operators.py` 构造 manager 时只传模型和 schema，不传 store；A/B/C 使用 `manager.ainvoke`（内部同步调用包在 to_thread），D 使用 `optimizer.ainvoke`。模型能力检查用 `bind_tools`/`with_structured_output` 是否可调用；不可用时 `LangMemOperators.__init__` 抛 `OperatorUnavailable`。

- [ ] **Step 4: 跑测试与静态检查**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_operators.py -q`
Expected: PASS（本 task 的测试全部通过）。
Run: `grep -R "create_memory_store_manager" backend/packages/harness/agentkit/agents/memory/canonical`
Expected: 无匹配。

- [ ] **Step 5: 提交**

~~~bash
git add backend/packages/harness/agentkit/agents/memory/canonical/evidence.py \
  backend/packages/harness/agentkit/agents/memory/canonical/proposals.py \
  backend/packages/harness/agentkit/agents/memory/canonical/operators.py \
  backend/packages/harness/agentkit/agents/memory/canonical/fakes.py \
  backend/tests/test_memory_operators.py
git commit -m "feat(memory): add stateless langmem operators"
~~~

## Task 5: Session 捕获、去重与关闭

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/capture.py`
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/sessions.py`
- Modify: `backend/packages/harness/agentkit/agents/memory/canonical/domain.py`
- Test: `backend/tests/test_memory_sessions.py`

**Interfaces:**
- Consumes: `MemoryRepository`、`MemoryConfig.episodic`、LangChain 消息对象。
- Produces:
  - `@dataclass(frozen=True) TurnRef(message_id, role, run_id, tool_name, tool_status, summary, text_sha256)`
  - `@dataclass(frozen=True) TurnDigest(intent, outcome, tool_calls, summary, evidence_refs)`
  - `build_turn_refs(messages, *, run_id, max_summary_chars=600) -> list[TurnRef]`
  - `SessionService(repo, config)`
  - `async capture_turn(user_id, thread_id, run_id, messages, *, terminal: bool) -> CaptureResult`
  - `async close_session(user_id, thread_id, reason: Literal["terminal","compression","explicit_remember","idle","max_turns"]) -> CloseResult | None`
  - `async close_idle_sessions(now) -> int` / `async close_max_turn_sessions() -> int`
  - `async count_open(user_id, thread_id) -> int`
  - `is_explicit_remember(text: str) -> bool`
  - `CaptureResult(session_id, appended, closed, job_id)`
- `capture_turn` 只追加引用/摘要，不调 LLM、不写 Milvus；`terminal=True` 时关闭并生成一个 job。
- `message_id` 去重以 session 已有 `turn_refs` 为准；消息没有 id 时用 `sha256(run_id + role + index)` 生成稳定 id，不重放旧 checkpoint。
- 同一 `(user_id, thread_id)` 只允许一个 open session；`capture_turn` 在 `max_turns_per_session` 达到时先关旧 session 再开新 session。
- `episode_key = f"{session_id}:{last_turn_id}:{reason}"`；`close_session` 重复调用返回同一个 job，不插入第二行。
- `turn_refs` 不保存完整 `content`；`summary` 由宿主截断/脱敏，`text_sha256` 只用于去重和审计。
- `close_idle_sessions` 使用 `last_activity_at < now - 30min`；`close_max_turn_sessions` 使用 `>=40` 个 user turn。

- [ ] **Step 1: 写失败测试**

~~~python
import asyncio
import json
from dataclasses import asdict

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentkit.agents.memory.canonical.capture import build_turn_refs
from agentkit.agents.memory.canonical.sessions import SessionService, is_explicit_remember


def test_turn_refs_drop_long_tool_payload_and_keep_only_refs():
    refs = build_turn_refs(
        [
            HumanMessage(id="u1", content="帮我修一下部署脚本"),
            AIMessage(id="a1", content="好的"),
            HumanMessage(id="u1", content="重复消息不应再次出现"),
        ],
        run_id="r1",
    )
    assert [ref.message_id for ref in refs] == ["u1", "a1"]
    assert json.dumps([asdict(ref) for ref in refs], ensure_ascii=False).count("帮我修一下部署脚本") <= 1
    assert all(len(ref.summary) <= 600 for ref in refs)


def test_tool_output_is_not_stored():
    refs = build_turn_refs(
        [ToolMessage(id="tool1", content="x" * 10_000, tool_call_id="c1")],
        run_id="r1",
    )
    payload = json.dumps([asdict(ref) for ref in refs], ensure_ascii=False)
    assert "x" * 1_000 not in payload
    assert len(payload) < 2_000


def test_explicit_remember_is_detected_without_an_llm():
    assert is_explicit_remember("记住这次部署的修复步骤") is True
    assert is_explicit_remember("remember this workflow") is True
    assert is_explicit_remember("这次用三行输出") is False


def test_concurrent_capture_keeps_one_open_session(tmp_path):
    async def scenario():
        _repo, sessions, _operators = await make_stack(tmp_path)
        await asyncio.gather(*[
            sessions.capture_turn(
                "u1", "t1", f"r{index}",
                [HumanMessage(id="u1", content="同一轮")],
                terminal=False,
            )
            for index in range(2)
        ])
        assert await sessions.count_open("u1", "t1") == 1
    asyncio.run(scenario())


def test_capture_dedupes_and_closes_once(tmp_path):
    async def scenario():
        repo, sessions, operators = await make_stack(tmp_path)
        first = await sessions.capture_turn(
            "u1", "t1", "r1",
            [HumanMessage(id="u1", content="第一轮"), AIMessage(id="a1", content="收到")],
            terminal=False,
        )
        second = await sessions.capture_turn(
            "u1", "t1", "r1",
            [HumanMessage(id="u1", content="第一轮"), AIMessage(id="a1", content="收到")],
            terminal=True,
        )
        assert first.session_id == second.session_id
        assert second.closed is True
        assert second.job_id is not None
        again = await sessions.close_session("u1", "t1", "terminal")
        assert again.job_id == second.job_id
        assert operators.calls == []
    asyncio.run(scenario())


def test_idle_and_max_turn_sweepers_use_config_values(tmp_path):
    async def scenario():
        _repo, sessions, _operators = await make_stack(tmp_path)
        await sessions.capture_turn("u1", "t1", "r1", [HumanMessage(id="u1", content="x")], terminal=False)
        assert await sessions.close_idle_sessions(now=utc_now() + timedelta(minutes=31)) == 1
        # 41st user turn closes the previous session and opens a new one.
        for index in range(41):
            await sessions.capture_turn(
                "u1", "t2", f"r{index}",
                [HumanMessage(id=f"u{index}", content="x")],
                terminal=False,
            )
        assert await sessions.count_open("u1", "t2") == 1
    asyncio.run(scenario())
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_sessions.py -q`
Expected: FAIL，`capture.py/sessions.py` 不存在。

- [ ] **Step 3: 实现 capture 和 sessions**

`capture.py` 用 `message_content_to_text` 提取文本，调用 `PIIRedactor.redact_for_memory`；只保留摘要和 hash，过滤 `hide_from_ui` 与 profile 注入消息。`sessions.py` 负责查找 open session、追加 refs、更新 `last_activity_at`、关闭和入队；关闭事务由 repository 提供。`max_turns` 按 `role == "user"` 计数，达到阈值时以 `max_turns` 关闭旧 session，再以当前 turn 创建新 session。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_sessions.py -q`
Expected: PASS（本 task 的测试全部通过）。

- [ ] **Step 5: 提交**

~~~bash
git add backend/packages/harness/agentkit/agents/memory/canonical/capture.py \
  backend/packages/harness/agentkit/agents/memory/canonical/sessions.py \
  backend/packages/harness/agentkit/agents/memory/canonical/domain.py \
  backend/tests/test_memory_sessions.py
git commit -m "feat(memory): capture turns into episode sessions"
~~~

## Task 5A: 任务分类器中间件与 TaskContext

**Files:**
- Create: `backend/packages/harness/agentkit/agents/middlewares/task_classifier.py`
- Modify: `backend/packages/harness/agentkit/agents/thread_state.py`
- Modify: `backend/packages/harness/agentkit/agents/middlewares/assembly.py`
- Test: `backend/tests/test_task_classifier.py`

**Interfaces:**
- Consumes: `MemoryConfig.task_classifier`、`AppConfig`、`ThreadState.messages`、`resolve_operator_model`。
- Produces:
  - `TaskBoundaryDecision = Literal["CONTINUE","NEW_TASK","UNCERTAIN"]`
  - `class TaskBoundaryResult(BaseModel): decision: TaskBoundaryDecision; task_goal: str | None`
  - `class RunPair(TypedDict)`：`run_id`、`human_message`、`final_ai_message`
  - `class TaskClassifierInput(TypedDict)`：`current_human_message`、`task_goal`、`completed_runs: list[RunPair]`
  - `class TaskContext(TypedDict)`：`task_id`、`task_goal`、`task_goal_source`、`status`、`started_at`、可选 `completed_runs: list[RunPair]`、`boundary_decision`、`boundary_uncertain`、`boundary_pending`、`previous_task_id`、`last_classified_message_id`
  - `class TaskClassifierMiddleware(AgentMiddleware)`：`async abefore_agent(state, runtime) -> dict | None`
  - `append_completed_run(context: TaskContext, pair: RunPair, *, max_history_runs: int) -> TaskContext`
  - `ThreadState.task_context: NotRequired[TaskContext | None]`
- 只写 `task_context` 和 `task:classified` 事件；不写记忆、不写 session。`task_id` 由宿主代码生成，模型只返回 `decision/task_goal`。

- [ ] **Step 1: 写失败测试**

~~~python
import asyncio
from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from agentkit.agents.middlewares.task_classifier import (
    TaskBoundaryResult,
    TaskClassifierMiddleware,
    append_completed_run,
)
from agentkit.config.memory_config import MemoryConfig


class FakeClassifier:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def classify(self, input):
        self.calls.append(input)
        if self.error:
            raise self.error
        return self.result


def _runtime():
    return SimpleNamespace(context={"user_id": "u1", "thread_id": "t1"})


def test_empty_goal_forces_new_task_and_uses_model_goal():
    classifier = FakeClassifier(TaskBoundaryResult(decision="NEW_TASK", task_goal="修复部署脚本"))
    middleware = TaskClassifierMiddleware(config=MemoryConfig(enabled=True), classifier=classifier)
    state = {"messages": [HumanMessage(id="u1", content="帮我看下部署")], "task_context": None}
    update = asyncio.run(middleware.abefore_agent(state, _runtime()))
    context = update["task_context"]
    assert context["task_id"]
    assert context["task_goal"] == "修复部署脚本"
    assert context["task_goal_source"] == "classifier"
    assert context["boundary_pending"] is True
    assert classifier.calls


def test_continue_does_not_rewrite_goal():
    classifier = FakeClassifier(TaskBoundaryResult(decision="CONTINUE", task_goal="另一个目标"))
    middleware = TaskClassifierMiddleware(config=MemoryConfig(enabled=True), classifier=classifier)
    state = {
        "messages": [HumanMessage(id="u2", content="继续")],
        "task_context": {"task_id": "task-1", "task_goal": "原目标", "task_goal_source": "classifier", "status": "open"},
    }
    update = asyncio.run(middleware.abefore_agent(state, _runtime()))
    assert update["task_context"]["task_id"] == "task-1"
    assert update["task_context"]["task_goal"] == "原目标"
    assert update["task_context"]["boundary_pending"] is False


def test_classifier_error_with_non_empty_goal_degrades_to_uncertain():
    classifier = FakeClassifier(error=TimeoutError())
    middleware = TaskClassifierMiddleware(config=MemoryConfig(enabled=True), classifier=classifier)
    state = {
        "messages": [HumanMessage(id="u3", content="继续")],
        "task_context": {"task_id": "task-1", "task_goal": "原目标", "status": "open"},
    }
    update = asyncio.run(middleware.abefore_agent(state, _runtime()))
    assert update["task_context"]["boundary_decision"] == "UNCERTAIN"
    assert update["task_context"]["task_id"] == "task-1"
    assert update["task_context"]["boundary_pending"] is False


def test_empty_goal_and_classifier_error_uses_fallback():
    classifier = FakeClassifier(error=TimeoutError())
    middleware = TaskClassifierMiddleware(config=MemoryConfig(enabled=True), classifier=classifier)
    state = {"messages": [HumanMessage(id="u4", content="帮我修复部署")], "task_context": None}
    update = asyncio.run(middleware.abefore_agent(state, _runtime()))
    context = update["task_context"]
    assert context["task_id"]
    assert context["task_goal"] == "帮我修复部署"
    assert context["task_goal_source"] == "fallback"
    assert context["boundary_decision"] == "NEW_TASK"


def test_classifier_input_uses_only_completed_run_pairs():
    classifier = FakeClassifier(TaskBoundaryResult(decision="CONTINUE"))
    middleware = TaskClassifierMiddleware(config=MemoryConfig(enabled=True), classifier=classifier)
    state = {
        "messages": [HumanMessage(id="u2", content="新 run")],
        "task_context": {
            "task_id": "task-1", "task_goal": "目标", "status": "open",
            "completed_runs": [
                {"run_id": "r1", "human_message": "旧 run", "final_ai_message": "旧 run 最终回复"},
            ],
        },
    }
    asyncio.run(middleware.abefore_agent(state, _runtime()))
    assert classifier.calls[0]["current_human_message"] == "新 run"
    assert classifier.calls[0]["completed_runs"] == [
        {"run_id": "r1", "human_message": "旧 run", "final_ai_message": "旧 run 最终回复"},
    ]


def test_append_completed_run_keeps_only_pairs_and_bounds():
    context = {"task_id": "task-1", "completed_runs": []}
    for index in range(3):
        context = append_completed_run(
            context,
            {"run_id": f"r{index}", "human_message": f"h{index}", "final_ai_message": f"a{index}"},
            max_history_runs=2,
        )
    assert [pair["run_id"] for pair in context["completed_runs"]] == ["r1", "r2"]
    assert all(set(pair) == {"run_id", "human_message", "final_ai_message"} for pair in context["completed_runs"])


def test_subagent_does_not_run_classifier():
    classifier = FakeClassifier(TaskBoundaryResult(decision="NEW_TASK", task_goal="不该发生"))
    middleware = TaskClassifierMiddleware(config=MemoryConfig(enabled=True), classifier=classifier)
    runtime = SimpleNamespace(context={"user_id": "u1", "subagent_name": "researcher"})
    assert asyncio.run(middleware.abefore_agent({"messages": []}, runtime)) is None
    assert classifier.calls == []
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_task_classifier.py -q`
Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现中间件与 TaskContext**

`thread_state.py` 增加 `TaskContext` 和 `task_context` channel；不要把它放进 `messages`。`task_classifier.py` 负责：
- 从 `state.messages` 取最新真实用户消息作为 `current_human_message`；
- 从 `state.task_context` 取 `task_goal/task_id/started_at/completed_runs`；
- 只把 `completed_runs` 中已有的 `human_message + final_ai_message` 对加入输入；当前新 run 只提供 `human_message`；按 `max_history_runs/max_input_tokens` 截断；不解析中间 tool-call AI；
- 调分类器（默认 `agents.default_model`，thinking 关闭，`timeout_seconds`），结构化输出 `TaskBoundaryResult`；
- `task_goal` 为空 -> 强制 NEW_TASK；调用分类器生成 goal；失败/禁用 -> fallback（首条脱敏消息，纯多模态用 `未命名任务`）；
- `CONTINUE/UNCERTAIN` 忽略模型 `task_goal`；
- `append_completed_run` 只接受 `{run_id, human_message, final_ai_message}`，保留最近 `max_history_runs` 个；NEW_TASK 时先清空；
- 非空 goal + NEW_TASK 的非法 goal -> `UNCERTAIN`，不切；
- NEW_TASK 用 `uuid4()` 生成 `task_id`，写 `boundary_pending=True`、`previous_task_id`；
- 子代理、无真实用户消息、无 user_id 直接返回 `None`。

在 `assembly.py` 的 `build_runtime_middlewares` 中把 `TaskClassifierMiddleware` 放在 `MemoryMiddleware` 之前；只在 lead 链安装；`memory.enabled=false` 时不安装。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_task_classifier.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add backend/packages/harness/agentkit/agents/middlewares/task_classifier.py \
  backend/packages/harness/agentkit/agents/thread_state.py \
  backend/packages/harness/agentkit/agents/middlewares/assembly.py \
  backend/tests/test_task_classifier.py
git commit -m "feat(memory): add task classifier middleware"
~~~

## Task 5B: Task context 持久化与 0007 迁移

**Files:**
- Create: `backend/packages/harness/agentkit/persistence/migrations/versions/0007_task_classifier.py`
- Modify: `backend/packages/harness/agentkit/agents/memory/canonical/models.py`
- Modify: `backend/packages/harness/agentkit/agents/memory/canonical/domain.py`
- Modify: `backend/packages/harness/agentkit/agents/memory/canonical/repository.py`
- Test: `backend/tests/test_memory_schema_v2.py`
- Test: `backend/tests/test_memory_repository.py`

**Interfaces:**
- Consumes: Task 2 的 `memory_records/memory_episode_sessions`；Task 5A 的 `TaskContext`。
- Produces:
  - `RecordDraft.task_id: str | None = None`
  - `MemorySessionView.task_id/task_goal/task_goal_source/boundary_decision/boundary_uncertain`
  - `MemoryRepository.open_session(..., task_id, task_goal, task_goal_source, boundary_decision, boundary_uncertain)`
  - `MemoryRepository.create_record` 写入 `task_id`
- `0007_task_classifier` 的 `down_revision="0006_agent_memory"`；只加列/索引，不改 0006。

- [ ] **Step 1: 写失败测试**

~~~python
from agentkit.persistence.schema import Base


def test_task_columns_are_registered():
    session = Base.metadata.tables["memory_episode_sessions"]
    record = Base.metadata.tables["memory_records"]
    assert {"task_id", "task_goal", "task_goal_source", "boundary_decision", "boundary_uncertain"} <= set(session.columns.keys())
    assert "task_id" in record.columns


def test_migration_follows_0006():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "packages/harness/agentkit/persistence/migrations/versions/0007_task_classifier.py"
    spec = importlib.util.spec_from_file_location("task_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0007_task_classifier"
    assert module.down_revision == "0006_agent_memory"
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_schema_v2.py -q`
Expected: FAIL，列不存在。

- [ ] **Step 3: 实现列、迁移和 repository 字段**

迁移按以下顺序：
1. 给 `memory_episode_sessions` 加 nullable 的 `task_id/task_goal/task_goal_source/boundary_decision` 和 `boundary_uncertain default false`；
2. 回填旧 session：`task_id=id`、`task_goal=''`、`task_goal_source='fallback'`；
3. 将 `task_id/task_goal/task_goal_source` 改为 not null；
4. 给 `memory_records` 加 nullable `task_id`；
5. 建 `(user_id, task_id)` 索引；SQLite 用 batch mode，Postgres 用 ALTER。
`models.py/domain.py/repository.py` 同步字段；`create_record` 始终把 `draft.task_id` 写入。不要修改 0006。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_schema_v2.py backend/tests/test_memory_repository.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add backend/packages/harness/agentkit/persistence/migrations/versions/0007_task_classifier.py \
  backend/packages/harness/agentkit/agents/memory/canonical/models.py \
  backend/packages/harness/agentkit/agents/memory/canonical/domain.py \
  backend/packages/harness/agentkit/agents/memory/canonical/repository.py \
  backend/tests/test_memory_schema_v2.py backend/tests/test_memory_repository.py
git commit -m "feat(memory): persist task context"
~~~

## Task 5C: Session 关闭/新建与 task context 接线

**Files:**
- Modify: `backend/packages/harness/agentkit/agents/memory/canonical/sessions.py`
- Modify: `backend/packages/harness/agentkit/agents/middlewares/memory_middleware.py`
- Test: `backend/tests/test_memory_sessions.py`
- Test: `backend/tests/test_memory_runtime_v2.py`

**Interfaces:**
- Consumes: `TaskContext`、`append_completed_run`、`MemoryRepository.open_session`、`RecordDraft.task_id`。
- Produces:
  - `SessionService.capture_turn(..., task_context: TaskContext | None = None, explicit_remember: bool = False)`
  - `close_reason` 支持 `task_boundary`
  - `MemoryMiddleware.after_agent` 消费 `task_context.boundary_pending`：先关闭旧 session，再追加当前轮
  - ingest job payload 带 `task_id/task_goal`；episode 记录带 `task_id` 由 Task 6 在 `episodic.py` 落库时完成（本 task 不做，因为 `episodic.py` 尚未创建）。
- 规则：同一 task_id 续写不换 session；task_id 变化必先关闭旧 session；UNCERTAIN 不换；旧任务消息不进入新 session；关闭失败不切换 task_id。
- 每个 run 的 `after_agent` 捕获后，调用 `append_completed_run` 把 `{run_id, human_message, final_ai_message}` 追加到 `task_context.completed_runs`，只保留最近 `max_history_runs` 个；NEW_TASK 清空，CONTINUE/UNCERTAIN 追加。

- [ ] **Step 1: 写失败测试**

~~~python
import asyncio

from langchain_core.messages import HumanMessage


def _task(task_id: str, goal: str) -> dict:
    return {
        "task_id": task_id,
        "task_goal": goal,
        "task_goal_source": "classifier",
        "status": "open",
        "started_at": "2026-10-26T00:00:00Z",
    }


def test_new_task_closes_old_session_once(tmp_path):
    async def scenario():
        repo, sessions, _ = await make_stack(tmp_path)
        old = await sessions.capture_turn(
            "u1", "t1", "r1",
            [HumanMessage(id="u1", content="旧任务")],
            terminal=False, task_context=_task("task-1", "旧目标"),
        )
        new = await sessions.capture_turn(
            "u1", "t1", "r2",
            [HumanMessage(id="u2", content="新任务")],
            terminal=False, task_context=_task("task-2", "新目标"),
        )
        old_view = await repo.get_session(old.session_id)
        new_view = await repo.get_session(new.session_id)
        assert new.session_id != old.session_id
        assert old_view.status == "closed"
        assert old_view.close_reason == "task_boundary"
        assert new_view.task_id == "task-2"
        assert [ref["message_id"] for ref in new_view.turn_refs] == ["u2"]

    asyncio.run(scenario())


def test_continue_keeps_same_session(tmp_path):
    async def scenario():
        repo, sessions, _ = await make_stack(tmp_path)
        first = await sessions.capture_turn(
            "u1", "t1", "r1",
            [HumanMessage(id="u1", content="第一步")],
            terminal=False, task_context=_task("task-1", "目标"),
        )
        second = await sessions.capture_turn(
            "u1", "t1", "r2",
            [HumanMessage(id="u2", content="继续")],
            terminal=False, task_context=_task("task-1", "目标"),
        )
        assert second.session_id == first.session_id
        view = await repo.get_session(first.session_id)
        assert view.task_id == "task-1"
        assert view.task_goal == "目标"
        assert [ref["message_id"] for ref in view.turn_refs] == ["u1", "u2"]

    asyncio.run(scenario())


def test_uncertain_does_not_split(tmp_path):
    async def scenario():
        repo, sessions, _ = await make_stack(tmp_path)
        first = await sessions.capture_turn(
            "u1", "t1", "r1",
            [HumanMessage(id="u1", content="第一步")],
            terminal=False, task_context=_task("task-1", "目标"),
        )
        second = await sessions.capture_turn(
            "u1", "t1", "r2",
            [HumanMessage(id="u2", content="可能是新任务")],
            terminal=False,
            task_context={**_task("task-1", "目标"), "boundary_uncertain": True},
        )
        assert second.session_id == first.session_id
        view = await repo.get_session(first.session_id)
        assert view.status == "open"
        assert view.task_id == "task-1"
        assert view.boundary_uncertain is True

    asyncio.run(scenario())


def test_job_payload_carries_task_id(tmp_path):
    async def scenario():
        repo, sessions, _ = await make_stack(tmp_path)
        captured = await sessions.capture_turn(
            "u1", "t1", "r1",
            [HumanMessage(id="u1", content="任务")],
            terminal=True, task_context=_task("task-1", "目标"),
        )
        job = await repo.get_ingest_job_by_id(captured.job_id)
        assert job.payload["task_id"] == "task-1"
        assert job.payload["task_goal"] == "目标"

    asyncio.run(scenario())
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_sessions.py -q`
Expected: FAIL，`capture_turn` 不接受 task_context 或 session 没有 task 字段。

- [ ] **Step 3: 实现接线**

`SessionService.capture_turn` 先比较 open session 的 `task_id` 与传入 `task_context.task_id`：不同则关闭旧 session（reason=`task_boundary`）再 open 新 session；相同则追加。关闭 payload 写 `task_id/task_goal`。`MemoryMiddleware.after_agent` 从 state 取 `task_context`；若 `boundary_pending=True`，先 `close_session(reason="task_boundary")`，再 capture；capture 成功后用本轮 human message 和最终可见 AI message 调用 `append_completed_run`，NEW_TASK 时先清空 `completed_runs`，并清除 `boundary_pending`（写回 state 或下一次分类覆盖）。episode 落库时的 `RecordDraft.task_id` 在 Task 6 接入，本 task 只保证 job payload 已带 task_id。

- [ ] **Step 4: 跑测试与运行时回归**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_sessions.py backend/tests/test_memory_runtime_v2.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

~~~bash
git add backend/packages/harness/agentkit/agents/memory/canonical/sessions.py \
  backend/packages/harness/agentkit/agents/middlewares/memory_middleware.py \
  backend/tests/test_memory_sessions.py backend/tests/test_memory_runtime_v2.py
git commit -m "feat(memory): wire task boundaries into sessions"
~~~

## Task 6: L1 门控、情景脱水与纠正

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/episodic.py`
- Test: `backend/tests/test_memory_episodic.py`

**Interfaces:**
- Consumes: `MemoryRepository`、`SessionService`、`LangMemOperators`/`FakeOperators`、`TurnDigest`。
- Produces:
  - `L1Gate(threshold=0.55).evaluate(signals) -> GateDecision(score, passed, reasons)`
  - `EpisodicService(repo, operators, gate, max_summary_tokens)`
  - `async process_ingest_job(job_id) -> MemoryRecordView | None`
  - `async correct(episode_id, *, content, outcome, evidence_refs, expected_revision) -> MemoryRecordView`
  - `async list_episodes(user_id, *, limit, cursor) -> EpisodePage`
- L1 分数取最大值：工具错误/重试 `+0.30`、用户纠正/否定 `+0.30`、多工具链 `+0.20`、长任务 `+0.10`、最终成功/失败 `+0.10`；`>=0.55` 通过。
- C 输出必须通过：`task_intent/final_outcome/key_takeaway` 非空、`evidence_refs` 非空、`outcome in {"SUCCESS","PARTIAL","FAILURE","ABORTED"}`、无原始 transcript 字段。
- 落库载荷只含摘要、环境、步骤签名、outcome、feedback、evidence refs；`kind="episodic"`、`subtype="episode"`、`status="active"`、`consolidation_status="pending"`。
- `RecordDraft.task_id` 从 `job.payload["task_id"]` 读取；旧 job 没有 task_id 时允许为空。
- 纠正：新记录 `status="active"`、`correction_of=old_id`；旧记录 `status="corrected"`；重复纠正同一旧记录必须被部分唯一索引拒绝。
- 重复 job：`episode_key` 已存在时返回已有记录，不新增版本/outbox。

- [ ] **Step 1: 写失败测试**

~~~python
import asyncio

from agentkit.agents.memory.canonical.episodic import L1Gate
from agentkit.agents.memory.canonical.fakes import DehydratedEpisode
from agentkit.agents.memory.canonical.sessions import SessionService


def test_l1_gate_uses_max_signal_and_threshold():
    gate = L1Gate(threshold=0.55)
    assert gate.evaluate(signals(tool_error=True, retry=True)).passed is True
    assert gate.evaluate(signals(greeting=True)).passed is False


def test_episode_job_is_idempotent_and_has_no_transcript(tmp_path):
    async def scenario():
        repo, sessions, ops = await make_stack(tmp_path)
        captured = await sessions.capture_turn(
            "u1", "t1", "r1",
            [HumanMessage(id="u1", content="部署失败后修复"), AIMessage(id="a1", content="已修复")],
            terminal=True,
            task_context={"task_id": "task-1", "task_goal": "修复部署", "task_goal_source": "classifier", "status": "open", "started_at": "2026-10-26T00:00:00Z"},
        )
        service = EpisodicService(repo, ops, L1Gate(0.55), max_summary_tokens=1200)
        first = await service.process_ingest_job(captured.job_id)
        second = await service.process_ingest_job(captured.job_id)
        assert first.id == second.id
        assert first.payload["final_outcome"] == "SUCCESS"
        assert "messages" not in first.payload
        assert "已修复" not in str(first.payload)
        assert first.consolidation_status == "pending"
        assert first.task_id == "task-1"
    asyncio.run(scenario())


def test_correction_supersedes_old_episode():
    async def scenario():
        repo, sessions, ops = await make_stack(tmp_path)
        # ... create old episode ...
        corrected = await EpisodicService(repo, ops, L1Gate(0.55), 1200).correct(
            old.id,
            content="更正后的摘要",
            outcome="SUCCESS",
            evidence_refs=["message:new"],
            expected_revision=old.revision,
        )
        assert corrected.correction_of == old.id
        assert (await repo.get_record("u1", old.id)).status == "corrected"
    asyncio.run(scenario())
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_episodic.py -q`
Expected: FAIL，`L1Gate` 不存在。

- [ ] **Step 3: 实现 episodic 流程**

`process_ingest_job`：领取/校验 job → 读取 closed session → `L1Gate` 拒绝则 finish job 且不写 memory → 调 C → 校验 schema/PII/长度 → `repo.create_record`（`RecordDraft.task_id` 从 job payload 读取；同一事务含 ledger/outbox）→ 标记 job done 和 `consolidation_status=pending`。C 只接收 `TurnDigest` 和工具摘要，不接收完整 transcript。`correct` 使用 CAS，先创建新 record 再把旧 record 转 corrected，两个写入同一事务。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_episodic.py -q`
Expected: PASS（本 task 的测试全部通过）。

- [ ] **Step 5: 提交**

~~~bash
git add backend/packages/harness/agentkit/agents/memory/canonical/episodic.py \
  backend/tests/test_memory_episodic.py
git commit -m "feat(memory): dehydrate episodes behind l1 gate"
~~~

## Task 7: 画像直写、手动编辑与字段墓碑

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/profile.py`
- Modify: `backend/packages/harness/agentkit/agents/memory/canonical/domain.py`
- Test: `backend/tests/test_memory_profile.py`

**Interfaces:**
- Consumes: `MemoryRepository`、`LangMemOperators.extract_profile`、`ProfileDocument`、`validate_profile_value`。
- Produces:
  - `ProfileService(repo, operators, auto_write_threshold=0.85)`
  - `async get(user_id) -> ProfileSnapshotView`
  - `async patch_manual(user_id, fields: Mapping[str, Any], *, expected_revision: int) -> ProfileSnapshotView`
  - `async clear_field(user_id, field_path: str) -> ProfileSnapshotView`
  - `async apply_extraction(user_id, *, current_message: str, proposal: ProfileProposal) -> ProfileApplyResult`
  - `ProfileSnapshotView(revision, fields, updated_at)`；`ProfileFieldView(value, source, evidence_tier, confidence, observed_at, tombstoned_at)`
- `patch_manual` 是普通写入：source `"user_manual"`、不设置 pin、revision +1；后续 `apply_extraction` 达到 `0.85` 可以覆盖。
- `apply_extraction` 只接受当前新用户消息；`evidence_quote` 必须归一化后是当前消息子串；字段白名单复用 `PROFILE_FIELD_PATHS`；不扫描历史 transcript/checkpoint/episodic。
- 低于 `0.85` 或同权威冲突：不写字段，写 `memory_conflicts` HOLD，返回 `conflict_id`；高证据覆盖要把旧值写进 `memory_mutation_ledger.inverse_json`。
- 清空字段写 `tombstones[field]={cleared_at}`，删除 profile 当前值；不物理删除 profile 行。

- [ ] **Step 1: 写失败测试**

~~~python
import asyncio


def test_manual_edit_has_no_pin_and_high_evidence_overwrites(tmp_path):
    async def scenario():
        repo, service = await profile_stack(tmp_path)
        first = await service.patch_manual(
            "u1", {"communication.language": "zh-CN"}, expected_revision=0
        )
        assert first.revision == 1
        assert "pinned" not in first.fields["communication.language"].model_dump()
        result = await service.apply_extraction(
            "u1",
            current_message="以后都用英文回答我",
            proposal=profile_proposal(
                field="communication.language", value="en-US",
                tier="EXPLICIT_MANDATE", quote="以后都用英文回答我",
            ),
        )
        assert result.applied is True
        assert result.snapshot.fields["communication.language"].value == "en-US"
        assert result.snapshot.revision == 2
        ledger = await repo.last_ledger("u1")
        assert ledger.inverse_json["profile"]["communication"]["language"]["value"] == "zh-CN"
    asyncio.run(scenario())


def test_low_evidence_or_foreign_quote_holds(tmp_path):
    async def scenario():
        _repo, service = await profile_stack(tmp_path)
        low = await service.apply_extraction(
            "u1", current_message="我可能喜欢英文",
            proposal=profile_proposal(
                field="communication.language", value="en-US",
                tier="BEHAVIORAL_OBSERVED", quote="我可能喜欢英文",
            ),
        )
        assert low.applied is False and low.conflict_id
        foreign = await service.apply_extraction(
            "u1", current_message="今天天气不错",
            proposal=profile_proposal(
                field="communication.language", value="en-US",
                tier="EXPLICIT_MANDATE", quote="以后都用英文回答我",
            ),
        )
        assert foreign.applied is False
    asyncio.run(scenario())


def test_stale_revision_is_conflict_and_clear_is_tombstone(tmp_path):
    async def scenario():
        _repo, service = await profile_stack(tmp_path)
        await service.patch_manual("u1", {"identity.preferred_name": "小王"}, expected_revision=0)
        with pytest.raises(Exception, match="revision"):
            await service.patch_manual("u1", {"identity.preferred_name": "小李"}, expected_revision=0)
        cleared = await service.clear_field("u1", "identity.preferred_name")
        assert "identity.preferred_name" not in cleared.fields
    asyncio.run(scenario())
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_profile.py -q`
Expected: FAIL，`canonical.profile` 不存在。

- [ ] **Step 3: 实现 profile 流程**

目标字段用 new canonical `ProfileFieldView`，不要复用带 `pinned` 的旧 `ProfileField`。`apply_extraction` 先校验 quote，再按 tier 计算固定 confidence；若低于阈值或当前值来源为同权威且值不同，写 conflict HOLD；否则 `repo.cas_profile` 覆盖并保留旧值。页面 PATCH 走 `patch_manual`，不做任何“手动优先”分支。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_profile.py -q`
Expected: PASS。旧 `test_memory_core` 的 pin 断言留到 Task 16 切换运行时后统一更新，避免在本 task 让旧写路径和新语义互相污染。

- [ ] **Step 5: 提交**

~~~bash
git add backend/packages/harness/agentkit/agents/memory/canonical/profile.py \
  backend/packages/harness/agentkit/agents/memory/canonical/domain.py \
  backend/tests/test_memory_profile.py
git commit -m "feat(memory): write profiles from current evidence"
~~~

## Task 8: 语义事实、pending_review 与自动晋升

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/semantic.py`
- Test: `backend/tests/test_memory_semantic.py`

**Interfaces:**
- Consumes: `MemoryRepository`、`LangMemOperators.extract_semantic`、`MemoryResolver`（新 canonical 版本）、`evidence`。
- Produces:
  - `SemanticService(repo, operators, resolver, config)`
  - `async ingest_episodes(user_id, episode_ids: Sequence[str], *, source_text: str) -> SemanticIngestResult`
  - `async promote(user_id, record_id: str, *, expected_revision: int) -> MemoryRecordView`
  - `async expire_pending(now) -> int`
  - `async list_pending(user_id, *, limit, cursor) -> PendingPage`
- A 输入 existing 包含 active 与同 topic `pending_review`；pending 不进 Milvus、不注入、不出现在 `memory_search`。
- 宿主解析：id 不在 existing → ADD；id 在 existing 且内容变化 → UPDATE；内容相同 → NOOP；`RemoveDoc` → DELETE；未知 id → 拒绝整条 proposal。
- confidence：`>=0.80 active`、`0.50–0.79 pending_review`、`<0.50 丢弃`；同权威冲突 → `memory_conflicts` open，旧 active 不变，不写 outbox。
- pending 后续证据达到 `>=0.80`：同一条记录原地晋升 active，revision+1，写 ledger + outbox + audit；达到 `30 天` 转 `expired`，不可直接 active。
- `explicit_remember` 只是语义保护标记，不是画像 pin；不能阻止用户删除。
- 从 episode 提取的语义记录继承第一个 source episode 的 `task_id`；没有 source task_id 时留空。

- [ ] **Step 1: 写失败测试**

~~~python
import asyncio


def test_semantic_thresholds_and_pending_is_not_projected(tmp_path):
    async def scenario():
        repo, service, ops = await semantic_stack(tmp_path)
        ops.semantic_results = [
            semantic_doc("用户偏好 pytest", tier="EXPLICIT_STATEMENT"),
            semantic_doc("用户可能喜欢 unittest", tier="BEHAVIORAL_OBSERVED"),
        ]
        result = await service.ingest_episodes("u1", ["e1"], source_text="我们用 pytest")
        assert [item.status for item in result.records] == ["active", "pending_review"]
        assert len(await repo.list_outbox("u1")) == 1
    asyncio.run(scenario())


def test_pending_promotes_after_new_evidence(tmp_path):
    async def scenario():
        repo, service, ops = await semantic_stack(tmp_path)
        first = await service.ingest_episodes("u1", ["e1"], source_text="我用 pytest")
        pending = first.records[0]
        assert pending.status == "pending_review"
        ops.semantic_results = [semantic_doc("用户偏好 pytest", tier="EXPLICIT_STATEMENT")]
        second = await service.ingest_episodes("u1", ["e2"], source_text="我明确用 pytest")
        assert second.promoted_ids == [pending.id]
        assert (await repo.get_record("u1", pending.id)).status == "active"
    asyncio.run(scenario())


def test_pending_expires_and_conflict_holds(tmp_path):
    async def scenario():
        repo, service, _ops = await semantic_stack(tmp_path)
        # seed pending/active, then expire with now + 31 days
        assert await service.expire_pending(now=utc_now() + timedelta(days=31)) == 1
        assert (await repo.get_record("u1", pending.id)).status == "expired"
        # same-authority conflicting content writes an open conflict, current stays active
        conflict = await service.ingest_episodes("u1", ["e3"], source_text="不是 pytest")
        assert conflict.conflict_ids
        assert (await repo.get_record("u1", active.id)).status == "active"
    asyncio.run(scenario())
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_semantic.py -q`
Expected: FAIL，`canonical.semantic` 不存在。

- [ ] **Step 3: 实现 semantic 流程**

A 只消费 episode 摘要和 evidence refs；写入时把第一个 source episode 的 `task_id` 传给 `RecordDraft`。在装配 `existing` 时显式把 pending 加入候选，但绝不把它们传给检索/投影。resolver 的输入是 proposal + current，输出 ADD/UPDATE/DELETE/NOOP/HOLD；模型不能决定胜负。写入 active 才产生 outbox；pending/HOLD 不产生。晋升用 CAS 和 ledger 记录旧状态；过期用条件更新，已 active 的行不能被过期任务改回。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_semantic.py -q`
Expected: PASS（本 task 的测试全部通过）。

- [ ] **Step 5: 提交**

~~~bash
git add backend/packages/harness/agentkit/agents/memory/canonical/semantic.py \
  backend/tests/test_memory_semantic.py
git commit -m "feat(memory): stage and promote semantic facts"
~~~

## Task 9: Milvus 投影、Outbox 与 Reindex

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/projection.py`
- Test: `backend/tests/test_memory_projection.py`

**Interfaces:**
- Consumes: `MemoryRepository.claim_outbox/finish_outbox/retry_outbox`、`MemoryConfig.vector_store/embedding`、`OpenAICompatibleEmbedder`、pymilvus `MilvusClient`。
- Produces:
  - `MemoryProjectionRow(pk, user_id, memory_id, kind, status, topic_key, scope_condition, valid_from, valid_to, occurred_at, confidence, salience, last_active_at, explicit_remember, indexed_revision, embedding, embedding_model, embedding_dimensions, updated_at)`
  - `MemoryMilvusProjection(config, dimensions, client=None)`，方法 `ensure_collection/upsert/delete/search/list_pks/close`
  - `MemoryEmbedder(config, client=None)`，方法 `embed_documents/embed_query`
  - `MemoryProjector(repo, projection, embedder, config)`，方法 `run_once(limit=50)`、`reindex(user_id: str | None = None)`、`start()`、`stop()`
- collection 字段严格按 spec 5.3；`valid_to=0` 表示 NULL；filter 必须包含 `user_id/kind/status/valid_to`。
- `run_once` 领取 outbox 后必须重读 PG：revision 更旧、status 非 active、记录不存在、epoch 不同 → 丢弃事件；upsert 只写 active；delete 按 `pk` 删除。
- 失败重试 5 次后 dead-letter（`state="error"` 或 `dead`），记录 `last_error` 类型，不记录正文。
- `reindex` 先扫描 PG active，再逐条 upsert；不依赖旧 outbox；完成后更新 `memory_projection_state` 的 epoch/水位。
- 不把 profile、skill candidate、Skill 正文写入该 collection。

- [ ] **Step 1: 写失败测试**

~~~python
import asyncio


class FakeMilvus:
    def __init__(self):
        self.rows = {}
        self.deleted = []
        self.filters = []

    def has_collection(self, name):
        return True

    def upsert(self, *, collection_name, data):
        for row in data:
            self.rows[row["pk"]] = row

    def delete(self, *, collection_name, filter):
        self.filters.append(filter)
        for key in list(self.rows):
            if key in filter:
                self.rows.pop(key)

    def search(self, **kwargs):
        self.filters.append(kwargs["filter"])
        return []


def test_projection_filter_and_active_only(tmp_path):
    async def scenario():
        repo, projector, fake = await projection_stack(tmp_path)
        await repo.create_record(active_episode("m1"))
        await repo.create_record(pending_semantic("m2"))
        assert await projector.run_once() == 1
        assert [row["pk"] for row in fake.rows.values()] == ["u1:m1"]
        assert "status == \"active\"" in fake.filters[-1]
        assert "valid_to == 0" in fake.filters[-1]
    asyncio.run(scenario())


def test_stale_outbox_after_delete_is_dropped(tmp_path):
    async def scenario():
        repo, projector, fake = await projection_stack(tmp_path)
        record = await repo.create_record(active_episode("m1"))
        await repo.soft_delete(record.id, reason="user_forget")
        assert await projector.run_once() == 1
        assert fake.rows == {}
    asyncio.run(scenario())


def test_reindex_rebuilds_from_pg_active(tmp_path):
    async def scenario():
        repo, projector, fake = await projection_stack(tmp_path)
        await repo.create_record(active_episode("m1"))
        await repo.create_record(active_episode("m2"))
        await repo.create_record(pending_semantic("m3"))
        assert await projector.reindex("u1") >= 2
        assert set(fake.rows) == {"u1:m1", "u1:m2"}
    asyncio.run(scenario())
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_projection.py -q`
Expected: FAIL，`projection.py` 不存在。

- [ ] **Step 3: 实现 projection 与 worker**

`MemoryMilvusProjection` 懒 import `pymilvus`；建 collection 时固定 schema/索引，校验已有 collection 维度，不匹配时抛 `DimensionMismatch` 并提示新建 collection。`MemoryProjector` 复用 `skills/outbox.py` 的批量领取/重试/取消模式；embedding 在 PG 事务外执行；upsert 前再次读 PG revision。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_projection.py -q`
Expected: PASS（本 task 的测试全部通过）。

- [ ] **Step 5: 提交**

~~~bash
git add backend/packages/harness/agentkit/agents/memory/canonical/projection.py \
  backend/tests/test_memory_projection.py
git commit -m "feat(memory): project active memories into milvus"
~~~

## Task 10: 混合检索、PG 确认与配额

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/retrieval.py`
- Test: `backend/tests/test_memory_retrieval.py`

**Interfaces:**
- Consumes: `MemoryMilvusProjection.search`、`MemoryEmbedder.embed_query`、`MemoryRepository.get_record/list_records`、`MemoryConfig.retrieval`。
- Produces:
  - `HybridRetriever(repo, projection, embedder, config)`
  - `async search(user_id, *, text, kinds: Sequence[str], limit: int, cursor: str | None) -> MemorySearchPage`
  - `async memory_search_tool_result(...) -> list[MemoryHitView]`（tool 只读包装）
- Milvus dense 与 PG lexical 各取 `candidate_k=50`；RRF `k=60`；每 kind quota `semantic=6 / episodic=2`；最终 `limit` 截断。
- PG lexical：PostgreSQL 用 `to_tsvector('simple', content)` + `plainto_tsquery`；SQLite 用 token overlap；两者都只查 `status='active' AND valid_to IS NULL AND kind IN (... )`。
- 每个 Milvus 命中必须 `repo.get_record` 校验 user_id/status/valid_to/revision；不匹配直接丢弃，不降级为未确认命中。
- query embedding 失败/维度不符：跳过 Milvus，仍返回 PG 词法；两者都失败才抛 `MemoryUnavailableError`。
- 只返回真正进入结果的命中才更新 `hit_count/last_active_at` + outbox；召回但被 quota 截掉的候选不强化。
- 排序分数：`rrf * (0.5 + 0.5 * 0.5 ** (age_days / half_life))`，semantic half-life `90` 天，episodic `7` 天；`explicit_remember` 不绕过 scope/status 过滤。

- [ ] **Step 1: 写失败测试**

~~~python
import asyncio


def test_milvus_down_falls_back_to_pg_lexical(tmp_path):
    async def scenario():
        repo, retriever, _fake = await retrieval_stack(tmp_path)
        await repo.create_record(active_semantic("m1", "用户偏好 pytest"))
        retriever.embedder.fail = True
        page = await retriever.search("u1", text="pytest", kinds=["semantic"], limit=8)
        assert [hit.record.id for hit in page.items] == ["m1"]
    asyncio.run(scenario())


def test_pg_confirmation_drops_cross_user_and_deleted_hits(tmp_path):
    async def scenario():
        repo, retriever, fake = await retrieval_stack(tmp_path)
        alive = await repo.create_record(active_semantic("m1", "pytest"))
        deleted = await repo.create_record(active_semantic("m2", "pytest"))
        await repo.soft_delete(deleted.id, reason="user_forget")
        fake.hits = [
            {"pk": "u2:m9", "memory_id": "m9", "score": 0.99},
            {"pk": f"u1:{deleted.id}", "memory_id": deleted.id, "score": 0.98},
            {"pk": f"u1:{alive.id}", "memory_id": alive.id, "score": 0.90},
        ]
        page = await retriever.search("u1", text="pytest", kinds=["semantic"], limit=8)
        assert [hit.record.id for hit in page.items] == [alive.id]
    asyncio.run(scenario())


def test_search_excludes_pending_and_profile(tmp_path):
    async def scenario():
        repo, retriever, _fake = await retrieval_stack(tmp_path)
        await repo.create_record(pending_semantic("p1"))
        await repo.ensure_profile("u1")
        page = await retriever.search("u1", text="pytest", kinds=["semantic", "episodic"], limit=8)
        assert page.items == []
    asyncio.run(scenario())


def test_per_kind_quota_keeps_episodic_bounded(tmp_path):
    async def scenario():
        repo, retriever, _fake = await retrieval_stack(tmp_path)
        for index in range(10):
            await repo.create_record(active_semantic(f"s{index}", "pytest"))
            await repo.create_record(active_episode(f"e{index}"))
        page = await retriever.search("u1", text="pytest", kinds=["semantic", "episodic"], limit=20)
        assert len([hit for hit in page.items if hit.record.kind == "semantic"]) <= 6
        assert len([hit for hit in page.items if hit.record.kind == "episodic"]) <= 2
    asyncio.run(scenario())
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_retrieval.py -q`
Expected: FAIL，`retrieval.py` 不存在。

- [ ] **Step 3: 实现检索**

先编码 query；失败只记 metric 并走 lexical。Milvus 和 PG 各自返回 `Candidate(id, score, kind)`；按 RRF 合并；对每个候选 `get_record` 确认；按 kind 配额和衰减分排序；最后只对返回记录 `repo.touch_hits`（同一事务写 outbox）。`memory_search` 默认 kind 只含 `semantic`，显式请求 episodic 才返回。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_retrieval.py -q`
Expected: PASS（本 task 的测试全部通过）。

- [ ] **Step 5: 提交**

~~~bash
git add backend/packages/harness/agentkit/agents/memory/canonical/retrieval.py \
  backend/tests/test_memory_retrieval.py
git commit -m "feat(memory): add hybrid retrieval with pg confirmation"
~~~

## Task 11: 技能候选、宿主聚类与现有技能桥接

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/skill_candidates.py`
- Modify: `backend/packages/harness/agentkit/agents/memory/canonical/repository.py`（增加 candidate/conflict 方法）
- Modify: `backend/packages/harness/agentkit/skills/author.py`
- Modify: `backend/packages/harness/agentkit/skills/store/sql.py`
- Test: `backend/tests/test_memory_skill_candidates.py`

**Interfaces:**
- Consumes: `MemoryRepository`、`LangMemOperators.propose_skill`、`MemoryEmbedder`、现有 `SkillAuthorService.create_draft/put_file/save`、`build_package`、`StagedFile`。
- Produces:
  - `SuccessfulEpisode` 视图：`id, episode_key, thread_id, task_class, environment_fingerprint, trigger_text, key_steps, outcome, feedback_signal, blocker`
  - `EpisodeClusterer(config).cluster(episodes) -> list[EpisodeCluster]`
  - `SkillCandidateService(repo, operators, embedder, skill_author, config)`
  - `async generate(user_id, *, explicit_episode_ids: Sequence[str] | None = None, explicit_user_request: bool = False) -> list[SkillCandidateView]`
  - `async list(user_id, *, status: str | None = None) -> list[SkillCandidateView]`
  - `async confirm(user_id, candidate_id, *, expected_revision: int, package_sha256: str, idempotency_key: str) -> SkillConfirmResult`
  - `async reject(user_id, candidate_id, *, expected_revision: int, reason: str) -> SkillCandidateView`
  - `async expire(now) -> int`
  - repository 追加：`skill_version_metadata(version_id) -> dict`、`count_skill_versions(skill_id) -> int`、`find_version_by_origin_candidate(candidate_id) -> SkillVersionRef | None`。
- 成功 episode：`outcome == "SUCCESS"`，或 `outcome == "PARTIAL" and feedback_signal == "ACCEPTED"`；且 `blocker is False`；同一 episode 只计一次；`status="corrected"` 不算。
- 宿主候选条件：同 `task_class` + 同 `environment_fingerprint`；`trigger_similarity >= 0.82`；至少 `2` 个共同关键步骤（步骤签名归一化后）；`>=2` 个 distinct thread；`success_rate >= 0.80`；默认 `>=3` 条，显式 `1` 条。
- 灰区 `0.82–0.92`：除硬条件外还要求 D 返回 `same_sop=True`；`>=0.92` 也不能跳过硬条件；D 的 `same_sop/mergeable` 只是建议，宿主 criteria 不满足时不生成候选。
- D 输出转 `SkillDraft`：必须含 `SKILL.md`，文件路径通过 `validate_skill_path`，总大小/文件数用现有 `SkillCreationConfig` 限制，`build_package` 校验 frontmatter；不执行 scripts。
- candidate 初始 `proposed` → `validating` → `pending_confirmation`；D 无法生成可校验 package → `failed` 并记录 `validation_report`；不创建 Skill。
- `origin_idempotency_key = sha256(user_id + sorted(source_episode_ids) + base_skill_id)`；候选过期 `30` 天。
- 确认：CAS `pending_confirmation -> creating`；校验 owner/expected_revision/package_sha256/idempotency_key；创建私有 Skill（不提交公开审核）→ 写所有文件 → `save`；`extra_metadata={"origin_candidate_id": candidate.id}` 合并进 `skill_versions.metadata_json`。成功后 `saved`；失败 `failed`，可用同一 idempotency key 重试。
- 并发确认只能有一个请求进入 `creating`；若 Skill 已创建但回写失败，后续重试必须通过 `origin_candidate_id` 找到已有 Skill/version，不重复创建。

- [ ] **Step 1: 写失败测试**

~~~python
import asyncio


def test_three_successful_episodes_across_threads_create_candidate():
    async def scenario():
        repo, service, ops = await skill_stack()
        ops.skill_result = valid_skill_draft()
        episodes = [
            success_episode("e1", thread="t1", trigger="修复部署失败", steps=["exec", "read"]),
            success_episode("e2", thread="t2", trigger="修部署错误", steps=["exec", "read"]),
            success_episode("e3", thread="t2", trigger="修复部署问题", steps=["exec", "read"]),
        ]
        candidates = await service.generate("u1", explicit_episode_ids=[e.id for e in episodes])
        assert len(candidates) == 1
        assert candidates[0].status == "pending_confirmation"
        assert candidates[0].source_episode_ids == ["e1", "e2", "e3"]
        assert candidates[0].package_sha256 == valid_skill_draft().package_sha256
    asyncio.run(scenario())


def test_host_criteria_reject_even_when_d_says_same_sop():
    async def scenario():
        repo, service, ops = await skill_stack()
        ops.skill_result = valid_skill_draft(same_sop=True)
        episodes = [
            success_episode("e1", environment={"os": "linux"}, steps=["exec"]),
            success_episode("e2", environment={"os": "windows"}, steps=["exec"]),
            success_episode("e3", environment={"os": "linux"}, steps=["exec"]),
        ]
        assert await service.generate("u1", explicit_episode_ids=[e.id for e in episodes]) == []
    asyncio.run(scenario())


def test_explicit_user_request_allows_one_episode_and_never_submits():
    async def scenario():
        repo, service, ops = await skill_stack()
        ops.skill_result = valid_skill_draft()
        candidates = await service.generate(
            "u1", explicit_episode_ids=["e1"], explicit_user_request=True,
        )
        assert len(candidates) == 1
        assert ops.skill_author.submit_calls == 0
    asyncio.run(scenario())


def test_confirm_is_idempotent_and_records_origin():
    async def scenario():
        repo, service, ops = await skill_stack()
        candidate = await seed_candidate(repo, service)
        result = await service.confirm(
            "u1", candidate.id, expected_revision=candidate.revision,
            package_sha256=candidate.package_sha256, idempotency_key="idem-1",
        )
        assert result.skill_id
        assert result.version_id
        assert await repo.skill_version_metadata(result.version_id) == {
            "origin_candidate_id": candidate.id
        }
        again = await service.confirm(
            "u1", candidate.id, expected_revision=candidate.revision,
            package_sha256=candidate.package_sha256, idempotency_key="idem-1",
        )
        assert again.skill_id == result.skill_id
        assert await repo.count_skill_versions(result.skill_id) == 1
    asyncio.run(scenario())


def test_confirm_rejects_tampered_hash_and_is_owner_only():
    async def scenario():
        repo, service, _ops = await skill_stack()
        candidate = await seed_candidate(repo, service)
        with pytest.raises(Exception, match="hash"):
            await service.confirm(
                "u1", candidate.id, expected_revision=candidate.revision,
                package_sha256="0" * 64, idempotency_key="idem-2",
            )
        with pytest.raises(Exception, match="not found"):
            await service.confirm(
                "u2", candidate.id, expected_revision=candidate.revision,
                package_sha256=candidate.package_sha256, idempotency_key="idem-3",
            )
    asyncio.run(scenario())
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_skill_candidates.py -q`
Expected: FAIL，`skill_candidates.py` 不存在。

- [ ] **Step 3: 实现宿主聚类与确认桥接**

`EpisodeClusterer` 先按 `(task_class, environment_fingerprint)` 分组，再用 `trigger_similarity` 以簇心聚合；相似度由 `MemoryEmbedder` 计算，fake 返回固定向量。步骤归一化只保留工具签名/动作名，不保留参数。`SkillCandidateService.generate` 先做宿主 criteria，全部通过后才调 D；D 结果再过 package 校验。`confirm` 用 repository 的 CAS 把 candidate 置 `creating`，调用现有 author service；`SkillAuthorService.save`/`record_saved_version` 增加可选 `extra_metadata` 并合并，不覆盖 manifest 元数据。失败时写 `failed` + `validation_report.error_type`，不回滚已创建 Skill（幂等重试靠 metadata）。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_skill_candidates.py -q`
Expected: PASS（本 task 的测试全部通过）。

- [ ] **Step 5: 提交**

~~~bash
git add backend/packages/harness/agentkit/agents/memory/canonical/skill_candidates.py \
  backend/packages/harness/agentkit/agents/memory/canonical/repository.py \
  backend/packages/harness/agentkit/skills/author.py \
  backend/packages/harness/agentkit/skills/store/sql.py \
  backend/tests/test_memory_skill_candidates.py
git commit -m "feat(memory): propose skills behind host confirmation"
~~~

## Task 12: Memory HTTP API、权限与错误契约

**Files:**
- Modify: `backend/app/gateway/routers/memory.py`
- Modify: `backend/app/gateway/errors.py`
- Modify: `backend/app/gateway/deps.py`
- Test: `backend/tests/test_memory_api_v2.py`
- Modify: `backend/tests/test_memory_routes.py`（fixture 注入新 fake service）

**Interfaces:**
- Consumes: `get_memory_service(request)`（Task 14 实现）、`require_scope("memory:read"/"memory:write"/"skills:write"/"admin")`、`RunCaller.user_id`。
- Produces 路由：
  - `GET/PATCH /api/memory/profile`；PATCH body `{fields, expected_revision}`。
  - `DELETE /api/memory/profile/{field_path:path}`。
  - `GET /api/memory/search?q=&kinds=&limit=&cursor=`。
  - `GET /api/memory/pending?limit=&cursor=`；`GET /api/memory/pending/aggregate`（admin）。
  - `GET /api/memory/episodes?limit=&cursor=`；`POST /api/memory/episodes/{id}/correct`。
  - `GET /api/memory/skill-candidates?status=`；`POST /{id}/confirm`；`POST /{id}/reject`。
  - `GET /api/memory/conflicts`；`POST /{id}/resolve`。
  - `POST /api/memory/items/semantic/{id}/promote` body `{expected_revision}`。
  - `POST /api/memory/items/{kind}/{id}/rollback` body `{target_revision}`（admin；产生新 active revision）。
  - `DELETE /api/memory/items/{kind}/{id}`；`GET /api/memory/export`；`DELETE /api/memory`。
  - `POST /api/memory/reindex` / `POST /api/memory/maintenance/run`（admin）。
- 保留旧 `GET /api/memory`、`GET /api/memory/items`、`GET /api/memory/facts`、`POST /remember` 的 wire 兼容；新字段只做相加，不删除旧字段。
- 请求模型全部 `extra="forbid"`；禁止 `user_id`/`owner_user_id` 入参。
- 错误：409 `memory_conflict`；422 `invalid_memory`；404 `memory_not_found`；403/404 跨用户；501 `memory_kind_unsupported`；500 `memory_corrupted` 固定 detail。
- `GET /pending` owner-only；admin 直接请求只能走 `/pending/aggregate`，返回 `{user_count,item_count}`，不含正文。

- [ ] **Step 1: 写失败测试**

~~~python
def test_pending_is_owner_only_and_admin_only_aggregates(client):
    owner, admin = client
    pending_id = seed_pending(owner, "u1")
    assert owner.get("/api/memory/pending").status_code == 200
    assert owner.get("/api/memory/pending").json()["items"][0]["id"] == pending_id
    assert owner.get("/api/memory/pending/aggregate").status_code == 403
    body = admin.get("/api/memory/pending/aggregate")
    assert body.status_code == 200
    assert set(body.json()) == {"user_count", "item_count"}
    assert "content" not in body.text


def test_promote_requires_owner_and_expected_revision(client):
    owner, _admin = client
    pending_id = seed_pending(owner, "u1")
    stale = owner.post(
        f"/api/memory/items/semantic/{pending_id}/promote",
        json={"expected_revision": 0},
    )
    assert stale.status_code == 409
    ok = owner.post(
        f"/api/memory/items/semantic/{pending_id}/promote",
        json={"expected_revision": 1},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "active"


def test_cross_user_id_is_rejected_by_schema(client):
    owner, _admin = client
    response = owner.get("/api/memory/search", params={"q": "x", "user_id": "u2"})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_memory"


def test_confirm_candidate_requires_hash_and_idempotency(client):
    owner, _admin = client
    candidate = seed_candidate_for(owner)
    assert owner.post(
        f"/api/memory/skill-candidates/{candidate.id}/confirm",
        json={"expected_revision": 1, "package_sha256": "0" * 64,
              "idempotency_key": "idem"},
    ).status_code == 409
    assert owner.post(
        f"/api/memory/skill-candidates/{candidate.id}/confirm",
        json={"expected_revision": 1, "package_sha256": candidate.package_sha256,
              "idempotency_key": "idem"},
    ).status_code == 200


def test_profile_has_no_pin_and_errors_are_coded(client):
    owner, _admin = client
    body = owner.patch(
        "/api/memory/profile",
        json={"fields": {"identity.preferred_name": "小王"}, "expected_revision": 0},
    ).json()
    assert "pinned" not in body["fields"]["identity.preferred_name"]
    bad = owner.patch(
        "/api/memory/profile",
        json={"fields": {"notes": "free"}, "expected_revision": 1},
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "invalid_memory"
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_api_v2.py -q`
Expected: FAIL，路由/错误 code 不存在。

- [ ] **Step 3: 实现路由与错误映射**

把 `_manager_and_scope` 换成 `service = get_memory_service(request)` + `user_id = caller.user_id`；所有调用 await，不再 `asyncio.to_thread`。新增 `InvalidMemory`（422/`invalid_memory`）并让旧 `InvalidMemoryProfile` 仅保留兼容。为 pending/aggregate 增加 admin 依赖；为 confirm/reject 使用 `skills:write`。旧响应模型保留，新响应模型 additive。`GET /memory/search` 对 `profile` 仍返回 422；`kinds` 只接受 `semantic/episodic`。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_api_v2.py backend/tests/test_memory_routes.py -q`
Expected: 通过；旧路由测试 fixture 已改为 fake service。

- [ ] **Step 5: 提交**

~~~bash
git add backend/app/gateway/routers/memory.py backend/app/gateway/errors.py \
  backend/app/gateway/deps.py backend/tests/test_memory_api_v2.py \
  backend/tests/test_memory_routes.py
git commit -m "feat(memory): expose canonical memory api"
~~~

## Task 13: 前端画像页、待确认页与技能建议

**Files:**
- Create: `frontend/src/core/api/memory.ts`
- Create: `frontend/src/core/api/memory.test.ts`
- Create: `frontend/src/app/workspace/profile/page.tsx`
- Create: `frontend/src/components/profile/my-profile-page.tsx`
- Create: `frontend/src/components/profile/my-profile-page.dom.test.tsx`
- Create: `frontend/src/app/workspace/memory/pending/page.tsx`
- Create: `frontend/src/components/memory/pending-memory-page.tsx`
- Create: `frontend/src/components/memory/pending-memory-page.dom.test.tsx`
- Create: `frontend/src/components/memory/skill-suggestions.tsx`
- Create: `frontend/src/components/memory/skill-suggestions.dom.test.tsx`
- Modify: `frontend/src/components/workspace/sidebar.tsx`
- Modify: `frontend/src/app/workspace/sidebar-container.tsx`
- Modify: `frontend/src/components/skills/my-skills-page.tsx`
- Modify: `frontend/src/components/workspace/sidebar.dom.test.tsx`

**Interfaces:**
- `memory.ts` 导出：`MemoryApiError`、`getProfile`、`patchProfile`、`clearProfileField`、`listPending`、`promoteSemantic`、`deleteMemoryItem`、`listSkillCandidates`、`confirmSkillCandidate`、`rejectSkillCandidate`、`listEpisodes`、`correctEpisode`。
- 类型：`ProfileFieldView {value, source, evidence_tier, confidence, observed_at}`、`ProfileView {revision, fields}`、`PendingItem`、`SkillCandidate`；confirm 参数必须含 `expected_revision/package_sha256/idempotency_key`。
- `MyProfilePage`：按 `identity/communication/work/accessibility/boundaries` 分组；显示 evidence tier/来源/时间；编辑与删除；409 时提示重读并刷新；无 pin 控件。
- `PendingMemoryPage`：只显示 semantic pending；显示 confidence、evidence quote、source、created_at；promote 需要当前 revision；delete 支持；空态/错误态；不显示 transcript。
- `SkillSuggestions`：显示候选名/描述/触发条件/source episode 数/package hash 短值；确认后调用 confirm 并 `router.push("/workspace/skills/{skill_id}")`；拒绝后刷新；不展示 package 内容。
- Sidebar 增加 `profileActive`、`pendingActive`；链接 `/workspace/profile`、`/workspace/memory/pending`；高亮按前缀。
- 所有请求使用现有 `apiFetch`/`apiUrl`，带 CSRF；错误按 `code` 展示，不拼接原始 detail 路径。

- [ ] **Step 1: 写失败测试**

~~~tsx
// memory.test.ts
it("sends expected_revision and parses coded conflict", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(json({ revision: 2, fields: {} }))
    .mockResolvedValueOnce(json({ code: "memory_conflict", detail: "stale" }, 409));
  vi.stubGlobal("fetch", fetchMock);
  await patchProfile({ "communication.language": "en-US" }, 1);
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
    fields: { "communication.language": "en-US" }, expected_revision: 1,
  });
  await expect(patchProfile({ "communication.language": "zh-CN" }, 1))
    .rejects.toMatchObject({ code: "memory_conflict", status: 409 });
});

// my-profile-page.dom.test.tsx
it("renders evidence and asks to reload on 409", async () => {
  mockGetProfile({ revision: 1, fields: {
    "communication.language": { value: "zh-CN", source: "user_manual",
      evidence_tier: "EXPLICIT_MANDATE", confidence: 0.95,
      observed_at: "2026-10-26T00:00:00Z" },
  }});
  mockPatchProfileReject({ status: 409, code: "memory_conflict" });
  render(<MyProfilePage />);
  expect(await screen.findByText("zh-CN")).toBeTruthy();
  expect(screen.getByText("EXPLICIT_MANDATE")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByLabelText("communication.language"), { target: { value: "en-US" } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("已被其他操作更新");
});

// pending-memory-page.dom.test.tsx
it("promotes with the visible revision and deletes", async () => {
  mockPending([{ id: "p1", content: "用户可能喜欢 unittest", confidence: 0.7,
    evidence: "我们有时用 unittest", created_at: "2026-10-26T00:00:00Z", revision: 3 }]);
  render(<PendingMemoryPage />);
  fireEvent.click(await screen.findByRole("button", { name: "确认保留" }));
  expect(mockPromote).toHaveBeenCalledWith("p1", 3);
});

// skill-suggestions.dom.test.tsx
it("confirms with package hash and opens the editor", async () => {
  mockCandidates([{ id: "c1", private_name: "deploy-fix", description: "修复部署",
    trigger_text: "部署失败", source_episode_ids: ["e1","e2","e3"],
    package_sha256: "a".repeat(64), revision: 2 }]);
  render(<SkillSuggestions />);
  fireEvent.click(await screen.findByRole("button", { name: "创建私有技能" }));
  expect(mockConfirm).toHaveBeenCalledWith("c1", {
    expected_revision: 2, package_sha256: "a".repeat(64), idempotency_key: expect.any(String),
  });
  expect(mockPush).toHaveBeenCalledWith("/workspace/skills/skill-1");
});
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && pnpm test:node src/core/api/memory.test.ts`
Expected: FAIL，`memory.ts` 不存在。
Run: `cd frontend && pnpm test:dom src/components/profile/my-profile-page.dom.test.tsx src/components/memory/pending-memory-page.dom.test.tsx src/components/memory/skill-suggestions.dom.test.tsx`
Expected: FAIL，组件不存在。

- [ ] **Step 3: 实现客户端、页面和导航**

复用 `skills.ts` 的请求/错误模式；`MemoryApiError` 保留 `status/code/detail/traceId`。画像页用 `useQuery(["memory","profile"])`、`useMutation`；成功后 `setQueryData` 或 invalidate。pending 页只请求 `/memory/pending`；没有 admin 入口。技能建议放在现有 `MySkillsPage` 的私有草稿上方或下方，不改变现有技能编辑流程。Sidebar 新增 `navItemClass` 项并传入 active props。

- [ ] **Step 4: 跑测试、类型检查与 lint**

Run: `cd frontend && pnpm test:node src/core/api/memory.test.ts`
Expected: PASS。
Run: `cd frontend && pnpm test:dom src/components/profile/my-profile-page.dom.test.tsx src/components/memory/pending-memory-page.dom.test.tsx src/components/memory/skill-suggestions.dom.test.tsx src/components/workspace/sidebar.dom.test.tsx`
Expected: PASS。
Run: `cd frontend && pnpm typecheck && pnpm lint`
Expected: exit 0。

- [ ] **Step 5: 提交**

~~~bash
git add frontend/src/core/api/memory.ts frontend/src/core/api/memory.test.ts \
  frontend/src/app/workspace/profile frontend/src/components/profile \
  frontend/src/app/workspace/memory frontend/src/components/memory \
  frontend/src/components/workspace/sidebar.tsx \
  frontend/src/app/workspace/sidebar-container.tsx \
  frontend/src/components/skills/my-skills-page.tsx \
  frontend/src/components/workspace/sidebar.dom.test.tsx
git commit -m "feat(memory): add profile and pending memory pages"
~~~

## Task 14: 运行时组合、中间件切换与后台 worker

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/service.py`
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/maintenance.py`
- Modify: `backend/packages/harness/agentkit/agents/middlewares/memory_middleware.py`
- Modify: `backend/packages/harness/agentkit/agents/memory/summarization_hook.py`
- Modify: `backend/packages/harness/agentkit/agents/memory/tools.py`
- Modify: `backend/packages/harness/agentkit/agents/leader_agent/agent.py`
- Modify: `backend/app/gateway/app.py`
- Modify: `backend/app/gateway/deps.py`
- Test: `backend/tests/test_memory_runtime_v2.py`

**Interfaces:**
- Consumes: 前面所有 canonical 组件、`get_session_factory()`、`AppConfig`、现有 checkpointer/run services、`SkillAuthorService`。
- Produces:
  - `MemoryService(repo, sessions, profile, semantic, episodic, skills, retriever, projector, metrics)`
  - `async get_profile/inject_profile/capture_turn/close_session/search/list_pending/promote/get_episodes/correct/list_candidates/confirm_candidate/reject_candidate/list_conflicts/resolve_conflict/export/clear/reindex`
  - `@dataclass MemoryRuntime(service: MemoryService | None, projector: MemoryProjector | None, maintenance: MemoryMaintenanceWorker | None)`
  - `build_memory_runtime(config, session_factory, *, skill_author=None, metrics=None) -> MemoryRuntime`
  - `MemoryMaintenanceWorker(service, config)`：`start()`、`stop()`、`run_once(now)`；负责 idle session、pending 过期、候选过期、投影对账（L2/L3 在 Task 15 补齐）。
  - `get_memory_service()` / `configure_memory_service(service)` / `reset_memory_service()`
  - `MemoryMiddleware` 只实现 `before_agent`（画像注入）和 `after_agent`（捕获/关闭）；删除 `after_model` 捕获。`TaskClassifierMiddleware` 在它之前挂载，只写 `task_context`，不持有 Repository。
  - `MemorySummarizationHook.__call__` 先只提交未见过的 `message_id` 到 capture，再调用 `close_session(reason="compression")`。
  - `MemoryService.inject_profile` 复用 `ProfileContextBuilder(max_tokens=config.profile.max_tokens)`、固定 `MEMORY_PROFILE_MESSAGE_ID`、隐藏消息和 5 秒超时；读失败返回空画像，不阻塞对话。
- `MemoryService.capture_turn` 必须在 `after_agent` 调用一次；子代理、`skip_memory_flush`、隐藏 profile 消息、非真实用户消息不捕获。
- 显式“记住这次”由规则检测，调用 `close_session(reason="explicit_remember")`，不等待 debounce。
- 后台 worker：`MemoryProjector.start()`、`MemoryMaintenanceWorker`（idle session、pending 过期、候选过期、L2 水位）。`memory.enabled=false` 时不建 service、不启动 worker。
- lifespan 关闭顺序：先停 middleware 新捕获 → flush/stop projector → stop maintenance → close service/embedder/projection；异常不得阻塞 checkpointer 关闭。

- [ ] **Step 1: 写失败测试**

~~~python
def test_task_classifier_is_before_memory_and_lead_only():
    chain = build_runtime_middlewares(SimpleNamespace(memory=MemoryConfig(enabled=True)))
    names = [type(item).__name__ for item in chain]
    assert names.index("TaskClassifierMiddleware") < names.index("MemoryMiddleware")


def test_after_model_does_not_enqueue_and_after_agent_captures_once(monkeypatch):
    calls = []

    async def fake_capture(*args, **kwargs):
        calls.append(kwargs)

    service = SimpleNamespace(capture_turn=fake_capture, config=MemoryConfig(enabled=True))
    middleware = MemoryMiddleware(service=service, user_id="u1")
    middleware.after_model({"messages": [HumanMessage(id="u1", content="hi")]}, runtime())
    assert calls == []
    asyncio.run(middleware.aafter_agent({"messages": [HumanMessage(id="u1", content="hi")]}, runtime()))
    assert len(calls) == 1


def test_memory_search_tool_excludes_pending(monkeypatch):
    service = FakeMemoryService(pending=["p1"], active=["a1"])
    tool = make_memory_search_tool(service)
    result = asyncio.run(tool.ainvoke({"query": "pytest"}))
    assert "p1" not in result
    assert "a1" in result


def test_lifespan_does_not_start_workers_when_disabled():
    config = MemoryConfig(enabled=False)
    assert build_memory_runtime(config, session_factory=None).service is None
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_runtime_v2.py -q`
Expected: FAIL，service/新 middleware 构造不存在。

- [ ] **Step 3: 实现 service、中间件与 lifespan**

`MemoryService` 只做编排，不直接写 SQL/Milvus。`MemoryMiddleware` 保留现有 profile 注入消息格式和 `MEMORY_PROFILE_MESSAGE_ID`；把 `_enqueue_turn` 改为 `await service.capture_turn`，删除 `after_model` 路径。`summarization_hook` 用 `asyncio.get_running_loop().create_task`（无 loop 时 `asyncio.run`）调度，并用 `message_id` 去重。`tools.py` 直接 await `service.search`；`leader_agent` 绑定 service 而不是旧 manager。`app.py` 用 `get_session_factory()` 构造 repository/embedder/projection；memory disabled 时 `app.state.memory_service=None`。

- [ ] **Step 4: 跑测试与回归**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_runtime_v2.py backend/tests/test_memory_routes.py backend/tests/test_memory_core.py backend/tests/test_task_classifier.py -q`
Expected: 通过。
Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_agent_factory.py backend/tests/test_summarization.py -q`
Expected: 通过。

- [ ] **Step 5: 提交**

~~~bash
git add backend/packages/harness/agentkit/agents/memory/canonical/service.py \
  backend/packages/harness/agentkit/agents/memory/canonical/maintenance.py \
  backend/packages/harness/agentkit/agents/middlewares/memory_middleware.py \
  backend/packages/harness/agentkit/agents/memory/summarization_hook.py \
  backend/packages/harness/agentkit/agents/memory/tools.py \
  backend/packages/harness/agentkit/agents/leader_agent/agent.py \
  backend/app/gateway/app.py backend/app/gateway/deps.py \
  backend/tests/test_memory_runtime_v2.py
git commit -m "feat(memory): run canonical memory service"
~~~

## Task 15: BaseStore 迁移、观测、验收与真 Milvus 冒烟

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/legacy.py`
- Create: `backend/packages/harness/agentkit/agents/memory/canonical/metrics.py`
- Modify: `backend/packages/harness/agentkit/agents/memory/canonical/maintenance.py`（补齐 L2/L3）
- Modify: `backend/app/gateway/app.py`（启动 pruning worker）
- Modify: `config.example.yaml`
- Test: `backend/tests/test_memory_legacy_migration.py`
- Test: `backend/tests/test_memory_acceptance.py`
- Test: `backend/tests/integration/test_memory_milvus_smoke.py`

**Interfaces:**
- Consumes: 旧 `LangMemBackend/BaseStore`（只读）、`MemoryRepository`、`MemoryMilvusProjection`、`MemoryMetrics`。
- Produces:
  - `LegacyMigrator(legacy_store, repo).migrate(*, user_id: str | None = None, dry_run: bool = True) -> MigrationReport`
  - `MigrationReport(scanned, migrated, skipped, errors, dry_run)`
  - `MemoryMetrics`：`session_opened/closed/swept`、`job_depth/dead_letter`、`operator_calls/failures/timeouts/schema_rejections`、`profile_direct/low_evidence/overrode_manual`、`semantic_active/pending/promoted/expired/conflict`、`milvus_p95_ms/lexical_fallback/confirm_dropped`、`outbox_lag_ms/reindexed/orphans`、`skill_candidate_pending/confirmed/rejected/failed/auto_created`；`inc(name, n=1)`、`observe(name, value)`、`snapshot() -> dict[str,int|float]`。
  - `MemoryMaintenanceWorker` 的 L2 水位、L3 折叠/对账扩展（基础 worker 已在 Task 14 创建）。
- 迁移只读旧 BaseStore 的 profile/record/tombstone；保留 `memory_id`；写 `memory_records/memory_record_versions/memory_mutation_ledger/memory_index_outbox`；同一 `memory_id` 重复迁移跳过；不复制原始 transcript/工具输出；`dry_run=True` 只报告。
- 旧 BaseStore 写路径在迁移完成后不再被新运行时调用；保留只读回滚窗口，不删除旧表。
- 观测日志/trace 只记录 `kind/status/count/latency/error_type`，绝不记录 `content/evidence_quote/Skill 正文/PII`。
- 真 Milvus smoke 需要 `MILVUS_URI`；未设置时 `pytest.mark.skipif`；覆盖建 collection、upsert、filter、delete、reindex、维度不匹配。
- 冷热/衰减：`retention = 0.5 ** (age_days / half_life)`，semantic S0=90 天、episodic S0=7 天；`retention >= 0.25` hot、`0.05–0.25` dormant、`<0.05` archived；`explicit_remember` 豁免自动淘汰；L3 默认 shadow，配置关闭 shadow 才执行归档。

- [ ] **Step 1: 写失败测试**

~~~python
import asyncio


def test_legacy_migration_preserves_ids_and_is_idempotent(tmp_path):
    async def scenario():
        repo, legacy = await legacy_stack(tmp_path)
        legacy.seed(profile={"identity": {"preferred_name": "小王"}}, records=[
            legacy_record(id="m1", kind="semantic", content="用户偏好 pytest",
                          status="active", confidence=0.85),
        ])
        migrator = LegacyMigrator(legacy, repo)
        first = await migrator.migrate(user_id="u1", dry_run=False)
        second = await migrator.migrate(user_id="u1", dry_run=False)
        assert first.migrated == 2
        assert second.migrated == 0
        assert await repo.get_record("u1", "m1") is not None
        assert "transcript" not in str(await repo.get_record("u1", "m1"))
    asyncio.run(scenario())


def test_metrics_do_not_contain_content(caplog):
    metrics = MemoryMetrics()
    metrics.inc("semantic_active", 2)
    metrics.observe("milvus_p95_ms", 12)
    assert metrics.snapshot()["semantic_active"] == 2
    assert "pytest" not in caplog.text


def test_acceptance_static_contracts():
    root = Path("backend/packages/harness/agentkit/agents/memory/canonical")
    assert "create_memory_store_manager" not in "".join(
        path.read_text(encoding="utf-8") for path in root.glob("*.py")
    )
    assert "pinned" not in (root / "profile.py").read_text(encoding="utf-8")
    assert "pinned" not in (root / "domain.py").read_text(encoding="utf-8")


def test_milvus_smoke_requires_explicit_uri():
    assert os.environ.get("MILVUS_URI") is None or os.environ["MILVUS_URI"]
~~~

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_legacy_migration.py backend/tests/test_memory_acceptance.py -q`
Expected: FAIL，`legacy.py/metrics.py` 不存在。

- [ ] **Step 3: 实现迁移、指标与维护 worker**

`legacy.py` 适配旧 `MemoryBackend.get_record/search_records`：旧 profile 映射到 `memory_profiles`，旧 semantic/episodic 映射到 `memory_records`；旧状态 `active/dormant/archived/deleted` 原样保留；旧 `pinned` 一律忽略（不写 pin 字段）。每行迁移后用 `memory_id` 查询，已存在则 skip。`metrics.py` 用线程安全/async-safe 计数器（`threading.Lock` 即可），暴露 `snapshot`；不引入新指标库。`maintenance.py` 复用 `L1/L2` 和 repository 的条件更新；默认 shadow，只有 `config.consolidation.shadow=false` 才执行归档/折叠。

`config.example.yaml` 追加与 spec 11 完全一致的 `memory` 树，`enabled: false`、`uri/token/base_url/api_key` 为空字符串或占位环境变量；不要把真实 secret 写进示例。

- [ ] **Step 4: 跑全量后端、前端和真实 Milvus smoke**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests -q`
Expected: 全部通过；Milvus smoke 在未设置 `MILVUS_URI` 时 skip。
Run: `MILVUS_URI=http://127.0.0.1:19530 PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/integration/test_memory_milvus_smoke.py -q`
Expected: 建表/upsert/filter/delete/reindex 全部通过（本机无 Milvus 时记录为未执行，不得声称通过）。
Run: `cd frontend && pnpm test:node && pnpm test:dom && pnpm typecheck && pnpm build`
Expected: exit 0。

- [ ] **Step 5: 提交**

~~~bash
git add backend/packages/harness/agentkit/agents/memory/canonical/legacy.py \
  backend/packages/harness/agentkit/agents/memory/canonical/metrics.py \
  backend/packages/harness/agentkit/agents/memory/canonical/maintenance.py \
  backend/app/gateway/app.py config.example.yaml \
  backend/tests/test_memory_legacy_migration.py \
  backend/tests/test_memory_acceptance.py \
  backend/tests/integration/test_memory_milvus_smoke.py
git commit -m "feat(memory): migrate legacy stores and verify acceptance"
~~~

## Task 16: 删除旧运行路径并完成最终审查

**Files:**
- Modify: `backend/packages/harness/agentkit/agents/memory/manager.py`（删除 ingest/backend 写路径；只保留 `legacy.py` 需要的只读适配）
- Modify: `backend/packages/harness/agentkit/agents/memory/ingestors/*`（删除或标记 deprecated；新运行路径不得 import）
- Modify: `backend/tests/test_memory_core.py`、`test_memory_backend_contract.py`、`test_memory_tombstone.py`（如果仍测旧路径则改为 legacy/新语义）
- Modify: `backend/packages/harness/agentkit/agents/memory/types.py`（从 canonical 导出新类型；旧 `pinned` 字段只允许在 legacy 迁移模型中出现）
- Test: `backend/tests/test_memory_acceptance.py`（新增 `test_no_legacy_runtime_import`）

**Interfaces:**
- Consumes: Task 14 的 `MemoryService`、Task 15 的迁移报告。
- Produces: 运行时只有一个 memory 写入口；旧模块不再被 `app.py`、middleware、tool、leader_agent import；`pinned` 不再出现在任何 API/页面/新 canonical 类型中。

- [ ] **Step 1: 写失败测试**

~~~python
def test_runtime_does_not_import_legacy_ingestors():
    import backend.app.gateway.app as app_module
    source = Path(app_module.__file__).read_text(encoding="utf-8")
    assert "LangMemSemanticIngestor" not in source
    assert "get_memory_manager" not in source
    assert "create_memory_store_manager" not in source


def test_new_types_have_no_pin():
    from agentkit.agents.memory.canonical.domain import ProfileFieldView
    assert "pinned" not in ProfileFieldView.model_fields
~~~

- [ ] **Step 2: 跑测试确认失败（旧 import 仍在）**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests/test_memory_acceptance.py -q`
Expected: FAIL，`app.py` 仍 import `get_memory_manager`。

- [ ] **Step 3: 删除旧运行路径**

从 `app.py` 移除 `make_memory_store/configure_memory_store/get_memory_manager/MemoryMaintenanceService`；从 middleware/tool/leader_agent 移除旧 manager import。保留 `backends/langmem/store.py` 仅供 `legacy.py` 迁移测试；删除 `ingestors/langmem_semantic.py` 或改为 deprecated 且无 import。把旧测试迁移到 canonical fake service；不得为了保留旧测试而恢复 pin 语义。

- [ ] **Step 4: 跑最终验收**

Run: `PYTHONPATH=. uv run --project backend --group dev --extra memory pytest backend/tests -q`
Expected: 全部通过。
Run: `cd frontend && pnpm test:node && pnpm test:dom && pnpm typecheck && pnpm build`
Expected: exit 0。
Run: `git diff --check`
Expected: 无空白错误。

- [ ] **Step 5: 提交**

~~~bash
git add backend/packages/harness/agentkit/agents/memory \
  backend/app/gateway/app.py backend/tests
git commit -m "refactor(memory): remove legacy runtime write path"
~~~

## 验收对照（执行时逐项勾选）

- [ ] T1–T10：任务分类器空 goal、CONTINUE、NEW_TASK、UNCERTAIN、超时、fallback、goal 漂移、task_id 注入、长期记忆关闭、run pair 输入通过（Task 5A–5C、Task 14）。
- [ ] S1–S4：session 捕获/关闭/空闲/40 轮/压缩去重通过（Task 5、Task 5C、Task 14）。
- [ ] P1–P4：画像只取当前消息、高证据覆盖、无 pin、revision 409（Task 7、Task 12、Task 13）。
- [ ] E1–E3：情景脱水、幂等、纠正（Task 6）。
- [ ] M1–M5：active/pending/晋升/过期/冲突 HOLD（Task 8）。
- [ ] K0–K6：技能候选硬条件、D 仅建议、显式 1 条、hash/owner/并发/公开边界（Task 11）。
- [ ] V1–V3：Milvus 降级、旧 outbox fencing、reindex（Task 9、Task 10、Task 15）。
- [ ] D1–D2：clear/forget 不复活、rollback 产生新 active revision（Task 3、Task 12、Task 15）。
- [ ] A1–A2：跨用户 403/404、admin 仅聚合（Task 12）。
- [ ] L1–L2：只有 A/B/C/D；LangMem 不可用时旧记忆可读（Task 4、Task 14、Task 16）。
- [ ] 真 Milvus smoke 已实际运行，或明确记录未运行原因（Task 15）。

## 执行建议

任务 1–4 建立接口与事务边界，5–5C 建立 session/task 边界，6–11 是四条记忆链路，12–13 是用户可见面，14–16 是切换/迁移/清理。推荐 **Subagent-driven**：Task 3、Task 7、Task 8、Task 11 的接口和测试最容易被写偏，需要每个 task 后由 fresh reviewer 检查；同时 Task 5–11 可以按依赖顺序并行准备 fake 测试，但不要并行改同一 canonical 文件。



