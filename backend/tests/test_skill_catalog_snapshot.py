"""Immutable per-user catalog snapshot, prompt roots, and registry."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentkit.skills.catalog import (
    SkillCatalogService,
    SkillSnapshot,
    get_skill_snapshot,
    set_skill_snapshot,
)
from agentkit.skills.domain import SkillScope, SkillVersionRecord, VisibleSkill
from agentkit.skills.prompt import _cache_key, build_skills_section, clear_skill_prompt_cache
from agentkit.skills.storage import SkillRegistry
from agentkit.skills.types import Skill, SkillCategory
from fakes.skill_tree import skills_config

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
COMMIT_ONE = "a" * 40
COMMIT_TWO = "b" * 40


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


def _skill(skill_id: str, name: str, owner: str) -> Skill:
    return Skill(
        name=name,
        description=f"{name} description",
        license=None,
        skill_dir=Path("unused"),
        skill_file=Path("unused/SKILL.md"),
        relative_path=Path(name),
        category=SkillCategory.CUSTOM,
        enabled=True,
        skill_id=skill_id,
        scope=SkillScope.PRIVATE,
        owner_user_id=owner,
        version_id=f"ver-{skill_id}",
        skill_version="v1",
    )


def _private(
    skill_id: str,
    name: str,
    owner: str = "u1",
    *,
    commit: str = COMMIT_ONE,
    path: str | None = None,
) -> VisibleSkill:
    version = _version(
        skill_id,
        commit=commit,
        path=path or f"users/{owner}/{skill_id}/SKILL.md",
    )
    return VisibleSkill(
        skill=_skill(skill_id, name, owner),
        scope=SkillScope.PRIVATE,
        version=version,
    )


@pytest.fixture(name="snapshot")
def snapshot_fixture() -> SkillSnapshot:
    return SkillSnapshot(
        epoch="e1",
        private_by_owner={
            "u1": (_private("s1", "alpha").skill, _private("s2", "beta").skill),
            "u2": (_private("s3", "gamma", "u2").skill,),
        },
    )


def test_snapshot_visible_for_filters_by_owner(snapshot: SkillSnapshot) -> None:
    assert [s.skill_id for s in snapshot.visible_for("u1", available_skills=None)] == [
        "s1",
        "s2",
    ]
    assert [s.skill_id for s in snapshot.visible_for("u2", available_skills=None)] == ["s3"]
    assert snapshot.visible_for("u3", available_skills=None) == []
    assert snapshot.visible_for(None, available_skills=None) == []


def test_visible_for_applies_the_allowlist_and_db_switch(snapshot: SkillSnapshot) -> None:
    assert [
        s.skill_id
        for s in snapshot.visible_for("u1", available_skills=frozenset({"beta"}))
    ] == ["s2"]

    disabled = replace(snapshot.private_by_owner["u1"][0], enabled=False)
    switched = SkillSnapshot(
        epoch="e2",
        private_by_owner={
            **snapshot.private_by_owner,
            "u1": (disabled, snapshot.private_by_owner["u1"][1]),
        },
    )
    assert [s.skill_id for s in switched.visible_for("u1", available_skills=None)] == ["s2"]


def test_private_skill_rows_include_their_container_path(tmp_path: Path) -> None:
    private = tuple(_private(f"s{i}", f"skill-{i}").skill for i in range(9))
    switched = SkillSnapshot(epoch="e4", private_by_owner={"u1": private})
    section = build_skills_section(
        config=skills_config(tmp_path),
        registry=SkillRegistry(
            config=skills_config(tmp_path), snapshot=switched, user_id="u1"
        ),
    )

    for index in range(9):
        assert f"- skill-{index} [private]" in section
        assert f"path=/mnt/skills/private/skill-{index}/SKILL.md" in section


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
    one = _private("s1", "alpha")
    two = _private("s2", "beta", commit=COMMIT_TWO)
    store = FakeStore([one, two])
    content = FakeContent(tmp_path / "cache")
    first_file = _materialize(content, COMMIT_ONE, one.version.skill_path)
    second_file = _materialize(content, COMMIT_TWO, two.version.skill_path)
    monkeypatch.setattr(
        "agentkit.skills.catalog.set_skill_snapshot",
        lambda snapshot: setattr(monkeypatch, "_published", snapshot),
    )

    snapshot = await SkillCatalogService(store, content).refresh()  # type: ignore[arg-type]

    assert store.calls == 1
    assert content.ensured == [COMMIT_ONE, COMMIT_TWO]
    assert [skill.skill_id for skill in snapshot.private_by_owner["u1"]] == ["s1", "s2"]
    assert snapshot.private_by_owner["u1"][0].skill_file == first_file
    assert snapshot.private_by_owner["u1"][1].skill_file == second_file
    assert (
        snapshot.private_by_owner["u1"][0].container_file
        == "/mnt/skills/private/alpha/SKILL.md"
    )


@pytest.mark.asyncio
async def test_refresh_skips_missing_content_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    one = _private("s1", "alpha")
    content = FakeContent(tmp_path / "cache")
    content.fail.add(COMMIT_ONE)
    caplog.set_level(logging.WARNING, logger="agentkit.skills.catalog")

    snapshot = await SkillCatalogService(FakeStore([one]), content).refresh()  # type: ignore[arg-type]

    assert snapshot.private_by_owner == {}
    assert "Skipping skill" in caplog.text


@pytest.mark.asyncio
async def test_refresh_sets_a_new_epoch_and_publishes_the_process_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = FakeContent(tmp_path / "cache")
    published: list[SkillSnapshot] = []
    monkeypatch.setattr("agentkit.skills.catalog.set_skill_snapshot", published.append)
    service = SkillCatalogService(FakeStore([]), content)  # type: ignore[arg-type]

    first = await service.refresh()
    second = await service.refresh()

    assert first.epoch and second.epoch and first.epoch != second.epoch
    assert published == [first, second]


def test_get_and_set_skill_snapshot_are_process_wide() -> None:
    original = get_skill_snapshot()
    replacement = SkillSnapshot("global")
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
    assert set(registry.index_by_container_file()) == {
        "/mnt/skills/private/alpha/SKILL.md",
        "/mnt/skills/private/beta/SKILL.md",
    }
    other = SkillRegistry(config=config, snapshot=snapshot, user_id="u2")
    assert [skill.skill_id for skill in other.visible()] == ["s3"]


def test_legacy_filesystem_registry_is_unchanged_without_a_snapshot(tmp_path: Path) -> None:
    from fakes.skill_tree import write_skill

    write_skill(tmp_path, "public", "legacy")
    registry = SkillRegistry(config=skills_config(tmp_path), snapshot=None)

    assert [skill.name for skill in registry.visible()] == ["legacy"]
    assert registry.find("legacy") is not None


def test_prompt_renders_only_the_private_root(
    snapshot: SkillSnapshot, tmp_path: Path
) -> None:
    clear_skill_prompt_cache()
    config = skills_config(tmp_path)
    registry = SkillRegistry(
        config=config, snapshot=snapshot, user_id="u1", available_skills=None
    )

    text = build_skills_section(config=config, registry=registry)

    assert "/mnt/skills/private/" in text
    assert "/mnt/skills/private/<name>/SKILL.md" in text
    assert "/mnt/skills/public/" not in text
    assert "- alpha [private]:" in text
    assert "- beta [private]:" in text
    assert str(tmp_path) not in text


def test_a_snapshot_registry_can_be_built_without_the_optional_config(
    snapshot: SkillSnapshot, tmp_path: Path
) -> None:
    registry = SkillRegistry(snapshot=snapshot, user_id="u1")
    text = build_skills_section(config=skills_config(tmp_path), registry=registry)
    assert "/mnt/skills/private/" in text


def test_prompt_cache_key_includes_owner_version_scope_and_description() -> None:
    base = _private("s1", "alpha").skill
    variants = [
        base,
        replace(base, scope=SkillScope.BUILTIN),
        replace(base, owner_user_id="u2"),
        replace(base, version_id="other-version"),
        replace(base, description="changed description"),
    ]

    keys = {_cache_key([skill], 120) for skill in variants}
    assert len(keys) == len(variants)


def test_render_skill_index_marks_private_scope() -> None:
    skills = [_private("s1", "alpha").skill]
    from agentkit.skills.prompt import render_skill_index

    rendered = render_skill_index(skills, max_description_chars=120)
    assert "- alpha [private]:" in rendered
    assert "[public]" not in rendered


@pytest.mark.asyncio
async def test_refresh_uses_the_configured_container_root(tmp_path: Path) -> None:
    one = _private("s1", "alpha")
    content = FakeContent(tmp_path / "cache")
    _materialize(content, COMMIT_ONE, one.version.skill_path)

    snapshot = await SkillCatalogService(  # type: ignore[arg-type]
        FakeStore([one]), content, "/opt/skills"
    ).refresh()

    assert snapshot.private_by_owner["u1"][0].container_path == "/opt/skills"
    assert (
        snapshot.private_by_owner["u1"][0].container_file
        == "/opt/skills/private/alpha/SKILL.md"
    )
