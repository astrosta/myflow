"""技能目录与索引渲染参数（M7 §3.1/§3.5）。

**没有模式开关**：索引（L0）是唯一形态，正文一律按需（§3.5）。旧设计的
deferred_discovery 被刻意删掉了——两套渲染等于两套提示词契约与两套测试。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agentkit.config.reload_boundary import format_field_description
from agentkit.config.runtime_paths import resolve_path

DEFAULT_SKILLS_PATH = "skills"
DEFAULT_SKILLS_CONTAINER_PATH = "/mnt/skills"
DEFAULT_INDEX_DESCRIPTION_MAX_CHARS = 120


class SkillsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        default=DEFAULT_SKILLS_PATH,
        description="技能树根目录。相对路径按项目根解析；其下是 public/ 与 custom/。",
    )
    container_path: str = Field(
        default=DEFAULT_SKILLS_CONTAINER_PATH,
        description=format_field_description("skills.container_path"),
    )
    index_description_max_chars: int = Field(
        default=DEFAULT_INDEX_DESCRIPTION_MAX_CHARS,
        ge=1,
        description="L0 索引里每行描述截断到多少字符。索引必须有界，否则每加一个技能就重写一遍系统提示词。",
    )

    def root_dir(self) -> Path:
        """宿主上的技能树根目录（用于扫描）。"""
        return resolve_path(self.path)
