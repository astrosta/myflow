"""Load ContextForge MCP tools and tag them for deferred retrieval."""

from __future__ import annotations

import logging
import re

from langchain_core.tools import BaseTool

from agentkit.config.contextforge_config import ContextForgeConfig
from agentkit.tools.contextforge_metadata import tag_contextforge_tool
from agentkit.tools.mcp_metadata import tag_mcp_tool

try:  # optional extra: ContextForge without the adapter degrades to no tools
    from langchain_mcp_adapters.tools import load_mcp_tools
except ImportError:  # pragma: no cover - exercised only in minimal installs
    load_mcp_tools = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

SERVER_NAME = "contextforge"
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ContextForgeToolNameError(ValueError):
    """A tool name from ContextForge cannot enter the model-facing tool table."""


def qualify_tool_name(server: str, original_name: str, *, prefix_enabled: bool) -> str:
    if not SERVER_NAME_RE.match(server):
        raise ContextForgeToolNameError(f"unsafe ContextForge server name: {server!r}")
    qualified = f"{server}_{original_name}" if prefix_enabled else original_name
    if not qualified or not TOOL_NAME_RE.match(qualified):
        raise ContextForgeToolNameError(
            f"unsafe tool name from ContextForge: {original_name!r}"
        )
    return qualified


async def load_contextforge_tools(config: ContextForgeConfig) -> list[BaseTool]:
    url = config.resolved_mcp_url()
    if not config.enabled or not url:
        return []
    if load_mcp_tools is None:
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
        discovered = await load_mcp_tools(
            None,
            connection=params,
            server_name=SERVER_NAME,
            tool_name_prefix=False,
            handle_tool_errors=True,
        )
    except Exception as exc:  # noqa: BLE001 - tool source failure is non-fatal
        logger.warning("ContextForge MCP tool loading failed: %s", type(exc).__name__)
        return []

    tagged: list[BaseTool] = []
    for tool in discovered:
        original_name = tool.name
        try:
            tool.name = qualify_tool_name(
                SERVER_NAME,
                original_name,
                prefix_enabled=config.tool_name_prefix,
            )
        except ContextForgeToolNameError as exc:
            logger.warning("Dropping a ContextForge tool: %s", exc)
            continue
        tag_contextforge_tool(tool, original_name=original_name)
        tag_mcp_tool(tool, server=SERVER_NAME, original_name=original_name)
        tagged.append(tool)
    return tagged


__all__ = [
    "SERVER_NAME",
    "ContextForgeToolNameError",
    "load_contextforge_tools",
    "qualify_tool_name",
]
