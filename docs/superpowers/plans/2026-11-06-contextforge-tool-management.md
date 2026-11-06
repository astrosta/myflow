# ContextForge Tool Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add ContextForge-backed tool CRUD in the MyFlow admin UI, load callable tools through ContextForge MCP, index them in the existing Milvus instance, and expose a hybrid BM25/vector/rerank search_tools built-in tool.

**Architecture:** The MyFlow gateway proxies ContextForge Admin REST for tool CRUD and subscribes to GET /admin/events for change notifications. A process-level ContextForgeRuntime owns the MCP tool snapshot, refreshes on frontend writes / admin events / retryable tool errors, and synchronizes tool documents into the Milvus collection myflow_tools_catalog. search_tools retrieves from Milvus, falls back to local BM25, and promotes hits through ThreadState.promoted plus ToolSearchMiddleware.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, httpx, langchain 1.x, langchain-mcp-adapters, pymilvus 2.5+, Milvus (existing instance), Next.js App Router, React Query, Tailwind.

**Spec:** docs/superpowers/specs/2026-11-06-contextforge-tool-management-design.md

## Global Constraints

- ContextForge target is 1.1.0. MCPGATEWAY_ADMIN_API_ENABLED=true and the admin token have admin.events (or *). The ContextForge Admin UI is not used.
- Reuse the existing Milvus instance. The only new collection is myflow_tools_catalog. Never write to myflow_knowledge or myflow_skills_active.
- MyFlow must not create a writable local tool catalog table. ContextForge is the management source of truth; Milvus is a derived retrieval index.
- All /api/tools routes require require_scope("admin"). Browser callers never receive ContextForge tokens or tool header values.
- Tool names must match ^[A-Za-z0-9_-]{1,64}$.
- Error retry is allowed only for not-found or invalid-argument tool errors, exactly once, with the same arguments. Never retry auth, timeout, 429, 5xx, network, or execution errors.
- Frontend write operations trigger asynchronous refresh by default. The response may return sync_status=queued and the UI polls /api/tools/status.
- tool_loading defaults to auto with deferred_threshold 24. search_tools is in ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES.
- sync.cache_ttl_seconds defaults to 60. It is an on-demand cache expiry, not a background poll.
- No new database tables or Alembic migrations.
- Tests must not require live ContextForge or Milvus; use httpx.MockTransport and fake vector-store clients. Live integration tests are opt-in through environment variables.

## Review Focus

- ContextForge response shape drift: bare list vs {tools: [...]}, snake_case vs camelCase, input_schema object vs JSON string, headers object vs list. Tested in Task 3.
- /admin/events SSE framing: multi-line data, : keepalive, blank-line event boundaries, reconnect, 404/401/403. Tested in Task 7.
- Credential handling: masked header values must round-trip and must never appear in errors, logs, or responses. Tested in Task 4.
- Milvus schema drift: missing BM25 sparse field, dimension mismatch, hybrid search confirmation, and graceful local-BM25 fallback. Tested in Task 5 and Task 9.
- Retry safety: only the two allowed error classes, one retry, no retry on auth/timeout/5xx, and no claim that an unbound tool can be retried. Tested in Task 12.

## Commands

~~~bash
cd backend && uv run pytest
cd backend && uv run pytest tests/test_contextforge_admin.py -q
cd frontend && pnpm test
cd frontend && pnpm lint
cd frontend && pnpm build
~~~

Live integration (optional):

~~~bash
CONTEXTFORGE_TEST_URL=http://127.0.0.1:4444 \
CONTEXTFORGE_TEST_TOKEN=... \
MILVUS_TEST_URI=http://127.0.0.1:19530 \
uv run pytest tests/test_contextforge_integration.py -q
~~~

## File Structure

New backend modules:

~~~text
backend/packages/harness/agentkit/retrieval/
  __init__.py
  tokenize.py            ASCII/CJK tokenization
  bm25.py                generic BM25 scoring

backend/packages/harness/agentkit/config/
  contextforge_config.py  ContextForgeConfig + sync/admin_events/search

backend/packages/harness/agentkit/contextforge/
  __init__.py
  models.py              ContextForgeTool, ToolWrite, errors
  admin.py               Admin REST client
  admin_events.py        /admin/events SSE parser + subscriber
  loader.py              MCP tools/list loading
  catalog.py             ToolCatalogEntry builder
  cache.py               process-level snapshot/cache
  index.py               Milvus tool vector index
  runtime.py             ContextForgeRuntime lifecycle + sync
  retry.py               strict tool error classification

backend/packages/harness/agentkit/tools/
  contextforge_metadata.py
  builtin/search_tools.py
  search/__init__.py
  search/service.py
  search/runtime.py

backend/packages/harness/agentkit/agents/middlewares/
  tool_search_middleware.py
  tool_refresh_retry_middleware.py

backend/app/gateway/
  models/tools.py
  tools_wire.py
  routers/tools.py
~~~

New frontend modules:

