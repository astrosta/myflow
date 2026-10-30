import pytest

from agentkit.agents.leader_agent import ASSEMBLY_INPUT_KEYS, AgentAssembly
from agentkit.client import DEFAULT_USER_ID, AgentClient
from agentkit.config import AppConfig, CheckpointerConfig, DatabaseConfig
from agentkit.runtime.checkpointer import provider


class _Graph:
    def __init__(self, checkpointer: object) -> None:
        self.checkpointer = checkpointer
        self.calls: list[dict] = []

    def stream(self, state, config=None, *, context=None, stream_mode=None):
        self.calls.append(
            {"state": state, "config": config, "context": context, "stream_mode": stream_mode}
        )
        return []


def _install_assemble(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    assembled: list[dict] = []

    def assemble(config, *, app_config=None, checkpointer=None):
        graph = _Graph(checkpointer)
        assembled.append(
            {
                "config": config,
                "app_config": app_config,
                "checkpointer": checkpointer,
                "graph": graph,
            }
        )
        return AgentAssembly(graph=graph, descriptor=None)

    monkeypatch.setattr("agentkit.client.assemble_agent", assemble)
    return assembled


def test_same_inputs_assemble_the_graph_once(monkeypatch: pytest.MonkeyPatch) -> None:
    assembled = _install_assemble(monkeypatch)
    client = AgentClient(app_config=AppConfig(), checkpointer=object())
    for _ in range(3):
        list(client.stream("hello", thread_id="t1"))
    assert len(assembled) == 1


@pytest.mark.parametrize("key", sorted(ASSEMBLY_INPUT_KEYS))
def test_changing_one_assembly_input_rebuilds(key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assembled = _install_assemble(monkeypatch)
    client = AgentClient(app_config=AppConfig(), checkpointer=object())
    list(client.stream("hello", thread_id="t1"))
    list(client.stream("hello", thread_id="t1", **{key: "changed"}))
    assert len(assembled) == 2
    assert assembled[1]["config"]["configurable"][key] == "changed"


def test_thread_coordinates_and_recursion_limit_do_not_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    assembled = _install_assemble(monkeypatch)
    client = AgentClient(app_config=AppConfig(), checkpointer=object())
    list(client.stream("hello", thread_id="t1"))
    list(client.stream("hello", thread_id="t2"))
    list(client.stream("hello", thread_id="t2", checkpoint_ns="child"))
    list(client.stream("hello", thread_id="t2", checkpoint_ns="child", recursion_limit=4))
    assert len(assembled) == 1
    last = assembled[0]["graph"].calls[-1]
    assert last["config"]["configurable"]["thread_id"] == "t2"
    assert last["config"]["configurable"]["checkpoint_ns"] == "child"
    assert last["config"]["recursion_limit"] == 4


def test_every_embedded_run_carries_a_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """M8 §10.2：委派预算按 run 计，嵌入路径也必须把 run_id 放进 runtime context。"""
    assembled = _install_assemble(monkeypatch)
    client = AgentClient(app_config=AppConfig(), checkpointer=object())

    list(client.stream("hello", thread_id="t1"))
    list(client.stream("hello", thread_id="t1"))

    contexts = [call["context"] for call in assembled[0]["graph"].calls]
    assert all(context["run_id"] for context in contexts)
    assert contexts[0]["run_id"] != contexts[1]["run_id"], "两次 stream 是两次 run"
    assert all(context["thread_id"] == "t1" for context in contexts)


def test_reset_checkpointer_rebuilds_onto_the_new_saver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provider,
        "_resolve_checkpointer_settings",
        lambda: (CheckpointerConfig(), DatabaseConfig()),
    )
    provider.reset_checkpointer()
    assembled = _install_assemble(monkeypatch)
    client = AgentClient(app_config=AppConfig())
    try:
        list(client.stream("hello", thread_id="t1"))
        first = assembled[0]["checkpointer"]
        provider.reset_checkpointer()
        list(client.stream("hello", thread_id="t1"))
        second = assembled[1]["checkpointer"]
        assert first is not second
        assert second is provider.get_checkpointer()
        assert assembled[1]["graph"].checkpointer is second
    finally:
        provider.reset_checkpointer()


def test_set_app_config_rebuilds(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentkit.config import app_config as app_config_module

    assembled = _install_assemble(monkeypatch)
    previous = (
        app_config_module._app_config,
        app_config_module._app_config_is_custom,
        app_config_module._app_config_path,
        app_config_module._app_config_signature,
    )
    first = AppConfig()
    second = AppConfig()
    try:
        app_config_module.set_app_config(first)
        client = AgentClient(checkpointer=object())
        list(client.stream("hello", thread_id="t1"))
        app_config_module.set_app_config(second)
        list(client.stream("hello", thread_id="t1"))
        assert assembled[0]["app_config"] is first
        assert assembled[1]["app_config"] is second
    finally:
        (
            app_config_module._app_config,
            app_config_module._app_config_is_custom,
            app_config_module._app_config_path,
            app_config_module._app_config_signature,
        ) = previous


def test_reset_agent_rebuilds(monkeypatch: pytest.MonkeyPatch) -> None:
    assembled = _install_assemble(monkeypatch)
    client = AgentClient(app_config=AppConfig(), checkpointer=object())
    list(client.stream("hello", thread_id="t1"))
    client.reset_agent()
    list(client.stream("hello", thread_id="t1"))
    assert len(assembled) == 2


def test_runtime_context_user_id_matches_the_cache_key(monkeypatch: pytest.MonkeyPatch) -> None:
    assembled = _install_assemble(monkeypatch)
    client = AgentClient(app_config=AppConfig(), checkpointer=object())
    list(client.stream("hello", thread_id="t1"))
    context = assembled[0]["graph"].calls[0]["context"]
    assert context["thread_id"] == "t1"
    assert context["user_id"] == DEFAULT_USER_ID
    assert context["user_id"] == client._cached[0].user_id
