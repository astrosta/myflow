# MCP Server 管理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the tool-management surface with user-private MCP Server management; MyFlow stores servers, discovers their tools, and executes `tools/call` directly.

**Architecture:** Add a new `agentkit/mcp` subsystem next to the existing catalog code: `mcp_servers` + `mcp_tools` tables, per-server MCP client, snapshot sync into Milvus, and direct execution. After the new path is tested, cut the app and built-in tools over and delete the old external-gateway client, config, routes, and tests.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/Alembic, Pydantic v2, `mcp>=1.30,<2`, `cryptography` (Fernet), Milvus, pytest/pytest-asyncio, Next.js/React/Vitest.

**Spec:** `docs/superpowers/specs/2026-11-08-mcp-server-management-design.md`

## Global Constraints

- MCP Server is private per MyFlow user: every runtime read is owner-scoped.
- `mcp_servers.name` is `[a-z0-9-]+`, unique per owner, immutable after create; `display_name` is mutable.
- Tool call name is `mcp__<server.name>__<tool.name>`; never `cf__`.
- Tables are `mcp_servers` and `mcp_tools`; Milvus collection is `myflow_mcp_tools`.
- `mcp_servers.headers_encrypted` uses Fernet with `MYFLOW_MCP_SERVER_KEY`; never reuse auth/JWT secrets.
- Supported transports: `streamable_http` and `sse` only. No stdio.
- `tool_execute` opens a short MCP session and calls the tool's own server directly; no external gateway.
- Preserve the existing sync skeleton: snapshot write, derived `callable`, content/schema hashes, DB-before-Milvus, failure keeps previous definitions, one process-level `asyncio.Lock`.
- Fixed single worker: `mcp_servers.enabled=true` and `GATEWAY_WORKERS>1` must fail startup.
- No MCP `notifications/tools/list_changed` in v1. Triggers are startup, server writes/refresh/state, local midnight, tool-error refresh, manual reindex.
- Service is not deployed: no historical data migration; old tables/collection may be dropped and rebuilt.

## Review Focus

- **SSRF and URL policy:** a server URL is an outbound connection target. A user-supplied `http://127.0.0.1`, link-local, metadata, or DNS-rebinding address must not become a backend request; `https` is the default, private networks require explicit config.
- **Credential leakage:** raw headers/tokens must never appear in API responses, logs, trace, error messages, or exception reprs; masked `****` must preserve the stored value without turning the literal into a credential.
- **Per-server failure isolation:** one unreachable server must leave its previous definitions present but non-callable, while every other server's tools remain callable and indexed. An empty/failed server must never wipe the whole catalog.
- **Cross-server identity:** two servers may expose the same remote tool name, and two users may use the same server name. `call_name` must stay unique within an owner and DB reads must not cross owners.
- **Unsafe retry:** a timeout, transport failure, or 5xx may already have executed upstream; only tool-not-found and schema/argument mismatch may refresh and retry once.

---

### Task 1: Server config, secret encryption, and `mcp_servers` store

**Files:**
- Create: `backend/packages/harness/agentkit/config/mcp_servers_config.py`
- Create: `backend/packages/harness/agentkit/config/tool_search_config.py`
- Create: `backend/packages/harness/agentkit/mcp/__init__.py`
- Create: `backend/packages/harness/agentkit/mcp/models.py`
- Create: `backend/packages/harness/agentkit/mcp/crypto.py`
- Create: `backend/packages/harness/agentkit/mcp/store/__init__.py`
- Create: `backend/packages/harness/agentkit/mcp/store/model.py`
- Create: `backend/packages/harness/agentkit/mcp/store/repository.py`
- Create: `backend/packages/harness/agentkit/persistence/migrations/versions/0018_mcp_servers.py`
- Modify: `backend/packages/harness/agentkit/config/app_config.py`
- Modify: `backend/packages/harness/agentkit/persistence/schema.py`
- Test: `backend/tests/test_mcp_servers_config.py`
- Test: `backend/tests/test_mcp_crypto.py`
- Test: `backend/tests/test_mcp_server_store.py`
- Test: `backend/tests/test_mcp_migration_0018.py`

**Interfaces:**
- Produces `McpServersConfig` with `enabled`, `encryption_key: SecretStr`, `request_timeout_seconds`, `connect_timeout_seconds`, `max_servers_per_user`, `allow_private_networks`, `allowed_schemes`, and `validate_for_startup() -> None`.
- Produces `ToolSearchConfig` with the existing budgets/channels and a collection pinned to `myflow_mcp_tools`; `AppConfig.tool_search` holds it.
- Produces `McpServerRecord(id, owner_user_id, name, display_name, description, transport, url, headers_encrypted, verify_tls, enabled, status, reachable, last_error, last_connected_at, last_tools_sync_at, created_at, updated_at)`.
- Produces `HeaderCipher(encryption_key)` with `encrypt(headers) -> str | None`, `decrypt(token) -> dict[str, str]`, and `generate_key() -> str`.
- Produces `McpServerStore(session_factory)` with `create(record)`, `get(server_id, owner_user_id)`, `get_by_name(owner_user_id, name)`, `list_for_owner(owner_user_id)`, `list_all()`, `update(server_id, owner_user_id, fields)`, `delete(server_id, owner_user_id)`, `set_enabled(...)`, and `set_status(...)`.

