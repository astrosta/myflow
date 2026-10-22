"""Bash tool. The model is told to background long-running commands.

M8 §11.3：这条路径走 ``run_sync_tool_after_async_init`` —— 长的 shell 命令是"取消之后还在
线程里跑"的典型，必须在释放沙箱租约之前能被**排空**（``shield_sandbox_work`` 登记它）。
"""

from __future__ import annotations

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agentkit.sandbox.exceptions import SandboxError
from agentkit.sandbox.lease import command_scope_from_context
from agentkit.sandbox.path_patterns import mask_host_paths
from agentkit.sandbox.runtime import ensure_sandbox_initialized, run_sync_tool_after_async_init


@tool("bash")
async def bash_tool(
    command: str,
    runtime: ToolRuntime,
    timeout: float | None = None,
    description: str = "",
) -> str:
    """在沙箱中执行 shell 命令。

    长时间运行的服务请放到后台（命令末尾加 ``&``），否则这次调用会一直等下去。
    前台命令超过超时时间后会被终止，子进程也会一起终止。

    Args:
        command: 要执行的 shell 命令。
        timeout: 可选，墙钟超时时间（秒）。
        description: 可选，界面上显示的短标签。
    """
    del description
    try:
        return await run_sync_tool_after_async_init(_run_command, runtime, command, timeout)
    except SandboxError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: {mask_host_paths(str(exc))}"


def _run_command(runtime: ToolRuntime, command: str, timeout: float | None) -> str:
    """同步体：``to_thread`` 里跑的就是它（所以它只碰沙箱，不碰事件循环）。"""
    sandbox = ensure_sandbox_initialized(runtime)
    output = sandbox.execute_command_in_scope(
        command,
        timeout=timeout,
        # §11.2：每个子代理一个 command scope（独立 shell 会话）；lead 没有 scope。
        scope=command_scope_from_context(runtime.context),
    )
    return mask_host_paths(output, sandbox) or "(no output)"
