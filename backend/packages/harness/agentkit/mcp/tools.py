"""MCP 工具的加载与包装（M7 §5.1/§5.3）。

* 名字在**加载边界**就正则约束：延迟工具不会被 provider 的 function-calling 校验扫到，
  而它的名字会进系统提示词——换行或尖括号足以伪造框架结构。
* 默认加 <server>_ 前缀：两个服务器都提供 search 很常见。前缀可以关，所以源路由
  必须认 metadata 里的 {server, original_name}，**不认名字前缀**。
* stdio 工具走会话池（§5.3）；HTTP/SSE 保持一次性会话，凭据拦截器仍然套上。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any

from langchain_core.tools import BaseTool
from langchain_core.tools.base import InjectedToolArg

from agentkit.mcp.session_pool import get_stdio_pool, is_disconnect_error, pool_key
from agentkit.tools.mcp_metadata import tag_mcp_tool

logger = logging.getLogger(__name__)

#: 工具名在加载边界就约束（§5.1）。
MCP_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

#: 服务器名自己也要是安全标识符，否则前缀会把脏字符带进工具名；与 §2.4 的键正则同一条。
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class McpToolNameError(ValueError):
    """服务器给的名字不能进我们的工具表 —— 丢掉这一个工具，不拖垮整台服务器。"""


def qualify_tool_name(server: str, original_name: str, *, prefix_enabled: bool) -> str:
    if not SERVER_NAME_RE.match(server):
        raise McpToolNameError(f"unsafe MCP server name: {server!r}")
    qualified = f"{server}_{original_name}" if prefix_enabled else original_name
    if not qualified or not MCP_TOOL_NAME_RE.match(qualified):
        raise McpToolNameError(f"unsafe tool name from server {server!r}: {original_name!r}")
    return qualified


async def load_server_tools(
    server_name: str,
    params: dict[str, Any],
    *,
    prefix_enabled: bool = True,
    interceptors: Sequence[Any] | None = None,
    stdio: bool = False,
    tool_call_timeout: float | None = None,
    server_config: Any = None,
) -> list[BaseTool]:
    """连一次、发现工具、改名打标（stdio 再包一层会话池）。单台失败由调用方兜。"""
    from langchain_mcp_adapters.tools import load_mcp_tools

    discovered = await load_mcp_tools(
        None,  # session=None + connection：适配器自己开一次会话做发现，工具绑连接不绑会话
        connection=params,
        server_name=server_name,
        tool_interceptors=list(interceptors or []),
        tool_name_prefix=False,
        handle_tool_errors=True,
    )
    loaded: list[BaseTool] = []
    for tool in discovered:
        original_name = tool.name
        try:
            qualified = qualify_tool_name(server_name, original_name, prefix_enabled=prefix_enabled)
        except McpToolNameError as exc:
            logger.warning("Dropping an MCP tool: %s", exc)
            continue
        tool.name = qualified
        tag_mcp_tool(tool, server=server_name, original_name=original_name)
        if stdio:
            tool = wrap_stdio_tool(
                tool,
                server=server_name,
                original_name=original_name,
                params=params,
                tool_call_timeout=tool_call_timeout,
                interceptors=interceptors,
                server_config=server_config,
            )
        loaded.append(tool)
    return loaded


def wrap_stdio_tool(
    tool: BaseTool,
    *,
    server: str,
    original_name: str,
    params: dict[str, Any],
    tool_call_timeout: float | None,
    interceptors: Sequence[Any] | None = None,
    server_config: Any = None,
) -> BaseTool:
    """把 stdio 工具的会话来源换成 (server, user:thread) 的池。

    拦截器链、内容块转换、isError 语义都复用适配器那一套（handle_tool_error 留在工具上）。
    """

    async def _call(runtime: Annotated[Any, InjectedToolArg()] = None, **arguments: Any) -> Any:
        user_id, thread_id = _runtime_scope(runtime)
        pool = get_stdio_pool()
        key = pool_key(server, user_id=user_id, thread_id=thread_id)
        # cwd / TMPDIR 属于**这个** (user, thread)：每次现算，不吃加载时的那一份
        session_params = params
        if server_config is not None:
            from agentkit.mcp.client import build_server_params

            session_params = build_server_params(
                server_config, server_name=server, user_id=user_id, thread_id=thread_id
            )
        session = await pool.acquire(
            server, user_id=user_id, thread_id=thread_id, params=session_params
        )

        async def _execute(request: Any) -> Any:
            call = session.call_tool(request.name, request.args or None)
            if tool_call_timeout is None:
                return await call
            return await asyncio.wait_for(call, timeout=float(tool_call_timeout))

        try:
            result = await _run_interceptor_chain(
                _execute,
                request_name=original_name,
                arguments=arguments,
                server_name=server,
                runtime=runtime,
                interceptors=interceptors,
            )
        except BaseException as exc:
            # 明确的断连只驱逐自己；晚到的旧错误会因为对象身份对不上而放过新会话。
            # **绝不自动重放**：重放一个有副作用的工具比报错糟得多（§5.3）。
            if is_disconnect_error(exc):
                pool.evict_on_disconnect(key, session)
            raise
        return _convert_result(result)

    return tool.model_copy(update={"coroutine": _call})


def _runtime_scope(runtime: Any) -> tuple[str | None, str | None]:
    """user/thread 从 ToolRuntime 的 context 读（§5.4），configurable 只做后备。"""
    context = getattr(runtime, "context", None)
    mapping = context if isinstance(context, Mapping) else {}
    user_id = mapping.get("user_id")
    thread_id = mapping.get("thread_id")
    if not thread_id:
        config = getattr(runtime, "config", None)
        configurable = config.get("configurable", {}) if isinstance(config, Mapping) else {}
        thread_id = configurable.get("thread_id")
    return (str(user_id) if user_id else None, str(thread_id) if thread_id else None)


def _request(request_name: str, arguments: dict[str, Any], server_name: str, runtime: Any) -> Any:
    from langchain_mcp_adapters.interceptors import MCPToolCallRequest

    return MCPToolCallRequest(
        name=request_name, args=arguments, server_name=server_name, headers=None, runtime=runtime
    )


async def _run_interceptor_chain(
    execute: Any,
    *,
    request_name: str,
    arguments: dict[str, Any],
    server_name: str,
    runtime: Any,
    interceptors: Sequence[Any] | None,
) -> Any:
    chain = list(interceptors or [])
    if not chain:
        return await execute(_request(request_name, arguments, server_name, runtime))
    try:
        from langchain_mcp_adapters.tools import _build_interceptor_chain
    except ImportError:  # pragma: no cover - 适配器改了内部结构
        logger.warning("MCP interceptor chain helper is unavailable; running the call directly")
        return await execute(_request(request_name, arguments, server_name, runtime))
    handler = _build_interceptor_chain(execute, chain)
    return await handler(_request(request_name, arguments, server_name, runtime))


def _convert_result(result: Any) -> Any:
    """MCP 结果 → LangChain 内容块。复用适配器的转换，保证图片/资源/isError 语义一致。"""
    from langchain_mcp_adapters.tools import _convert_call_tool_result

    return _convert_call_tool_result(result)
