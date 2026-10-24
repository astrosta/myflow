"""注入 `runtime` 的工具不能在参数序列化时喷 Pydantic 警告（M3 §5）。

现象：每次调用带 `runtime: ToolRuntime` 的工具，网关日志里都会出现

    PydanticSerializationUnexpectedValue(Expected `none` - serialized value may not be
    as expected [field_name='context', ...])

原因：LangChain 的 `BaseTool._parse_input` 会 `args_schema.model_dump()`，而裸 `ToolRuntime`
的泛型参数默认是 `None`，于是 `context` 的 schema 是 `none`，实际却塞进来一个 dict。
把它标成 `ToolRuntime[Any, Any]` 就按 `Any` 序列化，警告消失，注入行为不变。
"""

from __future__ import annotations

import warnings

import pytest
from langchain.tools import ToolRuntime

from agentkit.tools.builtin.search_and_load_skills import search_and_load_skills


def _runtime() -> ToolRuntime:
    return ToolRuntime(
        state={},
        context={"thread_id": "t1", "run_id": "r1"},
        config={},
        stream_writer=lambda _payload: None,
        tool_call_id="call-1",
        store=None,
    )


def _invoke(tool, args: dict) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            tool.invoke(
                {
                    "name": tool.name,
                    "args": {**args, "runtime": _runtime()},
                    "id": "call-1",
                    "type": "tool_call",
                }
            )
        except Exception:  # 工具本身可能因为没配置而失败，这里只关心序列化警告
            pass
    pydantic_warnings = [
        str(item.message) for item in caught if "Pydantic serializer warnings" in str(item.message)
    ]
    assert pydantic_warnings == [], pydantic_warnings


def test_injected_runtime_does_not_trigger_pydantic_serializer_warnings() -> None:
    # args_schema 版（模型只给业务字段）同样不能喷：runtime 不在 schema 里，但会被注入
    _invoke(
        search_and_load_skills,
        {"task_query": "PostgreSQL 多租户 search_path 串库", "domain_keywords": ["search_path"]},
    )


def test_bare_tool_runtime_still_warns_so_the_regression_is_real() -> None:
    """反证：裸 `ToolRuntime` 就是这么喷的，说明上面那条断言确实在守东西。"""

    from typing import Any

    from langchain_core.tools import tool

    @tool
    def probe(message: str = "", runtime: ToolRuntime = None) -> str:
        """probe."""
        return str(message)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        probe.invoke(
            {
                "name": "probe",
                "args": {"message": "x", "runtime": _runtime()},
                "id": "call-2",
                "type": "tool_call",
            }
        )
    assert any("Pydantic serializer warnings" in str(item.message) for item in caught)
