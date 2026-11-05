"""Author service: save -> scan -> activate for one owner's private skills."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agentkit.config.skills_config import SkillCreationConfig
from agentkit.persistence.schema import (
    Base,
    SkillIndexOutboxRow,
    SkillPrivateActiveRow,
    SkillRow,
)
from agentkit.skills.author import SkillAuthorService
from agentkit.skills.domain import (
    InvalidSkillPackage,
    SkillConflict,
    StagedFile,
    StagedFileInfo,
)
from agentkit.skills.git import GitCommit
from agentkit.skills.package import render_skill_md
from agentkit.skills.parser import parse_skill_text
from agentkit.skills.security_scan import SecurityScanOutcome
from agentkit.skills.store.sql import SqlSkillStore

T = TypeVar("T")


@dataclass
class FakeGit:
    commits: dict[str, dict[str, StagedFile]] = field(default_factory=dict)
    heads: dict[str, str] = field(default_factory=dict)
    save_calls: list[tuple[str, str, list[StagedFile], str]] = field(default_factory=list)
    _counter: int = 0

    def _new_commit(self, files: dict[str, StagedFile]) -> str:
        self._counter += 1
        commit = f"{self._counter:040x}"
        self.commits[commit] = dict(files)
        return commit

    async def save_draft(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        files: Sequence[StagedFile],
        message: str,
    ) -> GitCommit:
        copied = [StagedFile(item.path, item.content, item.mode) for item in files]
        self.save_calls.append((owner_user_id, skill_id, copied, message))
        ref = f"refs/heads/myflow/drafts/{owner_user_id}"
        path = f"users/{owner_user_id}/{skill_id}"
        committed = {
            f"{path}/{item.path}": StagedFile(
                f"{path}/{item.path}", item.content, item.mode
            )
            for item in copied
        }
        combined = dict(self.commits.get(self.heads.get(ref, ""), {}))
        combined.update(committed)
        commit = self._new_commit(combined)
        self.heads[ref] = commit
        return GitCommit(commit=commit, ref=ref, path=path)

    async def head(self, ref: str) -> str:
        return self.heads[ref]


class FakeContent:
    """Materialize the fake commit into a real temporary directory for scanning."""

    def __init__(self, root: Path, git: FakeGit) -> None:
        self._root = root
        self._git = git

    async def ensure(self, commit: str) -> Path:
        target = self._root / commit
        for path, staged in self._git.commits[commit].items():
            file = target / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(staged.content)
        return target


@dataclass
class FakeScanner:
    outcome: SecurityScanOutcome = field(
        default_factory=lambda: SecurityScanOutcome(
            status="completed",
            is_safe=True,
            max_severity="SAFE",
            scanner_version="2.3.2",
            duration_ms=9,
        )
    )
    calls: list[Path] = field(default_factory=list)
    saw_skill_md: list[bool] = field(default_factory=list)

    def scan(self, skill_directory: Path) -> SecurityScanOutcome:
        self.calls.append(skill_directory)
        self.saw_skill_md.append((skill_directory / "SKILL.md").is_file())
        return self.outcome


@dataclass
class Harness:
    store: SqlSkillStore
    service: SkillAuthorService
    git: FakeGit
    scanner: FakeScanner
    session_factory: async_sessionmaker[AsyncSession]
    creation: SkillCreationConfig

    def run(self, scenario: Callable[[], Awaitable[T]]) -> T:
        return asyncio.run(scenario())


@pytest.fixture(name="harness")
def harness_fixture(tmp_path: Path):
    creation = SkillCreationConfig(
        max_drafts_per_user=5,
        max_package_bytes=16_384,
        max_file_bytes=4_096,
        max_files=20,
    )

    async def _build() -> tuple[Harness, object]:
        engine = create_async_engine(
            "sqlite+aiosqlite:///" + (tmp_path / "skills.db").as_posix()
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        store = SqlSkillStore(factory)
        git = FakeGit()
        scanner = FakeScanner()
        service = SkillAuthorService(
            store,
            git,  # type: ignore[arg-type]
            FakeContent(tmp_path / "content", git),  # type: ignore[arg-type]
            creation,
            scanner=scanner,
        )
        return Harness(store, service, git, scanner, factory, creation), engine

    built, engine = asyncio.run(_build())
    yield built
    asyncio.run(engine.dispose())


def skill_md(name: str, body: str = "Body\n") -> bytes:
    return (render_skill_md(name, "Does things") + "\n" + body).encode()


def test_create_draft_stages_skill_md_without_commit(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        assert harness.git.save_calls == []
        files = await harness.service.list_files(owner_user_id="u1", skill_id=skill.id)
        assert [file.path for file in files] == ["SKILL.md"]
        assert all(isinstance(file, StagedFileInfo) for file in files)
        expected = render_skill_md("pdf-tools", "Does things").encode()
        assert files[0].sha256 == hashlib.sha256(expected).hexdigest()

    harness.run(scenario)


def test_save_updates_name_and_description_from_skill_md(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="old-name", description="旧描述"
        )
        await harness.service.put_file(
            owner_user_id="u1",
            skill_id=skill.id,
            path="SKILL.md",
            content=(render_skill_md("daily-report", "整理日报") + "\n正文\n").encode(),
        )

        version = await harness.service.save(owner_user_id="u1", skill_id=skill.id)

        assert version.metadata["name"] == "daily-report"
        assert version.metadata["description"] == "整理日报"

    harness.run(scenario)


def test_save_produces_an_inert_version(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        version = await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        assert version.skill_version == "v1"
        # Save commits the draft to Git but stays inert until activation.
        assert version.draft_commit == harness.git.heads["refs/heads/myflow/drafts/u1"]
        assert version.draft_ref == "refs/heads/myflow/drafts/u1"
        assert version.skill_path == f"users/u1/{skill.id}/SKILL.md"
        assert version.index_status == "unindexed"
        assert len(harness.git.save_calls) == 1
        assert await harness.service.active_version(
            owner_user_id="u1", skill_id=skill.id
        ) is None

        async with harness.session_factory() as session:
            assert await session.get(SkillPrivateActiveRow, skill.id) is None
            outbox = (
                await session.scalars(
                    select(SkillIndexOutboxRow).where(
                        SkillIndexOutboxRow.skill_id == skill.id
                    )
                )
            ).all()
            assert outbox == []

        same = await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        assert same.id == version.id
        await harness.service.put_file(
            owner_user_id="u1",
            skill_id=skill.id,
            path="SKILL.md",
            content=skill_md("pdf-tools", "Changed"),
        )
        v2 = await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        assert v2.skill_version == "v2"

    harness.run(scenario)


def test_scan_uses_the_saved_commit_and_persists_findings(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        version = await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        harness.scanner.outcome = SecurityScanOutcome(
            status="completed",
            is_safe=False,
            max_severity="HIGH",
            findings=(
                {
                    "rule_id": "R1",
                    "title": "Bad",
                    "severity": "HIGH",
                    "category": "prompt_injection",
                    "file_path": "SKILL.md",
                    "line_number": 2,
                    "remediation": "remove",
                },
            ),
            scanner_version="2.3.2",
            duration_ms=11,
        )

        scan = await harness.service.scan_security(
            owner_user_id="u1", skill_id=skill.id
        )
        assert scan.version_id == version.id
        assert scan.is_safe is False
        assert scan.max_severity == "HIGH"
        assert scan.findings_count == 1
        assert version.draft_commit in harness.scanner.calls[0].parts
        assert harness.scanner.saw_skill_md == [True]
        assert await harness.service.latest_scan(
            owner_user_id="u1", skill_id=skill.id, version_id=version.id
        ) == scan

    harness.run(scenario)


def test_scan_rejects_an_unrelated_version(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        version = await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        with pytest.raises(SkillConflict, match="latest"):
            await harness.service.scan_security(
                owner_user_id="u1", skill_id=skill.id, version_id="other"
            )
        assert harness.scanner.calls == []
        assert version.id

    harness.run(scenario)


def test_activation_requires_a_passing_scan_then_points_the_active_pointer(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        version = await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        assert len(harness.git.save_calls) == 1

        with pytest.raises(SkillConflict, match="scan"):
            await harness.service.activate(owner_user_id="u1", skill_id=skill.id)
        assert len(harness.git.save_calls) == 1

        harness.scanner.outcome = SecurityScanOutcome(
            status="completed", is_safe=False, max_severity="CRITICAL"
        )
        await harness.service.scan_security(owner_user_id="u1", skill_id=skill.id)
        with pytest.raises(SkillConflict, match="did not pass"):
            await harness.service.activate(owner_user_id="u1", skill_id=skill.id)

        harness.scanner.outcome = SecurityScanOutcome(
            status="completed",
            is_safe=True,
            max_severity="SAFE",
            scanner_version="2.3.2",
        )
        await harness.service.scan_security(owner_user_id="u1", skill_id=skill.id)
        active = await harness.service.activate(owner_user_id="u1", skill_id=skill.id)
        assert active.id == version.id
        assert active.index_status == "pending"
        assert active.draft_commit is not None
        assert len(harness.git.save_calls) == 1
        assert harness.git.save_calls[0][2][0].path == "SKILL.md"
        assert await harness.service.active_version(
            owner_user_id="u1", skill_id=skill.id
        ) == active

        async with harness.session_factory() as session:
            event = await session.scalar(
                select(SkillIndexOutboxRow).where(
                    SkillIndexOutboxRow.projection_key == f"private:u1:{skill.id}"
                )
            )
            assert event is not None and event.op == "upsert_active"

    harness.run(scenario)


def test_activation_without_a_projection_marks_index_disabled(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        harness.scanner.outcome = SecurityScanOutcome(
            status="completed", is_safe=True, max_severity="SAFE"
        )
        await harness.service.scan_security(owner_user_id="u1", skill_id=skill.id)

        active = await harness.service.activate(
            owner_user_id="u1", skill_id=skill.id, index_enabled=False
        )

        assert active.index_status == "disabled"
        async with harness.session_factory() as session:
            events = (
                await session.scalars(
                    select(SkillIndexOutboxRow).where(
                        SkillIndexOutboxRow.skill_id == skill.id
                    )
                )
            ).all()
            assert events == []

    harness.run(scenario)


def test_saving_v2_keeps_v1_active_until_v2_is_scanned_and_activated(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        v1 = await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        await harness.service.scan_security(owner_user_id="u1", skill_id=skill.id)
        await harness.service.activate(owner_user_id="u1", skill_id=skill.id)

        await harness.service.put_file(
            owner_user_id="u1",
            skill_id=skill.id,
            path="SKILL.md",
            content=skill_md("pdf-tools", "v2"),
        )
        v2 = await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        assert v2.id != v1.id
        active = await harness.service.active_version(
            owner_user_id="u1", skill_id=skill.id
        )
        assert active is not None and active.id == v1.id

        with pytest.raises(SkillConflict, match="scan"):
            await harness.service.activate(owner_user_id="u1", skill_id=skill.id)

        scan = await harness.service.scan_security(
            owner_user_id="u1", skill_id=skill.id
        )
        assert scan.version_id == v2.id
        activated = await harness.service.activate(owner_user_id="u1", skill_id=skill.id)
        assert activated.id == v2.id

    harness.run(scenario)


def test_open_draft_returns_staging_and_does_not_seed_from_active_content(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        await harness.store.delete_all_draft_files(skill.id)

        files = await harness.service.open_draft(
            owner_user_id="u1", skill_id=skill.id
        )
        assert files == []

    harness.run(scenario)


def test_only_the_owner_can_touch_a_skill(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        for action in (
            harness.service.list_files(owner_user_id="u2", skill_id=skill.id),
            harness.service.open_draft(owner_user_id="u2", skill_id=skill.id),
            harness.service.save(owner_user_id="u2", skill_id=skill.id),
            harness.service.scan_security(owner_user_id="u2", skill_id=skill.id),
            harness.service.activate(owner_user_id="u2", skill_id=skill.id),
            harness.service.delete_draft(owner_user_id="u2", skill_id=skill.id),
        ):
            with pytest.raises(SkillConflict):
                await action
        assert len(
            await harness.service.list_files(owner_user_id="u1", skill_id=skill.id)
        ) == 1

    harness.run(scenario)


def test_create_rejects_a_builtin_name(harness: Harness) -> None:
    service = SkillAuthorService(
        harness.store,
        harness.git,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        harness.creation,
        reserved_names=lambda: frozenset({"demo"}),
        scanner=harness.scanner,
    )

    async def scenario() -> None:
        with pytest.raises(SkillConflict, match="built-in"):
            await service.create_draft(
                owner_user_id="u1", name="demo", description="demo"
            )
        assert await harness.store.list_owned("u1") == []

    harness.run(scenario)


def test_failed_scan_result_is_stored_and_activation_stays_blocked(harness: Harness) -> None:
    async def scenario() -> None:
        skill = await harness.service.create_draft(
            owner_user_id="u1", name="pdf-tools", description="Does things"
        )
        await harness.service.save(owner_user_id="u1", skill_id=skill.id)
        harness.scanner.outcome = SecurityScanOutcome(
            status="failed",
            is_safe=False,
            max_severity="SAFE",
            scanner_version="2.3.2",
            error="SkillScannerError: boom",
        )
        scan = await harness.service.scan_security(
            owner_user_id="u1", skill_id=skill.id
        )
        assert scan.status == "failed"
        assert scan.error == "SkillScannerError: boom"
        with pytest.raises(SkillConflict, match="failed"):
            await harness.service.activate(owner_user_id="u1", skill_id=skill.id)

    harness.run(scenario)


def test_import_extracted_skill_creates_an_unscanned_extracted_draft(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        skill_md = render_skill_md("session-helper", "从会话提炼的技能")
        record = await harness.service.import_extracted_skill(
            owner_user_id="u1",
            skill_md=skill_md,
            source_uri="viking://user/u1/skills/session-helper",
            source_task_id="task-1",
        )

        assert record.private_name == "session-helper"
        assert record.source == "extracted"
        assert record.source_uri == "viking://user/u1/skills/session-helper"
        version = await harness.service.latest_version(
            owner_user_id="u1", skill_id=record.id
        )
        assert version is not None
        assert version.skill_version == "v1"
        assert version.index_status == "unindexed"
        assert await harness.service.latest_scan(
            owner_user_id="u1", skill_id=record.id, version_id=version.id
        ) is None
        files = await harness.service.list_files(
            owner_user_id="u1", skill_id=record.id
        )
        assert [file.path for file in files] == ["SKILL.md"]

        # The same generated URI is idempotent.
        again = await harness.service.import_extracted_skill(
            owner_user_id="u1",
            skill_md=skill_md,
            source_uri="viking://user/u1/skills/session-helper",
            source_task_id="task-1",
        )
        assert again.id == record.id
        assert len(await harness.service.list_mine(owner_user_id="u1")) == 1

    harness.run(scenario)


def test_import_extracted_skill_avoids_manual_name_collisions(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        manual = await harness.service.create_draft(
            owner_user_id="u1", name="session-helper", description="manual"
        )
        record = await harness.service.import_extracted_skill(
            owner_user_id="u1",
            skill_md=render_skill_md("session-helper", "从会话提炼的技能"),
            source_uri="viking://user/u1/skills/session-helper",
        )

        assert record.id != manual.id
        assert record.private_name.startswith("session-helper-")
        assert record.source == "extracted"
        staged = await harness.service.read_file(
            owner_user_id="u1", skill_id=record.id, path="SKILL.md"
        )
        assert staged is not None
        assert parse_skill_text(staged.content.decode()).name == record.private_name

    harness.run(scenario)


def test_import_extracted_skill_rejects_malformed_skill_md(harness: Harness) -> None:
    async def scenario() -> None:
        with pytest.raises(InvalidSkillPackage):
            await harness.service.import_extracted_skill(
                owner_user_id="u1",
                skill_md="not a skill",
                source_uri="viking://user/u1/skills/bad",
            )

    harness.run(scenario)
