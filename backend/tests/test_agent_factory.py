from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from agentkit.agents.leader_agent import (
    ASSEMBLY_INPUT_KEYS,
    agent_cache_key,
    assemble_agent,
)
from agentkit.config import AppConfig, ModelConfig


def _key(cfg: Mapping[str, Any] | None = None, *, app_config: AppConfig | None = None, checkpointer: Any = None, user_id: str = "default"):
    return agent_cache_key(
        cfg or {},
        app_config=app_config or AppConfig(),
        checkpointer=checkpointer if checkpointer is not None else object(),
        user_id=user_id,
    )


def test_missing_reasoning_effort_is_a_different_key_from_none() -> None:
    app_config = AppConfig()
    checkpointer = object()
    missing = _key({}, app_config=app_config, checkpointer=checkpointer)
    explicit_none = _key({"reasoning_effort": None}, app_config=app_config, checkpointer=checkpointer)
    assert missing != explicit_none
    assert hash(missing) != hash(explicit_none)


def test_reasoning_effort_negotiation_never_sends_unpublished_levels() -> None:
    from agentkit.agents.leader_agent.agent import _negotiate_reasoning_effort

    config = AppConfig(
        models=[
            ModelConfig.model_validate(
                {"name": "flash", "use": "langchain_openai:ChatOpenAI", "reasoning_efforts": ["low", "high"]}
            ),
            ModelConfig.model_validate({"name": "plain", "use": "langchain_openai:ChatOpenAI"}),
        ]
    )

    assert _negotiate_reasoning_effort("flash", None, config) is None
    assert _negotiate_reasoning_effort("flash", "high", config) == "high"
    # 过期等级（模型没公布）/ 模型不支持 → 不下发，回退 provider 默认
    assert _negotiate_reasoning_effort("flash", "medium", config) is None
    assert _negotiate_reasoning_effort("plain", "high", config) is None


def test_app_config_and_checkpointer_compare_by_identity() -> None:
    left_config = AppConfig()
    right_config = AppConfig()
    left_saver = object()
    right_saver = object()
    assert left_config == right_config
    assert _key(app_config=left_config, checkpointer=left_saver) != _key(
        app_config=right_config, checkpointer=left_saver
    )
    assert _key(app_config=left_config, checkpointer=left_saver) != _key(
        app_config=left_config, checkpointer=right_saver
    )
    assert _key(app_config=left_config, checkpointer=left_saver) == _key(
        app_config=left_config, checkpointer=left_saver
    )


def test_user_id_is_part_of_the_cache_key() -> None:
    app_config = AppConfig()
    checkpointer = object()
    assert _key(app_config=app_config, checkpointer=checkpointer, user_id="default") != _key(
        app_config=app_config, checkpointer=checkpointer, user_id="other"
    )


class _RecordingConfig(Mapping[str, Any]):
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = dict(data)
        self.read: set[str] = set()

    def __getitem__(self, key: str) -> Any:
        self.read.add(key)
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        self.read.add(key)
        return self._data.get(key, default)

    def __contains__(self, key: object) -> bool:
        self.read.add(str(key))
        return key in self._data

    def __iter__(self):
        self.read.update(self._data)
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def test_assemble_agent_reads_only_assembly_input_keys(monkeypatch) -> None:
    app_config = AppConfig(
        agents={"default_model": "m"},
        models=[ModelConfig(name="m", use="langchain_core.language_models.fake_chat_models.FakeListChatModel")],
    )
    watched = _RecordingConfig(
        {
            "thread_id": "t1",
            "checkpoint_ns": "",
            "model_name": "m",
            "run_id": "run",
        }
    )

    monkeypatch.setattr(
        "agentkit.agents.leader_agent.agent.create_chat_model",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "agentkit.agents.leader_agent.agent.get_available_tools",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "agentkit.agents.leader_agent.agent.create_agent",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    assemble_agent({"configurable": watched}, app_config=app_config, checkpointer=object())
    assert "model_name" in watched.read
    assert watched.read <= ASSEMBLY_INPUT_KEYS
    assert "thread_id" not in watched.read
