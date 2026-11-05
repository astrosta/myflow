"""Process-wide immutable view of each user's active skill catalog.

The synchronous prompt and middleware paths cannot query PostgreSQL.  This
module materializes one immutable snapshot of every active private row, with
only metadata and host cache paths; bodies stay on disk and are read on demand.
SkillSnapshot.visible_for is the per-user projection of that global snapshot,
so one user's metadata never leaks into another user's prompt.

There is no public catalog in v2.  A saved draft is not in this snapshot until
its owner completes the scan -> activate flow.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from agentkit.config.skills_config import DEFAULT_SKILLS_CONTAINER_PATH
from agentkit.skills.domain import VisibleSkill
from agentkit.skills.package import validate_skill_path
from agentkit.skills.types import Skill

if TYPE_CHECKING:
    from agentkit.skills.content import SkillContentStore
    from agentkit.skills.store.sql import SqlSkillStore

logger = logging.getLogger(__name__)

_snapshot_lock = threading.Lock()
_snapshot = None  # type: SkillSnapshot | None


@dataclass(frozen=True, slots=True)
class SkillSnapshot:
    """One immutable catalog generation.

    private_by_owner contains every owner's active private set.  The mapping and
    tuples are copied and frozen in __post_init__; callers cannot mutate a
    published snapshot.
    """

    epoch: str
    private_by_owner: Mapping[str, tuple[Skill, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "private_by_owner",
            MappingProxyType(
                {
                    str(owner): tuple(skills)
                    for owner, skills in self.private_by_owner.items()
                }
            ),
        )

    def visible_for(
        self,
        user_id: str | None,
        *,
        available_skills: frozenset[str] | None,
    ) -> list[Skill]:
        """Return only this owner's enabled active skills after the whitelist."""
        return self._visible_for(user_id, available_skills=available_skills)

    def _visible_for(
        self,
        user_id: str | None,
        *,
        available_skills: frozenset[str] | None,
    ) -> list[Skill]:
        candidates = tuple(self.private_by_owner.get(user_id, ())) if user_id else ()
        seen: set[tuple[object, object, object]] = set()
        visible: list[Skill] = []
        for skill in candidates:
            identity = (skill.skill_id, skill.name, skill.version_id)
            if identity in seen:
                continue
            seen.add(identity)
            if not skill.enabled:
                continue
            if available_skills is not None and skill.name not in available_skills:
                continue
            visible.append(skill)

        # Stable prompt/prefix behavior: never depend on database row order.
        return sorted(
            visible,
            key=lambda skill: (skill.name, skill.skill_id or ""),
        )


def _empty_snapshot() -> SkillSnapshot:
    return SkillSnapshot(epoch="")


def get_skill_snapshot() -> SkillSnapshot:
    """Return the current process-wide snapshot (never None)."""
    with _snapshot_lock:
        snapshot = _snapshot
        return snapshot if snapshot is not None else _empty_snapshot()


def set_skill_snapshot(snapshot: SkillSnapshot) -> None:
    """Atomically publish a new snapshot for synchronous readers."""
    global _snapshot
    if not isinstance(snapshot, SkillSnapshot):
        raise TypeError("snapshot must be a SkillSnapshot")
    with _snapshot_lock:
        _snapshot = snapshot


def _snapshot_skill(
    visible: VisibleSkill,
    *,
    content_root: Path,
    container_path: str,
) -> Skill | None:
    """Bind one active row to its host cache path without reading the body."""
    version = visible.version
    commit = version.draft_commit
    raw_path = version.skill_path
    if not commit or not raw_path:
        logger.warning(
            "Skipping skill %s: active pointer has no content commit/path",
            visible.skill.name,
        )
        return None
    try:
        relative = validate_skill_path(raw_path)
    except ValueError as exc:
        logger.warning("Skipping skill %s: invalid content path (%s)", visible.skill.name, exc)
        return None

    cache_file = content_root / relative
    return replace(
        visible.skill,
        skill_dir=cache_file.parent,
        skill_file=cache_file,
        relative_path=Path(visible.skill.name),
        container_path=container_path,
    )


class SkillCatalogService:
    """Build and publish immutable catalog snapshots from PostgreSQL + Git cache."""

    def __init__(
        self,
        store: SqlSkillStore,
        content: SkillContentStore,
        container_path: str = DEFAULT_SKILLS_CONTAINER_PATH,
    ) -> None:
        self._store = store
        self._content = content
        self._container_path = container_path.rstrip("/") or container_path

    async def refresh(self) -> SkillSnapshot:
        """Read every active pointer and publish the next immutable generation.

        A missing/unreadable commit cache drops that one skill with a warning;
        one broken row must not take down the whole prompt index.  The previous
        global snapshot is retained until the new one has been fully built.
        """
        active = await self._store.all_active()
        private: dict[str, list[Skill]] = {}

        for visible in active:
            commit = visible.version.draft_commit
            if not commit:
                logger.warning(
                    "Skipping skill %s: active pointer has no content commit",
                    visible.skill.name,
                )
                continue
            try:
                content_root = await self._content.ensure(commit)
            except Exception as exc:  # noqa: BLE001 - fail closed for this skill
                logger.warning(
                    "Skipping skill %s: content cache unavailable (%s)",
                    visible.skill.name,
                    exc,
                )
                continue

            skill = _snapshot_skill(
                visible,
                content_root=content_root,
                container_path=self._container_path,
            )
            if skill is None:
                continue
            owner = skill.owner_user_id
            if not owner:
                logger.warning("Skipping private skill %s: owner is missing", skill.name)
                continue
            private.setdefault(owner, []).append(skill)

        snapshot = SkillSnapshot(
            epoch=uuid.uuid4().hex,
            private_by_owner={
                owner: tuple(sorted(skills, key=lambda item: item.name))
                for owner, skills in private.items()
            },
        )
        set_skill_snapshot(snapshot)
        return snapshot

    def current(self) -> SkillSnapshot:
        return get_skill_snapshot()


__all__ = [
    "SkillCatalogService",
    "SkillSnapshot",
    "get_skill_snapshot",
    "set_skill_snapshot",
]
