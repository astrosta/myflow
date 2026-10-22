"""L1：describe_skill —— 只给元数据，不给正文（M7 §3.5）。

正文进工具结果就会进 checkpoint 和后续每一次模型调用，等于绕开"正文按需"。
要正文就 read_file —— 那条路径同时完成自主激活（§3.10）。
"""

from __future__ import annotations

import json
import logging

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agentkit.sandbox.runtime import runtime_user_bucket
from agentkit.sandbox.security import safe_app_config
from agentkit.skills.catalog import DEFAULT_CATALOG_LIMIT
from agentkit.skills.describe import describe_skills
from agentkit.skills.storage import SkillRegistry

logger = logging.getLogger(__name__)

TOOL_NAME = "describe_skill"


@tool
def describe_skill(query: str = "", runtime: ToolRuntime = None) -> str:
    """查看当前部署中可用的技能。

    Args:
        query: "" 返回前几个技能；"select:a,b" 精确返回这些技能（不设数量上限）；
            "+prefix" 按名称前缀匹配；其他内容按名称和描述做正则匹配
            （模式无效时退化为普通子串匹配）。
    """
    config = safe_app_config()
    if config is None:
        return "错误：应用配置不可用"
    registry = SkillRegistry(
        config=config.skills,
        user_id=runtime_user_bucket(runtime) if runtime is not None else None,
    )
    skills = registry.visible()  # 加载失败 → 空（fail-closed）
    payload = describe_skills(skills, query, limit=DEFAULT_CATALOG_LIMIT)
    return json.dumps({"skills": payload}, ensure_ascii=False)
