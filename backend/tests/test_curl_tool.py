"""curl 内置工具：命令构造、沙箱执行、错误与注册表开关。"""

from __future__ import annotations

import asyncio
import importlib
import shlex

from langchain.tools import ToolRuntime

from agentkit.config import AppConfig
from agentkit.sandbox import tools as sandbox_tools
from agentkit.tools.registry import get_available_tools


def _module():
    return importlib.import_module("agentkit.sandbox.tools.curl_tool")


class FakeSandbox:
    def __init__(self, output: str = '{"ok": true}', error: BaseException | None = None) -> None:
        self.output = output
        self.error = error
        self.calls: list[dict] = []

    def execute_command_in_scope(
        self,
        command: str,
        *,
        timeout: float | None = None,
        scope: str | None = None,
    ) -> str:
        self.calls.append({"command": command, "timeout": timeout, "scope": scope})
        if self.error is not None:
            raise self.error
        return self.output


def _runtime(context: dict | None = None) -> ToolRuntime:
    return ToolRuntime(
        state={},
        context=context or {},
        config={},
        stream_writer=lambda _payload: None,
        tool_call_id=None,
        store=None,
    )


def _install(monkeypatch, sandbox) -> None:
    module = _module()
    monkeypatch.setattr(module, "ensure_sandbox_initialized", lambda _runtime: sandbox)

    async def _direct(func, runtime, *args: object):
        return func(runtime, *args)

    monkeypatch.setattr(module, "run_sync_tool_after_async_init", _direct)


def _call(payload: dict, *, context: dict | None = None) -> str:
    return asyncio.run(sandbox_tools.curl_tool.ainvoke({**payload, "runtime": _runtime(context)}))


def test_get_request_returns_the_response_body(monkeypatch) -> None:
    sandbox = FakeSandbox("hello")
    _install(monkeypatch, sandbox)

    assert _call({"url": "https://example.com"}) == "hello"
    assert shlex.split(sandbox.calls[0]["command"]) == [
        "curl",
        "-sS",
        "--fail-with-body",
        "-X",
        "GET",
        "-L",
        "--",
        "https://example.com",
    ]


def test_post_forwards_headers_and_body_without_shell_injection(monkeypatch) -> None:
    sandbox = FakeSandbox()
    _install(monkeypatch, sandbox)

    _call(
        {
            "url": "https://api.example.com/items",
            "method": "post",
            "headers": {"Content-Type": "application/json", "X-Trace": "a b"},
            "body": '{"name": "a b"}',
        }
    )

    argv = shlex.split(sandbox.calls[0]["command"])
    assert argv == [
        "curl",
        "-sS",
        "--fail-with-body",
        "-X",
        "POST",
        "-L",
        "-H",
        "Content-Type: application/json",
        "-H",
        "X-Trace: a b",
        "--data-binary",
        '{"name": "a b"}',
        "--",
        "https://api.example.com/items",
    ]


def test_head_uses_curls_head_mode(monkeypatch) -> None:
    sandbox = FakeSandbox()
    _install(monkeypatch, sandbox)

    _call({"url": "https://example.com", "method": "head"})

    argv = shlex.split(sandbox.calls[0]["command"])
    assert "--head" in argv
    assert "-X" not in argv


def test_include_headers_and_redirect_switches_reach_curl(monkeypatch) -> None:
    sandbox = FakeSandbox()
    _install(monkeypatch, sandbox)

    _call(
        {
            "url": "https://example.com",
            "follow_redirects": False,
            "include_headers": True,
        }
    )

    argv = shlex.split(sandbox.calls[0]["command"])
    assert "-i" in argv
    assert "-L" not in argv


def test_timeout_is_applied_to_curl_and_the_sandbox(monkeypatch) -> None:
    sandbox = FakeSandbox()
    _install(monkeypatch, sandbox)

    _call({"url": "https://example.com", "timeout": 12.5})

    argv = shlex.split(sandbox.calls[0]["command"])
    assert argv[argv.index("--max-time") + 1] == "12.5"
    assert sandbox.calls[0]["timeout"] == 12.5


