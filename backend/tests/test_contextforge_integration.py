"""Opt-in ContextForge and Milvus integration checks.

These tests never run in CI by default: they need a live ContextForge and, for
the catalog / index round-trip, a disposable Milvus instance. Set the
environment variables below to exercise the real deployment contract:

    CONTEXTFORGE_TEST_URL=http://127.0.0.1:4444
    CONTEXTFORGE_TEST_TOKEN=...
    CONTEXTFORGE_TEST_MCP_URL=...        # optional; default <url>/mcp
    MILVUS_TEST_URI=http://127.0.0.1:19530
    MILVUS_TEST_DIMENSIONS=8              # must match the disposable collection
    MILVUS_TEST_TOKEN=...                 # optional
    MILVUS_TEST_DB=...                    # optional

The catalog test builds a temporary SQLite `contextforge_tools` store from the
live ContextForge views, indexes it into Milvus, and then drives the real
`tool_search` and `tool_execute` built-ins. The executor uses a recording MCP
stand-in so the test never runs an author's tool upstream.
"""

from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

CF_URL = os.getenv("CONTEXTFORGE_TEST_URL", "").rstrip("/")
CF_TOKEN = os.getenv("CONTEXTFORGE_TEST_TOKEN", "")
CF_MCP_URL = os.getenv("CONTEXTFORGE_TEST_MCP_URL", "")
MILVUS_URI = os.getenv("MILVUS_TEST_URI", "")

cf_required = pytest.mark.skipif(
    not CF_URL or not CF_TOKEN,
    reason="set CONTEXTFORGE_TEST_URL and CONTEXTFORGE_TEST_TOKEN to run",
)
milvus_required = pytest.mark.skipif(
    not MILVUS_URI,
    reason="set MILVUS_TEST_URI to run the Milvus round-trip",
)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {CF_TOKEN}"}


def _config(**overrides: Any) -> Any:
    """The one ContextForge config the integration checks share."""
    from agentkit.config.contextforge_config import ContextForgeConfig

    dimensions = int(os.getenv("MILVUS_TEST_DIMENSIONS", "8"))
    base: dict[str, Any] = {
        "enabled": True,
        "base_url": CF_URL,
        "admin_token": CF_TOKEN,
        "mcp_url": CF_MCP_URL,
        "search": {
            "enabled": True,
            "top_k": 5,
            # Keyword-only retrieval: the integration host does not need a live
            # embedding service, while the index still stores dense vectors.
            "embedding": {
                "enabled": False,
                "base_url": "http://embedding.invalid",
                "model": "integration-probe",
                "dimensions": dimensions,
            },
            "keyword": {"enabled": True},
            "vector_store": {
                "uri": MILVUS_URI,
                "token": os.getenv("MILVUS_TEST_TOKEN", ""),
                "db_name": os.getenv("MILVUS_TEST_DB", ""),
            },
        },
        "sync": {"startup_sync": False},
        "admin_events": {"enabled": False},
    }
    base.update(overrides)
    return ContextForgeConfig(**base)


class ConstantEmbedder:
    """A deterministic document embedder so the index round-trip needs no model."""

    def __init__(self, dimensions: int) -> None:
        self._dimensions = int(dimensions)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.01] * self._dimensions for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        return [0.01] * self._dimensions


