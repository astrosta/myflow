"""Explicit E2B sync: uploads, downloads, size caps, and failure paths."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agentkit.sandbox.e2b.sync import (
    LazySkillUploader,
    SkillSource,
    SkillUploadTooLarge,
    SyncLimits,
    download_tree,
    ensure_container_dirs,
    upload_tree,
)
from fakes.e2b_sdk import FakeCommands, FakeFiles, FakeSdkSandbox


def _sdk() -> FakeSdkSandbox:
    return FakeSdkSandbox(files=FakeFiles(), commands=FakeCommands())


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_upload_tree_writes_every_file_and_nested_directory(tmp_path: Path) -> None:
    source = tmp_path / "uploads"
    _write(source / "a.txt", "alpha")
    _write(source / "nested" / "b.txt", "beta")

    sdk = _sdk()
    stats = upload_tree(
        sdk,
        host_root=source,
        container_root="/mnt/user-data/uploads",
        limits=SyncLimits(),
        user="root",
        label="uploads",
    )

    assert stats.files == 2
    assert stats.bytes == 9
    assert sdk.files.read("/mnt/user-data/uploads/a.txt") == "alpha"
    assert sdk.files.read("/mnt/user-data/uploads/nested/b.txt") == "beta"


def test_upload_tree_missing_root_is_empty(tmp_path: Path) -> None:
    sdk = _sdk()
    stats = upload_tree(
        sdk,
        host_root=tmp_path / "missing",
        container_root="/mnt/user-data/uploads",
        limits=SyncLimits(),
        user="root",
        label="uploads",
    )
    assert stats.files == 0
    assert sdk.files.writes == []


def test_upload_tree_skips_an_oversized_file_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = tmp_path / "uploads"
    _write(source / "small.txt", "ok")
    _write(source / "large.txt", "toolarge")

    sdk = _sdk()
    with caplog.at_level(logging.WARNING):
        stats = upload_tree(
            sdk,
            host_root=source,
            container_root="/mnt/user-data/uploads",
            limits=SyncLimits(max_file_bytes=4, max_tree_bytes=1024),
            user="root",
            label="uploads",
        )

    assert stats.files == 1
    assert stats.skipped_files == 1
    assert sdk.files.writes == ["/mnt/user-data/uploads/small.txt"]
    assert any("max_file_bytes" in record.message for record in caplog.records)


def test_upload_tree_abandons_a_tree_over_the_tree_limit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = tmp_path / "uploads"
    _write(source / "a.txt", "12345")
    _write(source / "b.txt", "67890")

    sdk = _sdk()
    with caplog.at_level(logging.WARNING):
        stats = upload_tree(
            sdk,
            host_root=source,
            container_root="/mnt/user-data/uploads",
            limits=SyncLimits(max_file_bytes=1024, max_tree_bytes=8),
            user="root",
            label="uploads",
        )

    assert stats.skipped_trees == 1
    assert stats.files == 0
    assert sdk.files.writes == []
    assert any("max_tree_bytes" in record.message for record in caplog.records)


def test_upload_transport_failure_raises(tmp_path: Path) -> None:
    source = tmp_path / "uploads"
    _write(source / "a.txt", "alpha")

    sdk = _sdk()

    def fail(path, data, **kwargs):
        del path, data, kwargs
        raise OSError("transport down")

    sdk.files.write = fail

    with pytest.raises(OSError, match="Failed to upload"):
        upload_tree(
            sdk,
            host_root=source,
            container_root="/mnt/user-data/uploads",
            limits=SyncLimits(),
            user="root",
            label="uploads",
        )


def test_download_tree_overwrites_same_names_and_preserves_host_only_files(
    tmp_path: Path,
) -> None:
    sdk = _sdk()
    sdk.files.make_dir("/mnt/user-data/outputs/nested")
    sdk.files.write("/mnt/user-data/outputs/result.txt", "from sandbox")
    sdk.files.write("/mnt/user-data/outputs/nested/deep.txt", "deep")

    host = tmp_path / "outputs"
    _write(host / "result.txt", "old host copy")
    _write(host / "keep.txt", "host only")

    stats = download_tree(
        sdk,
        host_root=host,
        container_root="/mnt/user-data/outputs",
        limits=SyncLimits(),
        user="root",
        label="outputs",
    )

    assert stats.files == 2
    assert (host / "result.txt").read_text(encoding="utf-8") == "from sandbox"
    assert (host / "nested" / "deep.txt").read_text(encoding="utf-8") == "deep"
    assert (host / "keep.txt").read_text(encoding="utf-8") == "host only"


def test_download_tree_missing_container_root_is_empty(tmp_path: Path) -> None:
    sdk = _sdk()
    stats = download_tree(
        sdk,
        host_root=tmp_path / "outputs",
        container_root="/mnt/user-data/outputs",
        limits=SyncLimits(),
        user="root",
        label="outputs",
    )
    assert stats.files == 0


def test_download_tree_skips_an_oversized_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sdk = _sdk()
    sdk.files.make_dir("/mnt/user-data/outputs")
    sdk.files.write("/mnt/user-data/outputs/small.txt", "ok")
    sdk.files.write("/mnt/user-data/outputs/large.txt", "toolarge")

    host = tmp_path / "outputs"
    with caplog.at_level(logging.WARNING):
        stats = download_tree(
            sdk,
            host_root=host,
            container_root="/mnt/user-data/outputs",
            limits=SyncLimits(max_file_bytes=4, max_tree_bytes=1024),
            user="root",
            label="outputs",
        )

    assert stats.files == 1
    assert stats.skipped_files == 1
    assert (host / "small.txt").read_text(encoding="utf-8") == "ok"
    assert not (host / "large.txt").exists()
    assert any("max_file_bytes" in record.message for record in caplog.records)


def test_download_tree_read_failure_is_a_warning_and_other_files_continue(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sdk = _sdk()
    sdk.files.make_dir("/mnt/user-data/outputs")
    sdk.files.write("/mnt/user-data/outputs/bad.txt", "bad")
    sdk.files.write("/mnt/user-data/outputs/good.txt", "good")
    original = sdk.files.read

    def read(path, **kwargs):
        if path.endswith("bad.txt"):
            raise OSError("read failed")
        return original(path, **kwargs)

    sdk.files.read = read

    host = tmp_path / "outputs"
    with caplog.at_level(logging.WARNING):
        stats = download_tree(
            sdk,
            host_root=host,
            container_root="/mnt/user-data/outputs",
            limits=SyncLimits(),
            user="root",
            label="outputs",
        )

    assert stats.files == 1
    assert stats.skipped_files == 1
    assert (host / "good.txt").read_text(encoding="utf-8") == "good"
    assert not (host / "bad.txt").exists()
    assert any("failed to download" in record.message for record in caplog.records)


def test_download_tree_abandons_after_the_tree_limit(tmp_path: Path) -> None:
    sdk = _sdk()
    sdk.files.make_dir("/mnt/user-data/outputs")
    sdk.files.write("/mnt/user-data/outputs/a.txt", "1234")
    sdk.files.write("/mnt/user-data/outputs/b.txt", "5678")

    host = tmp_path / "outputs"
    stats = download_tree(
        sdk,
        host_root=host,
        container_root="/mnt/user-data/outputs",
        limits=SyncLimits(max_file_bytes=1024, max_tree_bytes=3),
        user="root",
        label="outputs",
    )

    assert stats.skipped_trees == 1
    downloaded = sorted(path.name for path in host.glob("*.txt"))
    assert downloaded == ["a.txt"]


def test_ensure_container_dirs_uses_mkdir_p() -> None:
    sdk = _sdk()
    ensure_container_dirs(
        sdk,
        ["/mnt/user-data/workspace", "/mnt/user-data/uploads", "/mnt/user-data/uploads"],
        user="root",
    )
    command = sdk.commands.calls[-1]["cmd"]
    assert command.startswith("mkdir -p -- ")
    assert command.count("/mnt/user-data/uploads") == 1


def test_lazy_skill_uploader_uploads_only_the_requested_file(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write(root / "private" / "demo" / "SKILL.md", "body")
    _write(root / "private" / "demo" / "other.md", "other")
    sdk = _sdk()
    uploader = LazySkillUploader(
        sdk,
        sources=(SkillSource(root, "/mnt/skills"),),
        limits=SyncLimits(),
        user="root",
    )

    uploader.ensure_file("/mnt/skills/private/demo/SKILL.md")
    uploader.ensure_file("/mnt/skills/private/demo/SKILL.md")

    assert sdk.files.writes.count("/mnt/skills/private/demo/SKILL.md") == 1
    assert "/mnt/skills/private/demo/other.md" not in sdk.files.writes


def test_lazy_skill_uploader_rejects_a_tree_over_the_limit(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write(root / "demo" / "SKILL.md", "body")
    uploader = LazySkillUploader(
        _sdk(),
        sources=(SkillSource(root, "/mnt/skills"),),
        limits=SyncLimits(max_file_bytes=1024, max_tree_bytes=2),
        user="root",
    )

    with pytest.raises(SkillUploadTooLarge, match="cannot upload skills on demand"):
        uploader.ensure_tree("/mnt/skills/demo")
