from agentkit.config.app_config import AppConfig

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
) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        agent_name=agent_name or "MyFlow",
        soul=_build_soul_section(),
        thinking_style=_build_thinking_style_section(),
        skills_section=_build_skills_section(app_config),
        subagent_section=_build_subagent_section(subagent_enabled),
        critical_reminders=_build_critical_reminders_section(),
    )


def _build_soul_section() -> str:
    """Static soul copy. M1 leaves the slot empty."""
    return ""


def _build_thinking_style_section() -> str:
    """Static thinking-style copy. M1 leaves the slot empty."""
    return ""


def _build_skills_section(_app_config: AppConfig | None = None) -> str:
    """M7 fills this section. Until then it stays empty."""
    return ""


def _build_subagent_section(subagent_enabled: bool = False) -> str:
    """M8 fills this section. Until then it stays empty."""
    if not subagent_enabled:
        return ""
    return ""


def _build_critical_reminders_section() -> str:
    """Framework-owned reminders. M1 leaves the slot empty."""
    return ""
