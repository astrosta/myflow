"""DB-backed profile and local personalization documents."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
async def test_profile_rendering_uses_labels_and_priority(profile) -> None:
    service, _tmp = profile
    view = await service.get_snapshot("u1")

    rendered = render_profile_view(
        type("View", (), {"profile_fields": view.fields})(),
        max_tokens=100,
    )

    assert "希望被称呼为：小王" in rendered.text
    assert "identity.preferred_name" not in rendered.text
    assert rendered.text.index("希望被称呼为") < rendered.text.index("主要语言")
    assert rendered.token_count > 0


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