class RecordingMcp:
    """Records tools/call instead of executing an author's tool upstream."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        timeout: float | None = None,
    ) -> Any:
        from mcp import types as mcp_types

        self.calls.append((name, dict(arguments)))
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="integration ok")],
            isError=False,
        )


def _resolve(schema: Any, root: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        node: Any = root
        for part in ref[2:].split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        return node if isinstance(node, dict) else {}
    return schema


def _minimal_value(schema: Any, root: dict[str, Any]) -> Any:
    """One value that satisfies the common JSON-Schema shapes MCP tools publish."""
    schema = _resolve(schema, root)
    if "const" in schema:
        return schema["const"]
    if isinstance(schema.get("default"), (str, int, float, bool)):
        return schema["default"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if isinstance(options, list) and options:
            return _minimal_value(options[0], root)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "string")
    if kind == "object":
        properties = schema.get("properties") or {}
        return {
            name: _minimal_value(properties.get(name, {}), root)
            for name in schema.get("required", [])
            if isinstance(name, str)
        }
    if kind == "array":
        return [_minimal_value(schema.get("items", {}), root)]
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    return ""


def _minimal_arguments(input_schema: Any) -> dict[str, Any]:
    schema = input_schema if isinstance(input_schema, dict) else {}
    arguments = _minimal_value(schema, schema)
    return arguments if isinstance(arguments, dict) else {}


def _probe_record(*, owner: str, name: str) -> Any:
    from agentkit.contextforge.store import (
        ContextForgeToolRecord,
        call_name_for,
        content_hash_for,
    )

    content = f"{name}\n{name}\nContextForge integration probe for tool retrieval"
    return ContextForgeToolRecord(
        id="tool-" + name,
        name=name,
        call_name=call_name_for(name),
        display_name="Integration Probe",
        description="Temporary tool document created by the opt-in integration test.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        tags=("integration",),
        parameters=("query",),
        owner_user_id=owner,
        enabled=True,
        reachable=True,
        callable=True,
        content=content,
        content_hash=content_hash_for(content, owner, call_name_for(name)),
    )


@cf_required
@pytest.mark.asyncio
async def test_admin_events_returns_sse_or_reports_why_not() -> None:
    """The subscription is optional in ContextForge; 404/403 must be explicit."""
    async with httpx.AsyncClient(base_url=CF_URL, headers=_headers(), timeout=20.0) as client:
        async with client.stream("GET", "/admin/events") as response:
            if response.status_code in {403, 404}:
                pytest.skip(
                    "ContextForge has no admin events endpoint: enable "
                    "MCPGATEWAY_ADMIN_API_ENABLED and grant admin.events"
                )
            assert response.status_code == 200, response.text
            assert "text/event-stream" in response.headers.get("content-type", "")


@cf_required
@pytest.mark.asyncio
async def test_mcp_tools_list_matches_admin_catalog() -> None:
    """MCP is the runtime source of truth; the admin catalog must not drift."""
    from agentkit.contextforge.admin import ContextForgeAdminClient
    from agentkit.contextforge.mcp_client import ContextForgeMcpClient

    config = _config(search={"enabled": False})
    mcp = ContextForgeMcpClient(config)
    admin = ContextForgeAdminClient(config)
    try:
        try:
            mcp_tools = await mcp.list_tools()
        except Exception as exc:  # noqa: BLE001 - opt-in smoke test reports the cause
            pytest.skip(f"MCP endpoint is not reachable: {type(exc).__name__}: {exc}")
        catalog = await admin.list_tools(limit=200, include_inactive=True)
    finally:
        await admin.aclose()

    admin_names = {tool.name for tool in catalog}
    mcp_names = {tool.name for tool in mcp_tools}
    assert mcp_names, "MCP endpoint returned no tools"
    assert mcp_names <= admin_names, (
        "MCP tools missing from the admin catalog: "
        + ", ".join(sorted(mcp_names - admin_names))
    )


@milvus_required
@pytest.mark.asyncio
async def test_tool_index_sync_and_query_round_trip() -> None:
    """Write one uniquely named record, recall it, then delete only that row."""
    from agentkit.config.contextforge_config import TOOL_COLLECTION, ToolVectorStoreConfig
    from agentkit.contextforge.index import ToolVectorIndex

    dimensions = int(os.getenv("MILVUS_TEST_DIMENSIONS", "8"))
    name = "myflow_integration_" + uuid.uuid4().hex[:12]
    record = _probe_record(owner="u-integration", name=name)
    config = ToolVectorStoreConfig(
        uri=MILVUS_URI,
        token=os.getenv("MILVUS_TEST_TOKEN", ""),
        db_name=os.getenv("MILVUS_TEST_DB", ""),
    )
    index = ToolVectorIndex(config, dimensions=dimensions)
    try:
        try:
            await index.ensure_collection()
        except Exception as exc:  # noqa: BLE001 - skip when the collection is incompatible
            pytest.skip(f"existing {TOOL_COLLECTION} collection is not compatible: {exc}")

        stats = await index.sync([record], embedder=ConstantEmbedder(dimensions))
        assert stats.error is None, stats
        keyword_hits = await index.keyword_search(
            query=name, owner_user_id=record.owner_user_id, limit=10
        )
        assert record.id in {hit.tool_id for hit in keyword_hits}
    finally:
        try:
            await index.delete_by_ids([record.id])
        except Exception:  # noqa: BLE001 - best-effort cleanup of an opt-in test
            pass
        await index.aclose()


@cf_required
@milvus_required
@pytest.mark.asyncio
async def test_the_live_catalog_drives_tool_search_and_tool_execute(tmp_path: Any) -> None:
    """Register live ContextForge tools, then retrieve and gate them in-process.

    `contextforge_tools` is rebuilt from the live MCP + Admin views into a
    throw-away SQLite database, the derived index is written, and the two real
    built-ins run against the result: `tool_search` returns a `cf__` card and
    `tool_execute` accepts exactly that call name, re-reads the stored schema
    and hands the unprefixed name to MCP.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from agentkit.contextforge.admin import ContextForgeAdminClient
    from agentkit.contextforge.index import ToolVectorIndex
    from agentkit.contextforge.mcp_client import ContextForgeMcpClient
    from agentkit.contextforge.runtime import (
        configure_contextforge_runtime,
        reset_contextforge_runtime,
    )
    from agentkit.contextforge.store import ContextForgeToolStore
    from agentkit.contextforge.sync import ContextForgeSyncService
    from agentkit.persistence.schema import Base
    from agentkit.tools.builtin.tool_execute import tool_execute
    from agentkit.tools.builtin.tool_search import tool_search
    from agentkit.tools.search.service import (
        ContextForgeToolSearchService,
        reset_tool_search_runtime,
        set_tool_search_runtime,
    )

    dimensions = int(os.getenv("MILVUS_TEST_DIMENSIONS", "8"))
    config = _config()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'contextforge.db'}")
    admin = ContextForgeAdminClient(config)
    mcp = ContextForgeMcpClient(config)
    index = ToolVectorIndex(
        config.search.vector_store,
        dimensions=dimensions,
        keyword=config.search.keyword,
        rrf_k=config.search.rrf_k,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    store = ContextForgeToolStore(session_factory)
    recorder = RecordingMcp()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sync = ContextForgeSyncService(
            config,
            admin,
            mcp,
            store,
            index,
            ConstantEmbedder(dimensions),
        )
        stats = await sync.sync("integration", full=True)
        assert stats.error is None, stats

        rows = [
            row
            for row in await store.list_all(None)
            if row.callable and row.owner_user_id and row.call_name
        ]
        if not rows:
            pytest.skip("live ContextForge has no owned, callable tool to exercise")
        record = rows[0]
        owner = record.owner_user_id

        service = ContextForgeToolSearchService(
            config=config.search,
            index=index,
            store=store,
            embedder=None,
        )
        set_tool_search_runtime(service)
        try:
            update = await tool_search.coroutine(
                task_query=f"{record.name} {record.description}",
                domain_keywords=[],
                runtime=SimpleNamespace(context={"user_id": owner}, state={}),
                tool_call_id="cf-int-search",
            )
        finally:
            reset_tool_search_runtime()

        message = update["messages"][0]
        payload = json.loads(message.content)
        assert record.call_name in update["retrieved_tool_names"], payload
        cards = {card["call_name"]: card for card in payload["tools"]}
        assert record.call_name in cards, payload
        card = cards[record.call_name]
        assert card["tool_id"] == record.id
        assert card["input_schema"], card

        configure_contextforge_runtime(
            SimpleNamespace(store=store, mcp_client=recorder, config=config)
        )
        try:
            executed = await tool_execute.coroutine(
                tool_name=card["call_name"],
                arguments=_minimal_arguments(card["input_schema"]),
                runtime=SimpleNamespace(
                    context={"user_id": owner},
                    state={"retrieved_tool_names": list(update["retrieved_tool_names"])},
                ),
                tool_call_id="cf-int-execute",
            )
        finally:
            reset_contextforge_runtime()

        if executed.status == "error":
            failure = json.loads(executed.content)
            # The name passed the retrieval gate and the DB lookup; a schema the
            # generated arguments could not satisfy is still a local rejection,
            # never "not_retrieved" and never an upstream call.
            assert failure.get("origin") != "not_retrieved", failure
            assert recorder.calls == [], recorder.calls
        else:
            assert recorder.calls, "tool_execute did not dispatch through MCP"
            assert recorder.calls[0][0] == record.name
    finally:
        await admin.aclose()
        await index.aclose()
        await engine.dispose()
