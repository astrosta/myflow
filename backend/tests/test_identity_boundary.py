"""M9 §2.1 / §2.2：身份优先级链，以及"图构建 / 中间件 / 文件路径落进同一个桶"。

这条线最典型的 bug 是**图构建和中间件落进不同用户桶**——只在多用户环境下出现，
单用户测试完全看不出来。所以这里按顺序断言：

1. `resolve_runtime_user_id` 的四级优先级：注入的 context → `server_identity` → ContextVar → default；
2. 注入值一律 `make_safe_user_id` 归一；
3. `storage_user_id` 用 `users.id`（不可变），没有行才退化到 identity 并打一次 warning；
4. 交错两个用户时，图构建侧、工具侧、`Paths.thread_dir` 三者对同一用户一致、对两个用户不同。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agentkit.agents.leader_agent import agent as leader_agent
from agentkit.config.paths import Paths
from agentkit.runtime.user_context import (
    DEFAULT_USER_ID,
    get_effective_user_id,
    reset_current_user,
    resolve_runtime_user_id,
    set_current_user,
    storage_user_id,
)
from agentkit.sandbox.runtime import runtime_user_bucket
from agentkit.utils.ids import make_safe_user_id, validate_user_id

_EMAIL = "Alice@Example.com"


@pytest.fixture(autouse=True)
def _reset_user_context():
    token = set_current_user(None)
    yield
    reset_current_user(token)


def _graph_side_user_id() -> str | None:
    """图构建那一侧：`config["context"]` → ContextVar（assemble_agent 走的就是它）。"""
    return leader_agent._resolve_user_id({"context": {}})


def _tool_side_user_id() -> str:
    """工具 / 中间件那一侧：`runtime.context` → ContextVar → default。"""
    runtime = SimpleNamespace(context={"thread_id": "t1"}, config={}, state={})
    return runtime_user_bucket(runtime)


def _three_views(paths: Paths) -> tuple[str | None, str, str]:
    resolved = _graph_side_user_id()
    # {base}/users/{user}/threads/{thread_id}：桶名是 thread_dir 的爷爷目录
    bucket = paths.thread_dir("t1", user_id=resolved).parent.parent.name
    return resolved, _tool_side_user_id(), bucket


def test_runtime_context_identity_beats_the_context_var() -> None:
    token = set_current_user("ctx-user")
    try:
        assert resolve_runtime_user_id({"user_id": _EMAIL}) == make_safe_user_id(_EMAIL)
        assert resolve_runtime_user_id({"user_id": "alice"}) == "alice"
    finally:
        reset_current_user(token)


def test_server_identity_beats_the_context_var() -> None:
    token = set_current_user("ctx-user")
    try:
        assert resolve_runtime_user_id({}, server_identity="bob@example.com") == make_safe_user_id("bob@example.com")
        assert resolve_runtime_user_id(None, server_identity="bob") == "bob"
    finally:
        reset_current_user(token)


def test_runtime_context_identity_beats_server_identity() -> None:
    assert resolve_runtime_user_id({"user_id": "alice"}, server_identity="bob") == "alice"


def test_the_context_var_is_the_third_choice() -> None:
    token = set_current_user(_EMAIL)
    try:
        assert resolve_runtime_user_id({}) == make_safe_user_id(_EMAIL)
        assert resolve_runtime_user_id({"thread_id": "t1"}) == make_safe_user_id(_EMAIL)
    finally:
        reset_current_user(token)


def test_without_any_source_the_chain_returns_none() -> None:
    assert resolve_runtime_user_id() is None
    assert resolve_runtime_user_id({}) is None
    assert resolve_runtime_user_id({"thread_id": "t1"}) is None
    assert resolve_runtime_user_id({}, server_identity="") is None
    assert resolve_runtime_user_id({"user_id": ""}) is None


def test_allow_default_is_the_last_step() -> None:
    assert resolve_runtime_user_id(allow_default=True) == DEFAULT_USER_ID
    assert resolve_runtime_user_id({}, allow_default=True) == DEFAULT_USER_ID

    token = set_current_user("ctx-user")
    try:
        assert resolve_runtime_user_id({}, allow_default=True) == "ctx-user"
    finally:
        reset_current_user(token)


def test_injected_identity_is_normalized_to_a_storage_id() -> None:
    safe = make_safe_user_id(_EMAIL)
    assert safe != _EMAIL
    assert validate_user_id(safe) == safe
    assert resolve_runtime_user_id({"user_id": _EMAIL}) == safe
    assert resolve_runtime_user_id({}, server_identity=_EMAIL) == safe

    token = set_current_user(_EMAIL)
    try:
        assert resolve_runtime_user_id({}) == safe
    finally:
        reset_current_user(token)


def test_get_effective_user_id_never_returns_none() -> None:
    assert get_effective_user_id() == DEFAULT_USER_ID
    assert get_effective_user_id(None) == DEFAULT_USER_ID
    assert get_effective_user_id({}) == DEFAULT_USER_ID
    assert get_effective_user_id({"user_id": _EMAIL}) == make_safe_user_id(_EMAIL)


def test_storage_user_id_comes_from_the_immutable_users_row() -> None:
    user = SimpleNamespace(id="u-1")
    assert storage_user_id(user, raw_identity=_EMAIL) == "u-1"
    assert storage_user_id(user, raw_identity=None) == "u-1"


def test_changing_the_email_does_not_change_the_bucket() -> None:
    before = storage_user_id(SimpleNamespace(id="u-1"), raw_identity="alice@example.com")
    after = storage_user_id(SimpleNamespace(id="u-1"), raw_identity="alice@new-domain.example")

    assert before == after == "u-1"
    assert before != make_safe_user_id("alice@example.com")


def test_a_users_row_logs_no_fallback_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="agentkit.runtime.user_context"):
        assert storage_user_id(SimpleNamespace(id="u-1"), raw_identity=_EMAIL) == "u-1"
    assert caplog.records == []


def test_without_a_users_row_the_identity_is_used_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="agentkit.runtime.user_context"):
        bucket = storage_user_id(None, raw_identity=_EMAIL)

    assert bucket == make_safe_user_id(_EMAIL)
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "identity-derived storage bucket" in warnings[0].getMessage()
    assert _EMAIL in warnings[0].getMessage()


def test_without_a_users_row_or_an_identity_it_is_the_default_bucket(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="agentkit.runtime.user_context"):
        assert storage_user_id(None, raw_identity=None) == DEFAULT_USER_ID
        assert storage_user_id(None, raw_identity="") == DEFAULT_USER_ID
    assert caplog.records == []


def test_graph_build_middleware_and_paths_share_one_bucket(tmp_path) -> None:
    paths = Paths(tmp_path)

    outer = set_current_user(_EMAIL)
    try:
        alice = _three_views(paths)
        inner = set_current_user("bob")
        try:
            bob = _three_views(paths)
        finally:
            reset_current_user(inner)
        assert _three_views(paths) == alice
    finally:
        reset_current_user(outer)

    assert alice == (make_safe_user_id(_EMAIL),) * 3
    assert bob == ("bob", "bob", "bob")
    assert set(alice).isdisjoint(bob)


def test_injected_runtime_context_wins_on_both_sides(tmp_path) -> None:
    paths = Paths(tmp_path)
    token = set_current_user("ctx-user")
    try:
        graph = leader_agent._resolve_user_id({"context": {"user_id": _EMAIL, "thread_id": "t1"}})
        tool = runtime_user_bucket(
            SimpleNamespace(context={"user_id": _EMAIL, "thread_id": "t1"}, config={}, state={})
        )
        directory = paths.thread_dir("t1", user_id=graph).parent.parent.name
    finally:
        reset_current_user(token)

    safe = make_safe_user_id(_EMAIL)
    assert graph == tool == directory == safe
    assert safe != "ctx-user"


def test_without_any_identity_each_side_uses_its_documented_default() -> None:
    """图构建侧返回 None（M7 skills 的 `__default__`），工具侧落 `default` 桶。"""
    assert _graph_side_user_id() is None
    assert _tool_side_user_id() == DEFAULT_USER_ID
