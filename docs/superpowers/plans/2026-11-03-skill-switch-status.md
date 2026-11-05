# 当前状态：技能启用开关、工具拆分与使用次数

> 日期：2026-11-03（2026-11-03 追加修订）
> 状态：功能主体已实现，**后端与前端完整测试通过**。
> 等待数据库迁移与远端网关部署。

## 本次目标

1. 已生效技能增加启用开关，创建时默认启用。
2. 关闭的技能不参与技能检索（PG 确认、词法召回、Milvus 过滤）。
3. 删除私有技能个人开关对 `skills_prefs.json` 的依赖，改存数据库。
4. `search_and_load_skills` 拆成两个工具：
   - `search_skills`：`skill-finder` 专属，只回 `skill_id / name / description`；
   - `load_skills`：系统内置工具，按 `skill_id` 回正文。
5. `load_skills` 累加使用次数：Redis ZSET 排名 + PostgreSQL `skills.load_count` 持久化。
6. 私有技能超过 `skills.usage.prompt_top_k`（默认 10）时，system prompt 只列 top-K。

## 已实现

### 数据库
- `skills.enabled` 新增，默认 `true`；迁移 `0013_skill_enabled`（revises `0012_skill_source`）。
- `skills.load_count` 新增，默认 0；迁移 `0014_skill_load_count`（head）。
- 已有技能迁移后默认启用、计数 0；软删除行不计入播种。

### 工具拆分
- 删除 `search_and_load_skills` 及其测试文件，新增
  `tools/builtin/search_skills.py` 与 `tools/builtin/load_skills.py`。
- `search_skills` 不读正文、不计数；`skills.search.enabled=false` 时整个工具不注册。
- `load_skills` 校验 owner + active + enabled，读 commit cache 的正文（剥离 frontmatter），
  成功后才计数；`skills.search.enabled=false` 时仍然可用。
- `load_skills` 进 `ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES` 与 durable-context 的
  loader 工具集合（默认 `{"load_skills"}`，兼容旧 `{"skills": [...]}` 载荷）。
- lead 装配：`search_skills` 一律从 lead 工具表移除，只经 `task.delegated_tools` 给
  `skill-finder`；委派关闭或白名单没有 skill-finder 时 lead 也没有它；
  `load_skills` 不参与认领/下沉，两种模式都在 lead 上。

### 使用次数
- `agentkit/skills/usage.py`：`SkillUsageService` 负责 per-user ZSET、播种、top-K 缓存。
- `load_skills` 成功 → `ZINCRBY`（Redis）与 `skills.load_count += 1`（PG），互不阻塞；
  任一失败只记日志，正文照样返回。
- 启动：`SkillRuntime.start()` 调度异步播种，`ZADD ... GT` 写入，避免把线上更新的计数降回去，
  启动流程不等待它。
- 刷新：每 `refresh_seconds` 从 Redis 重读已预热用户的 top-K，多 worker 收敛。
- Redis 不可用：warning + DB `load_count` 兜底，不隐藏技能。

### Prompt top-K
- 私有 active + enabled 技能 ≤ `prompt_top_k`：全列，不读 Redis。
- 超过：先按 Redis top-K skill_id 选，缺口用 DB `load_count` 再按名字稳定排序补齐。
- 内置技能永远在索引里，不占配额、不计数；私有行带 `id=<skill_id>` 与 `path=<container_file>`。
- prompt builder 是同步路径：读 `SkillUsageService` 的进程内 top-K 缓存；
  LRU key 含选中技能身份（含 `load_count`），不含原始计数文本。

### 向量投影
- `ProjectionRow.enabled` 从 `skills.enabled` 读取，Milvus filter 增加 `enabled == true`，
  新 collection 标量索引包含 `enabled`。
- 开关事件写 outbox 重新 upsert；切换时版本 `index_status` 置 `pending`。

### 前端
- “我的技能”中只有 active 技能显示开关（`role="switch"`）。
- 关闭时显示“已生效 · 已停用，不参与技能检索”。
- 调用 `setSkillPreference` 后使 mine/catalog 查询失效。
- 加载次数本轮没有暴露到 UI。

## 已知限制

