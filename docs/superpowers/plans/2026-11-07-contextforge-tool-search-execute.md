# ContextForge Tool Search / Execute Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 把 ContextForge 工具目录同步到 MyFlow 数据库和 Milvus，用两个内置工具 tool_search / tool_execute 完成检索、schema 注入和 MCP 执行。

**Architecture:** ContextForge 继续负责工具管理和 MCP 执行；Gateway 通过 MCP tools/list + Admin REST /tools 同步到 contextforge_tools，再写 Milvus。Agent 不再绑定每个 ContextForge 工具，只拿 tool_search / tool_execute；tool_search 返回 schema，tool_execute 校验后调用 ContextForge MCP。

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Alembic, Pydantic v2, mcp ClientSession, Milvus/pymilvus, jsonschema, LangChain 1.x, Next.js/React Query。

**Spec:** docs/superpowers/specs/2026-11-07-contextforge-tool-search-execute-design.md

## Global Constraints

- 固定 GATEWAY_WORKERS=1；contextforge.enabled=true 且 workers>1 时启动失败。
- /admin/events 是唯一实时外部更新信号；不使用 MCP tools/list_changed。
- 保留 /api/tools 管理代理和 /workspace/tools 页面。
- contextforge_tools 是运行期唯一可信源；Milvus 是派生索引。
- 工具名：模型/策略用 cf__<name>；ContextForge MCP 用原始 name。
- Milvus collection 固定为 myflow_contextforge_tools。
- 完整 input_schema 只在数据库；Milvus 只存检索字段。
- MCP tools/list 必须用 raw mcp ClientSession，不能经 langchain-mcp-adapters 的 Pydantic round-trip。
- owner filter 必须同时下推到 dense 和 sparse 两个 AnnSearchRequest。
- tool_search 最多返回 5 个工具；max_schema_chars=3000；max_result_chars=16000。
- tool_execute 失败刷新只对本地参数错误、not found、invalid params/schema mismatch；timeout/5xx/结果不确定不重试。
- 技能策略在 SkillToolPolicyMiddleware 检查内层 cf__ 名称，工具函数不读 secret context。
- 子代理 v1 不拿 tool_search / tool_execute。
- 没有历史数据；不写回填，不兼容 rev.6/7/8 表。
- Backend test invocation: from repo root use uv run python -m pytest backend/tests/...; from backend use PYTHONPATH=.. uv run pytest tests/...; plain cd backend && uv run pytest fails to import the backend package.

## Review Focus

- /admin/events 登 401/403/404 或断线时，外部变更必须降级到午夜/失败刷新，不能让管理页假装实时。
- owner filter 只加在 dense 请求、漏掉 sparse 请求时，RRF 会混入别人的工具；必须两边都过滤。
- tools/list 的 inputSchema 经过 adapter/Pydantic 后 $ref/anyOf/default 被改写，会和 ContextForge 实际执行不一致；必须保存 verbatim。
- 本地 jsonschema 校验失败若进入文本分类器，会触发无谓的 ContextForge 全域刷新和重试；必须用 origin=local_validation 短路。
- 单 worker 约束必须启动即拒绝 workers>1，否则两个进程会同时写 DB/Milvus 和订阅事件。

## Task 1: ContextForge tool table and store

**Files:**
- Create: backend/packages/harness/agentkit/contextforge/store/__init__.py
- Create: backend/packages/harness/agentkit/contextforge/store/model.py
- Create: backend/packages/harness/agentkit/contextforge/store/repository.py
- Modify: backend/packages/harness/agentkit/persistence/schema.py
- Create: backend/packages/harness/agentkit/persistence/migrations/versions/0016_contextforge_tools.py
- Test: backend/tests/test_contextforge_tool_table.py

**Interfaces:**
- Produces: ContextForgeToolRecord with fields id, name, call_name, display_name, description, input_schema, annotations, tags, parameters, owner_user_id, enabled, reachable, callable, content, content_hash, schema_hash, last_seen_at.
- Produces: ContextForgeToolStore(session_factory) with upsert_snapshot(records, seen_names), get_by_ids(ids, owner_user_id), get_by_call_name(call_name, owner_user_id), list_callable(owner_user_id), list_all(owner_user_id).
- Consumes: SQLAlchemy async_sessionmaker, existing Base.

