"""import 顺序不该决定成败（搬 store 到 persistence/ 时踩到过）。

真实故障：`persistence/run/base.py` 需要 `runtime.runs.schemas` 的两个枚举，
而 `runtime/runs/__init__.py` 一上来就 import `manager`（它又 import `persistence.run`）。
于是"先 import 谁"决定成败：

    import agentkit.persistence.run.base
      → runtime.runs.schemas → runtime.runs/__init__ → manager
      → persistence.run（只初始化了一半）→ ImportError: RunStore

修法是让 `runtime/runs/__init__.py` 懒加载。这类 bug 在日常跑测试时**看不出来**
（app 永远从 runtime 那一侧先进），所以用一个子进程逐模块钉住。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 每个模块都单独起一个进程、**第一个**被 import——顺序问题只有这样才暴露。
MODULES = [
    "agentkit.persistence.events.base",
    "agentkit.persistence.events.codec",
    "agentkit.persistence.events.db",
    "agentkit.persistence.events.jsonl",
    "agentkit.persistence.events.memory",
    "agentkit.persistence.events.model",
    "agentkit.persistence.schema",
    "agentkit.persistence.run.base",
    "agentkit.persistence.run.memory",
    "agentkit.persistence.run.model",
    "agentkit.persistence.run.sql",
    "agentkit.agents.middlewares.sandbox_middleware",
    "agentkit.runtime.events.journal",
    "agentkit.runtime.runs.manager",
    "agentkit.runtime.runs.schemas",
    "agentkit.runtime.run_services",
    "backend.app.gateway.app",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_first_in_a_fresh_process(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"{module} 不能作为第一个 import：\n{result.stderr}"


def test_runtime_runs_facade_still_works() -> None:
    """懒加载（PEP 562）不能把门面弄丢：老写法 `from agentkit.runtime.runs import X` 照常。"""
    from agentkit.runtime.runs import (  # noqa: F401
        AgentFactory,
        CancelOutcome,
        ConflictError,
        DisconnectMode,
        RunManager,
        RunRecord,
        RunStatus,
        ThreadOperationKind,
        run_after_metadata,
    )

    assert RunStatus.pending is not None
