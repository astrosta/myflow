from pydantic import BaseModel, ConfigDict, Field


class TitleConfig(BaseModel):
    """First-turn thread title. ``model_name`` empty means truncate the user's words."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_words: int = Field(default=6, ge=1, le=20)
    max_chars: int = Field(default=60, ge=10, le=200)
    model_name: str | None = None
    prompt_template: str = (
        "Generate a concise title (max {max_words} words) for this conversation.\n"
        "User: {user_msg}\nAssistant: {assistant_msg}\n\n"
        "Return ONLY the title, no quotes, no explanation."
    )


def get_title_config() -> TitleConfig:
    """Last resort when a middleware was built without a config snapshot."""
    from agentkit.config.app_config import get_app_config

    return get_app_config().title
