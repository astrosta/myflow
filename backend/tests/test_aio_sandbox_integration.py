"""Opt-in live round trip against a real Docker daemon and AIO image.

Creates and removes real containers, so it never runs by default. Enable it on a
host that can run `docker run` and reach Redis:

    AIO_INTEGRATION=1 \
    REDIS_URL=redis://127.0.0.1:6379/0 \
    uv run pytest backend/tests/test_aio_sandbox_integration.py -v

`AIO_IMAGE` overrides the pinned default image. Never point this at a shared
Docker host: it starts, stops and removes containers labelled with myflow.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from agentkit.config import AppConfig
from agentkit.config.paths import Paths
from agentkit.config.sandbox_config import DEFAULT_AIO_IMAGE
from agentkit.sandbox.aio_sandbox.docker import DockerClient
from agentkit.sandbox.aio_sandbox.provider import AioSandboxProviderDocker

_PROJECT_ROOT = Path(
    os.getenv("MYFLOW_PROJECT_ROOT") or Path(__file__).resolve().parents[2]
).resolve()
_USER_ID = "aio-itest"
_REQUIRED_ENV = ("REDIS_URL",)


def _skip_reason() -> str | None:
    if os.getenv("AIO_INTEGRATION") != "1":
        return (
            "set AIO_INTEGRATION=1 and REDIS_URL on a host with a working Docker "
            "daemon to run the live AIO sandbox round trip"
        )
    missing = [name for name in _REQUIRED_ENV if not os.getenv(name, "").strip()]
    if missing:
        return f"AIO_INTEGRATION=1 but {', '.join(missing)} is not set"
    return None


_SKIP_REASON = _skip_reason()
pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


def _image() -> str:
    return os.getenv("AIO_IMAGE", DEFAULT_AIO_IMAGE)


def _live_config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "models": [{"name": "aio-itest", "use": "langchain_deepseek:ChatDeepSeek"}],
            "sandbox": {
                "use": "agentkit.sandbox.aio_sandbox:AioSandboxProviderDocker",
                "aio": {
                    "image": _image(),
                    "release_action": "stop",
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


def test_aio_provider_round_trip() -> None:
    config = _live_config()
    paths = Paths(_PROJECT_ROOT / ".myflow")
    docker = DockerClient()
    provider = AioSandboxProviderDocker(
        app_config=config,
        docker_client=docker,
        paths=paths,
        instance_id=f"aio-itest-{uuid.uuid4().hex[:8]}",
    )
    thread_id = f"itest-{uuid.uuid4().hex[:12]}"
    thread_dir = paths.thread_dir(thread_id, user_id=_USER_ID)
    workspace = paths.sandbox_work_dir(thread_id, user_id=_USER_ID)
    container_name = provider._container_name(_USER_ID, thread_id)

    try:
        provider.startup_check()
        sandbox_id = provider.acquire(thread_id, user_id=_USER_ID)
        assert sandbox_id.startswith("aio:")
        # The acquire canary is removed on success; leave nothing in the workspace.
        assert not (workspace / ".myflow-canary").exists()

        sandbox = provider.get(sandbox_id)
        assert sandbox is not None

        output = sandbox.execute_command(
            "echo aio-echo-ok; python3 -c 'print(\"aio-python-ok\")'"
        )
        assert "aio-echo-ok" in output
        assert "aio-python-ok" in output

        # /mnt/user-data/workspace and the host workspace are one directory.
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

        globbed, glob_truncated = sandbox.glob("/mnt/user-data/workspace", "*.txt")
        assert any(path.endswith("integration.txt") for path in globbed)
        assert glob_truncated is False

        listed = sandbox.list_dir("/mnt/user-data/workspace")
        assert any("integration.txt" in line for line in listed)
        matches, _ = sandbox.grep("/mnt/user-data/workspace", "written-by-host")
        assert matches

        # release_action=stop: the container is stopped, and the next acquire
        # restarts the very same container instead of creating a new one.
        container_port = config.sandbox.aio.port
        container_id = docker.inspect(container_name, container_port=container_port).container_id
        provider.release(sandbox_id)
        assert provider.get(sandbox_id) is None
        assert docker.inspect(container_name, container_port=container_port).running is False

        resumed_id = provider.acquire(thread_id, user_id=_USER_ID)
        assert resumed_id == sandbox_id
        resumed = provider.get(resumed_id)
        assert resumed is not None
        assert "aio-resume-ok" in resumed.execute_command("echo aio-resume-ok")
        assert (
            docker.inspect(container_name, container_port=container_port).container_id
            == container_id
        )
    finally:
        provider.shutdown()
        try:
            docker.remove(container_name)
        except Exception:
            pass
        shutil.rmtree(thread_dir, ignore_errors=True)