~~~text
frontend/src/app/workspace/tools/page.tsx
frontend/src/components/tools/tool-management-page.tsx
frontend/src/components/tools/tool-editor-dialog.tsx
frontend/src/core/api/tools.ts
~~~

Modified backend files:

~~~text
agentkit/config/app_config.py
agentkit/config/__init__.py
agentkit/tools/registry.py
agentkit/tools/builtin/__init__.py
agentkit/skills/tool_policy.py
agentkit/agents/leader_agent/agent.py
agentkit/agents/leader_agent/prompt.py
agentkit/agents/middlewares/assembly.py
agentkit/agents/thread_state.py
backend/app/gateway/app.py
backend/app/gateway/errors.py
backend/app/gateway/deps.py
backend/packages/harness/pyproject.toml
backend/pyproject.toml
config.example.yaml
backend/README.md
~~~

Modified frontend files:

~~~text
frontend/src/components/workspace/sidebar.tsx
frontend/src/app/workspace/sidebar-container.tsx
frontend/src/components/ui/icons.tsx
~~~

---

### Task 1: Generic lexical retrieval primitives

**Files:**
- Create: backend/packages/harness/agentkit/retrieval/__init__.py
- Create: backend/packages/harness/agentkit/retrieval/tokenize.py
- Create: backend/packages/harness/agentkit/retrieval/bm25.py
- Test: backend/tests/test_retrieval_bm25.py

**Interfaces:**
- Consumes: standard library only.
- Produces: tokenize(text: str) -> list[str]; Bm25Document; bm25_scores(...).

- [ ] **Step 1: Write the failing tests**

~~~python
from agentkit.retrieval.bm25 import Bm25Document, bm25_scores
from agentkit.retrieval.tokenize import tokenize


def test_tokenize_keeps_ascii_terms_and_cjk_bigrams() -> None:
    assert tokenize("PostgreSQL 多租户") == ["postgresql", "多租", "租户"]


def test_bm25_prefers_name_hits_and_returns_empty_for_unknown_terms() -> None:
    docs = [
        Bm25Document(key="a", counts={"github": 1}, length=1, head_tokens=frozenset({"github"})),
        Bm25Document(key="b", counts={"github": 1}, length=20, head_tokens=frozenset()),
    ]
    scores = bm25_scores(docs, query_terms={"github": 1.0}, k1=1.2, b=0.75, name_weight=3.0)
    assert scores["a"] > scores["b"] > 0
    assert bm25_scores(docs, query_terms={"missing": 1.0}) == {}
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_retrieval_bm25.py -v
Expected: FAIL because agentkit.retrieval does not exist.

- [ ] **Step 3: Implement the module**

Use the same ASCII regex and CJK bigram rules as agentkit.skills.router. Implement BM25 with idf = log(1 + (N - df + 0.5) / (df + 0.5)), k1/b length normalization, and a per-term name_weight multiplier when the term is in head_tokens. Return only strictly positive scores.

- [ ] **Step 4: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_retrieval_bm25.py -v
Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/retrieval backend/tests/test_retrieval_bm25.py
git commit -m "feat: add shared lexical retrieval primitives"
~~~

---

### Task 2: ContextForge configuration

**Files:**
- Create: backend/packages/harness/agentkit/config/contextforge_config.py
- Modify: backend/packages/harness/agentkit/config/app_config.py
- Modify: backend/packages/harness/agentkit/config/__init__.py
- Test: backend/tests/test_contextforge_config.py

**Interfaces:**
- Consumes: agentkit.config.knowledge_config EmbeddingConfig, RerankConfig, MilvusConfig.
- Produces: ContextForgeConfig as AppConfig.contextforge; resolved_mcp_url(); configured search/admin_events/sync blocks.

- [ ] **Step 1: Write the failing tests**

~~~python
from agentkit.config.app_config import AppConfig


def test_contextforge_disabled_by_default() -> None:
    cfg = AppConfig.model_validate({"models": [{"name": "m", "use": "x:Y", "model": "m"}]})
    assert cfg.contextforge.enabled is False
    assert cfg.contextforge.tool_loading == "auto"
    assert cfg.contextforge.search.vector_store.collection == "myflow_tools_catalog"


def test_shared_embedding_rerank_and_vector_store_reach_contextforge() -> None:
    cfg = AppConfig.model_validate({
        "models": [{"name": "m", "use": "x:Y", "model": "m"}],
        "embedding": {"base_url": "http://embed", "dimensions": 8},
        "rerank": {"base_url": "http://rerank", "model": "r"},
        "vector_store": {"uri": "http://milvus:19530"},
        "contextforge": {"enabled": True, "base_url": "http://cf", "search": {"enabled": True}},
    })
    assert cfg.contextforge.search.embedding.base_url == "http://embed"
    assert cfg.contextforge.search.rerank.base_url == "http://rerank"
    assert cfg.contextforge.search.vector_store.uri == "http://milvus:19530"
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_contextforge_config.py -v
Expected: FAIL with AttributeError: contextforge.