- [ ] **Step 1: Write failing ORM/store tests**

Test that contextforge_tools has the exact columns above; UNIQUE(name), UNIQUE(call_name), owner/callable index; upsert_snapshot replaces changed fields and marks missing names callable=false.

- [ ] **Step 2: Run the failing test**

Run: cd backend && uv run pytest tests/test_contextforge_tool_table.py -q
Expected: FAIL because table/store do not exist.

- [ ] **Step 3: Implement the ORM model, migration, and store**

Use SQLAlchemy JSON for input_schema/annotations/tags/parameters; String(64) id/call_name; Text content; DateTime(timezone=True). In this isolated worktree the current Alembic head is 0015_skill_index_repair; create 0016_contextforge_tools.py with down_revision=0015_skill_index_repair. When merging onto a main branch that already has the unrelated 0016_drop_profile_suggestions, renumber/re-parent this migration. Register the model in persistence/schema.py.

- [ ] **Step 4: Run tests**

Run: cd backend && uv run pytest tests/test_contextforge_tool_table.py -q
Expected: PASS.

- [ ] **Step 5: Commit**

git add backend/packages/harness/agentkit/contextforge/store backend/packages/harness/agentkit/persistence backend/tests/test_contextforge_tool_table.py
git commit -m "feat: add contextforge tool store"

## Task 2: Raw ContextForge MCP client

**Files:**
- Create: backend/packages/harness/agentkit/contextforge/mcp_client.py
- Test: backend/tests/test_contextforge_mcp_client.py
- Modify: backend/packages/harness/pyproject.toml

**Interfaces:**
- Produces: ContextForgeMcpClient(config).list_tools() -> list[mcp.types.Tool]
- Produces: ContextForgeMcpClient.call_tool(name: str, arguments: dict, timeout: float | None = None) -> mcp.types.CallToolResult
- Consumes: ContextForgeConfig.resolved_mcp_url(), resolved_agent_token(), request_timeout_seconds.

- [ ] **Step 1: Write failing client tests**

Fake an mcp ClientSession/list_tools result with two pages; assert cursor pagination and verbatim inputSchema. Assert call_tool passes the original name and arguments, and Authorization header comes from agent_token.

- [ ] **Step 2: Run the failing test**

Run: cd backend && uv run pytest tests/test_contextforge_mcp_client.py -q
Expected: FAIL because mcp_client does not exist.

- [ ] **Step 3: Implement the raw MCP client**

Use mcp.client.streamable_http.streamablehttp_client, ClientSession, initialize, list_tools(cursor=...), and call_tool. Do not use langchain_mcp_adapters. Add mcp as an explicit dependency; add jsonschema and cryptography while editing the extra.

- [ ] **Step 4: Run tests**

Run: cd backend && uv run pytest tests/test_contextforge_mcp_client.py -q
Expected: PASS.

- [ ] **Step 5: Commit**

git add backend/packages/harness/agentkit/contextforge/mcp_client.py backend/packages/harness/pyproject.toml backend/tests/test_contextforge_mcp_client.py
git commit -m "feat: add raw ContextForge MCP client"

## Task 3: Milvus index with full retrieval fields and owner filter

**Files:**
- Modify: backend/packages/harness/agentkit/contextforge/index.py
- Modify: backend/tests/test_tool_vector_index.py

**Interfaces:**
- Produces: ToolVectorIndex.hybrid_search(vector, query, owner_user_id, limit) -> list[ToolVectorHit]
- Produces: ToolVectorIndex.sync(records, embedder, full=False) -> ToolIndexSyncStats
- Produces: ToolVectorIndex.delete_by_ids(ids) -> int
- Consumes: ContextForgeToolRecord.content/content_hash/owner_user_id/call_name.

- [ ] **Step 1: Write failing index tests**

Assert collection name myflow_contextforge_tools; fields tool_id/owner_user_id/tool_name/call_name/description/tags/parameters/content/content_hash/embedding/sparse; both AnnSearchRequests carry owner_user_id filter; delete_by_ids removes rows; content_hash changing owner/call_name triggers upsert.

- [ ] **Step 2: Run the failing test**

Run: cd backend && uv run pytest tests/test_tool_vector_index.py -q
Expected: FAIL on old schema/collection names.

