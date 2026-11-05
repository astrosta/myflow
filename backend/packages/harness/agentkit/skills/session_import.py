"""Import OpenViking session-extracted skills into the skill catalog.

OpenViking writes generated skills under the user's skill directory and reports
their root URIs as session_skill_uris on the completed commit task. This module
reads each generated SKILL.md and creates an extracted, unscanned private draft.
It never scans or activates anything: the normal skills UI owns that explicit
user decision.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

SESSION_SKILLS_ROOT = "skills"


class SkillTextReader(Protocol):
    """Minimal OpenViking read surface the importer needs."""

    async def read_text(self, ov_user: str, uri: str) -> str: ...


@dataclass(frozen=True, slots=True)
class ImportedSessionSkill:
    skill_id: str
    source_uri: str


class SessionSkillImporter:
    def __init__(
        self,
        author: Any,
        reader: SkillTextReader,
        *,
        max_skills_per_task: int = 20,
    ) -> None:
        self._author = author
        self._reader = reader
        self._max_skills_per_task = max(1, int(max_skills_per_task))

    async def import_skills(
        self,
        *,
        owner_user_id: str,
        ov_user: str,
        task_id: str,
        skill_uris: Iterable[str],
    ) -> list[ImportedSessionSkill]:
        imported: list[ImportedSessionSkill] = []
        seen: set[str] = set()
        for raw_uri in list(skill_uris)[: self._max_skills_per_task]:
            source_uri = _skill_root(raw_uri, ov_user=ov_user)
            if source_uri is None or source_uri in seen:
                if source_uri is None:
                    logger.warning("Ignoring invalid session skill URI: %r", str(raw_uri)[:300])
                continue
            seen.add(source_uri)
            try:
                skill_md = await self._reader.read_text(
                    ov_user, f"{source_uri}/SKILL.md"
                )
                record = await self._author.import_extracted_skill(
                    owner_user_id=owner_user_id,
                    skill_md=skill_md,
                    source_uri=source_uri,
                    source_task_id=task_id,
                )
            except Exception:
                logger.warning(
                    "Could not import session skill %s from task %s; continuing",
                    source_uri,
                    task_id,
                    exc_info=True,
                )
                continue
            imported.append(
                ImportedSessionSkill(
                    skill_id=str(record.id),
                    source_uri=source_uri,
                )
            )
        return imported


def _skill_root(raw_uri: str, *, ov_user: str) -> str | None:
    """Validate a generated-skill URI and return its root without SKILL.md."""

    if not isinstance(raw_uri, str):
        return None
    uri = raw_uri.strip().rstrip("/")
    if not uri.startswith("viking://"):
        return None
    prefix = f"viking://user/{ov_user}/{SESSION_SKILLS_ROOT}/"
    if not uri.startswith(prefix):
        return None
    suffix = uri[len(prefix) :]
    if not suffix or ".." in suffix.split("/"):
        return None
    if suffix.endswith("/SKILL.md"):
        root_suffix = suffix[: -len("/SKILL.md")]
        if not root_suffix:
            return None
        return f"{prefix}{root_suffix}"
    return f"{prefix}{suffix}"


__all__ = [
    "ImportedSessionSkill",
    "SESSION_SKILLS_ROOT",
    "SessionSkillImporter",
    "SkillTextReader",
]
