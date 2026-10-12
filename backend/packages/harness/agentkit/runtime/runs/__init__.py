"""运行时那一层的门面：manager / worker / schemas 的常用名字。

**懒加载（PEP 562）**，理由和 `persistence/events/__init__.py` 一模一样：
`persistence/run/base.py` 需要 `runtime.runs.schemas` 里的两个枚举，而
`manager` 又需要 `persistence.run` 的 `RunStore`。如果这个 `__init__` 一上来就
import `manager`，那"先 import 谁"就决定成败：

    persistence.run.base → runtime.runs.schemas → runtime.runs/__init__
        → manager → persistence.run（只初始化了一半）→ ImportError: RunStore

懒加载之后，import 一个子模块只执行那个子模块，顺序不再有意义。
`from agentkit.runtime.runs import RunRecord` 这类写法照常可用。

```
RunRecord / RunManager / ConflictError   → manager.py
RunStatus / ThreadOperationKind / …      → schemas.py
AgentFactory / run_after_metadata        → worker.py
```
"""

from __future__ import annotations

from importlib import import_module

_LAZY_EXPORTS: dict[str, str] = {
    # manager
    "ConflictError": "agentkit.runtime.runs.manager",
    "RunManager": "agentkit.runtime.runs.manager",
    "RunRecord": "agentkit.runtime.runs.manager",
    "UnsupportedStrategyError": "agentkit.runtime.runs.manager",
    # schemas
    "CancelOutcome": "agentkit.runtime.runs.schemas",
    "DisconnectMode": "agentkit.runtime.runs.schemas",
    "InvalidRunTransition": "agentkit.runtime.runs.schemas",
    "RunStartOutcome": "agentkit.runtime.runs.schemas",
    "RunStartupError": "agentkit.runtime.runs.schemas",
    "RunStatus": "agentkit.runtime.runs.schemas",
    "ThreadOperationKind": "agentkit.runtime.runs.schemas",
    # worker
    "AgentFactory": "agentkit.runtime.runs.worker",
    "run_after_metadata": "agentkit.runtime.runs.worker",
}

__all__ = sorted(_LAZY_EXPORTS)


def __getattr__(name: str):
    """PEP 562：第一次访问才 import，之后缓存成普通属性。"""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return __all__
