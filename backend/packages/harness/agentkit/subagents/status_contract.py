"""跨语言状态契约（M8 §9.3）。

对外只有五个 status 值。``in_progress`` 是**台账内部**的（还没终态的委派），
不进 ToolMessage、不进前端枚举。同一份契约以 JSON 形式放在
``contracts/subagent_status_contract.json``，前端 TypeScript 枚举按它钉；
``test_subagent_status_contract.py`` 负责双向钉死，一边改一边漏就红。

四条纪律：

* **运行时只读 ``additional_kwargs``，禁止解析散文**（§13.2）：``format_subagent_result_message``
  产出的那段英文是给人（和模型）看的，**不是**状态协议。看起来像错误的散文不算失败。
* **写侧永不产出 ``max_turns_reached``**：那是早期阶段已经删掉的错误设计。
  读侧见到历史值要归一化成 ``failed`` + ``turn_capped``。
* **provider 没给用量就缺省，不要写 0**（§15）：0 和"未知"是不同的信息。
* **护栏命中不改状态**（§13.1）：状态仍是 ``completed`` / ``failed``，另带一个 ``stop_reason``；
  新增状态枚举值会让所有已有消费者遇到未知值。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentkit.agents.middlewares.delegation_ledger import result_sha256, truncate_brief
from agentkit.config.runtime_paths import project_root

CONTRACT_VERSION = 2
CONTRACT_RELATIVE_PATH = Path("contracts") / "subagent_status_contract.json"

#: 对外 status 值。顺序即 JSON 里的顺序，测试按值比对，不按顺序。
SUBAGENT_STATUS_VALUES: tuple[str, ...] = (
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "polling_timed_out",
)

#: 三条护栏轴（§13）。
SUBAGENT_STOP_REASON_VALUES: tuple[str, ...] = ("token_capped", "turn_capped", "loop_capped")

#: 历史遗留值 → ``(status, stop_reason)``。**只读侧认**，写侧永不产出。
LEGACY_STATUS_VALUES: dict[str, tuple[str, str | None]] = {
    "max_turns_reached": ("failed", "turn_capped"),
}

#: ToolMessage.additional_kwargs 的键名，一次性定死（§9.3 的表）。
SUBAGENT_STATUS_KEY = "subagent_status"
SUBAGENT_STOP_REASON_KEY = "subagent_stop_reason"
SUBAGENT_ERROR_KEY = "subagent_error"
SUBAGENT_RESULT_BRIEF_KEY = "subagent_result_brief"
SUBAGENT_RESULT_SHA256_KEY = "subagent_result_sha256"
SUBAGENT_MODEL_NAME_KEY = "subagent_model_name"
SUBAGENT_TOKEN_USAGE_KEY = "subagent_token_usage"

SUBAGENT_TOOL_MESSAGE_KEYS = frozenset({
    SUBAGENT_STATUS_KEY,
    SUBAGENT_STOP_REASON_KEY,
    SUBAGENT_ERROR_KEY,
    SUBAGENT_RESULT_BRIEF_KEY,
    SUBAGENT_RESULT_SHA256_KEY,
    SUBAGENT_MODEL_NAME_KEY,
    SUBAGENT_TOKEN_USAGE_KEY,
})

#: 工具结果正文的上限（§17.1）：子代理可以写很长，但 lead 的上下文不是垃圾桶。
#: 全文照样进 ``subagent.end`` 事件与台账 brief——这里只是"回到对话里的那一段"。
SUBAGENT_RESULT_MAX_CHARS = 8192

_HEADLINES: dict[str, str] = {
    "completed": "任务成功",
    "failed": "任务失败",
    "cancelled": "任务已取消",
    "timed_out": "任务超时",
    "polling_timed_out": "任务超时（父侧已停止等待，结果可能不完整）",
}

_CAP_LABELS: dict[str, str] = {
    "token_capped": "token 预算",
    "turn_capped": "轮次预算",
    "loop_capped": "循环检测",
}


@dataclass(frozen=True, slots=True)
class SubagentUsage:
    """``subagent_token_usage`` 的值。provider 没给就整块缺省，不要写 0。"""

    input_tokens: int
    output_tokens: int
    total_tokens: int

    @classmethod
    def from_payload(cls, payload: object) -> "SubagentUsage | None":
        """读侧：畸形 / 缺字段 / 总量非正 → None（当"未知"，不估算成 0）。"""
        if not isinstance(payload, dict):
            return None
        values: list[int] = []
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            values.append(value)
        usage = cls(*values)
        return usage if usage.total_tokens > 0 else None

    def to_payload(self) -> dict[str, int] | None:
        """写侧：总量非正 → None，调用方据此**整个键都不写**。"""
        if self.total_tokens <= 0:
            return None
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


def normalize_status(value: object) -> tuple[str, str | None]:
    """读侧归一化：历史值 / 未知值 → ``(status, stop_reason)``。

    未知值按 ``failed`` 处理：宁可说"没做成"，也不要把没验证的东西显示成完成。
    """
    if isinstance(value, str):
        if value in SUBAGENT_STATUS_VALUES:
            return value, None
        legacy = LEGACY_STATUS_VALUES.get(value)
        if legacy is not None:
            return legacy
    return "failed", None


def make_subagent_additional_kwargs(
    *,
    status: str,
    error: str | None = None,
    result: str | None = None,
    stop_reason: str | None = None,
    model_name: str | None = None,
    usage: SubagentUsage | dict[str, int] | None = None,
) -> dict[str, Any]:
    """终态 ToolMessage 的元数据（§9.2）。只写有值的键——缺省和 0 不是一回事。

    ``subagent_result_brief` / ``subagent_result_sha256` **只跟 completed 走**（§9.3 的表）：
    失败时前端读的是 ``subagent_error``。
    """
    if status not in SUBAGENT_STATUS_VALUES:
        raise ValueError(
            f"unknown subagent status {status!r}; expected one of {list(SUBAGENT_STATUS_VALUES)} "
            f"(legacy values such as {sorted(LEGACY_STATUS_VALUES)} are read-side only)"
        )
    if stop_reason is not None and stop_reason not in SUBAGENT_STOP_REASON_VALUES:
        raise ValueError(
            f"unknown stop_reason {stop_reason!r}; expected one of {list(SUBAGENT_STOP_REASON_VALUES)}"
        )

    kwargs: dict[str, Any] = {SUBAGENT_STATUS_KEY: status}
    if stop_reason:
        kwargs[SUBAGENT_STOP_REASON_KEY] = stop_reason
    if error:
        kwargs[SUBAGENT_ERROR_KEY] = error
    if status == "completed" and result:
        kwargs[SUBAGENT_RESULT_BRIEF_KEY] = truncate_brief(result)
        kwargs[SUBAGENT_RESULT_SHA256_KEY] = result_sha256(result)
    if model_name:
        kwargs[SUBAGENT_MODEL_NAME_KEY] = model_name
    payload = usage.to_payload() if isinstance(usage, SubagentUsage) else usage
    if isinstance(payload, dict) and payload:
        kwargs[SUBAGENT_TOKEN_USAGE_KEY] = dict(payload)
    return kwargs


def format_subagent_result_message(
    *,
    status: str,
    result: str | None = None,
    error: str | None = None,
    stop_reason: str | None = None,
) -> str:
    """模型可见散文（§9.2）。运行时消费者**不许**解析这段文字当状态。

    例：``Task Succeeded (capped: token budget; partial result). / Result: ...``
    """
    headline = _HEADLINES.get(status, "Task Failed")
    if status == "completed" and stop_reason:
        label = _CAP_LABELS.get(stop_reason, stop_reason)
        headline += f"（触发护栏上限：{label}；结果为部分结果）"
    lines = [f"{headline}。"]
    if error:
        lines.append(f"原因：{error}")
    if result:
        lines.append(f"结果：{result}")
    return "\n".join(lines)


def subagent_status_contract() -> dict:
    """Python 侧的真值。测试拿它和 contracts/subagent_status_contract.json 对撞。"""
    return {
        "version": CONTRACT_VERSION,
        "valid_status_values": list(SUBAGENT_STATUS_VALUES),
        "valid_stop_reason_values": list(SUBAGENT_STOP_REASON_VALUES),
        "notes": [
            "in_progress 是台账内部状态（还没终态的委派），不对外、不进前端枚举",
            "polling_timed_out 与 timed_out 的差别只在谁先放弃等待：父侧轮询预算 or 子代理墙钟",
            "stop_reason 是附加字段：状态仍然是 completed / failed，消费者不认识就忽略",
            "max_turns_reached 是历史遗留值：读侧映射成 failed + turn_capped，写侧永不产出",
        ],
    }


def contract_path() -> Path:
    return project_root() / CONTRACT_RELATIVE_PATH


def load_subagent_status_contract(path: Path | None = None) -> dict:
    target = path if path is not None else contract_path()
    return json.loads(target.read_text(encoding="utf-8"))
