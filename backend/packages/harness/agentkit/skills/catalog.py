"""Process-wide immutable view of the active skill catalog.

The synchronous prompt and middleware paths cannot query PostgreSQL.  This
module materializes one immutable snapshot of every active public/private row,
with only metadata and host cache paths; bodies stay on disk and are read on
demand.  SkillSnapshot.visible_for is the per-user projection of that global
snapshot, so private metadata never leaks to another user's prompt.
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

from agentkit.skills import prefs as skill_prefs
from agentkit.skills.domain import SkillScope, VisibleSkill
from agentkit.skills.package import validate_skill_path
from agentkit.skills.types import Skill

if TYPE_CHECKING:
    from agentkit.skills.content import SkillContentStore
    from agentkit.skills.store.sql import SqlSkillStore

logger = logging.getLogger(__name__)

_snapshot_lock = threading.Lock()
_snapshot = None  # type: SkillSnapshot | None


def _scope_value(scope: object) -> str | None:
    if scope is None:
        return None
    return str(getattr(scope, "value", scope))


@dataclass(frozen=True, slots=True)
class SkillSnapshot:
    """One immutable catalog generation.

    public is the complete active public set.  private_by_owner contains every
    owner's private active set.  The mapping and tuples are copied and frozen in
    __post_init__; callers cannot mutate a published snapshot.
    """

    epoch: str
    public: tuple[Skill, ...]
    private_by_owner: Mapping[str, tuple[Skill, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "public", tuple(self.public))
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
        """Public plus this owner's private skills, with private precedence."""
        return self._visible_for(
            user_id,
            available_skills=available_skills,
            prefs=skill_prefs.read_user_skill_prefs(user_id),
        )

    def _visible_for(
        self,
        user_id: str | None,
        *,
        available_skills: frozenset[str] | None,
        prefs: Mapping[str, bool] | None,
    ) -> list[Skill]:
        """Internal variant used by SkillRegistry to honor a custom prefs path.

        Suppression happens before preference filtering: an owner who turned
        off their private skill must not silently fall back to the public row
        with the same skill id.
        """
        private = tuple(self.private_by_owner.get(user_id, ())) if user_id else ()
        private_ids = {
            skill.skill_id
            for skill in private
            if skill.skill_id is not None
        }
        candidates: list[Skill] = [
            skill
            for skill in self.public
            if skill.skill_id is None or skill.skill_id not in private_ids
        ]
        candidates.extend(private)

        seen: set[tuple[object, object, object]] = set()
        visible: list[Skill] = []
        for skill in candidates:
            identity = (skill.skill_id, skill.name, skill.version_id)
            if identity in seen:
                continue
            seen.add(identity)
            if not skill_prefs.effective_enabled(skill, prefs):
                continue
            if available_skills is not None and skill.name not in available_skills:
                continue
            visible.append(skill)

        # Stable prompt/prefix behavior: never depend on database row order.
        return sorted(
            visible,
            key=lambda skill: (
                skill.name,
                0 if _scope_value(skill.scope) == SkillScope.PUBLIC.value else 1,
                skill.skill_id or "",
            ),
        )


def _empty_snapshot() -> SkillSnapshot:
    return SkillSnapshot(epoch="", public=(), private_by_owner={})


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


def _commit_for(visible: VisibleSkill) -> str | None:
    if visible.scope is SkillScope.PUBLIC:
        release = visible.release
        return release.git_commit if release is not None else None
    return visible.version.draft_commit


def _path_for(visible: VisibleSkill) -> str | None:
    if visible.scope is SkillScope.PUBLIC:
        release = visible.release
        return release.skill_path if release is not None else None
    return visible.version.skill_path


def _snapshot_skill(
    visible: VisibleSkill,
    *,
    content_root: Path,
) -> Skill | None:
    """Bind one active row to its host cache path without reading the body."""
    commit = _commit_for(visible)
    raw_path = _path_for(visible)
    if not commit or not raw_path:
        logger.warning(
            "Skipping skill %s (%s): active pointer has no content commit/path",
            visible.skill.name,
            _scope_value(visible.scope),
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
    )


class SkillCatalogService:
    """Build and publish immutable catalog snapshots from PostgreSQL + Git cache."""

    def __init__(self, store: SqlSkillStore, content: SkillContentStore) -> None:
        self._store = store
        self._content = content

    async def refresh(self) -> SkillSnapshot:
        """Read every active pointer and publish the next immutable generation.

        A missing/unreadable commit cache drops that one skill with a warning;
        one broken row must not take down the whole prompt index.  The previous
        global snapshot is retained until the new one has been fully built.
        """
        active = await self._store.all_active()
        public: list[Skill] = []
        private: dict[str, list[Skill]] = {}

        for visible in active:
            commit = _commit_for(visible)
            if not commit:
                logger.warning(
                    "Skipping skill %s (%s): active pointer has no content commit",
                    visible.skill.name,
                    _scope_value(visible.scope),
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

            skill = _snapshot_skill(visible, content_root=content_root)
            if skill is None:
                continue
            if visible.scope is SkillScope.PUBLIC:
                public.append(skill)
            else:
                owner = skill.owner_user_id
                if not owner:
                    logger.warning("Skipping private skill %s: owner is missing", skill.name)
                    continue
                private.setdefault(owner, []).append(skill)

        snapshot = SkillSnapshot(
            epoch=uuid.uuid4().hex,
            public=tuple(public),
            private_by_owner={owner: tuple(skills) for owner, skills in private.items()},
        )
        set_skill_snapshot(snapshot)
        return snapshot

    def current(self) -> SkillSnapshot:
        """Return the process-wide current generation."""
        return get_skill_snapshot()


__all__ = [
    "SkillCatalogService",
    "SkillSnapshot",
    "get_skill_snapshot",
    "set_skill_snapshot",
]
