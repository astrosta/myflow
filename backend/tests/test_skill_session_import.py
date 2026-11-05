"""OpenViking session-skill import: URI validation and draft hand-off."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentkit.skills.session_import import SessionSkillImporter


class Reader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def read_text(self, ov_user: str, uri: str) -> str:
        self.calls.append((ov_user, uri))
        return "---\nname: session-helper\ndescription: helper\n---\nBody\n"


class Author:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def import_extracted_skill(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=f"skill-{len(self.calls)}")


@pytest.mark.asyncio
async def test_imports_only_generated_skill_files_under_the_user_skill_root() -> None:
    reader = Reader()
    author = Author()
    importer = SessionSkillImporter(author, reader)

    imported = await importer.import_skills(
        owner_user_id="u1",
        ov_user="u_hash",
        task_id="task-1",
        skill_uris=[
            "viking://user/u_hash/skills/session-helper",
            "viking://user/u_hash/skills/session-helper/SKILL.md",
            "viking://user/other/skills/foreign",
            "viking://user/u_hash/memories/tools/x.md",
            "not-a-uri",
        ],
    )

    assert [item.skill_id for item in imported] == ["skill-1"]
    assert reader.calls == [
        ("u_hash", "viking://user/u_hash/skills/session-helper/SKILL.md")
    ]
    assert author.calls[0]["owner_user_id"] == "u1"
    assert author.calls[0]["source_uri"] == "viking://user/u_hash/skills/session-helper"
    assert author.calls[0]["source_task_id"] == "task-1"


@pytest.mark.asyncio
async def test_import_failure_for_one_skill_does_not_block_the_rest() -> None:
    class FlakyAuthor(Author):
        async def import_extracted_skill(self, **kwargs):
            if "bad" in kwargs["source_uri"]:
                raise ValueError("bad skill")
            return await super().import_extracted_skill(**kwargs)

    author = FlakyAuthor()
    importer = SessionSkillImporter(author, Reader())

    imported = await importer.import_skills(
        owner_user_id="u1",
        ov_user="u_hash",
        task_id="task-2",
        skill_uris=[
            "viking://user/u_hash/skills/bad",
            "viking://user/u_hash/skills/good",
        ],
    )

    assert [item.source_uri for item in imported] == [
        "viking://user/u_hash/skills/good"
    ]
