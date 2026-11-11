"""bash 工具返回给模型的输出上限。

provider 的 1 MiB 限制只防失控，不足以保护上下文：一次搜索页 HTML 就可能
十几万字符。工具层保留头尾并明确标注截断，模型才知道还有内容没看到。
"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from pydantic import ValidationError

from agentkit.config.sandbox_config import DEFAULT_BASH_MAX_OUTPUT_CHARS, SandboxConfig

#: The package exports a StructuredTool named bash_tool, which shadows the module
#: in a normal import alias. Resolve the module explicitly so we can exercise the
#: helper and monkeypatch its globals.
bash_tool_module = importlib.import_module("agentkit.sandbox.tools.bash_tool")


def test_sandbox_config_defaults_to_twenty_thousand_chars() -> None:
    assert SandboxConfig().bash_max_output_chars == DEFAULT_BASH_MAX_OUTPUT_CHARS == 20_000


def test_sandbox_config_rejects_a_tiny_limit() -> None:
    with pytest.raises(ValidationError):
        SandboxConfig(bash_max_output_chars=999)


def test_short_output_is_unchanged() -> None:
    assert bash_tool_module.truncate_bash_output("short output", 100) == "short output"


def test_long_output_keeps_head_and_tail_with_a_marker() -> None:
    text = "HEAD" * 2_000 + "MIDDLE" * 2_000 + "TAIL" * 2_000

    output = bash_tool_module.truncate_bash_output(text, 1_000)

    assert len(output) <= 1_000
    assert output.startswith("HEAD")
    assert output.endswith("TAIL")
    assert "bash output truncated" in output


def test_a_budget_smaller_than_the_marker_falls_back_to_a_plain_cut() -> None:
    output = bash_tool_module.truncate_bash_output("x" * 500, 10)

    assert output == "x" * 10


def test_the_tool_applies_the_limit_before_returning_to_the_model(monkeypatch) -> None:
    async def fake_run_sync(*args, **kwargs) -> str:
        return "HEAD" * 1_000 + "TAIL" * 1_000

    monkeypatch.setattr(bash_tool_module, "run_sync_tool_after_async_init", fake_run_sync)
    monkeypatch.setattr(bash_tool_module, "_max_output_chars", lambda: 500)

    output = asyncio.run(
        bash_tool_module.bash_tool.coroutine(
            command="echo whatever",
            runtime=object(),
            description="test",
        )
    )

    assert len(output) <= 500
    assert output.startswith("HEAD")
    assert output.endswith("TAIL")
    assert "bash output truncated" in output