- [ ] **Step 3: Implement index changes**

Rename collection constant; add fields; build sync from records; use content_hash including content, owner, call_name; apply filter on both requests; delete stale by tool_id when full or missing.

- [ ] **Step 4: Run tests**

Run: cd backend && uv run pytest tests/test_tool_vector_index.py -q
Expected: PASS.

- [ ] **Step 5: Commit**

git add backend/packages/harness/agentkit/contextforge/index.py backend/tests/test_tool_vector_index.py
git commit -m "feat: index ContextForge tools with owner filter"

## Task 4: ContextForge sync service

**Files:**
- Create: backend/packages/harness/agentkit/contextforge/sync.py
- Test: backend/tests/test_contextforge_sync.py
- Modify: backend/packages/harness/agentkit/contextforge/catalog.py

**Interfaces:**
- Produces: ContextForgeSyncStats(scanned, changed, upserted, deleted, error)
- Produces: ContextForgeSyncService(config, admin, mcp, store, index, embedder).sync(reason, full=False) -> ContextForgeSyncStats
- Consumes: ContextForgeAdminClient.list_tools, ContextForgeMcpClient.list_tools, ContextForgeToolStore, ToolVectorIndex.

- [ ] **Step 1: Write failing sync tests**

Fake MCP tools and Admin rows; assert merge by name, MCP inputSchema wins, owner from annotations, missing MCP tool callable=false, Admin-only tool callable=false; assert DB write before Milvus; assert MCP/Admin failure leaves last good; assert full deletes stale.

- [ ] **Step 2: Run the failing test**

Run: cd backend && uv run pytest tests/test_contextforge_sync.py -q
Expected: FAIL because sync service does not exist.

- [ ] **Step 3: Implement sync service**

Build ContextForgeToolRecord from merge; call_name=cf__+name; content=name+display+description+tags+parameter names; content_hash includes content/owner/call_name; schema_hash from canonical input_schema. Acquire process asyncio.Lock; DB transaction first; Milvus after commit.

- [ ] **Step 4: Run tests**

Run: cd backend && uv run pytest tests/test_contextforge_sync.py -q
Expected: PASS.

- [ ] **Step 5: Commit**

git add backend/packages/harness/agentkit/contextforge/sync.py backend/packages/harness/agentkit/contextforge/catalog.py backend/tests/test_contextforge_sync.py
git commit -m "feat: sync ContextForge tools to DB and Milvus"

## Task 5: Runtime, /admin/events, midnight, single-worker guard

**Files:**
- Modify: backend/packages/harness/agentkit/contextforge/runtime.py
- Create: backend/packages/harness/agentkit/contextforge/daily.py
- Modify: backend/app/gateway/app.py
- Modify: backend/tests/test_contextforge_runtime.py
- Modify: backend/tests/test_contextforge_admin_events.py
- Test: backend/tests/test_contextforge_worker_guard.py

**Interfaces:**
- Produces: ContextForgeRuntime.start/stop/request_refresh/request_refresh_async/reindex/status
- Produces: next_local_midnight(now, timezone) -> datetime
- Consumes: ContextForgeSyncService, AdminEventSubscriber, config.sync.daily_at/timezone.

- [ ] **Step 1: Write failing runtime tests**

Assert startup sync; /admin/events tool_* triggers sync; debounce + dirty single-flight; midnight task calls sync; stop cancels task and subscriber; contextforge.enabled + workers>1 fails startup.

- [ ] **Step 2: Run failing tests**

Run: cd backend && uv run pytest tests/test_contextforge_runtime.py tests/test_contextforge_admin_events.py tests/test_contextforge_worker_guard.py -q
Expected: FAIL on old runtime and missing guard.

- [ ] **Step 3: Implement runtime and daily task**

Remove in-memory snapshot/get_entries; runtime owns sync service, subscriber, daily task, asyncio.Lock/dirty. Subscribe to /admin/events only. Add local-time midnight loop with jitter. Add startup guard for workers>1.

- [ ] **Step 4: Run tests**

Run: cd backend && uv run pytest tests/test_contextforge_runtime.py tests/test_contextforge_admin_events.py tests/test_contextforge_worker_guard.py -q
Expected: PASS.