- [ ] **Step 3: Implement the config models**

Add ContextForgeConfig, ContextForgeSyncConfig, AdminEventsConfig, ToolSearchConfig, ToolKeywordConfig, ToolEmbeddingConfig, ToolRerankConfig, ToolVectorStoreConfig. Validate: enabled requires base_url; tool_loading != eager requires search.enabled; search.enabled requires embedding.base_url and vector_store.uri; collection is fixed; candidate_k >= top_k; admin_events token defaults to empty; mcp_url falls back to base_url + /mcp or /servers/{id}/mcp. Add contextforge to AppConfig and to _SHARED_SERVICE_PATHS for embedding/rerank/vector_store.

- [ ] **Step 4: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_contextforge_config.py -v
Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/config backend/tests/test_contextforge_config.py
git commit -m "feat: add contextforge configuration"
~~~

---

### Task 3: ContextForge Admin REST client

**Files:**
- Create: backend/packages/harness/agentkit/contextforge/__init__.py
- Create: backend/packages/harness/agentkit/contextforge/models.py
- Create: backend/packages/harness/agentkit/contextforge/admin.py
- Test: backend/tests/test_contextforge_admin.py

**Interfaces:**
- Consumes: ContextForgeConfig; httpx.AsyncClient; MockTransport in tests.
- Produces: ContextForgeTool; ToolWrite; ContextForgeAdminClient methods list_tools/get_tool/create_tool/update_tool/delete_tool/set_tool_state/health/aclose; ContextForgeError subclasses.

- [ ] **Step 1: Write the failing tests**

~~~python
import httpx
import pytest

from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.contextforge.admin import ContextForgeAdminClient