- [ ] **Step 1: Write failing config tests**

```python
def test_enabled_requires_key() -> None:
    with pytest.raises(ValueError, match="encryption_key"):
        McpServersConfig(enabled=True).validate_for_startup()

def test_invalid_fernet_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="Fernet"):
        McpServersConfig(enabled=True, encryption_key="not-a-key").validate_for_startup()

def test_tool_collection_is_pinned() -> None:
    assert ToolSearchConfig().vector_store.collection == "myflow_mcp_tools"
    with pytest.raises(ValueError, match="myflow_mcp_tools"):
        ToolSearchConfig(vector_store={"collection": "wrong"})
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `PYTHONPATH=. uv run pytest backend/tests/test_mcp_servers_config.py -q`  
Expected: FAIL because the config module does not exist.

- [ ] **Step 3: Implement the config model and add it to `AppConfig`**

Use `ConfigDict(extra="forbid")`, `enabled=False` default, and a `validate_for_startup()` that checks a valid Fernet key and non-empty `allowed_schemes`. Add `mcp_servers: McpServersConfig = Field(default_factory=McpServersConfig)` and `tool_search: ToolSearchConfig = Field(default_factory=ToolSearchConfig)` to `AppConfig`. Add `("tool_search", "embedding")`, `("tool_search", "rerank")`, and `("tool_search", "vector_store")` to `_SHARED_SERVICE_PATHS`, replacing the old contextforge entries (Task 10 removes those).

- [ ] **Step 4: Write failing crypto tests**

```python
def test_round_trip_and_owner_key() -> None:
    cipher = HeaderCipher(HeaderCipher.generate_key())
    token = cipher.encrypt({"Authorization": "Bearer secret", "X-Client": "mcp/1.0.0"})
    assert token and "secret" not in token
    assert cipher.decrypt(token) == {"Authorization": "Bearer secret", "X-Client": "mcp/1.0.0"}
    with pytest.raises(ValueError):
        HeaderCipher(HeaderCipher.generate_key()).decrypt(token)
```

- [ ] **Step 5: Run crypto tests and confirm they fail, then implement `HeaderCipher`**

Use `cryptography.fernet.Fernet`; serialize the header map with `json.dumps(..., sort_keys=True)`; `encrypt({})` returns `None`; `decrypt(None)` returns `{}`. Never include plaintext in exceptions.

- [ ] **Step 6: Write failing store tests**

```python
async def test_owner_isolation_and_duplicate_name(store) -> None:
    mine = await store.create(server_record(owner="u1", name="anysearch"))
    assert await store.get(mine.id, "u1") == mine
    assert await store.get(mine.id, "u2") is None
    with pytest.raises(IntegrityError):
        await store.create(server_record(owner="u1", name="anysearch"))
    other = await store.create(server_record(owner="u2", name="anysearch"))
    assert other.id != mine.id
```

- [ ] **Step 7: Implement `McpServerRow`, repository, and schema import**

Create the table with `UNIQUE(owner_user_id, name)` and `INDEX(owner_user_id, enabled, status)`. Add the model import to `persistence/schema.py` so `Base.metadata` includes it. Follow the session pattern in `contextforge/store/repository.py`.

- [ ] **Step 8: Write the migration test and implement `0018_mcp_servers`**

Test that `upgrade()` is re-entrant (run twice) and `downgrade()` removes the table. `down_revision = "0017_contextforge_tools"`; `Base.metadata.tables["mcp_servers"].create(..., checkfirst=True)`.

- [ ] **Step 9: Run all Task 1 tests and commit**

Run: `PYTHONPATH=. uv run pytest backend/tests/test_mcp_servers_config.py backend/tests/test_mcp_crypto.py backend/tests/test_mcp_server_store.py backend/tests/test_mcp_migration_0018.py -q`  
Expected: PASS.

```bash
git add backend/packages/harness/agentkit/config/mcp_servers_config.py \
  backend/packages/harness/agentkit/config/tool_search_config.py \
  backend/packages/harness/agentkit/mcp backend/packages/harness/agentkit/persistence/migrations/versions/0018_mcp_servers.py \
  backend/packages/harness/agentkit/persistence/schema.py backend/packages/harness/agentkit/config/app_config.py \
  backend/tests/test_mcp_servers_config.py backend/tests/test_mcp_crypto.py \
  backend/tests/test_mcp_server_store.py backend/tests/test_mcp_migration_0018.py
git commit -m "feat: add MCP server config, encrypted headers, and server store"
```

---

### Task 2: Per-server MCP client (Streamable HTTP + SSE)

**Files:**
- Create: `backend/packages/harness/agentkit/mcp/client.py`
- Test: `backend/tests/test_mcp_client.py`

**Interfaces:**
- Produces `McpServerConnection(server_id, url, transport, headers, verify_tls, request_timeout_seconds, connect_timeout_seconds)`.
- Produces `McpServerClient.list_tools(connection) -> list[mcp.types.Tool]` and `call_tool(connection, name, arguments, timeout=None) -> mcp.types.CallToolResult`.
- Produces `open_mcp_session(request: McpSessionRequest)`, which selects `streamablehttp_client` for `streamable_http` and `sse_client` for `sse`.
- Produces `McpServerUnavailable`, `McpServerTimeout`, `McpProtocolError`, `McpToolError`.

- [ ] **Step 1: Write failing transport tests**

```python
async def test_streamable_and_sse_select_the_right_transport(fake_transport):
    client = McpServerClient(session_factory=fake_transport.factory)
    await client.list_tools(connection(transport="streamable_http"))
    await client.list_tools(connection(transport="sse"))
    assert fake_transport.transports == ["streamablehttp_client", "sse_client"]
