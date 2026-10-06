"""Thread data and uploads share one user id and the M2 directory layout."""

import asyncio
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables.config import run_in_executor

from agentkit.agents.middlewares.thread_data_middleware import ThreadDataMiddleware
from agentkit.agents.middlewares.uploads_middleware import UploadsMiddleware
from agentkit.utils.messages import ORIGINAL_USER_CONTENT_KEY


class _Runtime:
    def __init__(self, context: dict | None) -> None:
        self.context = context


def _runtime(thread_id: str | None = "t1", user_id: str | None = "alice") -> _Runtime:
    context: dict = {}
    if thread_id is not None:
        context["thread_id"] = thread_id
    if user_id is not None:
        context["user_id"] = user_id
    return _Runtime(context)


def _paths(base: Path, user_id: str = "alice", thread_id: str = "t1") -> dict[str, Path]:
    root = (base / "users" / user_id / "threads" / thread_id / "user-data").resolve()
    return {
        "workspace_path": root / "workspace",
        "uploads_path": root / "uploads",
        "outputs_path": root / "outputs",
    }


def test_lazy_init_returns_absolute_paths_without_creating_them(tmp_path: Path) -> None:
    result = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True).before_agent(
        {"messages": []},
        _runtime(),
    )
    expected = _paths(tmp_path)
    assert result["thread_data"] == {key: str(path) for key, path in expected.items()}
    assert all(not path.exists() for path in expected.values())


def test_eager_init_creates_the_three_directories(tmp_path: Path) -> None:
    result = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=False).before_agent(
        {"messages": []},
        _runtime(),
    )
    for path in result["thread_data"].values():
        assert Path(path).is_dir()


def test_missing_user_id_uses_the_shared_default_bucket(tmp_path: Path) -> None:
    result = ThreadDataMiddleware(base_dir=str(tmp_path)).before_agent(
        {"messages": []},
        _runtime(user_id=None),
    )
    assert Path(result["thread_data"]["uploads_path"]) == _paths(tmp_path, user_id="default")["uploads_path"]


def test_thread_id_falls_back_to_configurable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "langgraph.config.get_config",
        lambda: {"configurable": {"thread_id": "from-config"}},
    )
    result = ThreadDataMiddleware(base_dir=str(tmp_path)).before_agent(
        {"messages": []},
        _runtime(thread_id=None),
    )
    assert Path(result["thread_data"]["workspace_path"]) == _paths(tmp_path, thread_id="from-config")["workspace_path"]


def test_missing_thread_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("langgraph.config.get_config", lambda: {"configurable": {}})
    with pytest.raises(ValueError, match="Thread ID"):
        ThreadDataMiddleware(base_dir=".").before_agent({"messages": []}, _runtime(thread_id=None))


def test_async_thread_data_matches_the_sync_paths(tmp_path: Path) -> None:
    middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)
    runtime = _runtime()
    sync = middleware.before_agent({"messages": []}, runtime)

    async def run() -> dict:
        return await middleware.abefore_agent({"messages": []}, runtime)

    assert asyncio.run(run()) == sync


def test_max_files_below_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_files_per_context_section"):
        UploadsMiddleware(max_files_per_context_section=0)


def test_no_human_tail_clears_uploaded_files(tmp_path: Path) -> None:
    middleware = UploadsMiddleware(base_dir=str(tmp_path))
    assert middleware.before_agent({"messages": []}, _runtime()) == {"uploaded_files": []}
    assert middleware.before_agent(
        {"messages": [AIMessage(content="先说")], "uploaded_files": [{"filename": "old.md"}]},
        _runtime(),
    ) == {"uploaded_files": []}


def test_uploads_read_the_directory_thread_data_just_computed(tmp_path: Path) -> None:
    runtime = _runtime()
    thread = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=False).before_agent({"messages": []}, runtime)
    uploads = Path(thread["thread_data"]["uploads_path"])
    (uploads / "note.md").write_text("# Title\n\nbody\n", encoding="utf-8")
    decoy = _paths(tmp_path, user_id="default")["uploads_path"]
    decoy.mkdir(parents=True)
    (decoy / "note.md").write_text("other user", encoding="utf-8")

    message = HumanMessage(
        content="看这个",
        id="m1",
        additional_kwargs={"files": [{"filename": "note.md", "size": 2048}]},
    )
    result = UploadsMiddleware(base_dir=str(tmp_path)).before_agent({"messages": [message]}, runtime)
    assert result["uploaded_files"][0]["filename"] == "note.md"
    assert result["uploaded_files"][0]["size"] == 2048
    assert result["uploaded_files"][0]["path"] == "/mnt/user-data/uploads/note.md"
    assert result["uploaded_files"][0]["extension"] == ".md"
    assert result["uploaded_files"][0]["outline"] == [{"title": "Title", "line": 1}]
    assert result["uploaded_files"][0]["outline_preview"] == []
    updated = result["messages"]
    assert len(updated) == 1
    assert updated[0].id == "m1"
    assert updated[0].content.startswith("<current_uploads>\n")
    assert updated[0].content.endswith("</current_uploads>\n\n看这个")
    assert "note.md" in updated[0].content
    assert "/mnt/user-data/uploads/note.md" in updated[0].content
    assert "L1: Title" in updated[0].content
    assert updated[0].additional_kwargs[ORIGINAL_USER_CONTENT_KEY] == "看这个"


