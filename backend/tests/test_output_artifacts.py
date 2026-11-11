"""Output artifacts: what the sandbox wrote and what the UI can list."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agentkit.config.paths import Paths
from agentkit.sandbox.artifacts import OUTPUTS_VIRTUAL, list_output_artifacts
from backend.app.gateway.state_reader import read_thread_state


def test_output_artifacts_are_virtual_paths_under_outputs(tmp_path, monkeypatch) -> None:
    paths = Paths(tmp_path)
    paths.ensure_thread_dirs("t1", user_id="alice")
    outputs = paths.sandbox_outputs_dir("t1", user_id="alice")
    (outputs / "report.txt").write_text("ok", encoding="utf-8")
    (outputs / "nested").mkdir()
    (outputs / "nested" / "chart.svg").write_text("<svg/>", encoding="utf-8")
    (outputs / ".partial.part").write_text("ignore", encoding="utf-8")
    (outputs / "empty").mkdir()

    assert list_output_artifacts("t1", user_id="alice", paths=paths) == [
        f"{OUTPUTS_VIRTUAL}/nested/chart.svg",
        f"{OUTPUTS_VIRTUAL}/report.txt",
    ]


def test_output_artifacts_are_empty_without_a_directory(tmp_path) -> None:
    assert list_output_artifacts("missing", user_id="alice", paths=Paths(tmp_path)) == []


def test_output_artifacts_do_not_list_a_symlink_that_escapes_outputs(tmp_path) -> None:
    paths = Paths(tmp_path)
    paths.ensure_thread_dirs("t1", user_id="alice")
    outputs = paths.sandbox_outputs_dir("t1", user_id="alice")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = outputs / "secret.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform")

    assert list_output_artifacts("t1", user_id="alice", paths=paths) == []


def test_finalize_records_the_outputs_list_in_the_checkpoint(monkeypatch) -> None:
    from agentkit.runtime.runs import worker

    seen: dict = {}

    class _Mutation:
        async def aupdate_state(self, config, values, *, as_node):
            seen["config"] = config
            seen["values"] = values
            seen["as_node"] = as_node

    head = SimpleNamespace(config={"configurable": {"thread_id": "t1"}})

    class _Checkpointer:
        async def aget_tuple(self, _config):
            return head

    monkeypatch.setattr(worker, "build_state_mutation_graph", lambda *a, **kw: _Mutation())
    monkeypatch.setattr(
        worker,
        "list_output_artifacts",
        lambda thread_id, user_id=None: [f"{OUTPUTS_VIRTUAL}/report.txt"],
    )

    ctx = SimpleNamespace(checkpointer=_Checkpointer())
    record = SimpleNamespace(thread_id="t1", user_id="alice")
    asyncio.run(worker._record_artifacts(ctx, record))

    assert seen["values"]["artifacts"].value == [f"{OUTPUTS_VIRTUAL}/report.txt"]
    assert seen["as_node"] == "artifact_scan"


def test_thread_state_always_exposes_the_actual_outputs_list(tmp_path, monkeypatch) -> None:
    paths = Paths(tmp_path)
    paths.ensure_thread_dirs("t1", user_id="alice")
    (paths.sandbox_outputs_dir("t1", user_id="alice") / "answer.md").write_text(
        "done", encoding="utf-8"
    )
    monkeypatch.setattr("agentkit.sandbox.artifacts.get_paths", lambda: paths)

    saved = SimpleNamespace(
        checkpoint={"channel_values": {"messages": [], "artifacts": ["stale.txt"]}},
        config={"configurable": {"checkpoint_id": "cp-1", "checkpoint_ns": ""}},
        pending_writes=[],
    )

    class _Checkpointer:
        async def aget_tuple(self, _config):
            return saved

    services = SimpleNamespace(run_context=SimpleNamespace(checkpointer=_Checkpointer()))
    state = asyncio.run(read_thread_state(services, "t1", user_id="alice"))

    assert state.values["artifacts"] == [f"{OUTPUTS_VIRTUAL}/answer.md"]


def test_missing_checkpoint_still_exposes_output_artifacts(tmp_path, monkeypatch) -> None:
    paths = Paths(tmp_path)
    paths.ensure_thread_dirs("t1", user_id="alice")
    (paths.sandbox_outputs_dir("t1", user_id="alice") / "answer.md").write_text(
        "done", encoding="utf-8"
    )
    monkeypatch.setattr("agentkit.sandbox.artifacts.get_paths", lambda: paths)

    class _Checkpointer:
        async def aget_tuple(self, _config):
            return None

    services = SimpleNamespace(run_context=SimpleNamespace(checkpointer=_Checkpointer()))
    state = asyncio.run(read_thread_state(services, "t1", user_id="alice"))

    assert state.values["artifacts"] == [f"{OUTPUTS_VIRTUAL}/answer.md"]
