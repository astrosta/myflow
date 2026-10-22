"""M8 §4.2：``subagent_runtime`` 是**进程级**容量，四条字段全部 startup-only。

准入控制器在 Gateway 启动时构造（§10.1），热重载改 YAML 只影响下一个进程。
提示词与 Limit 中间件读的必须是控制器**冻结**的那个数：热重载不能把容量宣告得
比已创建的控制器更大，否则模型会一直撞墙。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentkit.config import AppConfig, SubagentRuntimeConfig
from agentkit.config.reload_boundary import STARTUP_ONLY_FIELDS, STARTUP_ONLY_PREFIX

PATHS = (
    "subagent_runtime.max_running",
    "subagent_runtime.max_queued",
    "subagent_runtime.admission_policy",
    "subagent_runtime.queue_timeout_seconds",
)


def _app(**runtime) -> AppConfig:
    return AppConfig.model_validate(
        {"models": [{"name": "deepseek-reasoner", "use": "langchain_deepseek:ChatDeepSeek"}], "subagent_runtime": runtime}
    )


def test_every_runtime_field_is_registered_as_startup_only() -> None:
    assert set(PATHS) <= set(STARTUP_ONLY_FIELDS)


@pytest.mark.parametrize("path", PATHS)
def test_field_description_carries_the_prefix_and_the_registered_reason(path: str) -> None:
    field = SubagentRuntimeConfig.model_fields[path.split(".")[-1]]
    description = field.description or ""
    assert description.startswith(f"{STARTUP_ONLY_PREFIX} {STARTUP_ONLY_FIELDS[path]}")


def test_defaults_are_the_documented_capacity() -> None:
    cfg = SubagentRuntimeConfig()
    assert cfg.max_running == 3
    assert cfg.max_queued == 64
    assert cfg.admission_policy == "queue"
    assert cfg.queue_timeout_seconds == 300


@pytest.mark.parametrize("value", [1, 3, 64])
def test_max_running_accepts_the_documented_range(value: int) -> None:
    assert SubagentRuntimeConfig(max_running=value).max_running == value


@pytest.mark.parametrize("value", [0, -1, 65])
def test_max_running_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValidationError):
        SubagentRuntimeConfig(max_running=value)


def test_admission_policy_is_queue_or_reject() -> None:
    assert SubagentRuntimeConfig(admission_policy="reject").admission_policy == "reject"
    with pytest.raises(ValidationError):
        SubagentRuntimeConfig(admission_policy="wait")


def test_queue_settings_reject_negative_values() -> None:
    assert SubagentRuntimeConfig(max_queued=0).max_queued == 0  # 0 = 满槽即拒绝
    with pytest.raises(ValidationError):
        SubagentRuntimeConfig(max_queued=-1)
    with pytest.raises(ValidationError):
        SubagentRuntimeConfig(queue_timeout_seconds=-1)


def test_section_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _app(max_runs=3)


def test_null_section_reads_as_missing() -> None:
    cfg = AppConfig.model_validate(
        {"models": [{"name": "m", "use": "x:Y"}], "subagent_runtime": None}
    )
    assert cfg.subagent_runtime == SubagentRuntimeConfig()


def test_hot_reload_cannot_raise_the_frozen_capacity() -> None:
    """控制器持有 freeze() 的深拷贝（§10.1）：之后 AppConfig 怎么改都不影响它。"""
    live = _app(max_running=3)
    frozen = live.subagent_runtime.freeze()
    live.subagent_runtime.max_running = 8
    assert frozen.max_running == 3
    assert frozen.effective_max_concurrent() == 3
    assert frozen.effective_max_concurrent(8) == 3
    assert live.subagent_runtime.effective_max_concurrent(8) == 8


def test_runtime_defaults_agree_with_the_section_defaults() -> None:
    """没写 subagent_runtime 时，解析出来的容量与显式写默认值完全一致。"""
    assert AppConfig.model_validate({"models": [{"name": "m", "use": "x:Y"}]}).subagent_runtime == SubagentRuntimeConfig()
