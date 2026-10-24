"""思考内容要能到前端：messages 帧里必须带 reasoning_content。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from fakes.chat_model import ReasoningChatModel

import agentkit.agents.leader_agent.agent as leader_agent_module
import agentkit.agents.middlewares.title_middleware as title_middleware_module
from agentkit.config import AppConfig, set_app_config
from backend.app.gateway.app import create_app


def _config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "models": [
                {
                    "name": "fake",
                    "use": "langchain_openai:ChatOpenAI",
                    "model": "fake-model",
                    "api_key": "sk-fake",
                    "supports_thinking": True,
                }
            ],
            "agents": {"default_model": "fake"},
            "database": {"backend": "memory"},
            "checkpointer": {"type": "memory"},
            "run_events": {"backend": "memory"},
            "auth": {"enabled": False},
            "memory": {"enabled": False},
            "pii": {"enabled": False},
            "knowledge_base": {"enabled": False},
            "stream_bridge": {"backend": "memory"},
            "sandbox": {
                "use": "agentkit.sandbox.local:LocalSandboxProvider",
                "allow_host_bash": False,
            },
        }
    )


@pytest.fixture(autouse=True)
def _restore_app_config():
    """`set_app_config` 是进程级注入：测试结束必须还原，否则会污染后续用例
    （`test_reset_admin` 就是被它顶掉真实配置后变红的）。"""

    import agentkit.config.app_config as app_config_module

    saved = (
        app_config_module._app_config,
        app_config_module._app_config_path,
        app_config_module._app_config_signature,
        app_config_module._app_config_is_custom,
    )
    yield
    (
        app_config_module._app_config,
        app_config_module._app_config_path,
        app_config_module._app_config_signature,
        app_config_module._app_config_is_custom,
    ) = saved


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(
        leader_agent_module,
        "create_chat_model",
        lambda **kwargs: ReasoningChatModel(reasoning="先想一步", reply="答案在这"),
    )
    monkeypatch.setattr(
        title_middleware_module,
        "create_chat_model",
        lambda **kwargs: ReasoningChatModel(reasoning="标题", reply="标题"),
    )
    set_app_config(_config())
    with TestClient(create_app()) as test_client:
        yield test_client


def test_thinking_mode_streams_reasoning_content(client: TestClient) -> None:
    assert client.post("/api/threads", json={"thread_id": "think-1"}).status_code == 201

    body = {
        "assistant_id": "lead_agent",
        "input": {"messages": [{"type": "human", "content": "你好"}]},
        "stream_mode": ["messages-tuple", "updates", "custom"],
        "context": {"thinking_enabled": True},
    }
    with client.stream("POST", "/api/threads/think-1/runs/stream", json=body) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())

    assert "reasoning_content" in text
    assert "先想一步" in text
    assert "答案在这" in text
    assert "event: end" in text
