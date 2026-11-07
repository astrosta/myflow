"""Slash 激活：把 SKILL.md 注入成本次模型调用的隐藏 HumanMessage（M7 §3.6）。

纪律（和 M6 的视觉载荷同一条）：**只改 request，不写 state**。SKILL.md 注入本身不进
checkpoint，所以第 2..N 次模型调用要从 run context 知道"已经激活过"——记的是消息 id。

两条激活路径的权威性不同：这里是用户显式 /skill-name（权威），另一条是模型自己
read_file 读 SKILL.md（自主，由 DurableContext §3.10 捕获）。
"""

from __future__ import annotations

import hashlib
import html
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage

from agentkit.agents.middlewares.message_utils import (
    insert_after_leading_system_messages,
    is_real_user_message,
)
from agentkit.agents.middlewares.provenance import ContentKind, provenance_kwargs
from agentkit.config.skills_config import SkillsConfig
from agentkit.runtime.secret_context import (
    SKILL_ACTIVATION_STATE_CONTEXT_KEY,
    SLASH_SKILL_SOURCE_CONTEXT_KEY,
    new_owner_token,
    read_secret_context,
    write_secret_context,
)
from agentkit.skills.slash import parse_slash_skill
from agentkit.skills.storage import SkillRegistry, validate_skill_file_path
from agentkit.skills.types import Skill, SkillPathError
from agentkit.utils.messages import get_original_user_content_text

if TYPE_CHECKING:
    from agentkit.skills.catalog import SkillSnapshot

logger = logging.getLogger(__name__)

#: M4 的 middleware:{tag} 归因（tag ≤ 21 字符）。
SKILL_ACTIVATION_AUDIT_TAG = "middleware:skill_activation"

PRODUCER_KIND = "skill_activation"


class SkillActivationMiddleware(AgentMiddleware[AgentState]):
    """Slash 激活。组装顺序：Activation 紧挨 Policy 紧挨 DurableContext（§3.9）。"""

    def __init__(
        self,
        *,
        skills_config: SkillsConfig | None = None,
        user_id: str | None = None,
        available_skills: frozenset[str] | None = None,
        owner_token: str | None = None,
        snapshot: "SkillSnapshot | None" = None,
    ) -> None:
        super().__init__()
        self._config = skills_config
        self._user_id = user_id
        self._available_skills = available_skills
        self._owner_token = owner_token or new_owner_token()
        # A snapshot is an immutable catalog generation.  When present, slash
        # resolution never rescans the filesystem; Skill.skill_file is the
        # authoritative body path prepared by the catalog service.
        self._snapshot = snapshot

    @property
    def owner_token(self) -> str:
        """只传给 Policy 这一对（§3.6）：没有它的读一律当无。"""
        return self._owner_token

    # ---- 现场解析 ----------------------------------------------------------

    def _runtime_user_id(self, runtime) -> str | None:
        context = getattr(runtime, "context", None)
        if isinstance(context, Mapping):
            value = context.get("user_id")
            if isinstance(value, str) and value:
                return value
        return self._user_id

    def _registry(self, runtime) -> SkillRegistry:
        user_id = self._runtime_user_id(runtime)
        return SkillRegistry(
            config=self._config,
            user_id=user_id,
            available_skills=self._available_skills,
            snapshot=self._snapshot,
        )

    def _read_body(self, skill: Skill) -> str | None:
        try:
            path = validate_skill_file_path(skill)
            return path.read_text(encoding="utf-8")
        except (OSError, SkillPathError) as exc:
            logger.warning("Cannot activate skill %s: %s", skill.name, exc)
            return None

    def _inject(self, request: ModelRequest) -> ModelRequest:
        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None)
        message = _latest_real_user_message(list(request.messages or []))
        if message is None:
            return request
        text = get_original_user_content_text(
            getattr(message, "content", ""), getattr(message, "additional_kwargs", None)
        )
        match = parse_slash_skill(text)
        if match is None:
            return request

        identity = _message_identity(message)
        state = read_secret_context(context, SKILL_ACTIVATION_STATE_CONTEXT_KEY, token=self._owner_token)
        activated_ids = list(state.get("message_ids") or []) if isinstance(state, Mapping) else []
        if identity in activated_ids:
            return request  # 同一 run 里同一条 slash 消息只读一次盘、只发一次 audit

        skill = self._registry(runtime).find(match.name)
        if skill is None:
            return request  # 未启用 / 不在白名单 / 不存在：语法对但不激活
        body = self._read_body(skill)
        if body is None:
            return request

        # slash 源路径：给 Policy 用，写在保留键里，token 只在这一对中间件之间共享。
        write_secret_context(
            context,
            SLASH_SKILL_SOURCE_CONTEXT_KEY,
            skill.container_file,
            token=self._owner_token,
        )
        write_secret_context(
            context,
            SKILL_ACTIVATION_STATE_CONTEXT_KEY,
            {
                "message_ids": [*activated_ids, identity],
                "skills": [skill.name],
            },
            token=self._owner_token,
        )
        logger.info(
            "%s run context recorded skill=%s path=%s",
            SKILL_ACTIVATION_AUDIT_TAG,
            skill.name,
            skill.container_file,
        )
        injected = HumanMessage(
            content=html.escape(body),
            additional_kwargs={
                "hide_from_ui": True,
                **provenance_kwargs(
                    ContentKind.SKILL_BODY, PRODUCER_KIND, producer_entity_id=skill.container_file
                ),
            },
        )
        messages = insert_after_leading_system_messages(list(request.messages or []), [injected])
        return request.override(messages=messages)

    # ---- hooks -------------------------------------------------------------

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._inject(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._inject(request))


def _latest_real_user_message(messages: list) -> object | None:
    for message in reversed(messages):
        if is_real_user_message(message):
            return message
    return None


def _message_identity(message: object) -> str:
    """优先用 LangChain 的 message.id；没有就退化成内容指纹（同一 run 内稳定）。"""
    message_id = getattr(message, "id", None)
    if isinstance(message_id, str) and message_id:
        return message_id
    content = getattr(message, "content", "")
    digest = hashlib.sha256(str(content).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"