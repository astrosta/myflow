"""Opt-in ContextForge and Milvus integration checks.

These tests never run in CI by default: they need a live ContextForge and,
for the index round-trip, a disposable Milvus instance. Set the environment
variables below to exercise the real deployment contract:

    CONTEXTFORGE_TEST_URL=http://127.0.0.1:4444
    CONTEXTFORGE_TEST_TOKEN=...
    CONTEXTFORGE_TEST_MCP_URL=...        # optional; default <url>/mcp
    MILVUS_TEST_URI=http://127.0.0.1:19530
    MILVUS_TEST_DIMENSIONS=8              # must match an existing collection
"""

from __future__ import annotations

import os
import uuid

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
    try:
        from langchain_mcp_adapters.tools import load_mcp_tools
    except ImportError:
        pytest.skip("langchain-mcp-adapters is not installed")

    from agentkit.config.contextforge_config import ContextForgeConfig
    from agentkit.contextforge.admin import ContextForgeAdminClient

    mcp_url = CF_MCP_URL or CF_URL + "/mcp"
    params = {
        "transport": "http",
        "url": mcp_url,
        "headers": _headers(),
        "timeout": 20.0,
    }
    try:
        tools = await load_mcp_tools(
            None,
            connection=params,
            server_name="contextforge-integration",
            tool_name_prefix=False,
        )
    except Exception as exc:  # noqa: BLE001 - opt-in smoke test reports the cause
        pytest.skip(f"MCP endpoint is not reachable: {type(exc).__name__}: {exc}")

    config = ContextForgeConfig(
        enabled=True,
        base_url=CF_URL,
        admin_token=CF_TOKEN,
        tool_loading="eager",
        search={"enabled": False},
    )
    admin = ContextForgeAdminClient(config)
    try:
        catalog = await admin.list_tools(limit=200, include_inactive=True)
    finally:
        await admin.aclose()

    admin_names = {tool.name for tool in catalog}
    mcp_names = {tool.name for tool in tools}
    assert mcp_names, "MCP endpoint returned no tools"
    assert mcp_names <= admin_names, (
        "MCP tools missing from the admin catalog: "
        + ", ".join(sorted(mcp_names - admin_names))
    )


@milvus_required
@pytest.mark.asyncio
async def test_tool_index_sync_and_query_round_trip() -> None:
    """Write one uniquely named document, recall it, then delete only that row."""
    try:
        from agentkit.config.contextforge_config import TOOLS_COLLECTION, ToolVectorStoreConfig
        from agentkit.contextforge.index import ToolCatalogDocument, ToolVectorIndex
    except ImportError:
        pytest.skip("pymilvus is not installed")

    dimensions = int(os.getenv("MILVUS_TEST_DIMENSIONS", "8"))
    name = "myflow_integration_" + uuid.uuid4().hex[:12]
    content = f"{name} ContextForge integration probe for tool retrieval"
    document = ToolCatalogDocument(
        name=name,
        display_name="Integration Probe",
        description="Temporary tool document created by the opt-in integration test.",
        tags=("integration",),
        parameters=("query",),
        server="contextforge",
        content=content,
        content_hash=uuid.uuid4().hex,
        updated_at=1,
    )

    class Embedder:
        async def embed_documents(self, texts):
            return [[0.01] * dimensions for _ in texts]

        async def embed_query(self, text):
            return [0.01] * dimensions

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
            pytest.skip(f"existing {TOOLS_COLLECTION} collection is not compatible: {exc}")

        await index.sync([document], embedder=Embedder())
        hits = await index.hybrid_search(
            vector=[0.01] * dimensions,
            query="ContextForge integration probe",
            limit=10,
        )
        assert name in {hit.tool_name for hit in hits}
    finally:
        client = index._client_instance()  # noqa: SLF001 - cleanup only this probe row
        try:
            client.delete(
                collection_name=TOOLS_COLLECTION,
                filter=f'tool_name == "{name}"',
            )
        except Exception:  # noqa: BLE001 - best-effort cleanup of an opt-in test
            pass
        await index.aclose()