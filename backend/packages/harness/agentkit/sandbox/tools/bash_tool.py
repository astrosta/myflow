"""Bash tool. The model is told to background long-running commands.

M8 §11.3：这条路径走 ``run_sync_tool_after_async_init`` —— 长的 shell 命令是"取消之后还在
线程里跑"的典型，必须在释放沙箱租约之前能被**排空**（``shield_sandbox_work`` 登记它）。
"""

from __future__ import annotations

from typing import Any
from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agentkit.config.sandbox_config import DEFAULT_BASH_MAX_OUTPUT_CHARS
from agentkit.sandbox.exceptions import SandboxError
from agentkit.sandbox.lease import command_scope_from_context
from agentkit.sandbox.path_patterns import mask_host_paths
from agentkit.sandbox.runtime import ensure_sandbox_initialized, run_sync_tool_after_async_init


@tool("bash")
async def bash_tool(
    command: str,
    runtime: ToolRuntime[Any, Any],
    timeout: float | None = None,
    description: str = "",
) -> str:
    """在沙箱中执行 shell 命令。

    长时间运行的服务请放到后台（命令末尾加 ``&``），否则这次调用会一直等下去。
    前台命令超过超时时间后会被终止，子进程也会一起终止。
    输出超过 sandbox.bash_max_output_chars 时会保留头尾并明确标注截断。

    Args:
        command: 要执行的 shell 命令。
        timeout: 可选，墙钟超时时间（秒）。
        description: 可选，界面上显示的短标签。
    """
    del description
    try:
        output = await run_sync_tool_after_async_init(_run_command, runtime, command, timeout)
        return truncate_bash_output(output, _max_output_chars())
    except SandboxError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: {mask_host_paths(str(exc))}"


def _run_command(runtime: ToolRuntime[Any, Any], command: str, timeout: float | None) -> str:
    """同步体：``to_thread`` 里跑的就是它（所以它只碰沙箱，不碰事件循环）。"""
    sandbox = ensure_sandbox_initialized(runtime)
    output = sandbox.execute_command_in_scope(
        command,
        timeout=timeout,
        # §11.2：每个子代理一个 command scope（独立 shell 会话）；lead 没有 scope。
        scope=command_scope_from_context(runtime.context),
    )
    return mask_host_paths(output, sandbox) or "(no output)"


def truncate_bash_output(text: str, max_chars: int) -> str:
    """把模型可见的 bash 输出限制在 max_chars 内，保留头尾并给出截断标记。

    工具层截断而不是只依赖 provider 的 1 MiB：一份 14 万字符的原始 HTML 仍在
    1 MiB 以内，却足以把子代理上下文从几千 token 抬到几万 token。
    """
    budget = max(1, int(max_chars))
    if len(text) <= budget:
        return text
    marker = (
        f"\n\n...[bash output truncated: {len(text):,} chars total; "
        "showing head and tail]...\n\n"
    )
    if budget <= len(marker):
        return text[:budget]
    keep = budget - len(marker)
    head_len = max(1, keep * 2 // 3)
    tail_len = max(0, keep - head_len)
    tail = text[-tail_len:] if tail_len else ""
    return text[:head_len] + marker + tail


def _max_output_chars() -> int:
    """当前配置的上限；配置不可用时回退默认值，读取失败不能让 bash 工具失效。"""
    try:
        from agentkit.config import get_app_config

        configured = get_app_config().sandbox.bash_max_output_chars
        return max(1000, int(configured))
    except Exception:
        return DEFAULT_BASH_MAX_OUTPUT_CHARS