```

- [ ] **Step 2: Run and confirm failure**

Run: `PYTHONPATH=. uv run pytest backend/tests/test_mcp_client.py -q`  
Expected: FAIL because `agentkit.mcp.client` does not exist.

- [ ] **Step 3: Implement the session transports**

Adapt `contextforge/mcp_client.py`. For Streamable HTTP, the SDK yields `(read, write, get_session_id)`; for SSE it yields `(read, write)`. Both enter a `ClientSession` and call `initialize()` once per short session. Pass `headers` and the `verify_tls` HTTP client factory exactly as the old client does.

- [ ] **Step 4: Write failing pagination/call tests**

```python
async def test_list_tools_follows_cursor_and_call_uses_original_name(fake_session):
    tools = await client.list_tools(connection())
    assert [t.name for t in tools] == ["search", "fetch"]
    result = await client.call_tool(connection(), "search", {"q": "x"})
    assert fake_session.calls == [("search", {"q": "x"})]
    assert result.content
```

- [ ] **Step 5: Implement list/call and error mapping**

Follow `tools/list` cursors until empty; reject a repeated cursor. Map `httpx.TimeoutException`/SDK timeouts to `McpServerTimeout`, connection/TLS errors to `McpServerUnavailable`, malformed initialize/list to `McpProtocolError`, and upstream call errors to `McpToolError`. Error strings must not include headers.

- [ ] **Step 6: Run tests and commit**

Run: `PYTHONPATH=. uv run pytest backend/tests/test_mcp_client.py -q`  
Expected: PASS.

```bash
git add backend/packages/harness/agentkit/mcp/client.py backend/tests/test_mcp_client.py
git commit -m "feat: add per-server MCP client for streamable http and sse"
```

---

### Task 3: Server service and `/api/mcp-servers` CRUD

**Files:**
- Create: `backend/packages/harness/agentkit/mcp/service.py`
- Create: `backend/app/gateway/models/mcp_servers.py`
- Create: `backend/app/gateway/routers/mcp_servers.py`
- Create: `backend/app/gateway/mcp_servers_deps.py`
- Create: `backend/app/gateway/mcp_servers_wire.py` (header masking/restoration helpers)
- Test: `backend/tests/test_mcp_servers_service.py`
- Test: `backend/tests/test_mcp_servers_router.py`

**Interfaces:**
- Produces `McpServerWrite(name, display_name, description, url, transport, headers, verify_tls, enabled)`.
- Produces `McpServerSync` protocol with `sync_server(server_id, reason="server_refresh")` and `remove_server_catalog(server_id)`.
- Produces `McpServerService(store, cipher, client, sync, tool_store, policy)` with:
  - `list_servers(owner_user_id)`, `get_server(server_id, owner_user_id)`
  - `create_server(owner_user_id, data)`, `update_server(server_id, owner_user_id, data)`
  - `delete_server(server_id, owner_user_id)`, `set_enabled(server_id, owner_user_id, enabled)`
  - `refresh_server(server_id, owner_user_id)`, `list_tools(server_id, owner_user_id)`
  - `masked_headers(record) -> dict[str, str]`
- Produces router endpoints `GET/POST /api/mcp-servers`, `GET/PUT/DELETE /api/mcp-servers/{id}`, `POST /{id}/state`, `POST /{id}/refresh`, `GET /{id}/tools`, `GET /status`, `POST /reindex`.
- Scope names: `mcp_servers:read` and `mcp_servers:write`.

- [ ] **Step 1: Write failing service tests**

```python
async def test_create_encrypts_headers_and_never_returns_them(service, store) -> None:
    record = await service.create_server("u1", write(headers={"Authorization": "Bearer secret"}))
    assert record.owner_user_id == "u1"
    stored = await store.get(record.id, "u1")
    assert stored.headers_encrypted and "secret" not in stored.headers_encrypted
    assert service.masked_headers(stored)["Authorization"] == "****"

async def test_owner_cannot_read_another_users_server(service) -> None:
    record = await service.create_server("u1", write())
    with pytest.raises(McpServerNotFound):
        await service.get_server(record.id, "u2")
