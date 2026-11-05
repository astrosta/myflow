"""Opt-in live round trip against a real OpenSandbox server.

Creates and destroys real sandboxes, so it never runs by default. Enable it where
an opensandbox-server is running and reachable:

    OPENSANDBOX_INTEGRATION=1 \
    OPENSANDBOX_DOMAIN=127.0.0.1:8080 \
    REDIS_URL=redis://127.0.0.1:6379/0 \
    uv run pytest backend/tests/test_opensandbox_integration.py -v

OPENSANDBOX_IMAGE overrides the default image. Never point this at a shared
server: it creates, connects and kills sandboxes labelled with myflow metadata.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from agentkit.config import AppConfig
from agentkit.config.paths import Paths
from agentkit.config.sandbox_config import DEFAULT_OPENSANDBOX_IMAGE
from agentkit.sandbox.opensandbox.provider import OpenSandboxProvider

_PROJECT_ROOT = Path(
    os.getenv("MYFLOW_PROJECT_ROOT") or Path(__file__).resolve().parents[2]
).resolve()
_USER_ID = "osb-itest"
_REQUIRED_ENV = ("REDIS_URL",)


def _skip_reason() -> str | None:
    if os.getenv("OPENSANDBOX_INTEGRATION") != "1":
        return (
            "set OPENSANDBOX_INTEGRATION=1 and REDIS_URL with a running "
            "opensandbox-server to run the live round trip"
        )
    missing = [name for name in _REQUIRED_ENV if not os.getenv(name, "").strip()]
    if missing:
        return f"OPENSANDBOX_INTEGRATION=1 but {', '.join(missing)} is not set"
    return None


_SKIP_REASON = _skip_reason()
pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


def _live_config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "models": [{"name": "osb-itest", "use": "langchain_deepseek:ChatDeepSeek"}],
            "sandbox": {
                "use": "agentkit.sandbox.opensandbox:OpenSandboxProvider",
                "opensandbox": {
                    "domain": os.getenv("OPENSANDBOX_DOMAIN", "127.0.0.1:8080"),
                    "image": os.getenv("OPENSANDBOX_IMAGE", DEFAULT_OPENSANDBOX_IMAGE),
                    "release_action": "destroy",
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


def test_opensandbox_provider_round_trip() -> None:
    config = _live_config()
    paths = Paths(_PROJECT_ROOT / ".myflow")
    provider = OpenSandboxProvider(
        app_config=config,
        paths=paths,
        instance_id=f"osb-itest-{uuid.uuid4().hex[:8]}",
    )
    thread_id = f"itest-{uuid.uuid4().hex[:12]}"
    thread_dir = paths.thread_dir(thread_id, user_id=_USER_ID)
    workspace = paths.sandbox_work_dir(thread_id, user_id=_USER_ID)

    try:
        provider.startup_check()
        sandbox_id = provider.acquire(thread_id, user_id=_USER_ID)
        assert sandbox_id.startswith("osb:")
        assert not (workspace / ".myflow-canary").exists()

        sandbox = provider.get(sandbox_id)
        assert sandbox is not None

        output = sandbox.execute_command(
            "echo osb-echo-ok; python3 -c 'print(\"osb-python-ok\")'"
        )
        assert "osb-echo-ok" in output
        assert "osb-python-ok" in output

        sandbox.write_file("/mnt/user-data/workspace/integration.txt", "written-by-sandbox\n")
        host_file = workspace / "integration.txt"
        assert host_file.read_text(encoding="utf-8").strip() == "written-by-sandbox"

        host_file.write_text("written-by-host\n", encoding="utf-8")
        assert (
            sandbox.read_file("/mnt/user-data/workspace/integration.txt").strip()
            == "written-by-host"
        )

        binary = bytes(range(256))
        sandbox.update_file("/mnt/user-data/workspace/integration.bin", binary)
        assert sandbox.download_file("/mnt/user-data/workspace/integration.bin") == binary

        globbed, _ = sandbox.glob("/mnt/user-data/workspace", "*.txt")
        assert any(path.endswith("integration.txt") for path in globbed)
        listed = sandbox.list_dir("/mnt/user-data/workspace")
        assert any("integration.txt" in line for line in listed)
        matches, _ = sandbox.grep("/mnt/user-data/workspace", "written-by-host")
        assert matches

        # release_action=destroy kills the sandbox, and the next acquire builds a
        # fresh one instead of reusing a dead id.
        provider.release(sandbox_id)
        assert provider.get(sandbox_id) is None

        resumed_id = provider.acquire(thread_id, user_id=_USER_ID)
        assert resumed_id == sandbox_id
        resumed = provider.get(resumed_id)
        assert resumed is not None
        assert "osb-resume-ok" in resumed.execute_command("echo osb-resume-ok")
        # The host directory is the source of truth: the file written by the
        # previous sandbox is still there.
        assert (
            resumed.read_file("/mnt/user-data/workspace/integration.txt").strip()
            == "written-by-host"
        )
    finally:
        provider.shutdown()
        shutil.rmtree(thread_dir, ignore_errors=True)
