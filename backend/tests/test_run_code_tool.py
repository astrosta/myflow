"""run_code tool: gating, forwarding, output and error shape."""

from __future__ import annotations

import asyncio
import importlib

from langchain.tools import ToolRuntime

from agentkit.config import AppConfig
from agentkit.sandbox import tools as sandbox_tools
from agentkit.tools.registry import get_available_tools

# The tool object is exported as agentkit.sandbox.tools.run_code, so the module
# with the wrappers has to be imported by name to be patched.
run_code_module = importlib.import_module("agentkit.sandbox.tools.run_code")


class FakeSandbox:
    def __init__(self, output: str = "42\n", error: BaseException | None = None) -> None:
        self.output = output
        self.error = error
        self.calls: list[dict] = []

    def run_code(self, code: str, *, language: str = "python", timeout=None) -> str:
        self.calls.append({"code": code, "language": language, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.output


def _runtime() -> ToolRuntime:
    return ToolRuntime(
        state={},
        context={},
        config={},
        stream_writer=lambda _payload: None,
        tool_call_id=None,
        store=None,
    )


def _install(monkeypatch, sandbox) -> None:
    monkeypatch.setattr(
        run_code_module, "ensure_sandbox_initialized", lambda _runtime: sandbox
    )

    async def _direct(func, runtime, *args: object):
        # The real wrapper initializes a provider first; that path is covered by
        # the sandbox runtime tests, and here it would need a live provider.
        return func(runtime, *args)

    monkeypatch.setattr(run_code_module, "run_sync_tool_after_async_init", _direct)


def _call(payload: dict) -> str:
    # ToolRuntime is a required field of the schema, so direct invocation has to
    # pass it; the graph injects it automatically.
    return asyncio.run(sandbox_tools.run_code.ainvoke({**payload, "runtime": _runtime()}))


def test_tool_returns_the_sandbox_output(monkeypatch) -> None:
    sandbox = FakeSandbox("42\n")
    _install(monkeypatch, sandbox)

    assert _call({"code": "6 * 7"}).strip() == "42"

def test_tool_forwards_language_and_timeout(monkeypatch) -> None:
    sandbox = FakeSandbox()
    _install(monkeypatch, sandbox)

    _call({"code": "console.log(1)", "language": "javascript", "timeout": 15})

    assert sandbox.calls == [
        {"code": "console.log(1)", "language": "javascript", "timeout": 15}
    ]


def test_tool_defaults_to_python(monkeypatch) -> None:
    sandbox = FakeSandbox()
    _install(monkeypatch, sandbox)

    _call({"code": "print(1)"})

    assert sandbox.calls[0]["language"] == "python"
    assert sandbox.calls[0]["timeout"] is None


def test_empty_output_becomes_a_placeholder(monkeypatch) -> None:
    _install(monkeypatch, FakeSandbox(""))

    assert _call({"code": "None"}) == "(no output)"


def test_sandbox_errors_become_error_strings(monkeypatch) -> None:
    from agentkit.sandbox.exceptions import SandboxRuntimeError

    _install(monkeypatch, FakeSandbox(error=SandboxRuntimeError("no interpreter")))
    assert _call({"code": "1"}) == "Error: no interpreter"

    _install(monkeypatch, FakeSandbox(error=RuntimeError("boom")))
    assert _call({"code": "1"}) == "Error: boom"


def test_registry_hides_run_code_until_enabled() -> None:
    base = {"models": [{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}]}
    off = AppConfig.model_validate(base)
    on = AppConfig.model_validate({**base, "sandbox": {"run_code": {"enabled": True}}})

    names_off = [tool.name for tool in get_available_tools(["sandbox"], app_config=off)]
    names_on = [tool.name for tool in get_available_tools(["sandbox"], app_config=on)]

    assert "run_code" not in names_off
    assert "run_code" in names_on


def test_run_code_config_defaults() -> None:
    config = AppConfig.model_validate(
        {"models": [{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}]}
    )
    assert config.sandbox.run_code.enabled is False
    assert config.sandbox.run_code.default_language == "python"
    assert config.sandbox.run_code.timeout_seconds == 120