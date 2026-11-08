"""search_skills —— 按任务意图检索技能元数据（skill-finder 专属）。

它只返回 `skill_id / name / description`；正文由 lead 再调用系统内置的
`load_skills` 加载。这样 skill-finder 不把整段 SOP 搬回 lead，也不会在检索阶段
增加技能使用次数。
"""

from __future__ import annotations

import json
import logging

from langchain.tools import ToolRuntime
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from agentkit.runtime.user_context import DEFAULT_USER_ID
from agentkit.sandbox.runtime import runtime_user_bucket
from agentkit.sandbox.security import safe_app_config
from agentkit.skills.search import (
    SkillSearchOutcome,
    SkillSearchUnavailable,
    get_skill_runtime,
)

logger = logging.getLogger(__name__)

TOOL_NAME = "search_skills"
MAX_DOMAIN_KEYWORDS = 8

EMPTY_RESULT_MESSAGE = (
    "没有检索到匹配的技能（当前部署可能一个技能都没启用，或技能树为空）。",
    "不要用本工具重复尝试相似的说法；需要正文时让 lead 用 load_skills。",
)
UNAVAILABLE_MESSAGE = (
    "技能检索暂不可用（技能目录或 Milvus 检索索引未就绪）。",
    "不要用本工具重复尝试相似的说法。",
)


class SearchSkillsInput(BaseModel):
    task_query: str = Field(
        description=(
            "自包含（Self-contained）的完整任务意图描述。"
            "【严禁使用代词】多轮对话中必须把背景、对象和当前诉求合并重写。"
            "【必须包含具体特征】写出技术栈、协议、报错或目标产出，不要只写高层抽象词。"
        )
    )
    domain_keywords: list[str] = Field(
        default_factory=list,
        description=(
            "1到4个预期在目标技能正文中出现的技术名词、CLI 命令、库名或 API 名。"
        ),
    )


@tool(TOOL_NAME, args_schema=SearchSkillsInput)
async def search_skills(
    task_query: str,
    domain_keywords: list[str] | None = None,
    runtime: ToolRuntime[None, None] = None,
) -> str:
    """在技能目录中按任务意图检索候选技能，只返回 skill_id、name 和 description。

    【何时必须调用】：
    1. 当前任务超出已有技能索引覆盖，需要按意图查找技能。
    2. 涉及内部 SOP、企业规范、框架或复杂排障，索引里对不上具体技能。
    【何时严禁调用】：
    1. 索引已有匹配技能：直接把它的 skill_id 交回 lead。
    2. 纯通用问答、润色/翻译、基础算法，或仅凭文件读写就能完成。
    3. 当前任务里已经用相同或高度相似的意图检索过。

    Args:
        task_query: 自包含的完整任务意图（禁止代词，带技术栈/报错/产出）。
        domain_keywords: 1~4 个技术名词、命令、库名或 API 名。
    """
    config = safe_app_config()
    if config is None:
        return "错误：应用配置不可用"
    if not config.skills.search.enabled:
        return json.dumps(
            {"skills": [], "message": ("技能检索在本部署已关闭。",)},
            ensure_ascii=False,
        )
    query = (task_query or "").strip()
    if not query:
        return json.dumps(
            {"skills": [], "message": ("task_query 不能为空：请重写成一句自包含的任务描述。",)},
            ensure_ascii=False,
        )
    keywords = _clean_keywords(domain_keywords)
    user_id = runtime_user_bucket(runtime) if runtime is not None else DEFAULT_USER_ID
    service = get_skill_runtime()
    if service is None:
        # There is no in-process BM25/RRF fallback: without the Milvus-backed
        # catalog runtime this tool fails closed instead of ranking locally.
        return json.dumps(
            {"skills": [], "message": UNAVAILABLE_MESSAGE}, ensure_ascii=False
        )
    return await _search_catalog(service, user_id=user_id, query=query, keywords=keywords)


async def _search_catalog(
    service: object,
    *,
    user_id: str,
    query: str,
    keywords: list[str],
) -> str:
    try:
        outcome = await service.search(  # type: ignore[attr-defined]
            user_id=user_id,
            task_query=query,
            domain_keywords=keywords,
            available_skills=None,
        )
    except SkillSearchUnavailable:
        logger.warning("Skill search catalog is unavailable; failing closed")
        return json.dumps(
            {"skills": [], "message": UNAVAILABLE_MESSAGE}, ensure_ascii=False
        )
    if not isinstance(outcome, SkillSearchOutcome):
        return json.dumps(
            {"skills": [], "message": UNAVAILABLE_MESSAGE}, ensure_ascii=False
        )
    retrieval = {
        "channels": list(outcome.channels),
        "candidates": outcome.candidates,
        "reranked": outcome.reranked,
        "warnings": list(outcome.warnings),
    }
    metadatas = [
        {
            "skill_id": hit.skill.skill_id or "",
            "name": hit.skill.name,
            "description": hit.skill.description,
        }
        for hit in outcome.hits
        if hit.skill.skill_id
    ]
    return _metadata_payload(metadatas, retrieval)


def _metadata_payload(skills: list[dict], retrieval: dict) -> str:
    if not skills:
        return json.dumps(
            {"skills": [], "retrieval": retrieval, "message": EMPTY_RESULT_MESSAGE},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "skills": skills,
            "retrieval": retrieval,
            "message": ("只返回元数据；需要正文时请调用 load_skills(skill_id)。",),
        },
        ensure_ascii=False,
    )


def _clean_keywords(domain_keywords: object) -> list[str]:
    if not isinstance(domain_keywords, list):
        return []
    cleaned: list[str] = []
    for item in domain_keywords:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned[:MAX_DOMAIN_KEYWORDS]


__all__ = ["EMPTY_RESULT_MESSAGE", "TOOL_NAME", "UNAVAILABLE_MESSAGE", "search_skills"]
