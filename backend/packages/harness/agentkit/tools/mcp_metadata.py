"""MCP 工具的标记（M7 §5.1）。

源路由、延迟提升、结果消毒都只认这个标，**不认名字前缀**——前缀可以被
tool_name_prefix: false 关掉，名字也会被去重改写。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: MCP 工具的元数据键。本仓库统一 ``myflow_`` 前缀（见 myflow_tool_meta）。
MCP_TOOL_META_KEY = "myflow_mcp"


@dataclass(frozen=True, slots=True)
class McpToolMeta:
    server: str
    original_name: str


def tag_mcp_tool(tool: Any, *, server: str, original_name: str) -> Any:
    """盖上 {server, original_name}。original_name 一定要记：结果消毒、审计、去重都用它，"""
    """而不是从前缀反推。"""
    tool.metadata = {
        **(getattr(tool, "metadata", None) or {}),
        MCP_TOOL_META_KEY: {"server": server, "original_name": original_name},
    }
    return tool


def mcp_tool_meta(tool: Any) -> McpToolMeta | None:
    metadata = getattr(tool, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    entry = metadata.get(MCP_TOOL_META_KEY)
    if not isinstance(entry, dict):
        return None
    server = entry.get("server")
    original_name = entry.get("original_name")
    if not isinstance(server, str) or not isinstance(original_name, str):
        return None
    return McpToolMeta(server=server, original_name=original_name)


def is_mcp_tool(tool: Any) -> bool:
    return mcp_tool_meta(tool) is not None
