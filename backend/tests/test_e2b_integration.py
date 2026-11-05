"""Opt-in live round trip against a real E2B-compatible cluster.

This module never runs on the Windows development machine: it creates real
sandboxes. Set E2B_INTEGRATION=1 plus the endpoints below and run it manually:

    cd backend
    E2B_INTEGRATION=1 \
    E2B_API_URL=http://<e2b-api> \
    E2B_TEMPLATE_ID=<template-id> \
    REDIS_URL=redis://127.0.0.1:6379/0 \
    uv run pytest tests/test_e2b_integration.py -v

E2B_API_KEY is optional. E2B_DOMAIN overrides the sandbox data-plane domain
(<port>-<sandbox-id>.<domain>, default cube.app); the host must resolve those
wildcard names, because the official SDK addresses each sandbox through them.
Never point this test at shared production capacity: it starts, pauses, and
kills sandboxes.
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
from agentkit.sandbox.e2b.client import E2BFactory
from agentkit.sandbox.e2b.provider import E2BSandboxProvider

_PROJECT_ROOT = Path(
    os.getenv("MYFLOW_PROJECT_ROOT") or Path(__file__).resolve().parents[2]
).resolve()
_USER_ID = "e2b-itest"
_REQUIRED_ENV = (
    "E2B_API_URL",
    "E2B_TEMPLATE_ID",
    "REDIS_URL",
)


def _skip_reason() -> str | None:
    if os.getenv("E2B_INTEGRATION") != "1":
        return (
            "set E2B_INTEGRATION=1 and provide E2B_API_URL, E2B_TEMPLATE_ID, "
            "and REDIS_URL on an E2B-capable host"
        )
    if sys.platform != "linux":
        return "the live E2B round trip requires a Linux host"
    missing = [name for name in _REQUIRED_ENV if not os.getenv(name, "").strip()]
    if missing:
        return f"E2B_INTEGRATION=1 but {', '.join(missing)} is not set"
    return None


_SKIP_REASON = _skip_reason()
pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


def _live_config() -> AppConfig:
    """Build the same config shape config.example.yaml documents."""

    return AppConfig.model_validate(
        {
            "models": [{"name": "e2b-itest", "use": "langchain_deepseek:ChatDeepSeek"}],
            "sandbox": {
                "use": "agentkit.sandbox.e2b:E2BSandboxProvider",
                "e2b": {
                    "api_url": os.environ["E2B_API_URL"],
                    "api_key": os.getenv("E2B_API_KEY", ""),
                    "template_id": os.environ["E2B_TEMPLATE_ID"],
                    "sandbox_domain": os.getenv("E2B_DOMAIN", "cube.app"),
                    "release_action": "pause",
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


def _sdk_factory() -> E2BFactory:
    return E2BFactory(
        api_url=os.environ["E2B_API_URL"],
        api_key=os.getenv("E2B_API_KEY", ""),
        template_id=os.environ["E2B_TEMPLATE_ID"],
        sandbox_domain=os.getenv("E2B_DOMAIN", "cube.app"),
        request_timeout=30,
    )


def _row_remote_id(row: dict) -> str | None:
    for key in ("sandboxID", "sandboxId", "sandbox_id", "id"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _kill_leftovers(factory: E2BFactory, thread_id: str) -> None:
    """Kill any sandbox this test leaves behind, including a paused one."""

    try:
        rows = factory.list_by_metadata(
            {"myflow.user": _USER_ID, "myflow.thread": thread_id}
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


def test_e2b_provider_round_trip(tmp_path: Path) -> None:
    config = _live_config()
    factory = _sdk_factory()
    paths = Paths(tmp_path / ".myflow")
    provider = E2BSandboxProvider(
        app_config=config,
        sdk_factory=factory,
        paths=paths,
        instance_id=f"e2b-itest-{uuid.uuid4().hex[:8]}",
    )
    thread_id = f"itest-{uuid.uuid4().hex[:12]}"
    thread_dir = paths.thread_dir(thread_id, user_id=_USER_ID)
    uploads = paths.sandbox_uploads_dir(thread_id, user_id=_USER_ID)
    outputs = paths.sandbox_outputs_dir(thread_id, user_id=_USER_ID)
    paths.ensure_thread_dirs(thread_id, user_id=_USER_ID)
    (uploads / "brief.txt").write_text("uploaded-by-host\n", encoding="utf-8")

    try:
        provider.startup_check()
        sandbox_id = provider.acquire(thread_id, user_id=_USER_ID)
        assert sandbox_id.startswith("e2b:")
        sandbox = provider.get(sandbox_id)
        assert sandbox is not None

        # Acquire uploaded the host uploads tree through the file API.
        assert (
            sandbox.read_file("/mnt/user-data/uploads/brief.txt").strip()
            == "uploaded-by-host"
        )

        # Commands run inside the sandbox; the template tools must exist.
        output = sandbox.execute_command(
            "echo e2b-echo-ok; python3 -c 'print(\"e2b-python-ok\")'"
        )
        assert "e2b-echo-ok" in output
        assert "e2b-python-ok" in output

        # The output is written in the sandbox, then downloaded on release.
        sandbox.write_file("/mnt/user-data/outputs/report.txt", "written-by-sandbox\n")
        provider.release(sandbox_id)
        assert provider.get(sandbox_id) is None
        assert (outputs / "report.txt").read_text(encoding="utf-8") == (
            "written-by-sandbox\n"
        )

        # release_action=pause drops the handle; a later acquire discovers the
        # paused sandbox and resumes the same remote instance.
        resumed_id = provider.acquire(thread_id, user_id=_USER_ID)
        assert resumed_id == sandbox_id
        resumed = provider.get(resumed_id)
        assert resumed is not None
        assert "e2b-resume-ok" in resumed.execute_command("echo e2b-resume-ok")

        # Skill files are uploaded lazily: only the file that is read crosses
        # the wire, and the read succeeds even though nothing was bind mounted.
        skill_files = sorted((_PROJECT_ROOT / "skills" / "builtin").glob("*/SKILL.md"))
        if skill_files:
            relative = skill_files[0].parent.name
            body = resumed.read_file(f"/mnt/skills/builtin/{relative}/SKILL.md")
            assert body.strip()
    finally:
        provider.shutdown()
        _kill_leftovers(factory, thread_id)
        shutil.rmtree(thread_dir, ignore_errors=True)
