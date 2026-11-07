"""allowed-tools 策略：激活的技能 → 本次模型调用能看见哪些工具（M7 §3.7–§3.9）。

**这是行为约束，不是安全边界。** 模型可以用 bash cat 读任意 SKILL.md（那条路径不会被
捕获），自主 skill_context 也有容量上限。真正的边界是沙箱与鉴权。

三条规则：

1. **只对"真正激活"的技能生效**：被动启用、agent 白名单都不收紧基线工具集。
2. **slash 支配**：用户显式 /some-skill 之后，模型再读别的 SKILL.md 不能扩大权限；
   没有 slash 时，自主捕获的技能之间取并集。并集里**未声明者贡献空集**，不是"放行全部"。
3. **框架工具永远可用**（§3.8），发现不等于可用：tool_search 提升出来的业务工具
   仍然要当前激活策略的显式声明。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence, Set as AbstractSet
from dataclasses import dataclass
from typing import Any

from agentkit.skills.types import Skill

#: 最严格的技能策略下也始终可用的**发现基础设施**（§3.8）。
#: task **不在**这里：否则受限技能可以通过委派绕过自己的策略。
#: load_skills 和 read_file 同级：两者都只是"把某个 SKILL.md 读进上下文"。
#: search_skills 是 skill-finder 的元数据工具，不属于 always-available。
ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES = frozenset(
    {"read_file", "tool_search", "load_skills", "search_tools"}
)

DECISION_VERSION = 1

MODE_PASSTHROUGH = "passthrough"
MODE_SLASH = "slash"
MODE_UNION = "union"
MODE_FAIL_CLOSED = "fail_closed"


def compute_allowed_tools(
    *, baseline: AbstractSet[str], activated: Sequence[Skill], slash_skill: Skill | None
) -> frozenset[str]:
    """本次模型调用可见的**业务**工具集合；框架工具由调用方并上（§3.8）。"""
    if slash_skill is not None and slash_skill.allowed_tools is not None:
        return frozenset(slash_skill.allowed_tools)
    declared = [skill.allowed_tools for skill in activated if skill.allowed_tools is not None]
    if not declared:
        return frozenset(baseline)
    return frozenset().union(*declared)


@dataclass(frozen=True, slots=True)
class ToolPolicyDecision:
    """版本化、JSON-safe、带路径签名的决策。同一轮的 tool_call 复用它（§3.9 第 3 步）。"""

    version: int
    mode: str
    unlimited: bool
    allowed: frozenset[str]
    activated_paths: tuple[str, ...]
    signature: str

    def is_allowed(self, name: str) -> bool:
        return self.unlimited or name in self.allowed

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "mode": self.mode,
            "unlimited": self.unlimited,
            "allowed": sorted(self.allowed),
            "activated_paths": list(self.activated_paths),
            "signature": self.signature,
        }


def build_decision(
    *,
    baseline: AbstractSet[str],
    activated: Sequence[Skill],
    slash_skill: Skill | None,
) -> ToolPolicyDecision:
    """现场解析出来的决策。unlimited=True 表示"没有任何技能声明 allowed-tools"。"""
    paths = tuple(sorted(skill.container_file for skill in activated))
    if slash_skill is not None and slash_skill.allowed_tools is not None:
        mode = MODE_SLASH
        business = frozenset(slash_skill.allowed_tools)
    else:
        declared = [skill.allowed_tools for skill in activated if skill.allowed_tools is not None]
        if declared:
            mode = MODE_UNION
            business = frozenset().union(*declared)
        else:
            mode = MODE_PASSTHROUGH
            business = frozenset(baseline)
    unlimited = mode == MODE_PASSTHROUGH
    allowed = business | ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES
    return ToolPolicyDecision(
        version=DECISION_VERSION,
        mode=mode,
        unlimited=unlimited,
        allowed=frozenset(allowed),
        activated_paths=paths,
        signature=decision_signature(mode=mode, allowed=allowed, activated_paths=paths),
    )


def framework_only_decision(*, reason: str) -> ToolPolicyDecision:
    """fail-closed：注册表加载失败，或激活集非空但一个都解析不出来（§3.9 的失败语义）。"""
    paths: tuple[str, ...] = ()
    allowed = frozenset(ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES)
    return ToolPolicyDecision(
        version=DECISION_VERSION,
        mode=MODE_FAIL_CLOSED,
        unlimited=False,
        allowed=allowed,
        activated_paths=paths,
        signature=decision_signature(mode=f"{MODE_FAIL_CLOSED}:{reason}", allowed=allowed, activated_paths=paths),
    )


def decision_signature(
    *, mode: str, allowed: AbstractSet[str], activated_paths: Sequence[str]
) -> str:
    """路径 + 结果的指纹：对不上的决策（畸形 / 过期 / 外来）一律作废，回退现场解析。"""
    material = "\n".join([mode, *sorted(allowed), *activated_paths])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def parse_decision(payload: Any) -> ToolPolicyDecision | None:
    """外来 / 畸形 payload → None。"""
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != DECISION_VERSION:
        return None
    mode = payload.get("mode")
    unlimited = payload.get("unlimited")
    allowed = payload.get("allowed")
    paths = payload.get("activated_paths") or []
    signature = payload.get("signature")
    if not isinstance(mode, str) or not isinstance(unlimited, bool):
        return None
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        return None
    if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
        return None
    if not isinstance(signature, str):
        return None
    expected = decision_signature(mode=mode, allowed=set(allowed), activated_paths=paths)
    if signature != expected:
        return None
    return ToolPolicyDecision(
        version=DECISION_VERSION,
        mode=mode,
        unlimited=unlimited,
        allowed=frozenset(allowed),
        activated_paths=tuple(paths),
        signature=signature,
    )
