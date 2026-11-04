import pytest

from agentkit.sandbox.security import VirtualMount, assert_virtual_path

MOUNTS = [
    VirtualMount("/mnt/user-data", read_only=False),
    VirtualMount("/mnt/user-data/workspace", read_only=False),
    VirtualMount("/mnt/skills", read_only=True),
]


def test_accepts_child_of_mount() -> None:
    assert assert_virtual_path("/mnt/user-data/workspace/a.py", MOUNTS, write=True) == "/mnt/user-data/workspace/a.py"


def test_longest_prefix_wins_for_read_only() -> None:
    with pytest.raises(PermissionError):
        assert_virtual_path("/mnt/user-data/workspace/a.py", [
            VirtualMount("/mnt/user-data"),
            VirtualMount("/mnt/user-data/workspace", read_only=True),
        ], write=True)


def test_rejects_parent_traversal() -> None:
    with pytest.raises(PermissionError):
        assert_virtual_path("/mnt/user-data/../skills/x", MOUNTS)


def test_rejects_shapes_that_are_not_absolute_posix() -> None:
    for path in ["mnt/user-data/a", "C:\\x", "\\\\server\\share", "file:///x", "/mnt/user-data/a\x00b"]:
        with pytest.raises(PermissionError):
            assert_virtual_path(path, MOUNTS)


def test_rejects_write_to_read_only_mount() -> None:
    with pytest.raises(PermissionError):
        assert_virtual_path("/mnt/skills/builtin/x", MOUNTS, write=True)
