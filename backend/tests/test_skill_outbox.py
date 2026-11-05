"""Task 11: active-projection outbox worker, reconciliation, and cache pruning."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentkit.persistence.schema import (
    Base,
    SkillIndexOutboxRow,
    SkillRow,
    SkillVersionRow,
)
from agentkit.skills.content import SkillContentStore
from agentkit.skills.domain import PackageManifest, StagedFile
from agentkit.skills.outbox import SkillIndexOutboxWorker
from agentkit.skills.package import build_package
from agentkit.skills.projection import ProjectionRow
from agentkit.skills.store.sql import SqlSkillStore

LIMITS = {"max_files": 20, "max_file_bytes": 4096, "max_package_bytes": 16384}
COMMITS = "abcdefghijklmnop"


def skill_md(name: str, description: str, body: str) -> bytes:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}".encode()


def manifest(name: str, description: str, body: str) -> PackageManifest:
    files = {
        "SKILL.md": StagedFile(
            "SKILL.md", skill_md(name, description, body), "text"
        )
    }
    return build_package(files, **LIMITS)


class FakeProjection:
    def __init__(self) -> None:
        self.rows: dict[str, ProjectionRow] = {}
        self.upserts: list[ProjectionRow] = []
        self.deletes: list[str] = []
        self.keys: list[str] = []

    async def upsert_active(self, row: ProjectionRow) -> None:
        self.upserts.append(row)
        self.rows[row.projection_key] = row
        if row.projection_key not in self.keys:
            self.keys.append(row.projection_key)

    async def delete_active(self, projection_key: str) -> None:
        self.deletes.append(projection_key)
        self.rows.pop(projection_key, None)
        if projection_key in self.keys:
            self.keys.remove(projection_key)

    async def scan_projection_keys(self, *, limit: int) -> tuple[list[str], bool]:
        keys = list(self.keys)[: max(0, int(limit))]
        return keys, len(self.keys) > len(keys)


class FakeContent:
    def __init__(self) -> None:
        self.texts: dict[tuple[str, str], str] = {}
        self.reads: list[tuple[str, str]] = []

    async def read_text(self, commit: str, path: str) -> str:
        self.reads.append((commit, path))
        return self.texts.get((commit, path), "# Body\n")


class FakeEmbedder:
    def __init__(self) -> None:
        self.fail = False
        self.calls: list[tuple[str, ...]] = []

    async def embed_documents(self, texts: list[str] | tuple[str, ...]) -> list[list[float]]:
        self.calls.append(tuple(texts))
        if self.fail:
            raise RuntimeError("embedding service is unavailable")
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


@dataclass
class Harness:
    store: SqlSkillStore
    session_factory: async_sessionmaker[AsyncSession]
    projection: FakeProjection = field(default_factory=FakeProjection)
    content: FakeContent = field(default_factory=FakeContent)
    embedder: FakeEmbedder = field(default_factory=FakeEmbedder)

    def worker(self, *, poll_seconds: float = 0.01) -> SkillIndexOutboxWorker:
        return SkillIndexOutboxWorker(
            self.store,
            self.content,  # type: ignore[arg-type]
            self.embedder,
            self.projection,  # type: ignore[arg-type]
            poll_seconds=poll_seconds,
        )


@pytest.fixture(name="harness")
def harness_fixture(tmp_path: Path):
    async def _build() -> tuple[Harness, object]:
        engine = create_async_engine(
            "sqlite+aiosqlite:///" + (tmp_path / "skills.db").as_posix()
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        return Harness(SqlSkillStore(factory), factory), engine

    built, engine = asyncio.run(_build())
    yield built
    asyncio.run(engine.dispose())


async def save_skill(
    harness: Harness,
    *,
    owner: str,
    name: str,
    seq: int,
    description: str | None = None,
    skill: object | None = None,
) -> tuple[object, object]:
    if skill is None:
        skill = await harness.store.create_skill(owner_user_id=owner, private_name=name)
    description = description or f"{name} description {seq}"
    body = f"# {name} body {seq}\n"
    commit = (COMMITS[seq % len(COMMITS)] * 40)
    path = f"users/{owner}/{skill.id}/SKILL.md"
    version = await harness.store.record_saved_version(
        skill_id=skill.id,
        owner_user_id=owner,
        manifest=manifest(name, description, body),
        draft_ref=f"refs/heads/myflow/drafts/{owner}",
        draft_commit=commit,
        skill_path=path,
    )
    harness.content.texts[(commit, path)] = body

    # v2 lifecycle: saving is inert; a passing scan + explicit activation is what
    # produces the projection event these worker tests consume.
    scan = await harness.store.record_security_scan(
        skill_id=skill.id,
        owner_user_id=owner,
        version_id=version.id,
        status="completed",
        is_safe=True,
        max_severity="SAFE",
        findings=[],
        scanner="cisco-ai-skill-scanner",
        scanner_version="2.3.2",
        duration_ms=1,
    )
    await harness.store.activate_version(
        skill_id=skill.id,
        owner_user_id=owner,
        version_id=version.id,
        scan_id=scan.id,
        actor=owner,
    )
    return skill, version


async def outbox_rows(
    harness: Harness, *, skill_id: str | None = None
) -> list[SkillIndexOutboxRow]:
    async with harness.session_factory() as session:
        statement = select(SkillIndexOutboxRow).order_by(SkillIndexOutboxRow.created_at)
        if skill_id is not None:
            statement = statement.where(SkillIndexOutboxRow.skill_id == skill_id)
        return list((await session.scalars(statement)).all())


async def mark_all_done(harness: Harness) -> None:
    async with harness.session_factory() as session:
        rows = (await session.scalars(select(SkillIndexOutboxRow))).all()
        for row in rows:
            row.status = "done"
        await session.commit()


async def version_status(harness: Harness, version_id: str) -> str | None:
    async with harness.session_factory() as session:
        row = await session.get(SkillVersionRow, version_id)
        return None if row is None else row.index_status


@pytest.mark.asyncio
async def test_pending_upsert_embeds_and_marks_the_current_version_ready(
    harness: Harness,
) -> None:
    skill, version = await save_skill(harness, owner="u1", name="pdf-tools", seq=1)
    worker = harness.worker()

    assert await worker.run_once() == 1

    assert len(harness.projection.upserts) == 1
    row = harness.projection.upserts[0]
    assert row.projection_key == f"private:u1:{skill.id}"
    assert row.scope == "private"
    assert row.owner_user_id == "u1"
    assert row.skill_id == skill.id
    assert row.version_id == version.id
    assert row.skill_version == "v1"
    assert row.enabled is True
    assert row.name == "pdf-tools"
    assert row.description == "pdf-tools description 1"
    assert row.package_sha256 == version.package_sha256
    assert row.skill_md_sha256 == version.skill_md_sha256
    assert row.embedding == [1.0, 0.0, 0.0]
    # 嵌入文本只有元数据：正文改变不影响向量，也不会撞 embedding 的 token 上限。
    assert harness.embedder.calls == [("pdf-tools\npdf-tools description 1",)]
    assert harness.content.reads == []
    assert await version_status(harness, version.id) == "ready"
    [event] = await outbox_rows(harness)
    assert event.status == "done"


@pytest.mark.asyncio
async def test_toggle_event_writes_the_new_enabled_flag(harness: Harness) -> None:
    skill, version = await save_skill(harness, owner="u1", name="pdf-tools", seq=1)
    worker = harness.worker()
    await worker.run_once()

    await harness.store.set_skill_enabled(
        skill_id=skill.id,
        owner_user_id="u1",
        enabled=False,
        index_enabled=True,
    )
    assert await worker.run_once() >= 1

    assert harness.projection.rows[f"private:u1:{skill.id}"].enabled is False
    assert await version_status(harness, version.id) == "ready"


@pytest.mark.asyncio
async def test_start_processes_in_the_background_and_stop_cancels(harness: Harness) -> None:
    await save_skill(harness, owner="u1", name="pdf-tools", seq=1)
    worker = harness.worker(poll_seconds=0.005)

    await worker.start()
    try:
        for _ in range(100):
            if harness.projection.upserts:
                break
            await asyncio.sleep(0.01)
        assert len(harness.projection.upserts) == 1
    finally:
        await worker.stop()

    processed = len(harness.projection.upserts)
    await asyncio.sleep(0.02)
    assert len(harness.projection.upserts) == processed


@pytest.mark.asyncio
async def test_delete_event_calls_delete_active(harness: Harness) -> None:
    skill, _version = await save_skill(harness, owner="u1", name="pdf-tools", seq=1)
    worker = harness.worker()
    assert await worker.run_once() == 1

    assert await harness.store.delete_private_draft(
        skill_id=skill.id, owner_user_id="u1", actor="u1"
    )
    assert await worker.run_once() == 1

    assert harness.projection.deletes == [f"private:u1:{skill.id}"]
    assert len(harness.projection.upserts) == 1
    events = await outbox_rows(harness)
    assert [event.op for event in events] == ["upsert_active", "delete_active"]
    assert all(event.status == "done" for event in events)


@pytest.mark.asyncio
async def test_old_upsert_after_a_newer_private_pointer_is_skipped_and_done(
    harness: Harness,
) -> None:
    skill, first = await save_skill(harness, owner="u1", name="pdf-tools", seq=1)
    _skill, second = await save_skill(
        harness, owner="u1", name="pdf-tools", seq=2, skill=skill
    )
    rows = await outbox_rows(harness, skill_id=skill.id)
    assert len(rows) == 2
    old_event = next(row for row in rows if row.version_id == first.id)
    newest_event = next(row for row in rows if row.version_id == second.id)
    # Leave only the old event pending: the newer event would coalesce it first.
    async with harness.session_factory() as session:
        newest = await session.get(SkillIndexOutboxRow, newest_event.id)
        assert newest is not None
        newest.status = "done"
        await session.commit()

    assert await harness.worker().run_once() == 1

    assert harness.projection.upserts == []
    old = next(row for row in await outbox_rows(harness) if row.id == old_event.id)
    assert old.status == "done"
    assert old.attempts == 1
    assert await version_status(harness, first.id) == "pending"
    assert await version_status(harness, second.id) == "pending"


@pytest.mark.asyncio
async def test_newest_pending_event_wins_for_one_projection_key(harness: Harness) -> None:
    skill, first = await save_skill(harness, owner="u1", name="pdf-tools", seq=1)
    _skill, second = await save_skill(
        harness, owner="u1", name="pdf-tools", seq=2, skill=skill
    )

    assert await harness.worker().run_once() == 2

    assert [row.version_id for row in harness.projection.upserts] == [second.id]
    assert await version_status(harness, first.id) == "pending"
    assert await version_status(harness, second.id) == "ready"
    rows = await outbox_rows(harness, skill_id=skill.id)
    assert [row.status for row in rows] == ["done", "done"]


@pytest.mark.asyncio
async def test_embedding_failure_retries_and_never_finishes_the_event(harness: Harness) -> None:
    skill, version = await save_skill(harness, owner="u1", name="pdf-tools", seq=1)
    harness.embedder.fail = True

    assert await harness.worker().run_once() == 1

    assert harness.projection.upserts == []
    [event] = await outbox_rows(harness, skill_id=skill.id)
    assert event.status == "pending"
    assert event.attempts == 1
    assert event.last_error
    assert await version_status(harness, version.id) == "pending"


@pytest.mark.asyncio
async def test_exhausted_event_marks_the_version_failed_and_finishes(harness: Harness) -> None:
    skill, version = await save_skill(harness, owner="u1", name="pdf-tools", seq=1)
    harness.embedder.fail = True
    async with harness.session_factory() as session:
        [event] = (await session.scalars(
            select(SkillIndexOutboxRow).where(SkillIndexOutboxRow.skill_id == skill.id)
        )).all()
        event.attempts = 4
        await session.commit()

    assert await harness.worker().run_once() == 1

    [event] = await outbox_rows(harness, skill_id=skill.id)
    assert event.status == "done"
    assert event.attempts == 5
    assert await version_status(harness, version.id) == "failed"


@pytest.mark.asyncio
async def test_repair_upserts_pending_versions_and_verify_deletes_orphan_keys(
    harness: Harness,
) -> None:
    u1_skill, _u1_version = await save_skill(harness, owner="u1", name="one", seq=1)
    u2_skill, _u2_version = await save_skill(harness, owner="u2", name="two", seq=2)
    u3_skill, u3_version = await save_skill(harness, owner="u3", name="three", seq=3)

    stats = await harness.worker().repair_missing(budget_rows=50, budget_seconds=5.0)

    assert (stats.scanned, stats.repaired, stats.failed) == (3, 3, 0)
    upserted = {row.projection_key: row for row in harness.projection.upserts}
    assert set(upserted) == {
        f"private:u1:{u1_skill.id}",
        f"private:u2:{u2_skill.id}",
        f"private:u3:{u3_skill.id}",
    }
    assert harness.content.reads == []  # 投影文本只有元数据，不读正文
    assert await version_status(harness, u3_version.id) == "ready"

    # 巡查：Milvus 少了 u3，多了两条孤儿。巡检自己不 embedding。
    harness.projection.keys = [
        f"private:u1:{u1_skill.id}",
        f"private:u2:{u2_skill.id}",
        "public:gone",
        "private:u9:gone",
    ]
    harness.projection.upserts.clear()
    verify = await harness.worker().verify_projection(budget_rows=50, budget_seconds=5.0)

    assert verify.missing == 1
    assert harness.projection.upserts == []
    assert set(harness.projection.deletes) == {"public:gone", "private:u9:gone"}
    assert await version_status(harness, u3_version.id) == "pending"

    # 快路径把巡查发现的缺失补回来。
    again = await harness.worker().repair_missing(budget_rows=50, budget_seconds=5.0)
    assert again.repaired == 1
    assert [row.projection_key for row in harness.projection.upserts] == [
        f"private:u3:{u3_skill.id}"
    ]
    assert await version_status(harness, u3_version.id) == "ready"


@pytest.mark.asyncio
async def test_a_truncated_verify_cannot_claim_a_key_is_missing(
    harness: Harness,
) -> None:
    """扫描被预算截断时不能把"没看到"当成"不存在"，否则会把健康行标回 pending。"""
    _skill, version = await save_skill(harness, owner="u1", name="one", seq=1)
    await harness.worker().repair_missing(budget_rows=50, budget_seconds=5.0)
    harness.projection.keys = ["private:u9:gone", "private:u8:gone"]

    verify = await harness.worker().verify_projection(budget_rows=1, budget_seconds=5.0)

    assert verify.truncated is True
    assert verify.missing == 0
    assert await version_status(harness, version.id) == "ready"


@pytest.mark.asyncio
async def test_repair_is_cheap_in_steady_state_and_backs_off_a_failing_row(
    harness: Harness,
) -> None:
    skill, version = await save_skill(harness, owner="u1", name="one", seq=1)
    worker = harness.worker()

    assert (await worker.repair_missing(budget_rows=50, budget_seconds=5.0)).repaired == 1
    harness.projection.upserts.clear()
    embedded = len(harness.embedder.calls)

    steady = await worker.repair_missing(budget_rows=50, budget_seconds=5.0)
    assert (steady.scanned, steady.repaired) == (0, 0)
    assert harness.projection.upserts == []
    assert len(harness.embedder.calls) == embedded

    # 坏行失败一次就退避：下一轮不再为它花一次 provider 调用。
    harness.embedder.fail = True
    await harness.store.set_version_index_status(version.id, "pending")
    failed = await worker.repair_missing(budget_rows=50, budget_seconds=5.0)
    assert failed.failed == 1
    assert (await worker.repair_missing(budget_rows=50, budget_seconds=5.0)).scanned == 0

    async with harness.session_factory() as session:
        row = await session.get(SkillVersionRow, version.id)
        assert row is not None
        assert row.index_status == "failed"
        assert row.index_attempts == 1
        assert row.index_next_attempt_at is not None
        assert row.index_last_error and "RuntimeError" in row.index_last_error


@pytest.mark.asyncio
async def test_repair_stops_at_its_row_budget_and_reports_truncation(
    harness: Harness,
) -> None:
    for index in range(3):
        await save_skill(harness, owner=f"u{index}", name=f"skill-{index}", seq=index + 1)
    worker = harness.worker()

    first = await worker.repair_missing(budget_rows=2, budget_seconds=5.0)
    assert (first.scanned, first.repaired, first.truncated) == (2, 2, True)

    second = await worker.repair_missing(budget_rows=2, budget_seconds=5.0)
    assert (second.scanned, second.repaired, second.truncated) == (1, 1, False)


@pytest.mark.asyncio
async def test_a_version_past_its_attempt_limit_is_left_for_an_operator(
    harness: Harness,
) -> None:
    _skill, version = await save_skill(harness, owner="u1", name="one", seq=1)
    worker = SkillIndexOutboxWorker(
        harness.store,
        harness.content,  # type: ignore[arg-type]
        harness.embedder,
        harness.projection,  # type: ignore[arg-type]
        repair_max_attempts=2,
    )
    harness.embedder.fail = True

    assert (await worker.repair_missing(budget_rows=10, budget_seconds=5.0)).failed == 1
    async with harness.session_factory() as session:
        row = await session.get(SkillVersionRow, version.id)
        assert row is not None
        row.index_next_attempt_at = None  # 到期：模拟退避时间已过
        await session.commit()
    assert (await worker.repair_missing(budget_rows=10, budget_seconds=5.0)).failed == 1

    async with harness.session_factory() as session:
        row = await session.get(SkillVersionRow, version.id)
        assert row is not None
        assert row.index_attempts == 2
        assert row.index_next_attempt_at is None
    # 到达上限之后不再自动重试，也不会被"到期"重新捡起来。
    assert (await worker.repair_missing(budget_rows=10, budget_seconds=5.0)).scanned == 0


@pytest.mark.asyncio
async def test_forget_commits_keeps_active_pending_and_recent_versions(
    harness: Harness, tmp_path: Path
) -> None:
    skill, versions = None, []
    for seq in range(1, 5):
        skill, version = await save_skill(
            harness, owner="u1", name="pdf-tools", seq=seq, skill=skill
        )
        versions.append(version)
    assert skill is not None
    await mark_all_done(harness)

    # v1 比"最近 2 版"更旧，但还有一条待处理事件引用它。
    await harness.store.enqueue_outbox(
        op="upsert_active",
        projection_key="private:u1:queued",
        scope="private",
        owner_user_id="u1",
        skill_id=skill.id,
        version_id=versions[0].id,
        payload={"enabled": True},
    )

    cache_dir = tmp_path / "cache"
    commits_dir = cache_dir / "commits"
    commits_dir.mkdir(parents=True)
    recent_commits = {versions[2].draft_commit, versions[3].draft_commit}
    pending_commit = versions[0].draft_commit
    prunable_commit = versions[1].draft_commit
    orphan_commit = "9" * 40
    for commit in [pending_commit, prunable_commit, *recent_commits, orphan_commit]:
        assert commit is not None
        (commits_dir / commit).mkdir(parents=True)

    content = SkillContentStore(cache_dir, None)  # type: ignore[arg-type]
    worker = SkillIndexOutboxWorker(
        harness.store,
        content,
        harness.embedder,
        harness.projection,  # type: ignore[arg-type]
    )

    assert await worker.forget_commits(skill.id, keep_versions=2) == 1

    assert (commits_dir / pending_commit).is_dir()
    assert (commits_dir / versions[2].draft_commit).is_dir()
    assert (commits_dir / versions[3].draft_commit).is_dir()
    assert not (commits_dir / prunable_commit).exists()
    # 别的技能的提交不在候选里：事件驱动清理只做这个技能自己的账。
    assert (commits_dir / orphan_commit).is_dir()
    assert harness.projection.deletes == []


@pytest.mark.asyncio
async def test_forget_commits_clears_everything_for_a_deleted_skill(
    harness: Harness, tmp_path: Path
) -> None:
    skill, version = await save_skill(harness, owner="u1", name="pdf-tools", seq=1)
    await mark_all_done(harness)
    commit = version.draft_commit
    assert commit is not None
    cache_dir = tmp_path / "cache"
    (cache_dir / "commits" / commit).mkdir(parents=True)

    await harness.store.delete_private_draft(
        skill_id=skill.id, owner_user_id="u1", actor="u1"
    )
    content = SkillContentStore(cache_dir, None)  # type: ignore[arg-type]
    worker = SkillIndexOutboxWorker(
        harness.store,
        content,
        harness.embedder,
        harness.projection,  # type: ignore[arg-type]
    )

    assert await worker.forget_commits(skill.id, keep_versions=20) == 1
    assert not (cache_dir / "commits" / commit).exists()


@pytest.mark.asyncio
async def test_forget_commits_normalizes_commit_ids(
    harness: Harness, tmp_path: Path
) -> None:
    _skill, version = await save_skill(harness, owner="u1", name="pdf-tools", seq=1)
    await mark_all_done(harness)
    commit = version.draft_commit
    assert commit is not None
    async with harness.session_factory() as session:
        row = await session.get(SkillVersionRow, version.id)
        assert row is not None
        row.draft_commit = commit.upper()
        await session.commit()

    cache_dir = tmp_path / "cache"
    cached = cache_dir / "commits" / commit
    cached.mkdir(parents=True)
    (cached / "SKILL.md").write_text("# Body\n")

    content = SkillContentStore(cache_dir, None)  # type: ignore[arg-type]
    worker = SkillIndexOutboxWorker(
        harness.store,
        content,
        harness.embedder,
        harness.projection,  # type: ignore[arg-type]
    )

    # 大写 commit 仍然指向同一个缓存目录，保留集必须能认出来。
    assert await worker.forget_commits(_skill.id, keep_versions=1) == 0
    assert (cache_dir / "commits" / commit).is_dir()
