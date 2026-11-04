"""Opt-in live round trip against a real CubeSandbox cluster.

This module never runs on the Windows development machine: it creates real
MicroVMs and real host mounts, so it is enabled only on the Linux host that runs
both Gateway and Cubelet. Set `CUBE_INTEGRATION=1` plus the endpoints below and
run it manually:

    cd backend
    CUBE_INTEGRATION=1 \
    CUBE_API_URL=http://<cube-api> \
    CUBE_TEMPLATE_ID=<template-id> \
    CUBE_PROXY_NODE_IP=<node-ip> \
    REDIS_URL=redis://127.0.0.1:6379/0 \
    uv run pytest tests/test_cube_integration.py -v

`CUBE_API_KEY` is optional. Never point this test at a shared production Cube:
it starts and pauses sandboxes.
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

from agentkit.config import AppConfig
from agentkit.config.paths import Paths
from agentkit.sandbox.cube.client import CubesandboxFactory
from agentkit.sandbox.cube.provider import CubeSandboxProvider

# The real project root, exactly what the Gateway uses: MYFLOW_PROJECT_ROOT when
# it is set, otherwise the repo root that contains this file. tmp_path cannot be
# used here: a containerized Cubelet only sees the root configured in
# `host_mount`, not pytest's private temp directory.
_PROJECT_ROOT = Path(
    os.getenv("MYFLOW_PROJECT_ROOT") or Path(__file__).resolve().parents[2]
).resolve()
_USER_ID = "cube-itest"
_REQUIRED_ENV = (
    "CUBE_API_URL",
    "CUBE_TEMPLATE_ID",
    "CUBE_PROXY_NODE_IP",
    "REDIS_URL",
)


def _skip_reason() -> str | None:
    if os.getenv("CUBE_INTEGRATION") != "1":
        return (
            "set CUBE_INTEGRATION=1 and provide CUBE_API_URL, CUBE_TEMPLATE_ID, "
            "CUBE_PROXY_NODE_IP, and REDIS_URL on the Linux Cube host"
        )
    if sys.platform != "linux":
        return "the live CubeSandbox round trip requires the Linux Cube host"
    missing = [name for name in _REQUIRED_ENV if not os.getenv(name, "").strip()]
    if missing:
        return f"CUBE_INTEGRATION=1 but {', '.join(missing)} is not set"
    return None


_SKIP_REASON = _skip_reason()
pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


def _live_config() -> AppConfig:
    """Build the same config shape `config.example.yaml` documents.

    Both host-mount roots are the real project root, so the Cubelet must see that
    exact path; this is the same-host deployment contract rather than pytest's
    private temp directory.
    """

    return AppConfig.model_validate(
        {
            "models": [{"name": "cube-itest", "use": "langchain_deepseek:ChatDeepSeek"}],
            "sandbox": {
                "use": "agentkit.sandbox.cube:CubeSandboxProvider",
                "cube": {
                    "api_url": os.environ["CUBE_API_URL"],
                    "api_key": os.getenv("CUBE_API_KEY", ""),
                    "template_id": os.environ["CUBE_TEMPLATE_ID"],
                    "proxy_node_ip": os.environ["CUBE_PROXY_NODE_IP"],
                    "release_action": "pause",
                    "host_mount": {
                        "local_root": str(_PROJECT_ROOT),
                        "host_root": str(_PROJECT_ROOT),
                    },
                    "lease": {"redis_url": os.environ["REDIS_URL"]},
                },
            },
            "skills": {
                "source": "filesystem",
                "path": str(_PROJECT_ROOT / "skills"),
                "builtin": {
                    "enabled": True,
                    "path": str(_PROJECT_ROOT / "skills" / "builtin"),
                },
            },
        }
    )


def _sdk_factory() -> CubesandboxFactory:
    return CubesandboxFactory(
        api_url=os.environ["CUBE_API_URL"],
        api_key=os.getenv("CUBE_API_KEY", ""),
        template_id=os.environ["CUBE_TEMPLATE_ID"],
        sandbox_domain="cube.app",
        proxy_node_ip=os.environ["CUBE_PROXY_NODE_IP"],
        proxy_port=80,
        request_timeout=30,
    )


def _row_remote_id(row: dict) -> str | None:
    for key in ("sandboxID", "sandboxId", "sandbox_id", "id"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _kill_leftovers(factory: CubesandboxFactory, thread_id: str) -> None:
    """Kill any sandbox this test leaves behind, including a paused one."""

    try:
        rows = factory.list_by_metadata(
            {"myflow": "1", "myflow.user": _USER_ID, "myflow.thread": thread_id}
        )
    except Exception:
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        remote_id = _row_remote_id(row)
        if remote_id is None:
            continue
        sdk = None
        try:
            sdk = factory.connect(remote_id)
            sdk.kill()
        except Exception:
            pass
        finally:
            if sdk is not None:
                try:
                    sdk.close()
                except Exception:
                    pass


def test_cube_provider_round_trip() -> None:
    config = _live_config()
    factory = _sdk_factory()
    paths = Paths(_PROJECT_ROOT / ".myflow")
    provider = CubeSandboxProvider(
        app_config=config,
        sdk_factory=factory,
        paths=paths,
        instance_id=f"cube-itest-{uuid.uuid4().hex[:8]}",
    )
    thread_id = f"itest-{uuid.uuid4().hex[:12]}"
    thread_dir = paths.thread_dir(thread_id, user_id=_USER_ID)
    workspace = paths.sandbox_work_dir(thread_id, user_id=_USER_ID)

    try:
        provider.startup_check()
        sandbox_id = provider.acquire(thread_id, user_id=_USER_ID)
        assert sandbox_id.startswith("cube:")
        # The acquire canary is removed on success; leave nothing in the workspace.
        assert not (workspace / ".myflow-canary").exists()
        sandbox = provider.get(sandbox_id)
        assert sandbox is not None

        # Commands run inside the MicroVM; both template tools must exist.
        output = sandbox.execute_command(
            "echo cube-echo-ok; python3 -c 'print(\"cube-python-ok\")'"
        )
        assert "cube-echo-ok" in output
        assert "cube-python-ok" in output

        # /mnt/user-data/workspace and the host workspace are one directory.
        sandbox_to_host = "written-by-sandbox\n"
        sandbox.write_file("/mnt/user-data/workspace/integration.txt", sandbox_to_host)
        host_file = workspace / "integration.txt"
        assert host_file.read_text(encoding="utf-8").strip() == sandbox_to_host.strip()

        host_to_sandbox = "written-by-host\n"
        host_file.write_text(host_to_sandbox, encoding="utf-8")
        assert (
            sandbox.read_file("/mnt/user-data/workspace/integration.txt").strip()
            == host_to_sandbox.strip()
        )

        # Explicit host -> sandbox -> host canary through the mount.
        canary = workspace / ".myflow-canary"
        canary.mkdir(parents=True, exist_ok=True)
        (canary / "in").write_text("host-canary", encoding="utf-8")
        assert sandbox.read_file("/mnt/user-data/workspace/.myflow-canary/in").strip() == (
            "host-canary"
        )
        sandbox.write_file("/mnt/user-data/workspace/.myflow-canary/out", "sandbox-canary")
        assert (canary / "out").read_text(encoding="utf-8").strip() == "sandbox-canary"

        # release_action=pause drops the handle; a later acquire discovers the
        # paused sandbox and resumes the same MicroVM.
        provider.release(sandbox_id)
        assert provider.get(sandbox_id) is None
        resumed_id = provider.acquire(thread_id, user_id=_USER_ID)
        assert resumed_id == sandbox_id
        resumed = provider.get(resumed_id)
        assert resumed is not None
        assert "cube-resume-ok" in resumed.execute_command("echo cube-resume-ok")
    finally:
        provider.shutdown()
        _kill_leftovers(factory, thread_id)
        shutil.rmtree(thread_dir, ignore_errors=True)