@pytest.mark.asyncio
async def test_list_normalizes_camel_case_and_json_string_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer adm"
        return httpx.Response(200, json={"tools": [{
            "id": "t1", "name": "github_search", "displayName": "GitHub",
            "description": "d", "integrationType": "MCP", "requestType": "STREAMABLEHTTP",
            "inputSchema": "{\"type\": \"object\"}", "headers": [{"name": "X-Env", "value": "prod"}],
            "enabled": True, "reachable": True,
        }]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ContextForgeConfig(enabled=True, base_url="http://cf", admin_token="adm", tool_loading="eager", search={"enabled": False})
    admin = ContextForgeAdminClient(config, client=client)
    tools = await admin.list_tools()
    assert tools[0].display_name == "GitHub"
    assert tools[0].input_schema == {"type": "object"}
    assert tools[0].headers == {"X-Env": "prod"}
    await admin.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_upstream_401_maps_to_auth_error_without_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "nope"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ContextForgeConfig(enabled=True, base_url="http://cf", admin_token="secret", tool_loading="eager", search={"enabled": False})
    admin = ContextForgeAdminClient(config, client=client)
    with pytest.raises(Exception) as exc:
        await admin.list_tools()
    assert "secret" not in str(exc.value)
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_contextforge_admin.py -v
Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement the client**

Implement canonical ContextForgeTool fields: id, name, display_name, description, url, enabled, reachable, integration_type, request_type, headers, input_schema, annotations, tags, visibility, created_at, updated_at, raw. ToolWrite mirrors the write shape and omits enabled from PUT if the upstream rejects it. Normalize snake/camel keys, JSON-string input_schema, and headers list/object. Use follow_redirects=False, Bearer auth, no blind write retries, and error classes that never include header values or tokens. Health uses GET /health and falls back to GET /tools?limit=1 on 404.

- [ ] **Step 4: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_contextforge_admin.py -v
Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/contextforge backend/tests/test_contextforge_admin.py
git commit -m "feat: add ContextForge admin REST client"
~~~

---

### Task 4: Gateway tool CRUD, masking, and errors

**Files:**
- Create: backend/app/gateway/models/tools.py
- Create: backend/app/gateway/tools_wire.py
- Create: backend/app/gateway/routers/tools.py
- Modify: backend/app/gateway/errors.py
- Modify: backend/app/gateway/app.py
- Test: backend/tests/test_tools_wire.py
- Test: backend/tests/test_tools_router.py

**Interfaces:**
- Consumes: ContextForgeAdminClient; require_scope("admin"); RunCaller.
- Produces: GET/POST/PUT/DELETE /api/tools, GET /api/tools/{id}, POST /api/tools/{id}/state; masked tool wire models; gateway error codes.

- [ ] **Step 1: Write the failing wire tests**

~~~python
from backend.app.gateway.tools_wire import mask_tool_headers, restore_masked_headers

MASK = "****"

def test_mask_and_restore_header_values() -> None:
    original = {"Authorization": "Bearer x", "X-Env": "prod"}
    masked = mask_tool_headers(original)
    assert masked == {"Authorization": MASK, "X-Env": "prod"}
    assert restore_masked_headers(masked, original) == original


def test_masked_value_without_stored_value_is_rejected() -> None:
    import pytest
    with pytest.raises(ValueError):
        restore_masked_headers({"Authorization": MASK}, {})
~~~

- [ ] **Step 2: Write the failing router tests**

Use FastAPI TestClient with dependency overrides: require_scope returns RunCaller(admin), and a fake admin client records calls. Assert 403 for a non-admin caller, 404 maps to tool_not_found, 422 does not echo header values, and list responses omit headers/input_schema.

- [ ] **Step 3: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_tools_wire.py tests/test_tools_router.py -v
Expected: FAIL because modules/routes do not exist.

- [ ] **Step 4: Implement the models, wire helpers, routes, and errors**

Use pydantic extra=forbid for write models. Mask sensitive header names case-insensitively. On update, fetch the stored tool and restore **** values before PUT; if no stored value exists, return 422. Route order: list, status placeholder, reindex placeholder, search placeholder, then /{tool_id}. Register the router in create_app with prefix /api.

- [ ] **Step 5: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_tools_wire.py tests/test_tools_router.py -v
Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/app/gateway backend/tests/test_tools_wire.py backend/tests/test_tools_router.py
git commit -m "feat: add gateway tool CRUD proxy"
~~~

---

### Task 5: Milvus tool vector index

**Files:**
- Create: backend/packages/harness/agentkit/contextforge/index.py
- Test: backend/tests/test_tool_vector_index.py

**Interfaces:**
- Consumes: ToolVectorStoreConfig, ToolCatalogEntry, embedder.
- Produces: TOOLS_COLLECTION; ToolVectorHit; ToolIndexSyncStats; ToolVectorIndex.ensure_collection/query_hashes/upsert/delete_not_in/hybrid_search/dense_search/sync/aclose.

- [ ] **Step 1: Write the failing tests**

Use a fake Milvus client with has_collection, create_schema, create_collection, create_index, load_collection, query, upsert, delete, hybrid_search, search. Assert the schema has content with enable_analyzer=True, sparse, embedding, and a BM25 Function; assert hybrid_search builds two AnnSearchRequest objects and RRFRanker(rrf_k); assert dimension mismatch raises; assert sync embeds only changed hashes.

- [ ] **Step 2: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_tool_vector_index.py -v
Expected: FAIL because index.py does not exist.

- [ ] **Step 3: Implement the index**

Use lazy pymilvus imports. Collection schema fields: pk, tool_name, display_name, description, content, tags, parameters, server, content_hash, embedding, sparse, updated_at. Indexes: embedding with configured metric/index; sparse with SPARSE_INVERTED_INDEX/BM25 and k1/b. Consistency default Strong. Provide dense_search for keyword disabled and hybrid_search for both. The sync method queries hashes, embeds changed entries, upserts records, and optionally deletes names not in the current set only when full=True.

- [ ] **Step 4: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_tool_vector_index.py -v
Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/contextforge/index.py backend/tests/test_tool_vector_index.py
git commit -m "feat: add Milvus tool vector index"
~~~

---

### Task 6: MCP loader, catalog, and snapshot cache

**Files:**
- Create: backend/packages/harness/agentkit/contextforge/loader.py
- Create: backend/packages/harness/agentkit/contextforge/catalog.py
- Create: backend/packages/harness/agentkit/contextforge/cache.py
- Test: backend/tests/test_contextforge_loader.py
- Test: backend/tests/test_contextforge_catalog.py

**Interfaces:**
- Consumes: ContextForgeConfig; langchain-mcp-adapters load_mcp_tools; BaseTool.
- Produces: load_contextforge_tools(config) -> list[BaseTool]; ToolCatalogEntry; build_entries(tools, enrichment) -> list[ToolCatalogEntry]; get_snapshot/set_snapshot/invalidate_contextforge_cache.

- [ ] **Step 1: Write the failing tests**

Assert:
- a fake load_mcp_tools result is tagged with myflow_contextforge and names are qualified;
- invalid tool names are dropped without raising;
- empty MCP list is a valid empty snapshot;
- entries use name/description/args_schema property names;
- content_hash changes when description changes;
- cache returns the same tuple object until invalidated.

- [ ] **Step 2: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_contextforge_loader.py tests/test_contextforge_catalog.py -v
Expected: FAIL because modules do not exist.

- [ ] **Step 3: Implement loader, catalog, cache**

Use load_mcp_tools(session=None, connection=params, server_name=contextforge, tool_name_prefix=False, handle_tool_errors=True) and tag_mcp_tool. Build content as name + display_name + description + tags + parameter names, truncated to max_document_chars. Snapshot stores tools, entries, loaded_at, and a signature; cache invalidation only affects the current process.

- [ ] **Step 4: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_contextforge_loader.py tests/test_contextforge_catalog.py -v
Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/contextforge backend/tests/test_contextforge_loader.py backend/tests/test_contextforge_catalog.py
git commit -m "feat: load and cache ContextForge MCP tools"
~~~

---

### Task 7: /admin/events SSE subscriber

**Files:**
- Create: backend/packages/harness/agentkit/contextforge/admin_events.py
- Test: backend/tests/test_contextforge_admin_events.py

**Interfaces:**
- Consumes: ContextForgeConfig.admin_events; httpx.AsyncClient.
- Produces: ContextForgeAdminEvent; parse_sse_block; should_refresh_tool_event; AdminEventSubscriber.start/stop/status.

- [ ] **Step 1: Write the failing parser tests**

~~~python
from agentkit.contextforge.admin_events import parse_sse_block, should_refresh_tool_event


def test_parse_multiline_event_and_keepalive() -> None:
    event = parse_sse_block("event: tool_updated\ndata: {\"id\": \"t1\"}\n\ndata: ignored\n\n")
    assert event is not None
    assert event.type == "tool_updated"
    assert event.data == {"id": "t1"}
    assert parse_sse_block(": keepalive\n\n") is None


def test_tool_and_gateway_events_refresh() -> None:
    assert should_refresh_tool_event("tool_updated") is True
    assert should_refresh_tool_event("gateway_activated") is True
    assert should_refresh_tool_event("tool_executed") is False
~~~

- [ ] **Step 2: Write the failing subscriber tests**

Use httpx.MockTransport with a streaming response: first request returns 404, next returns 401, next returns an SSE stream with tool_updated then EOF. Assert status transitions unsupported/auth_failed/reconnecting, callback is called once, and reconnect backoff is bounded. Add a test for a 404 when MCPGATEWAY_ADMIN_API_ENABLED is off.

- [ ] **Step 3: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_contextforge_admin_events.py -v
Expected: FAIL because admin_events.py does not exist.

- [ ] **Step 4: Implement the subscriber**

Parse event/data lines, ignore comments, join multi-line data, JSON-decode data, and ignore malformed data with a warning. Subscribe with GET {base_url}/admin/events, Accept text/event-stream, Authorization Bearer admin_events.token or admin_token. Map 404 to unsupported, 401/403 to auth_failed, transport errors to disconnected/reconnecting. Debounce is handled by the runtime; the subscriber only emits events and reconnect status.

- [ ] **Step 5: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_contextforge_admin_events.py -v
Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/packages/harness/agentkit/contextforge/admin_events.py backend/tests/test_contextforge_admin_events.py
git commit -m "feat: subscribe to ContextForge admin events"
~~~

---

### Task 8: ContextForgeRuntime lifecycle, refresh, status, and reindex

**Files:**
- Create: backend/packages/harness/agentkit/contextforge/runtime.py
- Modify: backend/app/gateway/app.py
- Modify: backend/app/gateway/deps.py
- Modify: backend/app/gateway/routers/tools.py
- Modify: backend/app/gateway/models/tools.py
- Test: backend/tests/test_contextforge_runtime.py
- Test: backend/tests/test_tools_router.py

**Interfaces:**
- Consumes: admin client, loader, cache, index, embedder, AdminEventSubscriber.
- Produces: ContextForgeRuntime(config, admin, index, embedder, reranker); build_contextforge_runtime(config); get_contextforge_runtime(); ContextForgeRuntime.start/stop/refresh_and_sync/request_refresh_async/status/reindex/aclose; GET /api/tools/status; POST /api/tools/reindex.

- [ ] **Step 1: Write the failing runtime tests**

Use fakes for loader, index, admin client, and event subscriber. Assert:
- start performs one initial sync and starts the subscriber;
- refresh_and_sync updates the snapshot then calls index.sync;
- index failure does not clear the snapshot and sets status degraded;
- request_refresh_async coalesces two calls into one refresh plus one dirty rerun;
- status returns ready/syncing/degraded/error and event status;
- stop cancels the subscriber and closes clients.

- [ ] **Step 2: Write the failing route tests**

Assert GET /api/tools/status is admin-only and returns the runtime status; POST /api/tools/reindex calls runtime.reindex once and returns counts; a disabled ContextForge returns 503 contextforge_disabled.

- [ ] **Step 3: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_contextforge_runtime.py tests/test_tools_router.py -v
Expected: FAIL because runtime/routes are incomplete.

- [ ] **Step 4: Implement the runtime and lifecycle**

Build the runtime in FastAPI lifespan when contextforge.enabled; use asyncio.create_task for start; app.state.contextforge holds it. Provide a process-level get_contextforge_runtime() for tools/middleware. Refresh uses a single-flight lock and dirty flag. Status includes upstream, admin_events, sync, index, channels, counts, and retry fields from the spec. Admin write routes call request_refresh_async(reason=admin). Reindex forces full sync with GC.

- [ ] **Step 5: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_contextforge_runtime.py tests/test_tools_router.py -v
Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/packages/harness/agentkit/contextforge/runtime.py backend/app/gateway backend/tests/test_contextforge_runtime.py backend/tests/test_tools_router.py
git commit -m "feat: wire ContextForge runtime lifecycle and status"
~~~

---

### Task 9: Hybrid tool search service

**Files:**
- Create: backend/packages/harness/agentkit/tools/search/__init__.py
- Create: backend/packages/harness/agentkit/tools/search/service.py
- Create: backend/packages/harness/agentkit/tools/search/runtime.py
- Modify: backend/app/gateway/routers/tools.py
- Test: backend/tests/test_tool_search_router.py

**Interfaces:**
- Consumes: ToolVectorIndex, embedder, reranker, current snapshot.
- Produces: ToolSearchHit; ToolSearchOutcome; ToolSearchService(index, embedder, reranker, config, snapshot_provider); ToolSearchService.search/invalidate; configure_tool_search_runtime/get_tool_search_runtime/reset_tool_search_runtime; GET /api/tools/search.

- [ ] **Step 1: Write the failing tests**

Use a fake index returning known tool names, a fake embedder returning fixed vectors, and a fake reranker returning reversed indices. Assert:
- Milvus hybrid hits are confirmed against the current snapshot; stale names are dropped;
- rerank reorders only confirmed candidates;
- rerank failure returns the RRF order with rerank_unavailable;
- Milvus failure returns local BM25 with vector_unavailable;
- empty query returns an empty outcome.

- [ ] **Step 2: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_tool_search_router.py -v
Expected: FAIL because the service does not exist.

- [ ] **Step 3: Implement the service**

Implement search: query embedding, index.hybrid_search (or dense_search when keyword disabled), confirm by current snapshot name, normalize scores in the candidate set, apply min_score, optional rerank, top_k. Fall back to local BM25 from snapshot on vector/index failure. Return index kind milvus/local_bm25 and warnings. Add a preview route that calls the same service without promotion.

- [ ] **Step 4: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_tool_search_router.py -v
Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/tools/search backend/app/gateway/routers/tools.py backend/tests/test_tool_search_router.py
git commit -m "feat: add hybrid tool search service"
~~~

---

### Task 10: search_tools built-in and promotion

**Files:**
- Create: backend/packages/harness/agentkit/tools/contextforge_metadata.py
- Create: backend/packages/harness/agentkit/tools/builtin/search_tools.py
- Test: backend/tests/test_search_tools_tool.py

**Interfaces:**
- Consumes: ToolSearchService; ToolRuntime; InjectedToolCallId.
- Produces: TOOL_NAME=search_tools; SearchToolsInput; search_tools(...) -> Command; tag_contextforge_tool/is_contextforge_tool.

- [ ] **Step 1: Write the failing tests**

Assert:
- empty task_query returns a message and no promoted update;
- a hit returns metadata only, includes retrieval diagnostics, and returns Command with promoted names;
- unavailable service returns the unavailable message;
- descriptions are clipped and no input_schema appears in the result;
- tag/is_contextforge_tool round-trip.

- [ ] **Step 2: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_search_tools_tool.py -v
Expected: FAIL.

- [ ] **Step 3: Implement the tool**

Use @tool("search_tools", args_schema=SearchToolsInput), Annotated[str, InjectedToolCallId], and Command(update={messages: [ToolMessage(...)], promoted: names}). The payload includes tool_name/display_name/description/tags/parameters/score and retrieval {index, channels, candidates, reranked, warnings}. Text says promoted tools are available next call and depend on active skill policy.

- [ ] **Step 4: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_search_tools_tool.py -v
Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/tools backend/tests/test_search_tools_tool.py
git commit -m "feat: add search_tools built-in"
~~~

---

### Task 11: ToolSearchMiddleware and agent assembly

**Files:**
- Create: backend/packages/harness/agentkit/agents/middlewares/tool_search_middleware.py
- Modify: backend/packages/harness/agentkit/tools/registry.py
- Modify: backend/packages/harness/agentkit/tools/builtin/__init__.py
- Modify: backend/packages/harness/agentkit/skills/tool_policy.py
- Modify: backend/packages/harness/agentkit/agents/leader_agent/agent.py
- Modify: backend/packages/harness/agentkit/agents/leader_agent/prompt.py
- Modify: backend/packages/harness/agentkit/agents/middlewares/assembly.py
- Modify: backend/packages/harness/agentkit/agents/thread_state.py
- Test: backend/tests/test_tool_search_middleware.py
- Test: backend/tests/test_agent_factory.py
- Test: backend/tests/test_skill_policy.py

**Interfaces:**
- Consumes: promoted state; contextforge tool metadata; app config.
- Produces: ToolSearchMiddleware(deferred: bool); registry fourth source; search_tools in always-available; auto/eager/deferred decision; PROMOTED_MAX_ENTRIES=64.

- [ ] **Step 1: Write the failing middleware tests**

Assert deferred mode hides contextforge tools until promoted; non-contextforge tools remain visible; search_tools remains visible; promoted cap keeps the newest 64; SkillToolPolicy still runs before ToolSearch and a restricted skill does not gain a promoted tool.

- [ ] **Step 2: Write the failing assembly tests**

Assert search_tools is registered when contextforge.search.enabled; it is hidden when disabled; auto mode chooses eager at or below deferred_threshold and deferred above; the prompt contains the external-tool section only in deferred mode.

- [ ] **Step 3: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_tool_search_middleware.py tests/test_agent_factory.py tests/test_skill_policy.py -v
Expected: FAIL for missing middleware/registration; existing policy tests still pass.

- [ ] **Step 4: Implement**

Register search_tools as ("tools", search_tools) in _BUILTIN_TOOLS and add it to ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES. Add the fourth source after builtins. Compute defer_contextforge in assemble_agent and pass it to build_middlewares and apply_prompt_template. Register ToolSearchMiddleware after the skill policy pair and before DurableContext. Cap merge_promoted at 64.

- [ ] **Step 5: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_tool_search_middleware.py tests/test_agent_factory.py tests/test_skill_policy.py -v
Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add backend/packages/harness/agentkit backend/tests/test_tool_search_middleware.py backend/tests/test_agent_factory.py backend/tests/test_skill_policy.py
git commit -m "feat: promote searched tools into the model schema"
~~~

---

### Task 12: Retryable tool error middleware

**Files:**
- Create: backend/packages/harness/agentkit/contextforge/retry.py
- Create: backend/packages/harness/agentkit/agents/middlewares/tool_refresh_retry_middleware.py
- Modify: backend/packages/harness/agentkit/agents/middlewares/assembly.py
- Test: backend/tests/test_contextforge_retry.py

**Interfaces:**
- Consumes: ContextForgeRuntime.refresh_and_sync; ToolMessage; ToolCallRequest.
- Produces: RefreshableToolError enum; classify_tool_error; ToolRefreshRetryMiddleware(enabled: bool, refresh_timeout_seconds: float, runtime_provider: Callable[[], ContextForgeRuntime | None]).

- [ ] **Step 1: Write the failing tests**

Assert not found and invalid params classify correctly; auth/timeout/429/5xx/execution errors return NONE; a retryable error triggers exactly one refresh and one handler retry; a second failure returns the second result; refresh timeout returns the original error; CancelledError/GraphBubbleUp are re-raised.

- [ ] **Step 2: Run tests to verify they fail**

Run: cd backend && uv run pytest tests/test_contextforge_retry.py -v
Expected: FAIL because retry.py/middleware do not exist.

- [ ] **Step 3: Implement retry classification and middleware**

Classify only structured/known markers (tool not found, unknown tool, no such tool, -32601, validation error, invalid arguments, invalid params, schema/input validation, -32602, 422, and the Chinese equivalents). Add ToolRefreshRetryMiddleware at the innermost position after PIIMiddleware in build_runtime_middlewares, enabled only when contextforge.sync.retry_on_tool_error. Use a local retried boolean, never modify the tool schema, and never retry policy/auth/timeout/5xx.

- [ ] **Step 4: Run tests to verify they pass**

Run: cd backend && uv run pytest tests/test_contextforge_retry.py -v
Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/contextforge/retry.py backend/packages/harness/agentkit/agents/middlewares/tool_refresh_retry_middleware.py backend/packages/harness/agentkit/agents/middlewares/assembly.py backend/tests/test_contextforge_retry.py
git commit -m "feat: refresh tools once on retryable MCP errors"
~~~

---

### Task 13: Frontend tools API client

**Files:**
- Create: frontend/src/core/api/tools.ts
- Test: frontend/src/core/api/tools.test.ts

**Interfaces:**
- Consumes: apiFetch/apiUrl; gateway /api/tools.
- Produces: ContextForgeTool, ContextForgeToolDetail, ToolListPage, ToolStatus, ToolSearchPreview, ToolApiError; listTools/getTool/createTool/updateTool/deleteTool/setToolEnabled/getToolsStatus/reindexTools/previewToolSearch.

- [ ] **Step 1: Write the failing tests**

Use vi.stubGlobal fetch and assert list mapping, 422 errors[] extraction, 404/503 ToolApiError, URL encoding of tool ids, and that create/update send JSON headers and body. Assert headers are passed through as masked values.

- [ ] **Step 2: Run tests to verify they fail**

Run: cd frontend && pnpm test src/core/api/tools.test.ts
Expected: FAIL because tools.ts does not exist.

- [ ] **Step 3: Implement the client**

Follow frontend/src/core/api/skills.ts. Normalize wire fields to snake_case page types. Parse {code, detail, trace_id, errors}. Use apiFetch with cookie/CSRF. No token storage.

- [ ] **Step 4: Run tests to verify they pass**

Run: cd frontend && pnpm test src/core/api/tools.test.ts
Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add frontend/src/core/api/tools.ts frontend/src/core/api/tools.test.ts
git commit -m "feat: add frontend tools API client"
~~~

---

### Task 14: Tools management page and navigation

**Files:**
- Create: frontend/src/app/workspace/tools/page.tsx
- Create: frontend/src/components/tools/tool-management-page.tsx
- Create: frontend/src/components/tools/tool-editor-dialog.tsx
- Modify: frontend/src/components/workspace/sidebar.tsx
- Modify: frontend/src/app/workspace/sidebar-container.tsx
- Modify: frontend/src/components/ui/icons.tsx
- Test: frontend/src/components/tools/tool-management-page.dom.test.tsx
- Test: frontend/src/components/tools/tool-editor-dialog.dom.test.tsx

**Interfaces:**
- Consumes: tools API client; useSession; Button/Skeleton/Tooltip.
- Produces: /workspace/tools page; nav item after Skills before Knowledge; all status badges; editor dialog.

- [ ] **Step 1: Write the failing page tests**

Assert loading/empty/error states; admin gate (user and disabled auth); status bar renders ContextForge, admin events, sync, index, channels, counts; create opens dialog and invalidates queries; toggle calls setToolEnabled; delete shows a confirmation with the tool name; preview calls previewToolSearch and renders warnings.

- [ ] **Step 2: Write the failing dialog tests**

Assert required name/description; invalid JSON schema shows inline error without fetch; masked header round-trips; edit prefill; 422 errors[] maps to fields.

- [ ] **Step 3: Run tests to verify they fail**

Run: cd frontend && pnpm test src/components/tools
Expected: FAIL because components do not exist.

- [ ] **Step 4: Implement the page, dialog, nav, and icon**

Use React Query keys ["tools", params], ["tools", "status"], ["tools", "detail", id]. Show all statuses in a compact top bar and per-tool badges: enabled, reachable, shadowed_by, index_status, source. Add WrenchIcon. Sidebar gets toolsActive and the container passes pathname.startsWith("/workspace/tools"). Non-admin route shows a 403-style message.

- [ ] **Step 5: Run tests to verify they pass**

Run: cd frontend && pnpm test src/components/tools
Expected: PASS.

- [ ] **Step 6: Commit**

~~~bash
git add frontend/src/app/workspace/tools frontend/src/components/tools frontend/src/components/workspace/sidebar.tsx frontend/src/app/workspace/sidebar-container.tsx frontend/src/components/ui/icons.tsx
git commit -m "feat: add tool management UI"
~~~

---

### Task 15: Documentation, config example, and integration verification

**Files:**
- Modify: config.example.yaml
- Modify: backend/README.md
- Modify: backend/packages/harness/pyproject.toml
- Modify: backend/pyproject.toml
- Create: backend/tests/test_contextforge_integration.py
- Optional: frontend/e2e/tools.spec.ts

**Interfaces:**
- Consumes: all prior tasks.
- Produces: documented config and deployment prerequisites; opt-in integration tests; optional Playwright smoke test.

- [ ] **Step 1: Add config example**

Add contextforge with enabled false, base_url, admin_token, agent_token, mcp_url, sync, admin_events, search, and collection. Add comments for MCPGATEWAY_ADMIN_API_ENABLED=true and admin.events. Do not add a live token.

- [ ] **Step 2: Add extras and README**

Add contextforge extra = langchain-mcp-adapters + pymilvus to harness, and backend contextforge = agentkit[contextforge]. Document /admin/events requirements, 404/401/403 statuses, Milvus collection reuse, and the three update paths.

- [ ] **Step 3: Add opt-in integration tests**

Skip unless CONTEXTFORGE_TEST_URL and CONTEXTFORGE_TEST_TOKEN are set; skip Milvus assertions unless MILVUS_TEST_URI is set. Verify GET /admin/events returns SSE (or a clear 404/403), the ContextForge MCP tools/list matches admin names, and the tool index sync/query round-trips.

- [ ] **Step 4: Run the full verification**

Run:
- cd backend && uv run pytest
- cd frontend && pnpm test
- cd frontend && pnpm lint
- cd frontend && pnpm build
Expected: all pass; integration tests skip without env.

- [ ] **Step 5: Commit**

~~~bash
git add config.example.yaml backend/README.md backend/packages/harness/pyproject.toml backend/pyproject.toml backend/tests/test_contextforge_integration.py frontend/e2e/tools.spec.ts
git commit -m "docs: document ContextForge tool management operations"
~~~

---

## Self-Review

- Spec coverage: management CRUD (Tasks 3-4, 13-14), Milvus index (Task 5), MCP loading (Task 6), admin events (Task 7), runtime/status (Task 8), search (Tasks 9-11), retry (Task 12), deployment/docs (Task 15).
- Type consistency: ContextForgeAdminClient methods are used by Task 8 and the routes; ToolCatalogEntry is shared by Tasks 5, 6, 9; search_tools uses ToolSearchOutcome from Task 9; ToolSearchMiddleware reads promoted from Task 10.
- Review Focus tests are owned by Tasks 3, 7, 4, 5/9, and 12.
- Defaults: port 8001, restart required for config schema changes, admin-only, no new DB tables.
