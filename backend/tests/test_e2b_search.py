"""E2BSandbox tree listing and search contract tests.

list_dir renders the same glyphs as the local provider from sdk.files.list.
glob / grep delegate to one Python helper dropped into the sandbox, which
rejects escaping patterns and caps results before serializing. These tests drive
the fake command handler for the JSON contract, and run the helper source itself
on the host for its own path and truncation rules.
"""

from __future__ import annotations

import base64
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agentkit.sandbox.e2b import files as e2b_files
from agentkit.sandbox.e2b.sandbox import E2BSandbox
from agentkit.sandbox.security import PathMapping
from fakes.e2b_sdk import FakeCommandResult, FakeCommands, FakeFiles, FakeSdkSandbox

_USER_DATA = "/mnt/user-data"
_WORKSPACE = "/mnt/user-data/workspace"
_HELPER = "/tmp/.myflow/search.py"


def _sandbox(tmp_path: Path, *, read_only: bool = False) -> E2BSandbox:
    sdk = FakeSdkSandbox(files=FakeFiles(), commands=FakeCommands())
    mappings = [PathMapping(_USER_DATA, str(tmp_path / "user-data"), read_only)]
    return E2BSandbox(sdk, sandbox_id="e2b:s1", path_mappings=mappings)


def _reply(sandbox: E2BSandbox, payload: dict) -> None:
    """Make every command return one helper JSON payload."""

    sandbox._sdk.commands.handler = lambda cmd, **kw: FakeCommandResult(
        json.dumps(payload)
    )


def _helper_command(sandbox: E2BSandbox) -> str:
    commands = [call["cmd"] for call in sandbox._sdk.commands.calls]
    helpers = [cmd for cmd in commands if cmd.startswith("python3 ")]
    assert len(helpers) == 1
    return helpers[0]


# -- list_dir --------------------------------------------------------------


