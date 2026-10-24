"""Task 13: immutable catalog snapshot, prompt roots, and snapshot-aware registry."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentkit.config.skills_prefs import write_user_skill_pref
from agentkit.skills.catalog import (
    SkillCatalogService,
    SkillSnapshot,
    get_skill_snapshot,
    set_skill_snapshot,
)
from agentkit.skills.domain import (
    SkillReleaseRecord,
    SkillScope,
    SkillVersionRecord,
    VisibleSkill,
)
from agentkit.skills.prompt import (
    _cache_key,
    build_skills_section,
    clear_skill_prompt_cache,
    render_skill_index,
)
from agentkit.skills.storage import SkillRegistry
from agentkit.skills.types import Skill, SkillCategory
from fakes.skill_tree import skills_config

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
COMMIT_PUBLIC = "a" * 40
COMMIT_PRIVATE = "b" * 40


def _version(skill_id: str, *, commit: str, path: str, version: str = "v1") -> SkillVersionRecord:
    return SkillVersionRecord(
        id=f"ver-{skill_id}",
        skill_id=skill_id,
        skill_version=version,
        version_seq=1,
        package_sha256="0" * 64,
        skill_md_sha256="0" * 64,
        metadata={},
        draft_ref="refs/heads/myflow/drafts/u1",
        draft_commit=commit,
        skill_path=path,
        index_status="ready",
        created_at=NOW,
    )


def _skill(
    *,
    skill_id: str,
    name: str,
    scope: SkillScope,
    owner: str | None,
    version_id: str,
    enabled: bool = True,
) -> Skill:
    return Skill(
        name=name,
        description=f"{name} description",
        license=None,
        skill_dir=Path("unused"),
        skill_file=Path("unused/SKILL.md"),
        relative_path=Path(name),
        category=SkillCategory.CUSTOM,
        enabled=enabled,
        skill_id=skill_id,
        scope=scope,
        owner_user_id=owner,
        version_id=version_id,
        skill_version="v1",
    )


def _public(
    skill_id: str,
    name: str,
    *,
    enabled: bool = True,
    commit: str = COMMIT_PUBLIC,
    path: str | None = None,
) -> VisibleSkill:
    version = _version(skill_id, commit=COMMIT_PRIVATE, path=f"users/u1/{skill_id}/SKILL.md")
    release = SkillReleaseRecord(
        id=f"rel-{skill_id}",
        batch_id="batch",
        version_id=version.id,
        skill_id=skill_id,
        skill_version="v1",
        git_repository="origin",
        git_commit=commit,
        skill_path=path or f"public/{name}/SKILL.md",
        public_name=name,
        source_commit=COMMIT_PRIVATE,
        approved_by="admin",
        created_at=NOW,
    )
    return VisibleSkill(
        skill=_skill(
            skill_id=skill_id,
            name=name,
            scope=SkillScope.PUBLIC,
            owner="u1",
            version_id=version.id,
            enabled=enabled,
        ),
        scope=SkillScope.PUBLIC,
        version=version,
        release=release,
    )


def _private(
    skill_id: str,
    name: str,
    owner: str = "u1",
    *,
    commit: str = COMMIT_PRIVATE,
    path: str | None = None,
) -> VisibleSkill:
    version = _version(
        skill_id,
        commit=commit,
        path=path or f"users/{owner}/{skill_id}/SKILL.md",
    )
    return VisibleSkill(
        skill=_skill(
            skill_id=skill_id,
            name=name,
            scope=SkillScope.PRIVATE,
            owner=owner,
            version_id=version.id,
        ),
        scope=SkillScope.PRIVATE,
        version=version,
        release=None,
    )


@pytest.fixture(name="snapshot")
def snapshot_fixture() -> SkillSnapshot:
    return SkillSnapshot(
        epoch="e1",
        public=(_public("s2", "beta").skill,),
        private_by_owner={"u1": (_private("s1", "alpha").skill,)},
    )


def test_snapshot_visible_for_filters_private_owner(snapshot: SkillSnapshot) -> None:
    assert [s.skill_id for s in snapshot.visible_for("u1", available_skills=None)] == [
        "s1",
        "s2",
    ]
    assert [s.skill_id for s in snapshot.visible_for("u2", available_skills=None)] == ["s2"]
    assert [s.skill_id for s in snapshot.visible_for(None, available_skills=None)] == ["s2"]


def test_private_skill_suppresses_public_skill_for_owner() -> None:
    snapshot = SkillSnapshot(
        epoch="e1",
        public=(_public("s1", "same").skill,),
        private_by_owner={"u1": (_private("s1", "same").skill,)},
    )

    ids = [s.skill_id for s in snapshot.visible_for("u1", available_skills=None)]
    assert ids.count("s1") == 1
    chosen = next(s for s in snapshot.visible_for("u1", available_skills=None) if s.skill_id == "s1")
    assert chosen.scope == "private"
    assert snapshot.visible_for("u2", available_skills=None)[0].scope == "public"


def test_visible_for_applies_allowlist_and_personal_preferences(
    snapshot: SkillSnapshot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MYFLOW_SKILLS_PREFS_PATH", str(tmp_path / "skills_prefs.json"))
    assert [s.skill_id for s in snapshot.visible_for("u1", available_skills=frozenset({"beta"}))] == [
        "s2"
    ]

    write_user_skill_pref("u1", "s1", False)

    assert [s.skill_id for s in snapshot.visible_for("u1", available_skills=None)] == ["s2"]


def test_visible_for_accepts_legacy_name_keyed_preferences(
    snapshot: SkillSnapshot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MYFLOW_SKILLS_PREFS_PATH", str(tmp_path / "skills_prefs.json"))
    write_user_skill_pref("u1", "alpha", False)

    assert [s.skill_id for s in snapshot.visible_for("u1", available_skills=None)] == ["s2"]


def test_globally_disabled_public_skills_are_hidden() -> None:
    snapshot = SkillSnapshot(
        epoch="e1",
        public=(_public("s1", "off", enabled=False).skill, _public("s2", "on").skill),
        private_by_owner={},
    )

    assert [s.skill_id for s in snapshot.visible_for("u1", available_skills=None)] == ["s2"]


def test_the_snapshot_is_immutable(snapshot: SkillSnapshot) -> None:
    with pytest.raises(Exception):
        snapshot.epoch = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        snapshot.private_by_owner["u2"] = ()  # type: ignore[index]


class FakeStore:
    def __init__(self, active: list[VisibleSkill]) -> None:
        self.active = active
        self.calls = 0

    async def all_active(self) -> list[VisibleSkill]:
        self.calls += 1
        return list(self.active)


class FakeContent:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.ensured: list[str] = []
        self.fail: set[str] = set()

    async def ensure(self, commit: str) -> Path:
        self.ensured.append(commit)
        if commit in self.fail:
            raise OSError(f"missing commit {commit}")
        return self.root / "commits" / commit


def _materialize(content: FakeContent, commit: str, path: str) -> Path:
    target = content.root / "commits" / commit / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# body is never read into the snapshot", encoding="utf-8")
    return target


@pytest.mark.asyncio
async def test_refresh_builds_host_paths_without_reading_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    public = _public("s2", "beta")
    private = _private("s1", "alpha")
    store = FakeStore([public, private])
    content = FakeContent(tmp_path / "cache")
    public_file = _materialize(content, COMMIT_PUBLIC, public.release.skill_path)  # type: ignore[union-attr]
    private_file = _materialize(content, COMMIT_PRIVATE, private.version.skill_path)
    monkeypatch.setattr("agentkit.skills.catalog.get_skill_snapshot", lambda: SkillSnapshot("old", (), {}))

    snapshot = await SkillCatalogService(store, content).refresh()  # type: ignore[arg-type]

    assert store.calls == 1
    assert content.ensured == [COMMIT_PUBLIC, COMMIT_PRIVATE]
    assert [skill.skill_id for skill in snapshot.public] == ["s2"]
    assert [skill.skill_id for skill in snapshot.private_by_owner["u1"]] == ["s1"]
    assert snapshot.public[0].skill_file == public_file
    assert snapshot.private_by_owner["u1"][0].skill_file == private_file
    assert snapshot.public[0].container_file == "/mnt/skills/public/beta/SKILL.md"
    assert snapshot.private_by_owner["u1"][0].container_file == "/mnt/skills/private/alpha/SKILL.md"
    assert get_skill_snapshot() is snapshot


@pytest.mark.asyncio
async def test_refresh_skips_missing_content_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    public = _public("s2", "beta")
    private = _private("s1", "alpha")
    content = FakeContent(tmp_path / "cache")
    _materialize(content, COMMIT_PUBLIC, public.release.skill_path)  # type: ignore[union-attr]
    content.fail.add(COMMIT_PRIVATE)
    caplog.set_level(logging.WARNING, logger="agentkit.skills.catalog")

    snapshot = await SkillCatalogService(FakeStore([public, private]), content).refresh()  # type: ignore[arg-type]

    assert [skill.skill_id for skill in snapshot.public] == ["s2"]
    assert snapshot.private_by_owner == {}
    assert "Skipping skill" in caplog.text


@pytest.mark.asyncio
async def test_refresh_fails_closed_when_the_content_store_raises(tmp_path: Path) -> None:
    private = _private("s1", "alpha")
    content = FakeContent(tmp_path / "cache")
    content.fail.add(COMMIT_PRIVATE)

    snapshot = await SkillCatalogService(FakeStore([private]), content).refresh()  # type: ignore[arg-type]

    assert snapshot.public == ()
    assert snapshot.private_by_owner == {}


@pytest.mark.asyncio
async def test_refresh_sets_a_new_epoch_and_updates_the_process_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeStore([])
    content = FakeContent(tmp_path / "cache")
    published: list[SkillSnapshot] = []
    monkeypatch.setattr("agentkit.skills.catalog.set_skill_snapshot", published.append)
    service = SkillCatalogService(store, content)  # type: ignore[arg-type]

    first = await service.refresh()
    second = await service.refresh()

    assert first.epoch and second.epoch and first.epoch != second.epoch
    assert published == [first, second]


def test_get_and_set_skill_snapshot_are_process_wide() -> None:
    original = get_skill_snapshot()
    replacement = SkillSnapshot("global", (), {})
    try:
        set_skill_snapshot(replacement)
        assert get_skill_snapshot() is replacement
    finally:
        set_skill_snapshot(original)


def test_snapshot_registry_uses_the_per_user_view(
    snapshot: SkillSnapshot, tmp_path: Path
) -> None:
    config = skills_config(tmp_path)
    registry = SkillRegistry(
        config=config, snapshot=snapshot, user_id="u1", available_skills=None
    )

    assert [skill.skill_id for skill in registry.visible()] == ["s1", "s2"]
    assert registry.find("alpha").scope == "private"
    assert registry.find("beta").container_file == "/mnt/skills/public/beta/SKILL.md"
    assert set(registry.index_by_container_file()) == {
        "/mnt/skills/private/alpha/SKILL.md",
        "/mnt/skills/public/beta/SKILL.md",
    }


def test_snapshot_registry_honors_the_allowlist_and_owner(
    snapshot: SkillSnapshot, tmp_path: Path
) -> None:
    config = skills_config(tmp_path)

    other = SkillRegistry(
        config=config, snapshot=snapshot, user_id="u2", available_skills=None
    )
    assert [skill.skill_id for skill in other.visible()] == ["s2"]

    narrowed = SkillRegistry(
        config=config,
        snapshot=snapshot,
        user_id="u1",
        available_skills=frozenset({"beta"}),
    )
    assert [skill.skill_id for skill in narrowed.visible()] == ["s2"]


def test_find_prefers_a_private_hit_when_names_collide(tmp_path: Path) -> None:
    snapshot = SkillSnapshot(
        epoch="e1",
        public=(_public("s1", "same").skill,),
        private_by_owner={"u1": (_private("s2", "same").skill,)},
    )
    registry = SkillRegistry(config=skills_config(tmp_path), snapshot=snapshot, user_id="u1")

    found = registry.find("same")

    assert found is not None
    assert found.skill_id == "s2"
    assert found.scope == "private"


def test_legacy_filesystem_registry_is_unchanged_without_a_snapshot(tmp_path: Path) -> None:
    from fakes.skill_tree import write_skill

    write_skill(tmp_path, "public", "legacy")
    registry = SkillRegistry(config=skills_config(tmp_path), snapshot=None)

    assert [skill.name for skill in registry.visible()] == ["legacy"]
    assert registry.find("legacy") is not None


def test_prompt_renders_public_and_private_roots(
    snapshot: SkillSnapshot, tmp_path: Path
) -> None:
    clear_skill_prompt_cache()
    config = skills_config(tmp_path)
    registry = SkillRegistry(
        config=config, snapshot=snapshot, user_id="u1", available_skills=None
    )

    text = build_skills_section(config=config, registry=registry)

    assert "/mnt/skills/public/" in text
    assert "/mnt/skills/private/" in text
    assert "/mnt/skills/public/<name>/SKILL.md" in text
    assert "/mnt/skills/private/<name>/SKILL.md" in text
    assert "- alpha [private]:" in text
    assert "- beta [public]:" in text
    assert str(tmp_path) not in text


def test_a_snapshot_registry_can_be_built_without_the_optional_config(
    snapshot: SkillSnapshot, tmp_path: Path
) -> None:
    registry = SkillRegistry(snapshot=snapshot, user_id="u1")

    assert [skill.skill_id for skill in registry.visible()] == ["s1", "s2"]
    text = build_skills_section(config=skills_config(tmp_path), registry=registry)
    assert "/mnt/skills/private/" in text


def test_prompt_does_not_show_legacy_custom_roots_for_a_catalog_snapshot(
    snapshot: SkillSnapshot, tmp_path: Path
) -> None:
    clear_skill_prompt_cache()
    config = skills_config(tmp_path)
    registry = SkillRegistry(config=config, snapshot=snapshot, user_id="u1")

    text = build_skills_section(config=config, registry=registry)

    assert "public 或 custom" not in text
    assert "[custom]" not in text


def test_render_skill_index_marks_catalog_scope() -> None:
    skills = [_public("s1", "alpha").skill, _private("s2", "beta").skill]

    rendered = render_skill_index(skills, max_description_chars=120)

    assert "- alpha [public]:" in rendered
    assert "- beta [private]:" in rendered


def test_prompt_cache_key_includes_scope_owner_version_and_description() -> None:
    base = _public("s1", "alpha").skill
    variants = [
        base,
        replace(base, scope=SkillScope.PRIVATE),
        replace(base, owner_user_id="u2"),
        replace(base, version_id="other-version"),
        replace(base, description="changed description"),
    ]

    keys = {_cache_key([skill], 120) for skill in variants}

    assert len(keys) == len(variants)


def test_snapshot_visible_for_is_stable_and_returns_private_before_find(
    snapshot: SkillSnapshot,
) -> None:
    first = snapshot.visible_for("u1", available_skills=None)
    second = snapshot.visible_for("u1", available_skills=None)

    assert [skill.skill_id for skill in first] == [skill.skill_id for skill in second]
    assert [skill.skill_id for skill in first] == ["s1", "s2"]
