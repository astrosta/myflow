"""Task 2：技能包领域记录、路径校验、稳定哈希与 frontmatter 渲染。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from agentkit.skills.domain import (
    InvalidSkillPackage,
    PackageManifest,
    PrivateActiveRecord,
    PublicActiveRecord,
    ReviewDetail,
    ReviewItem,
    SkillConflict,
    SkillRecord,
    SkillReleaseRecord,
    SkillScope,
    SkillSubmissionRecord,
    SkillVersionRecord,
    StagedFile,
    StagedFileInfo,
    VisibleSkill,
)
from agentkit.skills.package import build_package, render_skill_md, validate_skill_path
from agentkit.skills.parser import ParsedSkill, parse_skill_text
from agentkit.skills.types import Skill, SkillCategory

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
LIMITS = {"max_files": 20, "max_file_bytes": 4096, "max_package_bytes": 16384}


def skill_md(name: str = "pdf-tools", description: str = "Does PDF things") -> bytes:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# Body\n".encode()


def manifest(files: Mapping[str, StagedFile], **overrides: int) -> PackageManifest:
    return build_package(files, **{**LIMITS, **overrides})


def make_skill(**overrides: object) -> Skill:
    values: dict[str, object] = {
        "name": "pdf-tools",
        "description": "Does PDF things",
        "license": None,
        "skill_dir": Path("/host/skills/public/pdf-tools"),
        "skill_file": Path("/host/skills/public/pdf-tools/SKILL.md"),
        "relative_path": Path("pdf-tools"),
        "category": SkillCategory.CUSTOM,
    }
    values.update(overrides)
    return Skill(**values)  # type: ignore[arg-type]


def test_scope_values_and_staged_file_defaults():
    assert SkillScope.PUBLIC == "public"
    assert SkillScope.PRIVATE == "private"
    assert StagedFile("SKILL.md", b"x").mode == "text"


def test_package_hash_is_stable_and_ignores_mapping_order():
    first = {
        "SKILL.md": StagedFile("SKILL.md", skill_md(), "text"),
        "scripts/run.sh": StagedFile("scripts/run.sh", b"echo ok\n", "executable"),
    }
    second = dict(reversed(list(first.items())))

    left = manifest(first)
    right = manifest(second)

    assert left.package_sha256 == right.package_sha256
    assert left.skill_md_sha256 == right.skill_md_sha256
    assert [item.path for item in left.files] == ["SKILL.md", "scripts/run.sh"]
    assert isinstance(left.parsed, ParsedSkill)
    assert left.parsed.name == "pdf-tools"


def test_package_hash_uses_canonical_nul_delimited_input():
    files = {
        "SKILL.md": StagedFile("SKILL.md", skill_md(), "text"),
        "assets/logo.bin": StagedFile("assets/logo.bin", b"\x00\x01", "binary"),
    }
    expected = b"".join(
        path.encode("utf-8")
        + b"\x00"
        + deployed.mode.encode("utf-8")
        + b"\x00"
        + deployed.content
        + b"\x00"
        for path, deployed in sorted(files.items())
    )

    built = manifest(files)

    assert built.package_sha256 == hashlib.sha256(expected).hexdigest()
    assert built.skill_md_sha256 == hashlib.sha256(files["SKILL.md"].content).hexdigest()


def test_package_hash_changes_with_content_path_or_mode():
    base = {"SKILL.md": StagedFile("SKILL.md", skill_md(), "text")}
    changed_content = {"SKILL.md": StagedFile("SKILL.md", skill_md(description="Changed"), "text")}
    changed_mode = {"SKILL.md": StagedFile("SKILL.md", skill_md(), "binary")}
    changed_path = {
        "SKILL.md": StagedFile("SKILL.md", skill_md(), "text"),
        "extra.txt": StagedFile("extra.txt", b"x", "text"),
    }

    hashes = {
        manifest(base).package_sha256,
        manifest(changed_content).package_sha256,
        manifest(changed_mode).package_sha256,
        manifest(changed_path).package_sha256,
    }

    assert len(hashes) == 4


@pytest.mark.parametrize("path", ["SKILL.md", "scripts/run.sh", "assets/图.png", "a-b_c.txt"])
def test_validate_skill_path_accepts_posix_relative_paths(path: str):
    assert validate_skill_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "..",
        "../x",
        "/etc/passwd",
        "a/../../b",
        "a//b",
        "a/./b",
        "dir/",
        r"a\b",
        "a\x00b",
        "C:/x",
        "//server/share",
    ],
)
def test_package_rejects_parent_traversal_and_absolute_paths(path: str):
    with pytest.raises(InvalidSkillPackage) as caught:
        validate_skill_path(path)

    assert caught.value.code == "invalid_skill_package"


class _DuplicateKeyMapping(Mapping[str, StagedFile]):
    """A mapping can theoretically expose duplicate keys; build must reject them."""

    def __init__(self, key: str, value: StagedFile) -> None:
        self._key = key
        self._value = value

    def __getitem__(self, key: str) -> StagedFile:
        if key != self._key:
            raise KeyError(key)
        return self._value

    def __iter__(self) -> Iterator[str]:
        yield self._key
        yield self._key

    def __len__(self) -> int:
        return 2


def test_package_rejects_duplicate_normalized_paths():
    deployed = StagedFile("SKILL.md", skill_md(), "text")

    with pytest.raises(InvalidSkillPackage, match="duplicate"):
        manifest(_DuplicateKeyMapping("SKILL.md", deployed))


def test_package_rejects_mapping_key_and_file_path_disagreement():
    with pytest.raises(InvalidSkillPackage):
        manifest({"SKILL.md": StagedFile("other.md", skill_md(), "text")})


def test_package_requires_skill_md_and_valid_frontmatter():
    for files in (
        {},
        {"README.md": StagedFile("README.md", b"# readme\n", "text")},
        {"SKILL.md": StagedFile("SKILL.md", b"# no frontmatter\n", "text")},
        {"SKILL.md": StagedFile("SKILL.md", b"---\nname: Bad Name\n---\n", "text")},
        {"skill.md": StagedFile("skill.md", skill_md(), "text")},
    ):
        with pytest.raises(InvalidSkillPackage):
            manifest(files)


def test_package_wraps_non_utf8_and_parser_errors():
    for content in (b"\xff\xfe", b"---\nname: [unterminated\n---\n"):
        with pytest.raises(InvalidSkillPackage):
            manifest({"SKILL.md": StagedFile("SKILL.md", content, "text")})


def test_package_enforces_file_count_and_byte_limits():
    base = {"SKILL.md": StagedFile("SKILL.md", skill_md(), "text")}

    with pytest.raises(InvalidSkillPackage, match="too many files"):
        manifest({**base, "a.txt": StagedFile("a.txt", b"a", "text")}, max_files=1)
    with pytest.raises(InvalidSkillPackage, match="file too large"):
        manifest({**base, "big.bin": StagedFile("big.bin", b"12345", "text")}, max_file_bytes=4)

    total = sum(len(item.content) for item in base.values())
    manifest(base, max_package_bytes=total)
    with pytest.raises(InvalidSkillPackage, match="package too large"):
        manifest(base, max_package_bytes=total - 1)


def test_render_skill_md_emits_only_name_and_description():
    rendered = render_skill_md("pdf-tools", "处理 PDF：加标签")

    assert rendered.startswith("---\n")
    assert rendered.endswith("---\n")
    parsed = parse_skill_text(rendered)
    assert parsed.name == "pdf-tools"
    assert parsed.description == "处理 PDF：加标签"
    assert parsed.license is None
    assert parsed.allowed_tools is None
    assert parsed.required_secrets == ()
    assert parsed.body == ""
    frontmatter = rendered.removeprefix("---\n").removesuffix("---\n")
    assert set(yaml.safe_load(frontmatter)) == {"name", "description"}


def test_private_skill_container_path_uses_private_root():
    skill = make_skill(scope=SkillScope.PRIVATE, name="pdf-tools")

    assert skill.container_file == "/mnt/skills/private/pdf-tools/SKILL.md"


def test_public_skill_container_path_uses_public_root():
    skill = make_skill(scope=SkillScope.PUBLIC, name="pdf-tools")

    assert skill.container_file == "/mnt/skills/public/pdf-tools/SKILL.md"


def test_skill_without_scope_keeps_legacy_category_path():
    skill = make_skill()

    assert skill.container_dir == "/mnt/skills/custom/pdf-tools"
    assert skill.container_file == "/mnt/skills/custom/pdf-tools/SKILL.md"


def test_skill_catalog_fields_default_to_none_at_the_end():
    skill = make_skill()

    assert skill.skill_id is None
    assert skill.scope is None
    assert skill.owner_user_id is None
    assert skill.version_id is None
    assert skill.skill_version is None
    assert skill.release_id is None
    assert skill.git_repository is None
    assert skill.git_commit is None
    assert skill.repo_path is None


def test_domain_records_are_constructible_and_immutable():
    version = SkillVersionRecord(
        id="v1",
        skill_id="s1",
        skill_version="v1",
        version_seq=1,
        package_sha256="a" * 64,
        skill_md_sha256="b" * 64,
        metadata={"name": "pdf-tools"},
        draft_ref="refs/heads/myflow/drafts/u1",
        draft_commit="c" * 40,
        skill_path="users/u1/s1/SKILL.md",
        index_status="pending",
        created_at=NOW,
    )
    release = SkillReleaseRecord(
        id="r1",
        batch_id="b1",
        version_id="v1",
        skill_id="s1",
        skill_version="v1",
        git_repository="https://example.invalid/skills.git",
        git_commit="d" * 40,
        skill_path="public/pdf-tools/SKILL.md",
        public_name="pdf-tools",
        source_commit="c" * 40,
        approved_by="admin",
        created_at=NOW,
    )
    item = ReviewItem(
        submission_id="sub1",
        skill_id="s1",
        name="pdf-tools",
        version="v1",
        owner_user_id="u1",
        submitted_at=NOW,
        submitted_commit="c" * 40,
    )

    records = (
        SkillRecord(
            id="s1",
            owner_user_id="u1",
            private_name="pdf-tools",
            public_name=None,
            created_at=NOW,
            updated_at=NOW,
            deleted_at=None,
        ),
        version,
        SkillSubmissionRecord(
            id="sub1",
            skill_id="s1",
            version_id="v1",
            submitted_commit="c" * 40,
            owner_user_id="u1",
            status="pending",
            submitted_at=NOW,
            reviewed_by=None,
            reviewed_at=None,
            reject_reason=None,
            public_release_id=None,
        ),
        release,
        PublicActiveRecord(
            skill_id="s1", release_id="r1", activated_by="admin", updated_at=NOW
        ),
        PrivateActiveRecord(
            skill_id="s1", owner_user_id="u1", version_id="v1", updated_at=NOW
        ),
        VisibleSkill(
            skill=make_skill(scope=SkillScope.PUBLIC),
            scope=SkillScope.PUBLIC,
            version=version,
            release=release,
        ),
        item,
        ReviewDetail(
            item=item,
            files=[
                StagedFileInfo(
                    path="SKILL.md",
                    size_bytes=len(skill_md()),
                    mode="text",
                    sha256=hashlib.sha256(skill_md()).hexdigest(),
                )
            ],
            skill_md=skill_md().decode(),
            diff="+ body",
            metadata={"name": "pdf-tools"},
            package_bytes=len(skill_md()),
        ),
    )

    assert len(records) == 9
    assert version.metadata == {"name": "pdf-tools"}
    assert records[6].version is version
    assert records[6].release is release
    with pytest.raises(AttributeError):
        version.index_status = "ready"  # type: ignore[misc]


def test_domain_error_codes_are_stable():
    assert InvalidSkillPackage.code == "invalid_skill_package"
    assert SkillConflict.code == "skill_conflict"
    assert isinstance(InvalidSkillPackage(), ValueError)
    assert isinstance(SkillConflict(), RuntimeError)
