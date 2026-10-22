from pydantic import BaseModel, ConfigDict, Field


class TitleConfig(BaseModel):
    """First-turn thread title. ``model_name`` empty means truncate the user's words."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_words: int = Field(default=6, ge=1, le=20)
    max_chars: int = Field(default=60, ge=10, le=200)
    model_name: str | None = None
    prompt_template: str = (
        "为下面这段对话生成一个简洁标题（最多 {max_words} 个词）。\n"
        "用户：{user_msg}\n助手：{assistant_msg}\n\n"
        "只返回标题本身，不要引号，不要解释。"
    )


def get_title_config() -> TitleConfig:
    """Last resort when a middleware was built without a config snapshot."""
    from agentkit.config.app_config import get_app_config

    return get_app_config().title
