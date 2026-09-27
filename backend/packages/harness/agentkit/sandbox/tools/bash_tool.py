"""Bash tool. The model is told to background long-running commands."""

from __future__ import annotations

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agentkit.sandbox.exceptions import SandboxError
from agentkit.sandbox.path_patterns import mask_host_paths
from agentkit.sandbox.runtime import ensure_sandbox_initialized


@tool("bash")
def bash_tool(
    command: str,
    runtime: ToolRuntime,
    timeout: float | None = None,
    description: str = "",
) -> str:
    """Run a shell command in the sandbox.

    Start long-running servers in the background (end the command with ``&``)
    so this call can return. A foreground command is killed, along with its
    child processes, when it exceeds the timeout.

    Args:
        command: Shell command to run.
        timeout: Optional wall-clock timeout in seconds.
        description: Optional short label shown in the UI.
    """
    del description
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        output = sandbox.execute_command(command, timeout=timeout)
        return mask_host_paths(output, sandbox) or "(no output)"
    except SandboxError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: {mask_host_paths(str(exc))}"