def test_subagent_command_scope_is_passed_through(monkeypatch) -> None:
    sandbox = FakeSandbox()
    _install(monkeypatch, sandbox)

    _call(
        {"url": "https://example.com"},
        context={"sandbox_command_scope_id": "scope-1"},
    )

    assert sandbox.calls[0]["scope"] == "scope-1"


def test_shell_metacharacters_stay_inside_their_arguments(monkeypatch) -> None:
    sandbox = FakeSandbox()
    _install(monkeypatch, sandbox)
    url = "https://example.com/path?a=1&b=$(id)"
    body = "x'; touch /tmp/pwned; echo '"
    header = "`id`; rm -rf /"

    _call(
        {
            "url": url,
            "method": "POST",
            "headers": {"X-Test": header},
            "body": body,
        }
    )

    argv = shlex.split(sandbox.calls[0]["command"])
    assert argv[-1] == url
    assert argv[argv.index("--data-binary") + 1] == body
    assert argv[argv.index("-H") + 1] == f"X-Test: {header}"


def test_non_http_urls_are_rejected_before_running_curl(monkeypatch) -> None:
    sandbox = FakeSandbox()
    _install(monkeypatch, sandbox)

    assert _call({"url": "file:///etc/passwd"}).startswith("Error:")
    assert sandbox.calls == []


def test_invalid_method_and_header_are_rejected_before_running_curl(monkeypatch) -> None:
    sandbox = FakeSandbox()
    _install(monkeypatch, sandbox)

    assert _call({"url": "https://example.com", "method": "GE T"}).startswith("Error:")
    assert _call(
        {"url": "https://example.com", "headers": {"X-Test": "a\r\nX-Evil: 1"}}
    ).startswith("Error:")
    assert sandbox.calls == []


def test_body_that_starts_with_at_is_not_read_as_a_sandbox_file(monkeypatch) -> None:
    sandbox = FakeSandbox()
    _install(monkeypatch, sandbox)

    result = _call(
        {"url": "https://example.com", "method": "POST", "body": "@/etc/passwd"}
    )

    assert result.startswith("Error:")
    assert "must not start with '@'" in result
    assert sandbox.calls == []


def test_empty_output_becomes_a_placeholder(monkeypatch) -> None:
    _install(monkeypatch, FakeSandbox(""))

    assert _call({"url": "https://example.com"}) == "(no output)"


def test_sandbox_errors_become_error_strings(monkeypatch) -> None:
    from agentkit.sandbox.exceptions import SandboxRuntimeError

    _install(monkeypatch, FakeSandbox(error=SandboxRuntimeError("curl is not installed")))
    assert _call({"url": "https://example.com"}) == "Error: curl is not installed"


def test_long_output_is_truncated_for_the_model(monkeypatch) -> None:
    module = _module()
    _install(monkeypatch, FakeSandbox("HEAD" * 1_000 + "TAIL" * 1_000))
    monkeypatch.setattr(module, "_max_output_chars", lambda: 500)

    output = _call({"url": "https://example.com"})

    assert len(output) <= 500
    assert output.startswith("HEAD")
    assert output.endswith("TAIL")
    assert "curl output truncated" in output


def test_registry_hides_curl_when_host_bash_is_disabled() -> None:
    base = {"models": [{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}]}
    names = {tool.name for tool in get_available_tools(["sandbox"], app_config=AppConfig.model_validate(base))}

    assert "curl" not in names


def test_registry_exposes_curl_when_the_sandbox_can_run_commands() -> None:
    config = AppConfig.model_validate(
        {
            "models": [{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}],
            "sandbox": {"allow_host_bash": True},
        }
    )
    names = {tool.name for tool in get_available_tools(["sandbox"], app_config=config)}

    assert "curl" in names