- 旧 `source=filesystem` 调试路径仍保留 `skills_prefs.json` 兼容代码；生产 Git 私有技能已不再使用该文件。
- filesystem 技能的 `skill_id` 用技能名充当，不写数据库、不进 Redis 计数。
- 使用次数不是强一致：Redis 排名、PG 持久化与 prompt 缓存之间的短暂偏差是设计允许的。
- 远端网关仍是旧代码，远端数据库尚未执行 0013/0014。

## 验证状态（本地）

```text
uv run python -m pytest backend/tests -q
-> 2218 passed, 6 skipped（2224 collected）

cd frontend
pnpm typecheck  -> passed
pnpm lint       -> passed
pnpm test       -> 35 files / 317 tests passed
```

覆盖要点：

- `test_skill_tools.py`：`search_skills` 只回元数据、不调 `load_skill`；
  `load_skills` 回正文、按 id 查、查不到/关闭时返回空；检索关闭时 `load_skills` 仍在。
- `test_skill_usage.py`：DB 播种（`ZADD GT` 不降值）、Redis+PG 双写、单边故障互不影响、
  缓存只留 top-K、配置默认值与 `config.example.yaml`。
- `test_skill_prompt_topk.py`：未超限不读 Redis、超限按 top-K、冷缓存用 `load_count` 兜底、
  同分按名字、内置不占配额。
- `test_skill_search_service.py`：`load_skill` 只在正文读到后计数、search 从不计数、
  owner/启用/正文缺失都返回 `None`。
- `test_skill_store.py`：`increment_skill_load_count` 自增、`list_skill_load_counts` 只列 >0、
  未知/已删除技能不计数。
- `test_skill_migration_0011.py`：迁移到 head `0014_skill_load_count`，`load_count` 列存在。
- `test_retrieval_subagents.py`：`search_skills` 只给 skill-finder、任何情况下都不留给 lead；
  `load_skills` 委派与直连都在 lead。

## 部署注意

- 当前前端代理到 `192.168.154.141:8001` 的远端网关；该进程仍是旧代码，未加载本次改动。
- 远端数据库尚未执行 0013/0014；迁移前部署新代码会因为缺少 `skills.enabled` / `skills.load_count` 失败。
- `skills.usage.redis_url` 留空时没有 ZSET 排名，prompt 走 DB `load_count` 兜底（功能可用、排序退化）。
- 当前配置 `skills.search.embedding.enabled=false`，所以没有 Milvus 投影/outbox worker。
- 部署顺序建议：备份数据库 -> `alembic upgrade head` -> 部署后端 -> 重启网关 -> 前端刷新。

## 关键文件

- `backend/packages/harness/agentkit/tools/builtin/search_skills.py`
- `backend/packages/harness/agentkit/tools/builtin/load_skills.py`
- `backend/packages/harness/agentkit/skills/usage.py`
- `backend/packages/harness/agentkit/skills/search.py`
- `backend/packages/harness/agentkit/skills/prompt.py`
- `backend/packages/harness/agentkit/skills/store/sql.py`
- `backend/packages/harness/agentkit/skills/tool_policy.py`
- `backend/packages/harness/agentkit/agents/leader_agent/agent.py`
- `backend/packages/harness/agentkit/agents/middlewares/durable_context_middleware.py`
- `backend/packages/harness/agentkit/subagents/builtins/skill_finder.py`
- `backend/packages/harness/agentkit/config/skills_config.py`
- `backend/packages/harness/agentkit/persistence/migrations/versions/0013_skill_enabled.py`
- `backend/packages/harness/agentkit/persistence/migrations/versions/0014_skill_load_count.py`
- `backend/app/gateway/skills_catalog.py`
- `backend/app/gateway/routers/skills.py`
- `frontend/src/components/skills/my-skills-page.tsx`
- `frontend/src/core/api/skills.ts`

## 2026-11-04 补丁：投影 collection 自愈

**事故**：`myflow_skills_active` 存在但没有向量索引 → `load_collection` 报 700
`index not found`；旧实现把"集合存在"当"集合可用"，于是 outbox 事件每次重试都走
"已存在 → 只 load"，同一事件必然失败到重试耗尽，版本停在 `failed`；删集合也没用，
下一次重试会再建出一个没有索引的集合。

**修复**（`agentkit/skills/projection.py`）：

- `_ensure_collection_sync` 的不变式改成"存在 + 维度匹配 + embedding 索引存在 + 已 load"；
  已存在的集合先用 `list_indexes` / `describe_index` 自检，缺索引就补建再 load。
