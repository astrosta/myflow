"""run_code tool. One call: a snippet in, its output out.

The tool is hidden unless sandbox.run_code.enabled is true, so the model never
sees a tool it cannot use. Providers with a code interpreter (OpenSandbox) run
the snippet in it; the rest share the command channel in agentkit.sandbox.run_code.
"""

from __future__ import annotations

from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agentkit.sandbox.exceptions import SandboxError
from agentkit.sandbox.path_patterns import mask_host_paths
from agentkit.sandbox.runtime import ensure_sandbox_initialized, run_sync_tool_after_async_init


@tool("run_code")
async def run_code(
    code: str,
    runtime: ToolRuntime[Any, Any],
    language: str = "python",
    timeout: float | None = None,
    description: str = "",
) -> str:
    """执行一段代码并返回输出。

    适合做计算、处理文件、跑小脚本；需要保留内容时用 write_file 落盘，
    这里的临时文件执行完就删。长时间任务请用 bash 放后台。

    Args:
        code: 要执行的代码。
        language: 语言，默认 python；没有代码解释器时只支持 python / javascript / bash。
        timeout: 可选，墙钟超时（秒）。
        description: 可选，界面上显示的短标签。
    """
    del description
    try:
        return await run_sync_tool_after_async_init(_run, runtime, code, language, timeout)
    except SandboxError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: {mask_host_paths(str(exc))}"


def _run(
    runtime: ToolRuntime[Any, Any],
    code: str,
    language: str,
    timeout: float | None,
) -> str:
    """Sync body: runs in a worker thread, so it only touches the sandbox."""

    sandbox = ensure_sandbox_initialized(runtime)
    output = sandbox.run_code(code, language=language, timeout=timeout)
    return mask_host_paths(output, sandbox) or "(no output)"


__all__ = ["run_code"]
