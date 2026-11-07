"""Load ContextForge MCP tools and tag them for deferred retrieval."""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool

from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.tools.contextforge_metadata import tag_contextforge_tool
from agentkit.tools.mcp_metadata import tag_mcp_tool

logger = logging.getLogger(__name__)

SERVER_NAME = "contextforge"


async def load_contextforge_tools(config: ContextForgeConfig) -> list[BaseTool]:
    url = config.resolved_mcp_url()
    if not config.enabled or not url:
        return []
    try:
        from agentkit.mcp.tools import load_server_tools
    except ImportError:
        logger.warning("MCP adapter is unavailable; ContextForge tools are skipped")
        return []

    headers = {}
    token = config.resolved_agent_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = {
        "transport": "http",
        "url": url,
        "headers": headers,
        "timeout": config.request_timeout_seconds,
    }
    try:
        tools = await load_server_tools(
            SERVER_NAME,
            params,
            prefix_enabled=config.tool_name_prefix,
            tool_call_timeout=config.request_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - tool source failure is non-fatal
        logger.warning("ContextForge MCP tool loading failed: %s", type(exc).__name__)
        return []

    tagged: list[BaseTool] = []
    for tool in tools:
        tag_contextforge_tool(tool, original_name=tool.name)
        tag_mcp_tool(tool, server=SERVER_NAME, original_name=tool.name)
        tagged.append(tool)
    return tagged


__all__ = ["SERVER_NAME", "load_contextforge_tools"]