def test_list_dir_renders_tree_and_empty(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_file(f"{_WORKSPACE}/a.txt", "x")
    sandbox.write_file(f"{_WORKSPACE}/sub/b.txt", "y")
    lines = sandbox.list_dir(_WORKSPACE)
    assert lines == ["├── sub/", "│   └── b.txt", "└── a.txt"]


def test_list_dir_missing_is_file_not_found(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(FileNotFoundError):
        sandbox.list_dir("/mnt/user-data/missing")


def test_list_dir_on_a_file_is_not_a_directory(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_file(f"{_WORKSPACE}/a.txt", "x")
    with pytest.raises(NotADirectoryError):
        sandbox.list_dir(f"{_WORKSPACE}/a.txt")


def test_list_dir_empty_directory_returns_no_lines(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox._sdk.files.make_dir(_WORKSPACE)
    assert sandbox.list_dir(_WORKSPACE) == []


def test_list_dir_honors_max_depth(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox.write_file(f"{_WORKSPACE}/a.txt", "x")
    sandbox.write_file(f"{_WORKSPACE}/sub/b.txt", "y")
    assert sandbox.list_dir(_WORKSPACE, max_depth=1) == ["├── sub/", "└── a.txt"]


def test_list_dir_skips_dot_entries(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox._sdk.files.list = lambda path, **kw: [
        {"name": ".", "type": "FILE_TYPE_DIRECTORY"},
        {"name": "..", "type": "FILE_TYPE_DIRECTORY"},
        {"name": "a.txt", "type": "FILE_TYPE_FILE"},
    ]
    assert sandbox.list_dir(_WORKSPACE) == ["└── a.txt"]


def test_list_dir_rejects_an_unusable_entry(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    sandbox._sdk.files.list = lambda path, **kw: [{"type": "FILE_TYPE_FILE"}]
    with pytest.raises(OSError) as excinfo:
        sandbox.list_dir(_WORKSPACE)
    assert "name" in str(excinfo.value)


def test_list_dir_lists_a_read_only_mount(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path, read_only=True)
    sandbox._sdk.files.write(f"{_WORKSPACE}/a.txt", "x")
    assert sandbox.list_dir(_WORKSPACE) == ["└── a.txt"]


# -- glob ------------------------------------------------------------------


def test_glob_parses_helper_json_and_truncation(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _reply(sandbox, {"matches": [f"{_WORKSPACE}/a.py"], "truncated": True})
    matches, truncated = sandbox.glob(_WORKSPACE, "**/*.py")
    assert matches == [f"{_WORKSPACE}/a.py"]
    assert truncated is True


def test_glob_sends_a_base64_payload_to_the_helper(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _reply(sandbox, {"matches": [], "truncated": False})
    sandbox.glob(_WORKSPACE, "**/*.py", include_dirs=True, max_results=7)

    argv = shlex.split(_helper_command(sandbox))
    assert argv[:2] == ["python3", _HELPER]
    payload = json.loads(base64.b64decode(argv[2]).decode("utf-8"))
    assert payload == {
        "mode": "glob",
        "path": _WORKSPACE,
        "pattern": "**/*.py",
        "include_dirs": True,
        "max_results": 7,
    }


def test_glob_missing_path_is_file_not_found(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _reply(
        sandbox,
        {
            "error": "/mnt/user-data/missing: no such file or directory",
            "missing": True,
        },
    )
    with pytest.raises(FileNotFoundError):
        sandbox.glob("/mnt/user-data/missing", "*.py")


# -- grep ------------------------------------------------------------------


def test_grep_parses_helper_json(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _reply(
        sandbox,
        {
            "matches": [
                {"path": f"{_WORKSPACE}/a.py", "line_number": 2, "line": "needle"}
            ],
            "truncated": False,
        },
    )
    matches, truncated = sandbox.grep(_WORKSPACE, "needle", literal=True)
    assert matches[0].path.endswith("a.py")
    assert matches[0].line_number == 2
    assert truncated is False


def test_grep_sends_a_base64_payload_to_the_helper(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _reply(sandbox, {"matches": [], "truncated": False})
    sandbox.grep(
        _WORKSPACE,
        "needle",
        glob="*.py",
        literal=True,
        case_sensitive=True,
        max_results=5,
    )

    argv = shlex.split(_helper_command(sandbox))
    payload = json.loads(base64.b64decode(argv[2]).decode("utf-8"))
    assert payload == {
        "mode": "grep",
        "path": _WORKSPACE,
        "pattern": "needle",
        "glob": "*.py",
        "literal": True,
        "case_sensitive": True,
        "max_results": 5,
    }


def test_grep_helper_error_is_an_os_error(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _reply(sandbox, {"error": "invalid pattern: unbalanced parenthesis"})
    with pytest.raises(OSError) as excinfo:
        sandbox.grep(_WORKSPACE, "(")
    assert "invalid pattern" in str(excinfo.value)


# -- helper installation ---------------------------------------------------


def test_search_helper_is_written_once(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    _reply(sandbox, {"matches": [], "truncated": False})
    sandbox.glob(_WORKSPACE, "*.py")
    sandbox.glob(_WORKSPACE, "*.py")
    assert sandbox._sdk.files.writes.count(_HELPER) == 1
    assert e2b_files.SEARCH_HELPER_PATH == _HELPER


# -- the helper script itself ---------------------------------------------


def _run_helper(tmp_path: Path, payload: dict) -> dict:
    script = tmp_path / "search.py"
    script.write_text(e2b_files.SEARCH_HELPER, encoding="utf-8")
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    completed = subprocess.run(
        [sys.executable, str(script), encoded],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.fixture()
def search_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "a.py").write_text("print('a')\n", encoding="utf-8")
    (root / "sub" / "b.py").write_text("needle here\n", encoding="utf-8")
    (root / "notes.md").write_text("Needle too\n", encoding="utf-8")
    return root


def test_helper_glob_finds_nested_files(tmp_path: Path, search_root: Path) -> None:
    result = _run_helper(
        tmp_path,
        {
            "mode": "glob",
            "path": str(search_root),
            "pattern": "**/*.py",
            "include_dirs": False,
            "max_results": 10,
        },
    )
    assert sorted(Path(match).name for match in result["matches"]) == ["a.py", "b.py"]
    assert result["truncated"] is False


def test_helper_glob_caps_results_before_serializing(
    tmp_path: Path, search_root: Path
) -> None:
    result = _run_helper(
        tmp_path,
        {
            "mode": "glob",
            "path": str(search_root),
            "pattern": "**/*",
            "include_dirs": True,
            "max_results": 2,
        },
    )
    assert len(result["matches"]) == 2
    assert result["truncated"] is True


def test_helper_glob_rejects_an_absolute_pattern(
    tmp_path: Path, search_root: Path
) -> None:
    result = _run_helper(
        tmp_path,
        {
            "mode": "glob",
            "path": str(search_root),
            "pattern": "/etc/*",
            "include_dirs": False,
            "max_results": 10,
        },
    )
    assert "relative" in result["error"]


def test_helper_glob_rejects_an_escaping_pattern(
    tmp_path: Path, search_root: Path
) -> None:
    result = _run_helper(
        tmp_path,
        {
            "mode": "glob",
            "path": str(search_root),
            "pattern": "../*.py",
            "include_dirs": False,
            "max_results": 10,
        },
    )
    assert "escape" in result["error"]


def test_helper_glob_reports_a_missing_root(tmp_path: Path, search_root: Path) -> None:
    result = _run_helper(
        tmp_path,
        {
            "mode": "glob",
            "path": str(tmp_path / "absent"),
            "pattern": "*.py",
            "include_dirs": False,
            "max_results": 10,
        },
    )
    assert result["missing"] is True


def test_helper_grep_reports_paths_and_line_numbers(
    tmp_path: Path, search_root: Path
) -> None:
    result = _run_helper(
        tmp_path,
        {
            "mode": "grep",
            "path": str(search_root),
            "pattern": "needle",
            "glob": None,
            "literal": False,
            "case_sensitive": False,
            "max_results": 10,
        },
    )
    hits = {
        (Path(match["path"]).name, match["line_number"], match["line"])
        for match in result["matches"]
    }
    assert hits == {("b.py", 1, "needle here"), ("notes.md", 1, "Needle too")}
    assert result["truncated"] is False


def test_helper_grep_filters_files_by_glob(tmp_path: Path, search_root: Path) -> None:
    result = _run_helper(
        tmp_path,
        {
            "mode": "grep",
            "path": str(search_root),
            "pattern": "needle",
            "glob": "*.md",
            "literal": True,
            "case_sensitive": False,
            "max_results": 10,
        },
    )
    assert [Path(match["path"]).name for match in result["matches"]] == ["notes.md"]


def test_helper_grep_honors_literal_and_case_sensitivity(
    tmp_path: Path, search_root: Path
) -> None:
    case_sensitive = _run_helper(
        tmp_path,
        {
            "mode": "grep",
            "path": str(search_root),
            "pattern": "needle",
            "glob": "*.md",
            "literal": True,
            "case_sensitive": True,
            "max_results": 10,
        },
    )
    assert case_sensitive["matches"] == []

    regex = _run_helper(
        tmp_path,
        {
            "mode": "grep",
            "path": str(search_root),
            "pattern": "ne+dle",
            "glob": "*.md",
            "literal": False,
            "case_sensitive": False,
            "max_results": 10,
        },
    )
    assert len(regex["matches"]) == 1


def test_helper_grep_caps_and_reports_truncation(
    tmp_path: Path, search_root: Path
) -> None:
    result = _run_helper(
        tmp_path,
        {
            "mode": "grep",
            "path": str(search_root),
            "pattern": "e",
            "glob": None,
            "literal": True,
            "case_sensitive": False,
            "max_results": 1,
        },
    )
    assert len(result["matches"]) == 1
    assert result["truncated"] is True
