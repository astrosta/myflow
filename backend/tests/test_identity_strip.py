"""M9 §2.1 / §2.9：客户端声明的身份一律**删掉**，且到不了 `RunCaller`。

`strip_client_identity` 是四条入口（HTTP / IM / 调度 / 嵌入）共用的那一处：
`metadata` / `config.configurable` / `config.context` / `context` 四处同一条规则。
唯一权威来源是服务端认证注入的 `runtime.context.user_id`，所以 body 里的身份
既不能被使用，也不能被"校验后使用"。
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from backend.app.gateway.deps import caller_from_request
from backend.app.gateway.errors import install_error_handlers
from backend.app.gateway.run_models import RunCaller, RunCreateRequest
from backend.app.gateway.services import strip_client_identity

_IDENTITY_KEYS = ("user_id", "owner_id", "auth_user", "auth_user_id")

#: 不在剥离名单里的同层键：它们由后面的规则（collect_client_switches 等）处理。
_NEIGHBOURS: dict[str, Any] = {
    "channel_user_id": "wechat-1",
    "is_internal": True,
    "user_role": "admin",
    "authz_attributes": {"tenant": "t1"},
    "run_id": "client-run-id",
    "non_interactive": True,
}


def _bag(**extra: Any) -> dict[str, Any]:
    return {key: f"client-{key}" for key in _IDENTITY_KEYS} | extra


def _body(**overrides: Any) -> RunCreateRequest:
    payload: dict[str, Any] = {
        "assistant_id": "lead_agent",
        "metadata": _bag(topic="x"),
        "config": {"configurable": _bag(model_name="m"), "context": _bag(topic="x")},
        "context": _bag(topic="x"),
    }
    payload.update(overrides)
    return RunCreateRequest(**payload)


def _bags(body: RunCreateRequest) -> tuple[dict[str, Any], ...]:
    return (body.metadata, body.config["configurable"], body.config["context"], body.context)


def test_the_client_identity_list_matches_the_design() -> None:
    from backend.app.gateway.services import _CLIENT_IDENTITY_KEYS

    assert set(_CLIENT_IDENTITY_KEYS) == set(_IDENTITY_KEYS)
    assert len(_CLIENT_IDENTITY_KEYS) == 4


def test_strip_removes_identity_from_all_four_bags() -> None:
    body = _body()
    strip_client_identity(body)

    assert set(body.metadata) == {"topic"}
    assert set(body.config["configurable"]) == {"model_name"}
    assert set(body.config["context"]) == {"topic"}
    assert set(body.context) == {"topic"}
    for bag in _bags(body):
        assert set(bag) & set(_IDENTITY_KEYS) == set()


def test_strip_keeps_the_other_keys_in_the_same_bag() -> None:
    body = _body()
    strip_client_identity(body)

    assert body.metadata == {"topic": "x"}
    assert body.config["configurable"] == {"model_name": "m"}
    assert body.config["context"] == {"topic": "x"}
    assert body.context == {"topic": "x"}


def test_only_the_four_identity_keys_are_removed() -> None:
    body = _body()
    for bag in _bags(body):
        bag.update(_NEIGHBOURS)
    strip_client_identity(body)

    for bag in _bags(body):
        assert set(_NEIGHBOURS) <= set(bag)
        assert bag["channel_user_id"] == "wechat-1"
        assert bag["is_internal"] is True
        assert bag["authz_attributes"] == {"tenant": "t1"}
        assert set(bag) & set(_IDENTITY_KEYS) == set()


def test_missing_containers_do_not_explode() -> None:
    body = RunCreateRequest(assistant_id="lead_agent")
    assert (body.metadata, body.config, body.context) == (None, None, None)

    strip_client_identity(body)

    assert (body.metadata, body.config, body.context) == (None, None, None)


def test_non_dict_bags_inside_config_do_not_explode() -> None:
    body = RunCreateRequest(
        assistant_id="lead_agent",
        metadata=_bag(topic="x"),
        config={"configurable": None, "context": "not-a-dict"},
        context=_bag(topic="x"),
    )

    strip_client_identity(body)

    assert body.config == {"configurable": None, "context": "not-a-dict"}
    assert body.metadata == {"topic": "x"}
    assert body.context == {"topic": "x"}


def _bare_app() -> FastAPI:
    """裸 app：**不装** AuthMiddleware，所以 `request.state.auth` 缺失 → 匿名 caller（§2.9）。"""
    application = FastAPI()
    install_error_handlers(application)

    @application.post("/probe/caller")
    def probe(body: RunCreateRequest, caller: RunCaller = Depends(caller_from_request)) -> dict[str, Any]:
        strip_client_identity(body)
        return {
            "caller_user_id": caller.user_id,
            "caller_kind": str(caller.kind),
            "metadata": body.metadata,
            "config": body.config,
            "context": body.context,
        }

    return application


def test_body_identity_never_reaches_the_caller() -> None:
    client = TestClient(_bare_app())

    response = client.post(
        "/probe/caller",
        headers={"X-DeerFlow-Owner-User-Id": "someone"},
        json={
            "assistant_id": "lead_agent",
            "metadata": {"user_id": "someone", "owner_id": "someone", "topic": "x"},
            "config": {
                "configurable": {"user_id": "someone", "model_name": "m"},
                "context": {"auth_user": "someone", "topic": "x"},
            },
            "context": {"user_id": "someone", "auth_user_id": "someone", "topic": "x"},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["caller_user_id"] is None
    assert payload["caller_kind"] == "http"
    assert payload["metadata"] == {"topic": "x"}
    assert payload["config"]["configurable"] == {"model_name": "m"}
    assert payload["config"]["context"] == {"topic": "x"}
    assert payload["context"] == {"topic": "x"}
    assert "someone" not in response.text


def test_the_owner_and_internal_headers_alone_are_not_an_identity() -> None:
    client = TestClient(_bare_app())

    response = client.post(
        "/probe/caller",
        headers={
            "X-DeerFlow-Owner-User-Id": "someone",
            "X-DeerFlow-Internal-Token": "not-a-real-token",
        },
        json={"assistant_id": "lead_agent", "context": {"topic": "x"}},
    )

    assert response.status_code == 200
    assert response.json()["caller_user_id"] is None