- `_load` 遇到 700 先补建索引再重试一次（覆盖索引刚建好不可见、索引被手工删掉）。
- 新建不再把 `index_params` 塞进 `create_collection`：显式 `create_collection` →
  `create_index` → `load`；任何一步失败都 `drop_collection` 丢弃半成品。
- 补索引前先 `release_collection`；并发 worker 撞到 "index already exist" 视为成功。
- `_index_maintenance_loop` 首轮延迟改成 `min(interval, 30s)`，部署重启后约 30 秒
  就会把 `index_status != ready` 的版本修回来。

**已经卡住的版本**：索引补齐后，下一轮 `reconcile()` 会因为 `index_status != ready`
重新 embedding + upsert，把版本带回 `ready`；耗尽重试的 outbox 事件不需要复活。
集合本身不需要手工删除。

**验证**：`test_skill_projection.py` 新增 5 条用例（已存在无索引 → 补建后 load 成功、
load 报 700 → 补索引并只重试一次、健康集合不重建索引、建索引失败丢弃半成品、
索引永远建不出来时干净失败），`test_skill_runtime_wiring.py` 新增首轮对账延迟用例。

```text
uv run python -m pytest backend/tests -q
-> 2204 passed, 6 skipped
```


## 2026-11-04 补丁：对账规模化

**背景**：原来的每轮对账做三件全量的事（全表版本 + 一次 `limit=16384` 的 Milvus 主键
query + 提交缓存目录 walk），技能/版本/worker 一多就会空转；主键 query 超过 16 384
行会静默截断，窗口外的行每轮都被判成缺失、持续重新 embedding。

**改成两条路径 + 事件驱动清理**（设计 §14）：

- **快路径**（`skills.index.reconciliation_interval_minutes`，默认 10 分钟）：只查
  `index_status IN (pending, failed, unindexed)` 且退避到期的 active 版本，逐行
  embedding + upsert，成功后 `ready` 并清零退避；失败写 `index_attempts /
  index_last_error / index_next_attempt_at`（指数退避，`repair_max_attempts` 后停），
  每轮受 `repair_budget_rows`（500）与 `repair_budget_seconds`（180s）约束。
- **完整性巡查**（`verify_interval_minutes`，默认 60 分钟，首轮 5 分钟内）：
  `query_iterator` 流式读主键（不再有 16 384 截断），把"active 有、Milvus 没有"的版本
  标回 `pending` 交给快路径，删除孤儿行；扫描被预算截断时**不**判定缺失。
- **分布式锁**：两条路径各自 `pg_try_advisory_lock`（专用连接 + finally 解锁 +
  SQLite 放行），抢不到就跳过本轮并记 `skipped_due_to_lock`。
- **提交缓存改事件驱动**：删除 `Worker.prune()`；保存新版本、删除技能时，只对该技能
  计算保留集（active、pending outbox、最近 `keep_versions_per_skill` 版）并删掉超出
  `cache_ttl_days` 的提交目录。缓存缺失由 `SkillContentStore.ensure()` 从 Git 重新
  物化，所以清理不影响正确性。
- 迁移 `0015_skill_index_repair`：`skill_versions` 增加 `index_attempts`、
  `index_last_error`、`index_next_attempt_at`，并为 `index_status` 建索引。

**验收**：`test_skill_outbox.py`（修复/巡查/预算/退避/上限/事件驱动清理）、
`test_skill_runtime_wiring.py`（预算透传、锁抢不到跳过、锁持有者解锁）、
`test_skill_projection.py`（流式扫描、截断标记）、`test_skill_content_store.py`
（`forget_commits` 只删点名缓存、TTL 安全期、删后可从 Git 重新物化）、
`test_skill_author_routes.py`（保存/删除触发清理）、`test_skill_migration_0011.py`
（head `0015_skill_index_repair`）。

## 下一步

1. 应用 `0013` / `0014` / `0015` 三个迁移并部署/重启远端网关。
2. 真实环境验证：开关 -> 检索不可见；`load_skills` -> `skills.load_count` 增长 ->
   Redis ZSET 排名变化 -> prompt 只列 top-K。
3. 视需要把 `load_count` 暴露到技能页（当前只有启用开关）。
4. 决定是否彻底删除 legacy filesystem 的 JSON 开关路径。