```

- [ ] **Step 2: Run and confirm failure**

Run: `PYTHONPATH=. uv run pytest backend/tests/test_mcp_servers_service.py -q`  
Expected: FAIL because the service module does not exist.

- [ ] **Step 3: Implement URL/header validation and CRUD orchestration**

Add a `ServerUrlPolicy` with:
- scheme allowed by `allowed_schemes` (default `https`);
- reject `localhost`, loopback, link-local, private IP literals unless `allow_private_networks`;
- reject userinfo and fragments; accept path/query.
Validate header names with `[A-Za-z0-9!#$%&'*+.^_`|~-]+`, reject CRLF, cap count at 32 and total value bytes at 32 KiB.
CRUD: check owner/name conflict, encrypt headers, create a `pending` row, call `sync.sync_server(record.id)`, return the refreshed row; on sync failure return the row with `status=error` and a sanitized `last_error`.

- [ ] **Step 4: Write failing router tests**

Use `FastAPI()` + `install_error_handlers` + a fake service, following `test_tools_router.py`. Assert:
- list/detail never serializes `headers_encrypted`;
- `PUT` cannot change `name`;
- `DELETE` calls `remove_server_catalog`;
- a 404 for another owner's id;
- `GET /{id}/tools` returns summaries only.

- [ ] **Step 5: Implement the wire models and router**

Use `ConfigDict(extra="forbid")` on write/state models. `McpServerDetail` includes `headers: dict[str,str]` (masked), `tool_count`, `status`, `reachable`, `last_error`, `last_tools_sync_at`. `McpServerToolSummary` includes `name`, `call_name`, `display_name`, `description`, `parameters`, `callable`, `enabled`. Static routes (`/status`, `/reindex`) must be declared before `/{server_id}`.

- [ ] **Step 6: Run tests and commit**

Run: `PYTHONPATH=. uv run pytest backend/tests/test_mcp_servers_service.py backend/tests/test_mcp_servers_router.py -q`  
Expected: PASS.

```bash
git add backend/packages/harness/agentkit/mcp/service.py backend/app/gateway/models/mcp_servers.py \
  backend/app/gateway/routers/mcp_servers.py backend/app/gateway/mcp_servers_deps.py \
  backend/tests/test_mcp_servers_service.py backend/tests/test_mcp_servers_router.py
git commit -m "feat: add MCP server CRUD service and API"
```

---

### Task 4: `mcp_tools` table and snapshot store

**Files:**
- Modify: `backend/packages/harness/agentkit/mcp/store/model.py`
- Create: `backend/packages/harness/agentkit/mcp/store/tool_repository.py`
- Create: `backend/packages/harness/agentkit/persistence/migrations/versions/0019_mcp_tools.py`
- Modify: `backend/packages/harness/agentkit/persistence/schema.py`
- Test: `backend/tests/test_mcp_tool_store.py`
- Test: `backend/tests/test_mcp_migration_0019.py`

**Interfaces:**
- Produces `McpToolRecord(id, server_id, owner_user_id, name, call_name, display_name, description, input_schema, annotations, parameters, enabled, reachable, callable, content, content_hash, schema_hash, last_seen_at)`.
- Produces `McpToolSnapshotStats(seen, inserted, updated, unchanged, disabled, upsert_ids, delete_ids)`.
- Produces `call_name_for(server_name, tool_name) -> str`, `is_safe_call_name(name) -> bool`, `content_hash_for(...)`, `schema_hash_for(...)`.
- Produces `McpToolStore(session_factory)` with `upsert_snapshot(records, seen_ids)`, `get_by_call_name(call_name, owner_user_id)`, `get_by_ids(ids, owner_user_id)`, `list_all(owner_user_id=None)`, `list_for_server(server_id, owner_user_id)`, `delete_by_server(server_id) -> list[str]`.

- [ ] **Step 1: Write failing store tests**

```python
async def test_call_name_and_callable_gate(store) -> None:
    record = tool_record(server_id="s1", server_name="anysearch", name="search", owner="u1")
    assert call_name_for("anysearch", "search") == "mcp__anysearch__search"
    await store.upsert_snapshot([record], seen_ids={record.id})
    assert (await store.get_by_call_name(record.call_name, "u1")).callable is True
    assert await store.get_by_call_name(record.call_name, "u2") is None

async def test_unseen_and_disabled_are_not_callable(store) -> None:
    record = tool_record(enabled=False)
    await store.upsert_snapshot([record], seen_ids={record.id})
    assert (await store.list_all())[0].callable is False
```

- [ ] **Step 2: Run and confirm failure**

Run: `PYTHONPATH=. uv run pytest backend/tests/test_mcp_tool_store.py -q`  
Expected: FAIL because the new store does not exist.

- [ ] **Step 3: Implement the ORM model and snapshot writer**

Copy the invariants from `contextforge/store/repository.py`:
- `id = uuid5(NAMESPACE_URL, server_id + ":" + name)`;
- `call_name = "mcp__" + server_name + "__" + name`;
- `callable = seen and enabled and reachable and owner and safe call_name`;
- `last_seen_at` only moves for seen tools;
- content/schema hashes are recomputed, never trusted;
- `UNIQUE(server_id, name)`, `UNIQUE(owner_user_id, call_name)`.
`upsert_snapshot` writes one transaction and returns changed IDs for Milvus.

- [ ] **Step 4: Write migration test and implement `0019_mcp_tools`**

`down_revision = "0018_mcp_servers"`; create `mcp_tools`, re-entrant; no backfill. The old tool table is dropped in Task 10.

- [ ] **Step 5: Run tests and commit**

Run: `PYTHONPATH=. uv run pytest backend/tests/test_mcp_tool_store.py backend/tests/test_mcp_migration_0019.py -q`  
Expected: PASS.

```bash
git add backend/packages/harness/agentkit/mcp/store/model.py backend/packages/harness/agentkit/mcp/store/tool_repository.py \
  backend/packages/harness/agentkit/persistence/migrations/versions/0019_mcp_tools.py \
  backend/packages/harness/agentkit/persistence/schema.py backend/tests/test_mcp_tool_store.py \
  backend/tests/test_mcp_migration_0019.py
git commit -m "feat: add mcp_tools snapshot store"
```

---

### Task 5: Per-server tool discovery sync

**Files:**
- Create: `backend/packages/harness/agentkit/mcp/sync.py`
- Test: `backend/tests/test_mcp_tool_sync.py`

**Interfaces:**
- Produces `McpSyncStats(scanned, servers_scanned, servers_failed, changed, upserted, deleted, error, index_failed, duration_ms)`.
- Produces `McpCatalogSync(server_store, tool_store, client, index=None, embedder=None)` with `async sync(reason="manual", full=False) -> McpSyncStats`.

- [ ] **Step 1: Write failing isolation test**

```python
async def test_one_failed_server_does_not_hide_the_other(sync, server_store, tool_store, client) -> None:
    good = await server_store.create(server_record(name="good"))
    bad = await server_store.create(server_record(name="bad"))
    client.results[good.id] = [mcp_tool("search")]
    client.errors[bad.id] = McpServerUnavailable("down")
    await tool_store.upsert_snapshot([old_tool(bad.id, "old")], seen_ids=set())

    stats = await sync.sync("test")

    assert stats.servers_scanned == 2
    assert stats.servers_failed == 1
    good_row = await tool_store.get_by_call_name("mcp__good__search", bad.owner_user_id)
    bad_row = await tool_store.get_by_call_name("mcp__bad__old", bad.owner_user_id)
    assert good_row.callable is True
    assert bad_row.callable is False
```

- [ ] **Step 2: Run and confirm failure**

Run: `PYTHONPATH=. uv run pytest backend/tests/test_mcp_tool_sync.py -q`  
Expected: FAIL because the sync service does not exist.

- [ ] **Step 3: Implement snapshot construction**

Load all servers. For each enabled server, call `client.list_tools(connection)`; build `McpToolRecord` values with server-derived owner, enabled/reachable, `call_name_for`, content, parameters. On failure, carry prior rows for that server with `reachable=False` and exclude them from `seen_ids`. Disabled servers also carry prior rows as unseen. Use `build_tool_content` from `agentkit/contextforge/catalog.py` (copy it into `agentkit/mcp/catalog.py`) with server name included.

- [ ] **Step 4: Write failing duplicate-name/pagination tests**

```python
async def test_same_tool_name_on_two_servers_gets_distinct_call_names(...):
    ...
    assert one.call_name == "mcp__alpha__search"
    assert two.call_name == "mcp__beta__search"
```

- [ ] **Step 5: Implement DB commit + Milvus index**

Call `tool_store.upsert_snapshot(records, seen_ids)`; then `index.sync(await tool_store.list_all(), embedder=..., full=full)` when index/embedder exist. Index failure sets `index_failed` but does not undo DB. Log only server id/name and exception class.

- [ ] **Step 6: Run tests and commit**

Run: `PYTHONPATH=. uv run pytest backend/tests/test_mcp_tool_sync.py -q`  
Expected: PASS.

```bash
git add backend/packages/harness/agentkit/mcp/sync.py backend/packages/harness/agentkit/mcp/catalog.py \
  backend/tests/test_mcp_tool_sync.py
git commit -m "feat: sync tools from each MCP server"
```

---

### Task 6: Milvus index, search service, and `tool_search`

**Files:**
- Create: `backend/packages/harness/agentkit/mcp/index.py`
- Modify: `backend/packages/harness/agentkit/tools/search/service.py`
- Modify: `backend/packages/harness/agentkit/tools/search/__init__.py`
- Modify: `backend/packages/harness/agentkit/tools/builtin/tool_search.py`
- Test: `backend/tests/test_mcp_index.py`
- Test: `backend/tests/test_tool_search_tool.py`

**Interfaces:**
- Produces `McpToolSearchService(config, index, store, embedder, reranker)`; same `search(task_query, domain_keywords, user_id)` outcome shape as the old service.
- `ToolSearchCard` gains `server_name: str`; `tool_name` is the remote name; `call_name` is `mcp__server__tool`.
- `tool_search` cards include `server_name`.

- [ ] **Step 1: Write failing index tests**

Assert the collection is `myflow_mcp_tools`, output fields include `server_id`/`server_name`, and the owner filter is on both dense and sparse requests. Reuse the old index tests with the new record type.

- [ ] **Step 2: Implement `McpToolVectorIndex`**

Copy `contextforge/index.py`; replace record imports, collection name, and scalar fields. Add `server_id` and `server_name` to output fields and `ToolVectorHit`. Keep the two-channel owner filter.

- [ ] **Step 3: Write failing search-card tests**

```python
async def test_card_uses_mcp_namespace_and_server_name(...):
    outcome = await service.search("search code", user_id="u1")
    assert outcome.hits[0].call_name == "mcp__anysearch__search"
    assert outcome.hits[0].server_name == "anysearch"
```

- [ ] **Step 4: Adapt the search service and `tool_search` card**

Rename the service class/imports; add `server_name` to `ToolSearchCard`; keep budgets, owner filter, rerank fallback, and run-scoped name write. Update all user-facing messages from ContextForge to MCP.

- [ ] **Step 5: Run tests and commit**

Run: `PYTHONPATH=. uv run pytest backend/tests/test_mcp_index.py backend/tests/test_tool_search_tool.py -q`  
Expected: PASS.

```bash
git add backend/packages/harness/agentkit/mcp/index.py backend/packages/harness/agentkit/tools/search \
  backend/packages/harness/agentkit/tools/builtin/tool_search.py \
  backend/tests/test_mcp_index.py backend/tests/test_tool_search_tool.py
git commit -m "feat: search MCP tools by server-aware call names"
```

---

### Task 7: Direct `tool_execute` through the tool's MCP server

**Files:**
- Modify: `backend/packages/harness/agentkit/tools/builtin/tool_execute.py`
- Create: `backend/packages/harness/agentkit/mcp/runtime_protocol.py`
- Test: `backend/tests/test_tool_execute_tool.py`

**Interfaces:**
- Produces `McpRuntimeProtocol` with `tool_store`, `server_store`, and `async call_tool(record: McpToolRecord, arguments: dict) -> mcp.types.CallToolResult`.
- `runtime_protocol.py` owns the process-global `get_mcp_runtime()`, `set_mcp_runtime(runtime)`, and `reset_mcp_runtime()`; Task 8's runtime calls `set_mcp_runtime`, and `tool_execute` imports `get_mcp_runtime` from here.
- `tool_execute` keeps the existing `require_retrieved`, jsonschema, origin, and metadata behavior; only the lookup/store and call path change.
- Origins rename `contextforge_unavailable` to `mcp_unavailable`; add `server_unavailable` and `server_disabled`.

- [ ] **Step 1: Write failing direct-call test**

```python
async def test_execute_calls_the_owning_server(monkeypatch) -> None:
    runtime = FakeRuntime(record=tool_record(server_id="s1", name="search"))
    monkeypatch.setattr(tool_execute_module, "get_mcp_runtime", lambda: runtime)
    await tool_execute.ainvoke({"tool_name": "mcp__anysearch__search", "arguments": {"q": "x"}})
    assert runtime.calls == [("s1", "search", {"q": "x"})]
```

- [ ] **Step 2: Replace the runtime/store lookup**

Use `runtime.tool_store.get_by_call_name(call_name, owner)`; the row carries `server_id` and remote `name`. Remove all external admin/MCP gateway references. Keep the post-refresh reread and schema-change reply.

- [ ] **Step 3: Implement refresh-and-retry rules**

Only `McpToolNotFound` and schema/argument mismatch trigger `runtime.refresh_server(record.server_id)` then one retry. Timeout, unavailable, and upstream tool errors are final for the call. Preserve `origin=local_validation` and the retry metadata.

- [ ] **Step 4: Write failure-origin tests**

Cover: not retrieved, other-owner/non-callable row, disabled server, schema unresolved, timeout no retry, not-found one retry, upstream isError no retry. Assert response/tool meta origins and that no header/token appears in the message.

- [ ] **Step 5: Run tests and commit**

Run: `PYTHONPATH=. uv run pytest backend/tests/test_tool_execute_tool.py -q`  
Expected: PASS.

```bash
git add backend/packages/harness/agentkit/tools/builtin/tool_execute.py \
  backend/packages/harness/agentkit/mcp/runtime_protocol.py backend/tests/test_tool_execute_tool.py
git commit -m "feat: execute MCP tools directly against their server"
```

---

### Task 8: Runtime, app wiring, and agent tool assembly

**Files:**
- Create: `backend/packages/harness/agentkit/mcp/runtime.py`
- Modify: `backend/app/gateway/app.py`
- Modify: `backend/app/gateway/errors.py`
- Modify: `backend/app/gateway/routers/__init__.py`
- Modify: `backend/packages/harness/agentkit/tools/registry.py`
- Modify: `backend/packages/harness/agentkit/agents/middlewares/assembly.py`
- Modify: `backend/packages/harness/agentkit/agents/leader_agent/prompt.py`
- Modify: `backend/packages/harness/agentkit/skills/tool_policy.py`
- Test: `backend/tests/test_mcp_runtime.py`
- Test: `backend/tests/test_mcp_worker_guard.py`

**Interfaces:**
- Produces `McpToolRuntime(config, sync, server_store, tool_store, mcp_client, cipher, index=None, embedder=None, reranker=None, daily_delay=None)` with `start()`, `stop()`, `request_refresh(reason)`, `refresh_now(reason)`, `refresh_server(server_id)`, `remove_server_catalog(server_id)`, `reindex()`, `status(owner_user_id=None)`, and `async call_tool(record, arguments)`.
- Produces `build_mcp_runtime(config, session_factory) -> McpToolRuntime | None`; it publishes via `set_mcp_runtime()` and clears via `reset_mcp_runtime()` from `runtime_protocol.py`.
- App startup uses `mcp_servers.enabled`; single-worker guard fails when enabled and workers > 1.
- `get_available_tools` hides `tool_search`/`tool_execute` unless `app_config.mcp_servers.enabled`.

- [ ] **Step 1: Write failing runtime tests**

```python
async def test_start_syncs_and_status_counts_servers(runtime, sync, server_store, tool_store) -> None:
    await runtime.start()
    assert sync.reasons == ["startup"]
    status = await runtime.status()
    assert status["servers_total"] == 1
    assert status["indexed_tools"] == 1
```

- [ ] **Step 2: Implement the runtime**

Adapt `contextforge/runtime.py`: remove admin/subscriber/debounce event logic; keep startup sync, daily task, sync lock trigger, retry record, status, and process-global getters. `call_tool` looks up the server, checks owner/enabled/reachable, decrypts headers, builds `McpServerConnection`, and calls `McpServerClient.call_tool(record.name, arguments)`.

- [ ] **Step 3: Write failing app/guard tests**

Assert `preflight_mcp_servers` rejects workers > 1 only when enabled, and app startup registers `app.state.mcp_runtime` + mounts `/api/mcp-servers`. Test disabled config publishes no tool search service.

- [ ] **Step 4: Wire app startup/stop and errors**

Replace the old start/stop/preflight calls in `app.py`. Install `McpServerNotFound`, `McpServerConflict`, `McpServerValidationError`, `McpServerUnavailable`, `McpServerTimeout`, `McpToolError` in `install_error_handlers` with coded 4xx/5xx responses. Mount the new router.

- [ ] **Step 5: Update agent assembly/prompt/policy namespaces**

Replace ContextForge text with MCP text; update `cf__` examples to `mcp__`; keep tool_search/tool_execute policy behavior unchanged. `tool_search` and `tool_execute` are available only when `mcp_servers.enabled`.

- [ ] **Step 6: Run tests and commit**

Run: `PYTHONPATH=. uv run pytest backend/tests/test_mcp_runtime.py backend/tests/test_mcp_worker_guard.py backend/tests/test_tool_execute_tool.py backend/tests/test_tool_search_tool.py -q`  
Expected: PASS.

```bash
git add backend/packages/harness/agentkit/mcp/runtime.py backend/app/gateway/app.py backend/app/gateway/errors.py \
  backend/app/gateway/routers/__init__.py backend/packages/harness/agentkit/tools/registry.py \
  backend/packages/harness/agentkit/agents/middlewares/assembly.py \
  backend/packages/harness/agentkit/agents/leader_agent/prompt.py backend/packages/harness/agentkit/skills/tool_policy.py \
  backend/tests/test_mcp_runtime.py backend/tests/test_mcp_worker_guard.py
git commit -m "feat: wire MCP server runtime and agent tools"
```

---

### Task 9: Frontend MCP Server management

**Files:**
- Create: `frontend/src/core/api/mcp-servers.ts`
- Create: `frontend/src/components/mcp-servers/mcp-server-management-page.tsx`
- Create: `frontend/src/components/mcp-servers/mcp-server-editor-dialog.tsx`
- Create: `frontend/src/app/workspace/mcp-servers/page.tsx`
- Modify: `frontend/src/app/workspace/tools/page.tsx` (redirect to `/workspace/mcp-servers`)
- Modify: `frontend/src/components/workspace/sidebar.tsx`
- Modify: `frontend/src/app/workspace/sidebar-container.tsx`
- Delete after the new page passes: `frontend/src/components/tools/tool-management-page.tsx`, `frontend/src/components/tools/tool-editor-dialog.tsx`
- Test: `frontend/src/core/api/mcp-servers.test.ts`
- Test: `frontend/src/components/mcp-servers/mcp-server-management-page.dom.test.tsx`
- Test: `frontend/src/components/mcp-servers/mcp-server-editor-dialog.dom.test.tsx`
- Modify: `frontend/src/components/workspace/sidebar.dom.test.tsx`

**Interfaces:**
- `listMcpServers()`, `getMcpServer(id)`, `createMcpServer(input)`, `updateMcpServer(id, input)`, `deleteMcpServer(id)`, `setMcpServerEnabled(id, enabled)`, `refreshMcpServer(id)`, `listMcpServerTools(id)`, `getMcpServerStatus()`, `reindexMcpServers()`.
- `McpServerInput { name, display_name, description, url, transport, headers, verify_tls, enabled }`.
- Form fields: name (read-only on edit), display_name, description, url, transport (`streamable_http`/`sse`), header rows, verify_tls, enabled.
- No tool create/edit/claim controls anywhere.

- [ ] **Step 1: Write failing API client test**

```ts
it("creates a streamable server with headers", async () => {
  mockedFetch.mockResolvedValue(jsonResponse(serverDetail));
  await createMcpServer({ name: "anysearch", url: "https://api.anysearch.com/mcp",
    transport: "streamable_http", headers: { Authorization: "Bearer x" } });
  expect(JSON.parse(mockedFetch.mock.calls[0][1].body)).toMatchObject({
    name: "anysearch", transport: "streamable_http",
    headers: { Authorization: "Bearer x" },
  });
});
```

- [ ] **Step 2: Implement the API client and run it**

Use `request<T>`/error-body parsing from `core/api/tools.ts`; do not reintroduce tool write calls. Run: `cd frontend && pnpm test -- src/core/api/mcp-servers.test.ts`.

- [ ] **Step 3: Write failing page/editor DOM tests**

Assert the sidebar link says “MCP 服务”, the page lists server status/tool count, the editor has `STREAMABLEHTTP`/`SSE` options, masked header `****` is submitted unchanged, and no tool editor/claim button exists.

- [ ] **Step 4: Implement the page and editor**

Follow the existing tool page's query/mutation/error patterns; use React Query keys `["mcp-servers"]`, `["mcp-servers", id, "tools"]`, `["mcp-servers", "status"]`. On create/update success invalidate the list and detail; on refresh/reindex invalidate tools/status. Show `last_error`, `reachable`, `tool_count`.

- [ ] **Step 5: Route/sidebar cutover**

Rename the sidebar prop `toolsActive` to `mcpServersActive` (update tests). `/workspace/tools` redirects to `/workspace/mcp-servers`; update `metadata.title` to “MCP 服务”.

- [ ] **Step 6: Run frontend tests and typecheck**

Run: `cd frontend && pnpm test -- src/core/api/mcp-servers.test.ts src/components/mcp-servers src/components/workspace/sidebar.dom.test.tsx`  
Expected: PASS.  
Run: `cd frontend && pnpm typecheck`  
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/core/api/mcp-servers.ts frontend/src/core/api/mcp-servers.test.ts \
  frontend/src/components/mcp-servers frontend/src/app/workspace/mcp-servers \
  frontend/src/app/workspace/tools/page.tsx frontend/src/components/workspace/sidebar.tsx \
  frontend/src/app/workspace/sidebar-container.tsx frontend/src/components/workspace/sidebar.dom.test.tsx
git commit -m "feat: manage MCP servers from the workspace"
```

---

### Task 10: Remove the old gateway path and verify end to end

**Files:**
- Delete: `backend/packages/harness/agentkit/contextforge/`
- Delete: `backend/packages/harness/agentkit/config/contextforge_config.py`
- Delete: `backend/app/gateway/routers/tools.py`, `models/tools.py`, `tools_deps.py`, `tools_wire.py`
- Delete: `frontend/src/components/tools/`, `frontend/src/core/api/tools.ts`
- Delete: old `backend/tests/test_contextforge_*`, `test_tools_router.py`; the old search preview route/tests are removed with the tool management API
- Create: `backend/packages/harness/agentkit/persistence/migrations/versions/0020_drop_old_contextforge_tools.py`
- Modify: `backend/packages/harness/agentkit/config/app_config.py`, `config.example.yaml`, `docker/docker-compose.yml`, `docker/backend-compose.yml`, `backend/pyproject.toml`, `README.md`
- Test: full backend + frontend suites

**Interfaces:**
- No runtime import may contain `contextforge`, `cf__`, `ContextForge`, `/api/tools`, or the old admin/agent tokens.
- Migration 0020 drops the old tool table when present; `downgrade()` is a no-op or recreates nothing (old data is intentionally discarded).

- [ ] **Step 1: Remove old imports and mounts**

Use `rg -n "contextforge|ContextForge|cf__|/api/tools"` across `backend`, `frontend/src`, `config.example.yaml`, `docker`. Remove old app wiring, error registrations, router include, config fields, tests, and frontend page. The new `/api/mcp-servers` path is the only management API.

- [ ] **Step 2: Add migration 0020**

`down_revision = "0019_mcp_tools"`; drop `contextforge_tools` with `checkfirst` if present. Do not migrate old rows.

- [ ] **Step 3: Update config and Docker**

Replace the old gateway block with `mcp_servers:` from the spec. Add `MYFLOW_MCP_SERVER_KEY` to `.env`/compose; remove external gateway services, its DB init, admin/agent tokens, and the `agentkit[contextforge]` extra if nothing else uses it. Keep `mcp` and `cryptography` dependencies.

- [ ] **Step 4: Update docs and config example**

Point README/config.example to `mcp_servers`, `tool_search`, `myflow_mcp_tools`, and `MYFLOW_MCP_SERVER_KEY`. State that old data/collection is intentionally discarded.

- [ ] **Step 5: Run full backend suite**

Run: `PYTHONPATH=. uv run pytest backend/tests -q`  
Expected: all pass (live MCP/Milvus integration tests may skip without services).

- [ ] **Step 6: Run frontend suite and typecheck**

Run: `cd frontend && pnpm test`  
Expected: all pass.  
Run: `cd frontend && pnpm typecheck`  
Expected: no errors.

- [ ] **Step 7: Grep the removed surface and commit**

Run: `rg -n "contextforge|ContextForge|cf__|/api/tools" backend frontend/src config.example.yaml docker || true`  
Expected: no runtime/config matches (historical `docs/` is allowed).

```bash
git add -A
git commit -m "refactor: remove the external gateway and finish MCP server management"
```

---

## Execution Notes

- Tasks 1-8 are backend-only; keep the old runtime running until Task 8's cutover commit, then run the full suite before Task 9.
- Tasks 6-8 may be executed with `subagent-driven-development`: one subagent per task, fresh eyes on the Interfaces block.
- Do not commit generated keys or raw headers. Use a local `MYFLOW_MCP_SERVER_KEY` only in `.env` (ignored).
- Live MCP/Milvus tests are opt-in; unit tests use fake sessions and fake stores.
