"""按请求注入 header：context.secrets 的键（M7 §5.5 层 ③）。

映射里存的是**名字**，不是值（GET 时不打码，PUT 整块替换）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain_core.tools import ToolException

from agentkit.mcp.headers import apply_header_overrides, illegal_header_value_reason
from agentkit.runtime.secret_context import extract_request_secrets

logger = logging.getLogger(__name__)

ON_MISSING_PASSTHROUGH = "passthrough"


class ContextHeadersInterceptor:
    """把 context.secrets 的值映射成 header。**fail-closed 是默认**（§5.5）。"""

    def __init__(self, mapping: Mapping[str, str], *, on_missing: str = "fail") -> None:
        self._mapping = dict(mapping)
        self._on_missing = on_missing

    async def __call__(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        # ★ 从 ToolRuntime 读：运行上下文挂在 runtime 上，不在 RunnableConfig 里（§5.4）
        secrets = extract_request_secrets(getattr(getattr(request, "runtime", None), "context", None))
        overrides: dict[str, str] = {}
        missing: list[str] = []
        for header, secret_name in self._mapping.items():
            value = secrets.get(secret_name)
            if not value:
                missing.append(secret_name)
                continue
            reason = illegal_header_value_reason(str(value))
            if reason is not None:
                # 非法值**无视** on_missing=passthrough：那是"缺值"的开关，不是"脏值也能送"
                raise ToolException(f"invalid credential for {secret_name}: {reason}")
            overrides[header] = str(value)
        if missing and self._on_missing != ON_MISSING_PASSTHROUGH:
            raise ToolException(f"missing required secret(s): {', '.join(sorted(missing))}")
        if not overrides:
            return await handler(request)
        return await handler(
            request.override(headers=apply_header_overrides(request.headers, overrides))
        )
