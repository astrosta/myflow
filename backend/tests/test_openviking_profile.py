"""DB-backed profile and local personalization documents."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.agents.memory.openviking.models import MemoryProfileDocumentRow
from agentkit.agents.memory.openviking.profile import (
    PersonalizationService,
    ProfileConflict,
    ProfileService,
    render_profile_view,
)
from agentkit.persistence.base import Base


@pytest_asyncio.fixture
async def profile(tmp_path: Path):
    seed = tmp_path / "profile.md"
    seed.write_text(
        "---\npreferred_name: 小王\nlanguage: zh-CN\nformat: [bullets]\n---\n"
        "# 用户画像\n\n- 称呼：小王\n",
        encoding="utf-8",
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=[Base.metadata.tables["memory_profile_documents"]]
            )
        )
    service = ProfileService(async_sessionmaker(engine, expire_on_commit=False), seed)
    yield service, tmp_path
    await engine.dispose()


@pytest.mark.asyncio
async def test_removed_fields_are_ignored_and_dropped_on_next_write(tmp_path: Path) -> None:
    """旧行里的已删除字段不再映射；下一次写入时从 frontmatter 里消失。"""

    seed = tmp_path / "legacy.md"
    seed.write_text(
        "---\npreferred_name: 小王\npronouns: they/them\ntimezone: Asia/Shanghai\n"
        "accessibility_needs: [字号调大]\nboundaries: [不要主动发邮件]\n---\n",
        encoding="utf-8",
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=[Base.metadata.tables["memory_profile_documents"]]
            )
        )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    service = ProfileService(sessions, seed)
    try:
        first = await service.get_snapshot("u1")
        assert first.fields == {"identity.preferred_name": "小王"}

        second = await service.patch("u1", {"work.role": "后端工程师"}, etag=first.etag)
        assert "identity.pronouns" not in second.fields
        assert "identity.timezone" not in second.fields
        assert "accessibility.needs" not in second.fields
        assert "boundaries" not in second.fields

        async with sessions() as session:
            row = await session.get(MemoryProfileDocumentRow, "u1")
            assert row is not None
            assert "pronouns" not in row.content
            assert "timezone" not in row.content
            assert "accessibility_needs" not in row.content
            assert "boundaries" not in row.content
            assert "role: " in row.content
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_profile_seed_patch_and_etag_conflict(profile) -> None:
    service, _tmp = profile
    first = await service.get_snapshot("u1")

    assert first.fields["identity.preferred_name"] == "小王"
    assert first.fields["communication.language"] == "zh-CN"

    second = await service.patch(
        "u1",
        {"communication.tone": "direct", "communication.format": ["steps"]},
        etag=first.etag,
    )
    assert second.fields["communication.tone"] == "direct"
    assert second.fields["communication.format"] == ["steps"]

    with pytest.raises(ProfileConflict):
        await service.patch("u1", {"communication.verbosity": "concise"}, etag=first.etag)


@pytest.mark.asyncio
async def test_concurrent_first_reads_create_one_row(profile) -> None:
    service, _tmp = profile

    views = await asyncio.gather(*(service.get_snapshot("u1") for _ in range(8)))

    assert {view.etag for view in views} == {views[0].etag}
    assert all(view.fields["identity.preferred_name"] == "小王" for view in views)


@pytest.mark.asyncio
async def test_concurrent_patches_with_the_same_etag_have_one_winner(profile) -> None:
    service, _tmp = profile
    first = await service.get_snapshot("u1")

    results = await asyncio.gather(
        service.patch("u1", {"communication.tone": "direct"}, etag=first.etag),
        service.patch("u1", {"communication.tone": "friendly"}, etag=first.etag),
        return_exceptions=True,
    )

    assert sum(isinstance(item, ProfileConflict) for item in results) == 1
    final = await service.get_snapshot("u1")
    assert final.fields["communication.tone"] in {"direct", "friendly"}


@pytest.mark.asyncio
async def test_profile_clear_and_delete(profile) -> None:
    service, _tmp = profile
    first = await service.get_snapshot("u1")
    after = await service.delete_field("u1", "identity.preferred_name", etag=first.etag)
    assert "identity.preferred_name" not in after.fields

    cleared = await service.clear("u1", etag=after.etag)
    assert cleared.fields == {}


@pytest.mark.asyncio
async def test_update_fields_reads_the_current_etag(profile) -> None:
    service, _tmp = profile

    view = await service.update_fields(
        "u1", {"communication.tone": "direct", "work.role": "后端工程师"}
    )

    assert view.fields["communication.tone"] == "direct"
    assert view.fields["work.role"] == "后端工程师"
    # 种子里的字段必须保留：工具是增量写，不是整份覆盖
    assert view.fields["identity.preferred_name"] == "小王"


@pytest.mark.asyncio
async def test_update_fields_retries_once_after_a_conflict(profile, monkeypatch) -> None:
    service, _tmp = profile
    original = service.patch
    attempts = {"count": 0}

    async def flaky_patch(user_id, fields, *, etag):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ProfileConflict("stale etag")
        return await original(user_id, fields, etag=etag)

    monkeypatch.setattr(service, "patch", flaky_patch)

    view = await service.update_fields("u1", {"communication.tone": "friendly"})

    assert attempts["count"] == 2
    assert view.fields["communication.tone"] == "friendly"


@pytest.mark.asyncio
async def test_update_fields_gives_up_on_a_second_conflict(profile, monkeypatch) -> None:
    service, _tmp = profile

    async def always_conflict(user_id, fields, *, etag):
        raise ProfileConflict("stale etag")

    monkeypatch.setattr(service, "patch", always_conflict)

    with pytest.raises(ProfileConflict):
        await service.update_fields("u1", {"communication.tone": "friendly"})


@pytest.mark.asyncio
async def test_profile_rendering_uses_labels_and_priority(profile) -> None:
    service, _tmp = profile
    view = await service.get_snapshot("u1")

    rendered = render_profile_view(
        type("View", (), {"profile_fields": view.fields})(),
        max_tokens=100,
    )

    assert "希望被称呼为：小王" in rendered.text
    assert "identity.preferred_name" not in rendered.text
    assert rendered.text.index("希望被称呼为") < rendered.text.index("语言偏好")
    assert rendered.token_count > 0


@pytest.mark.asyncio
async def test_language_preference_can_follow_conversation(profile) -> None:
    service, _tmp = profile
    first = await service.get_snapshot("u1")

    patched = await service.patch(
        "u1",
        {"communication.language": "auto"},
        etag=first.etag,
    )

    assert patched.fields["communication.language"] == "auto"
    rendered = render_profile_view(
        type("View", (), {"profile_fields": patched.fields})(),
        max_tokens=100,
    )
    assert "语言偏好：跟随当前对话语言" in rendered.text
    assert "auto" not in rendered.text


@pytest.mark.asyncio
async def test_personalization_missing_files_is_empty(profile) -> None:
    service, tmp_path = profile
    personalization = PersonalizationService(
        service,
        identity_path=tmp_path / "missing-identity.md",
        soul_path=tmp_path / "missing-soul.md",
    )
    view = await personalization.render("u1")

    assert view.profile_fields["identity.preferred_name"] == "小王"
    assert view.identity.content == ""
    assert view.soul.content == ""
    assert view.digest