- [ ] **Step 5: Commit**

git add backend/packages/harness/agentkit/contextforge/runtime.py backend/packages/harness/agentkit/contextforge/daily.py backend/app/gateway/app.py backend/tests/test_contextforge_runtime.py backend/tests/test_contextforge_admin_events.py backend/tests/test_contextforge_worker_guard.py
git commit -m "feat: wire ContextForge runtime and admin events"

## Task 6: tool_search built-in

**Files:**
- Create: backend/packages/harness/agentkit/tools/builtin/tool_search.py
- Modify: backend/packages/harness/agentkit/tools/search/service.py
- Modify: backend/packages/harness/agentkit/tools/builtin/__init__.py
- Modify: backend/packages/harness/agentkit/agents/thread_state.py
- Modify: backend/tests/test_tool_search_router.py
- Test: backend/tests/test_tool_search_tool.py
- Test: backend/tests/test_thread_state.py

**Interfaces:**
- Produces: ToolSearchInput(task_query: str, domain_keywords: list[str])
- Produces: tool_search.coroutine -> Command with messages + retrieved_tool_names
- Produces: ContextForgeToolSearchService.search(task_query, domain_keywords, user_id) -> ToolSearchOutcome
- Consumes: ToolVectorIndex, ContextForgeToolStore, embedder, reranker.

- [ ] **Step 1: Write failing tests**

Assert Milvus candidate IDs joined to DB; owner filtered; result includes tool_id, tool_name, call_name, input_schema; output budget max 5; empty result clears retrieved_tool_names; schema truncation keeps valid JSON; no full-scan fallback.

- [ ] **Step 2: Run failing tests**

Run: cd backend && uv run pytest tests/test_tool_search_tool.py -q
Expected: FAIL because tool_search does not exist.

- [ ] **Step 3: Implement search service, state field, and tool_search**

Add retrieved_tool_names to ThreadState with a replace reducer and max 5. Add global set_tool_search_runtime/get_tool_search_runtime; tool_search uses ToolRuntime user_id, calls service, returns Command(update={"messages":[...], "retrieved_tool_names":[...]}). Do not call ContextForge MCP.

- [ ] **Step 4: Run tests**

Run: cd backend && uv run pytest tests/test_tool_search_tool.py tests/test_thread_state.py tests/test_tool_search_router.py -q
Expected: PASS.

- [ ] **Step 5: Commit**

git add backend/packages/harness/agentkit/tools/builtin/tool_search.py backend/packages/harness/agentkit/tools/search/service.py backend/packages/harness/agentkit/tools/builtin/__init__.py backend/packages/harness/agentkit/agents/thread_state.py backend/tests/test_tool_search_tool.py backend/tests/test_thread_state.py backend/tests/test_tool_search_router.py
git commit -m "feat: add tool_search built-in"

## Task 7: tool_execute built-in and failure fallback

**Files:**
- Create: backend/packages/harness/agentkit/tools/builtin/tool_execute.py
- Test: backend/tests/test_tool_execute_tool.py
- Modify: backend/packages/harness/agentkit/contextforge/retry.py

**Interfaces:**
- Produces: ToolExecuteInput(tool_name: str, arguments: dict[str, Any])
- Produces: tool_execute.coroutine -> ToolMessage/content blocks
- Consumes: ContextForgeToolStore.get_by_call_name, ContextForgeMcpClient.call_tool, ContextForgeRuntime.request_refresh, jsonschema.

- [ ] **Step 1: Write failing executor tests**

Assert missing retrieved name rejected; owner/callable DB check; local schema failure refreshes ContextForge and returns new schema without auto retry; not found refreshes and retries once; upstream invalid params/schema mismatch refreshes and returns new schema; timeout/5xx no retry; result is stamped with inner name/tool_id/call_name.

- [ ] **Step 2: Run failing tests**

Run: cd backend && uv run pytest tests/test_tool_execute_tool.py -q
Expected: FAIL because tool_execute does not exist.

- [ ] **Step 3: Implement executor and error classification**

Use Draft202012Validator with remote refs disabled. Add local_validation origin marker and make classify_tool_error honor it. Call ContextForgeMcpClient.call_tool with original name. Convert text/image/resource blocks; isError -> error ToolMessage.

- [ ] **Step 4: Run tests**

