"""The only model-facing write surface for the bounded user profile.

The tool writes the DB-backed profile through `ProfileService`, which makes it
independent of the OpenViking switch: a deployment with long-term memory turned
off still lets the assistant record a name or a language the user stated
explicitly. Reads/writes of OpenViking memories stay read-only for the model.

The description is normative: it lists every supported field and spells out when
to call (and when not to). Tests assert those trigger phrases exist, so keep the
wording in sync with the design document.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Protocol

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool

from agentkit.agents.memory.openviking.profile import ProfileConflict
from agentkit.agents.memory.profile_schema import (
    PROFILE_FIELD_SPECS,
    field_descriptions,
    validate_profile_value,
)
from agentkit.runtime.user_context import resolve_runtime_user_id

logger = logging.getLogger(__name__)

TOOL_NAME = "update_user_profile"
_ALLOWED_FIELDS = tuple(spec.path for spec in PROFILE_FIELD_SPECS)


class ProfileUpdater(Protocol):
    """Minimal profile-write surface the tool needs (satisfied by ProfileService)."""

    async def update_fields(self, user_id: str, fields: dict[str, Any]) -> Any: ...


_UPDATER: ProfileUpdater | None = None


def configure_profile_updater(updater: ProfileUpdater | None) -> None:
    """Install (or clear) the process-local profile writer used by the agent."""

    global _UPDATER
    _UPDATER = updater


def get_profile_updater() -> ProfileUpdater | None:
    return _UPDATER


def make_profile_tools() -> list[BaseTool]:
    """The profile tool list for agent assembly; empty until a writer is wired."""

    updater = _UPDATER
    if updater is None:
        return []
    return [make_update_user_profile_tool(updater)]


def tool_description() -> str:
    return "\n".join(
        [
            "更新当前用户的「我的画像」，保存跨会话稳定的身份、沟通和工作偏好。",
            "",
            "只在用户明确、持久地表达画像信息时调用，例如「我叫小林」「以后都用英文」"
            "「我是后端工程师」；一次性指令（如「这次用英文」）、推测和敏感信息"
            "（密钥、令牌、证件号、联系方式）不要写入。",
            "",
            "所有字段都按自由文本填写；字段说明里的「常见」只是提示，不要求逐字匹配。"
            "删除字段传 null；列表字段传字符串数组；字段名必须来自下面清单。",
            "与画像无关的普通事实不要写入。",
            "",
            "支持的字段：",
            *field_descriptions(),
            "",
            '成功返回 {"ok": true, "updated": [...], "cleared": [...], "profile_etag": "..."}；',
            "参数非法时返回 error 和 message，请按 message 修正后重试。",
        ]
    )


def make_update_user_profile_tool(updater: ProfileUpdater) -> BaseTool:
    @tool(
        TOOL_NAME,
        description=tool_description(),
        extras={"interrupt_before": True},
    )
    async def update_user_profile(
        fields: dict[str, Any],
        runtime: ToolRuntime[Any, Any] = None,
        note: str | None = None,
    ) -> str:
        if note:
            logger.debug("update_user_profile note: %s", note[:200])
        user_id = resolve_runtime_user_id(_context(runtime))
        if not user_id:
            return _error("missing_user", "缺少用户身份，画像不可用", [])
        if not isinstance(fields, Mapping) or not fields:
            return _error(
                "invalid_field",
                "fields must be a non-empty object of profile fields",
                list(_ALLOWED_FIELDS),
            )
        cleaned: dict[str, Any] = {}
        cleared: list[str] = []
        for path, value in fields.items():
            if path not in _ALLOWED_FIELDS:
                return _error(
                    "invalid_field",
                    f"unknown profile field: {path}",
                    list(_ALLOWED_FIELDS),
                )
            if value is None:
                cleared.append(path)
                cleaned[path] = None
                continue
            try:
                cleaned[path] = validate_profile_value(path, value)
            except ValueError as exc:
                return _error(
                    "invalid_value",
                    str(exc),
                    list(_ALLOWED_FIELDS),
                )
        try:
            view = await updater.update_fields(user_id, cleaned)
        except ProfileConflict:
            return _error(
                "profile_update_failed",
                "画像正在被其他操作更新，请稍后重试",
                [],
            )
        except ValueError as exc:
            return _error("invalid_value", str(exc), list(_ALLOWED_FIELDS))
        except Exception:  # noqa: BLE001 - tool boundary: never raise into the graph
            logger.exception("update_user_profile failed")
            return _error(
                "profile_update_failed",
                "画像写入失败，请稍后重试",
                [],
            )
        etag = getattr(view, "etag", "")
        return json.dumps(
            {
                "ok": True,
                "updated": [path for path in cleaned if path not in cleared],
                "cleared": cleared,
                "profile_etag": str(etag or ""),
            },
            ensure_ascii=False,
        )

    return update_user_profile


def _context(runtime: Any) -> dict[str, Any]:
    if runtime is None:
        return {}
    context = getattr(runtime, "context", None)
    return dict(context) if isinstance(context, Mapping) else {}


def _error(code: str, message: str, allowed: list[str]) -> str:
    payload: dict[str, Any] = {"error": code, "message": message}
    if allowed:
        payload["allowed_fields"] = allowed
    return json.dumps(payload, ensure_ascii=False)


__all__ = [
    "ProfileUpdater",
    "TOOL_NAME",
    "configure_profile_updater",
    "get_profile_updater",
    "make_profile_tools",
    "make_update_user_profile_tool",
    "tool_description",
]
