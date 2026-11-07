import httpx
import pytest

from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.contextforge.admin import ContextForgeAdminClient
from agentkit.contextforge.models import (
    ContextForgeAuthError,
    ContextForgeTool,
    ToolWrite,
)


def _config(token: str = "adm") -> ContextForgeConfig:
    return ContextForgeConfig(
        enabled=True,
        base_url="http://cf",
        admin_token=token,
        tool_loading="eager",
        search={"enabled": False},
    )


@pytest.mark.asyncio
async def test_list_normalizes_camel_case_schema_and_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer adm"
        assert request.url.path == "/tools"
        return httpx.Response(
            200,
            json={
                "tools": [
                    {
                        "id": "t1",
                        "name": "github_search",
                        "displayName": "GitHub",
                        "description": "d",
                        "integration_type": "MCP",
                        "request_type": "STREAMABLEHTTP",
                        "inputSchema": "{\"type\": \"object\"}",
                        "headers": {"X-Env": "prod"},
                        "enabled": True,
                        "reachable": True,
                        "tags": [{"name": "github"}],
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    admin = ContextForgeAdminClient(_config(), client=http)
    tools = await admin.list_tools()
    assert tools[0].display_name == "GitHub"
    assert tools[0].input_schema == {"type": "object"}
    assert tools[0].headers == {"X-Env": "prod"}
    assert tools[0].tags == ("github",)
    await admin.aclose()
    await http.aclose()


@pytest.mark.asyncio
async def test_upstream_401_maps_to_auth_error_without_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "nope"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    admin = ContextForgeAdminClient(_config("secret"), client=http)
    with pytest.raises(ContextForgeAuthError) as exc:
        await admin.list_tools()
    assert "secret" not in str(exc.value)
    await admin.aclose()
    await http.aclose()


@pytest.mark.asyncio
async def test_create_and_update_payloads_use_upstream_names() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        seen.append(payload)
        tool = {
            "id": "t1",
            "name": "demo",
            "displayName": payload.get("displayName"),
            "description": payload.get("description"),
            "integration_type": payload.get("integration_type", "REST"),
            "request_type": payload.get("request_type", "SSE"),
            "input_schema": payload.get("inputSchema") or payload.get("input_schema") or {},
            "headers": payload.get("headers") or {},
            "enabled": True,
            "reachable": True,
        }
        return httpx.Response(200, json=tool)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    admin = ContextForgeAdminClient(_config(), client=http)
    write = ToolWrite(
        name="demo",
        display_name="Demo",
        description="desc",
        request_type="SSE",
        input_schema={"type": "object"},
    )
    await admin.create_tool(write)
    await admin.update_tool("t1", write)
    assert seen[0]["displayName"] == "Demo"
    assert seen[0]["inputSchema"] == {"type": "object"}
    assert seen[1]["displayName"] == "Demo"
    await admin.aclose()
    await http.aclose()

def test_tool_from_wire_reads_owner_annotation() -> None:
    from agentkit.contextforge.models import tool_from_wire

    tool = tool_from_wire(
        {
            "id": "t1",
            "name": "mine",
            "annotations": {"myflow_owner": "u1", "readOnlyHint": True},
        }
    )
    assert tool.owner == "u1"
    assert tool.annotations["readOnlyHint"] is True