Run: cd backend && uv run pytest tests/test_tool_execute_tool.py tests/test_contextforge_retry.py -q
Expected: PASS.

- [ ] **Step 5: Commit**

git add backend/packages/harness/agentkit/tools/builtin/tool_execute.py backend/packages/harness/agentkit/contextforge/retry.py backend/tests/test_tool_execute_tool.py
git commit -m "feat: add tool_execute built-in"

## Task 8: Agent registry, prompt, assembly

**Files:**
- Modify: backend/packages/harness/agentkit/tools/registry.py
- Modify: backend/packages/harness/agentkit/agents/leader_agent/agent.py
- Modify: backend/packages/harness/agentkit/agents/leader_agent/prompt.py
- Modify: backend/packages/harness/agentkit/agents/middlewares/assembly.py
- Delete: backend/packages/harness/agentkit/agents/middlewares/tool_search_middleware.py
- Delete: backend/packages/harness/agentkit/agents/middlewares/contextforge_ownership_middleware.py
- Delete: backend/packages/harness/agentkit/agents/middlewares/tool_refresh_retry_middleware.py
- Create: backend/packages/harness/agentkit/agents/middlewares/retrieval_scope_middleware.py
- Test: backend/tests/test_agent_factory.py

**Interfaces:**
- Produces: RetrievalScopeMiddleware.before_agent reset for retrieved_tool_names.
- Consumes: tool_search / tool_execute builtins.

- [ ] **Step 1: Write failing agent tests**

Assert get_available_tools has builtins + tool_search + tool_execute and no ContextForge tool objects; prompt has External tools; subagents remove both tools; old middlewares not registered; RetrievalScopeMiddleware resets retrieved_tool_names.

- [ ] **Step 2: Run failing tests**

Run: cd backend && uv run pytest tests/test_agent_factory.py tests/test_thread_state.py -q
Expected: FAIL on old registry/state.

- [ ] **Step 3: Implement registry/prompt/assembly**

Add builtins; remove should_defer_contextforge, defer flags, ToolSearchMiddleware, ContextForgeOwnershipMiddleware, ToolRefreshRetryMiddleware; add RetrievalScopeMiddleware and prompt section; strip tools in subagent assembly.

- [ ] **Step 4: Run tests**

Run: cd backend && uv run pytest tests/test_agent_factory.py tests/test_thread_state.py -q
Expected: PASS.

- [ ] **Step 5: Commit**

git add backend/packages/harness/agentkit/agents backend/packages/harness/agentkit/tools/registry.py backend/tests/test_agent_factory.py
git commit -m "feat: expose tool_search and tool_execute to agents"

## Task 9: Skill policy for cf__ names

**Files:**
- Modify: backend/packages/harness/agentkit/skills/tool_policy.py
- Modify: backend/packages/harness/agentkit/agents/middlewares/skill_tool_policy_middleware.py
- Modify: backend/tests/test_skill_policy.py
- Test: backend/tests/test_contextforge_policy.py

**Interfaces:**
- Produces: ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES contains tool_search and tool_execute.
- Produces: SkillToolPolicyMiddleware inner check on tool_execute.args.call_name and filtering of tool_search Command summary.
- Consumes: ToolPolicyDecision; cf__ namespace.

- [ ] **Step 1: Write failing policy tests**

Assert restricted skill cannot execute cf__read_file via tool_execute; tool_search summary filters disallowed cf__ names; missing stored decision fails closed; ordinary tools unchanged.

- [ ] **Step 2: Run failing tests**

Run: cd backend && uv run pytest tests/test_contextforge_policy.py tests/test_skill_policy.py -q
Expected: FAIL on old always-available set/policy.

- [ ] **Step 3: Implement policy enforcement**

Add builtins; read request.tool_call.args.call_name for tool_execute; filter Command/ToolMessage tool summaries for tool_search; do not read secret context inside tool functions.

- [ ] **Step 4: Run tests**

Run: cd backend && uv run pytest tests/test_contextforge_policy.py tests/test_skill_policy.py -q
Expected: PASS.

- [ ] **Step 5: Commit**

