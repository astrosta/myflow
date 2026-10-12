from agentkit.config import AppConfig
from agentkit.skills.prompt import build_skills_section

SYSTEM_PROMPT_TEMPLATE = """
<role>
You are {agent_name}, an open-source super agent.
</role>
{soul}
{thinking_style}
{skills_section}
{subagent_section}
{critical_reminders}
"""


def apply_prompt_template(
    *,
    agent_name: str | None = None,
    subagent_enabled: bool = False,
    app_config: AppConfig | None = None,
    user_id: str | None = None,
    available_skills: frozenset[str] | None = None,
) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        agent_name=agent_name or "MyFlow",
        soul=_build_soul_section(),
        thinking_style=_build_thinking_style_section(),
        skills_section=_build_skills_section(
            app_config, user_id=user_id, available_skills=available_skills
        ),
        subagent_section=_build_subagent_section(subagent_enabled),
        critical_reminders=_build_critical_reminders_section(),
    )


def _build_soul_section() -> str:
    """Static soul copy. M1 leaves the slot empty."""
    return ""


def _build_thinking_style_section() -> str:
    """Static thinking-style copy. M1 leaves the slot empty."""
    return ""


def _build_skills_section(
    app_config: AppConfig | None = None,
    *,
    user_id: str | None = None,
    available_skills: frozenset[str] | None = None,
) -> str:
    """M7 §3.5：静态提示词的**尾部**只有 L0 索引（每技能一行，描述截断）。

    0 个技能 → 整个块不出现；注册表加载失败 → 也是空（fail-closed）。
    """
    if app_config is None:
        return ""
    return build_skills_section(
        config=app_config.skills, user_id=user_id, available_skills=available_skills
    )


def _build_subagent_section(subagent_enabled: bool = False) -> str:
    """M8 fills this section. Until then it stays empty."""
    if not subagent_enabled:
        return ""
    return ""


def _build_critical_reminders_section() -> str:
    """Framework-owned reminders. M1 leaves the slot empty."""
    return ""
