"""整条 run 的收尾：SSE 必须收到 `end`，run 状态必须落地。

回归背景：worker 的 `finally` 里用了 `suppress(Exception)` 却没 import，
于是每次 run 都在收尾时抛 NameError——帧能发出去，但 `end` 永远不来，
`publish_end` / 终态 / 标题投影全被跳过，前端看起来就是"没有流式输出"。
"""

from __future__ import annotations

import queue
import threading

import pytest
from fastapi.testclient import TestClient

import agentkit.agents.leader_agent.agent as leader_agent_module
import agentkit.agents.middlewares.title_middleware as title_middleware_module
from agentkit.config import AppConfig, set_app_config
from backend.app.gateway.app import create_app
from fakes.chat_model import FakeChatModel

#: 收尾一旦抛异常，流就永远不结束。读流必须有上限，否则这条用例会挂住整个 CI。
STREAM_DEADLINE_SECONDS = 30.0


def _config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "models": [
                {
                    "name": "fake",
                    "use": "langchain_openai:ChatOpenAI",
                    "model": "fake-model",
                    "api_key": "sk-fake",
                }
            ],
            "agents": {"default_model": "fake"},
            "database": {"backend": "memory"},
            "checkpointer": {"type": "memory"},
            "run_events": {"backend": "memory"},
            "auth": {"enabled": False},
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
    """真跑一遍 agent 组装，只把模型换成确定性 fake。"""

    monkeypatch.setattr(leader_agent_module, "create_chat_model", lambda **kwargs: FakeChatModel())
    monkeypatch.setattr(
        title_middleware_module, "create_chat_model", lambda **kwargs: FakeChatModel()
    )
    set_app_config(_config())
    application = create_app()
    with TestClient(application) as test_client:
        yield test_client


def _read_stream_bounded(client: TestClient, url: str, body: dict) -> str:
    """在守护线程里读 SSE；到点还没结束就判定失败（守护线程不会把 pytest 挂死）。"""

    outcome: queue.Queue[tuple[str, object]] = queue.Queue()

    def run() -> None:
        try:
            with client.stream("POST", url, json=body) as response:
                outcome.put(("status", response.status_code))
                outcome.put(("text", "".join(response.iter_text())))
        except BaseException as exc:  # noqa: BLE001 - 交给调用方判定
            outcome.put(("error", exc))

    thread = threading.Thread(target=run, name="sse-reader", daemon=True)
    thread.start()

    status = outcome.get(timeout=STREAM_DEADLINE_SECONDS)
    assert status == ("status", 200)
    kind, payload = outcome.get(timeout=STREAM_DEADLINE_SECONDS)
    assert kind == "text", payload
    return str(payload)


def test_stream_ends_and_run_is_success(client: TestClient) -> None:
    assert client.post("/api/threads", json={"thread_id": "end-1"}).status_code == 201

    body = {
        "assistant_id": "lead_agent",
        "input": {"messages": [{"type": "human", "content": "你好"}]},
        "stream_mode": ["messages-tuple", "updates", "custom"],
    }
    text = _read_stream_bounded(client, "/api/threads/end-1/runs/stream", body)

    assert "event: metadata" in text
    # 这条断言就是那个 NameError 的护栏：帧发完之后必须还有 end
    assert "event: end" in text

    runs = client.get("/api/threads/end-1/runs").json()["data"]
    assert [run["status"] for run in runs] == ["success"]
    assert runs[0]["error"] is None