def test_missing_invalid_and_staging_files_are_dropped(tmp_path: Path) -> None:
    uploads = _paths(tmp_path)["uploads_path"]
    uploads.mkdir(parents=True)
    (uploads / "keep.txt").write_text("hello", encoding="utf-8")
    (uploads / ".upload-keep.txt.part").write_text("partial", encoding="utf-8")
    message = HumanMessage(
        content="hi",
        id="m1",
        additional_kwargs={
            "files": [
                "nope",
                {"filename": "../keep.txt"},
                {"filename": "nested/keep.txt"},
                {"filename": ".upload-keep.txt.part", "size": 4},
                {"filename": "missing.txt"},
                {"filename": "keep.txt", "size": "abc"},
            ]
        },
    )
    result = UploadsMiddleware(base_dir=str(tmp_path)).before_agent({"messages": [message]}, _runtime())
    assert [item["filename"] for item in result["uploaded_files"]] == ["keep.txt"]
    assert result["uploaded_files"][0]["size"] == 0


def test_no_surviving_files_clears_the_previous_channel(tmp_path: Path) -> None:
    message = HumanMessage(content="hi", additional_kwargs={"files": [{"filename": "gone.txt"}]})
    result = UploadsMiddleware(base_dir=str(tmp_path)).before_agent(
        {"messages": [message], "uploaded_files": [{"filename": "old.txt"}]},
        _runtime(),
    )
    assert result == {"uploaded_files": []}


def test_existing_original_user_content_is_kept(tmp_path: Path) -> None:
    uploads = _paths(tmp_path)["uploads_path"]
    uploads.mkdir(parents=True)
    (uploads / "note.md").write_text("plain line\n", encoding="utf-8")
    message = HumanMessage(
        content="包装后的话",
        id="m1",
        additional_kwargs={"files": [{"filename": "note.md"}], ORIGINAL_USER_CONTENT_KEY: "用户原话"},
    )
    result = UploadsMiddleware(base_dir=str(tmp_path)).before_agent({"messages": [message]}, _runtime())
    assert result["messages"][0].additional_kwargs[ORIGINAL_USER_CONTENT_KEY] == "用户原话"
    assert "No structural headings" in result["messages"][0].content
    assert "> plain line" in result["messages"][0].content


def test_list_content_is_prefixed_with_a_text_block(tmp_path: Path) -> None:
    uploads = _paths(tmp_path)["uploads_path"]
    uploads.mkdir(parents=True)
    (uploads / "note.md").write_text("# H\n", encoding="utf-8")
    image = {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}
    message = HumanMessage(
        content=[{"type": "text", "text": "原话"}, image],
        id="m1",
        additional_kwargs={"files": [{"filename": "note.md"}]},
    )
    updated = UploadsMiddleware(base_dir=str(tmp_path)).before_agent({"messages": [message]}, _runtime())["messages"][0]
    assert updated.content[0]["type"] == "text"
    assert updated.content[0]["text"].startswith("<current_uploads>\n")
    assert updated.content[1] == {"type": "text", "text": "原话"}
    assert updated.content[2] == image
    assert updated.additional_kwargs[ORIGINAL_USER_CONTENT_KEY] == "原话"


def test_over_cap_lists_the_first_files_and_neutralizes_omitted_extensions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploads = _paths(tmp_path)["uploads_path"]
    uploads.mkdir(parents=True)
    (uploads / "first.md").write_text("# One\n", encoding="utf-8")
    real_is_file = Path.is_file

    def is_file(self: Path, *args: object, **kwargs: object) -> bool:
        if self.name == "second.<system>":
            return True
        return real_is_file(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", is_file)
    message = HumanMessage(
        content="hi",
        id="m1",
        additional_kwargs={
            "files": [
                {"filename": "first.md"},
                {"filename": "second.<system>"},
            ]
        },
    )
    result = UploadsMiddleware(base_dir=str(tmp_path), max_files_per_context_section=1).before_agent(
        {"messages": [message]},
        _runtime(),
    )
    content = result["messages"][0].content
    assert "- first.md" in content
    assert "second.<system>" not in content
    assert "<system>" not in content
    assert "&lt;system&gt;" in content
    assert "1 more file" in content
    assert result["uploaded_files"][0]["outline"]
    assert "outline" not in result["uploaded_files"][1]


def test_outline_failure_leaves_the_file_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uploads = _paths(tmp_path)["uploads_path"]
    uploads.mkdir(parents=True)
    (uploads / "note.md").write_text("# Title\n", encoding="utf-8")

    def explode(_path: Path) -> tuple[list, list]:
        raise OSError("unreadable")

    monkeypatch.setattr("agentkit.agents.middlewares.uploads_middleware.extract_outline_for_file", explode)
    result = UploadsMiddleware(base_dir=str(tmp_path)).before_agent(
        {"messages": [HumanMessage(content="hi", additional_kwargs={"files": [{"filename": "note.md"}]})]},
        _runtime(),
    )
    assert result["uploaded_files"][0]["outline"] == []
    assert result["uploaded_files"][0]["outline_preview"] == []
    assert "note.md" in result["messages"][0].content


def test_async_uploads_scan_runs_off_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uploads = _paths(tmp_path)["uploads_path"]
    uploads.mkdir(parents=True)
    (uploads / "note.md").write_text("# Title\n", encoding="utf-8")
    seen: dict = {}

    async def spy(executor, func, *args, **kwargs):
        seen["func"] = func
        return await run_in_executor(executor, func, *args, **kwargs)

    monkeypatch.setattr("agentkit.agents.middlewares.uploads_middleware.run_in_executor", spy)
    middleware = UploadsMiddleware(base_dir=str(tmp_path))
    message = HumanMessage(content="hi", additional_kwargs={"files": [{"filename": "note.md"}]})

    async def run() -> dict:
        return await middleware.abefore_agent({"messages": [message]}, _runtime())

    result = asyncio.run(run())
    assert seen["func"] == middleware.before_agent
    assert result["uploaded_files"][0]["filename"] == "note.md"