git add backend/packages/harness/agentkit/skills/tool_policy.py backend/packages/harness/agentkit/agents/middlewares/skill_tool_policy_middleware.py backend/tests/test_contextforge_policy.py backend/tests/test_skill_policy.py
git commit -m "feat: enforce skill policy for ContextForge tools"

## Task 10: Management API, status, and frontend

**Files:**
- Modify: backend/app/gateway/routers/tools.py
- Modify: backend/app/gateway/models/tools.py
- Modify: frontend/src/core/api/tools.ts
- Modify: frontend/src/components/tools/tool-management-page.tsx
- Test: backend/tests/test_tools_router.py
- Test: frontend/src/core/api/tools.test.ts
- Test: frontend/src/components/tools/tool-management-page.dom.test.tsx

**Interfaces:**
- Produces: /api/tools CRUD proxy unchanged; /api/tools/status includes sync/index/events; /api/tools/search uses ContextForgeToolSearchService; /api/tools/reindex uses runtime.reindex.
- Consumes: ContextForgeRuntime, admin client.

- [ ] **Step 1: Write failing API/UI tests**

Assert management routes still proxy ContextForge; status fields; reindex full; search preview uses DB+Milvus and returns tool_name/parameters; page renders event/sync/index status; no raw token/headers.

- [ ] **Step 2: Run failing tests**

Run: cd backend && uv run pytest tests/test_tools_router.py -q
Run: cd frontend && pnpm test -- tools
Expected: FAIL on stale status/search behavior.

- [ ] **Step 3: Implement API/status/frontend adjustments**

Keep management proxy; route status to runtime.status(); reindex full; search preview uses new service. Update TypeScript status types and labels; do not add new management surface.

- [ ] **Step 4: Run tests**

Run: cd backend && uv run pytest tests/test_tools_router.py -q
Run: cd frontend && pnpm test -- tools
Expected: PASS.

- [ ] **Step 5: Commit**

git add backend/app/gateway/routers/tools.py backend/app/gateway/models/tools.py frontend/src/core/api/tools.ts frontend/src/components/tools/tool-management-page.tsx backend/tests/test_tools_router.py frontend/src/core/api/tools.test.ts frontend/src/components/tools/tool-management-page.dom.test.tsx
git commit -m "feat: keep ContextForge management surface in sync"

## Task 11: Config, dependency cleanup, and integration

**Files:**
- Modify: backend/packages/harness/agentkit/config/contextforge_config.py
- Modify: config.example.yaml
- Modify: backend/README.md
- Modify: backend/packages/harness/pyproject.toml
- Delete: backend/packages/harness/agentkit/contextforge/loader.py
- Delete: backend/packages/harness/agentkit/contextforge/cache.py
- Delete: backend/tests/test_contextforge_loader.py
- Delete: backend/tests/test_tool_search_middleware.py
- Delete: backend/tests/test_contextforge_ownership_middleware.py
- Delete: backend/tests/test_search_tools_tool.py
- Modify: backend/tests/test_contextforge_integration.py
- Test: backend/tests/test_contextforge_config.py

**Interfaces:**
- Consumes: all prior tasks.
- Produces: final config shape with sync/admin_events/search, single-worker guard, no tool_loading/deferred_threshold.

- [ ] **Step 1: Write failing config/cleanup tests**

Assert new config fields parse; removed fields fail; search requires embedding/vector_store; daily_at/timezone; collection fixed; workers>1 guard; old loader/cache imports no longer used.

- [ ] **Step 2: Run failing tests**

Run: cd backend && uv run pytest tests/test_contextforge_config.py tests/test_contextforge_integration.py -q
Expected: FAIL on old config/imports.

- [ ] **Step 3: Implement config/dependency/docs cleanup**

Remove langchain-mcp-adapters; add mcp/jsonschema/cryptography; update config.example.yaml and README; delete obsolete loaders/caches/tests; update integration test to register ContextForge tools and exercise tool_search/tool_execute.

- [ ] **Step 4: Run full backend test suite**

Run: cd backend && uv run pytest
Expected: PASS (live integration skips without env vars).

- [ ] **Step 5: Commit**

git add backend config.example.yaml backend/README.md
git commit -m "chore: finalize ContextForge tool search and execute"

## Execution Handoff

After all tasks: run backend full suite, frontend typecheck/test/build, and the opt-in ContextForge integration test. Then review the whole branch before merge.
