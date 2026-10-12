"""M7 §5.7 第 1 条：API 注册/启用 stdio 只接受允许列表里的裸可执行文件名。

配置文件可以写任何东西（那是运维的文件），HTTP API 是不可信输入。M7 没有 PUT 路由，
所以能碰到这条校验的入口就是 PATCH（启用一台已有服务器）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.config.extensions_config import ExtensionsConfig
from backend.app.gateway import deps
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.auth_middleware import PUBLIC_EXACT_PATHS, PUBLIC_PATH_PREFIXES
from backend.app.gateway.errors import StdioCommandRejected
from backend.app.gateway.mcp_wire import check_stdio_commands, stdio_command_allowlist
from backend.app.gateway.routers import mcp, skills
from backend.app.gateway.run_models import CallerKind, RunCaller
from fakes.skill_tree import write_app_config

ADMIN = frozenset({"admin"})


@pytest.fixture
def config_path(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "extensions_config.json"
    path.write_text(json.dumps({"mcpServers": {}, "skills": {}}), encoding="utf-8")
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(path))
    monkeypatch.setenv("MYFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("MYFLOW_CONFIG_PATH", str(write_app_config(tmp_path)))
    monkeypatch.delenv("MYFLOW_MCP_STDIO_COMMAND_ALLOWLIST", raising=False)
    monkeypatch.delenv("DEER_FLOW_MCP_STDIO_COMMAND_ALLOWLIST", raising=False)
    return path


def _client(monkeypatch, scopes: frozenset[str] | None = ADMIN) -> TestClient:
    application = FastAPI(redirect_slashes=False)
    # M9 §2.11：这些用例测的是业务路由，不是认证门；显式钉住"认证关闭"，
    # 免得结果取决于仓库里有没有 config.yaml。
    install_middleware(application, auth_settings=AuthSettings.disabled())
    install_error_handlers(application)
    application.include_router(mcp.router, prefix="/api")
    application.include_router(skills.router, prefix="/api")
    if scopes is not None:
        monkeypatch.setattr(
            deps,
            "caller_from_request",
            lambda request: RunCaller(kind=CallerKind.HTTP, user_id=None, scopes=scopes),
        )
    return TestClient(application, raise_server_exceptions=False)


def _seed(path: Path, server: dict) -> None:
    path.write_text(
        json.dumps({"mcpServers": {"local": server}, "skills": {}}), encoding="utf-8"
    )


def _disk(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_default_allowlist_is_bare_npx_and_uvx(config_path, monkeypatch):
    assert stdio_command_allowlist() == frozenset({"npx", "uvx"})


def test_the_env_var_extends_the_allowlist(config_path, monkeypatch):
    monkeypatch.setenv("MYFLOW_MCP_STDIO_COMMAND_ALLOWLIST", "python3, bash")

    assert {"python3", "bash"} <= stdio_command_allowlist()


def test_the_checker_rejects_everything_that_is_not_a_bare_allowed_name():
    for command in ("/usr/bin/python", "python", "npx; rm -rf /", "npx ", "../npx", ""):
        with pytest.raises(StdioCommandRejected) as raised:
            check_stdio_commands({"local": {"enabled": True, "type": "stdio", "command": command}})
        message = str(raised.value)
        assert "npx" in message and "uvx" in message
        if command.strip():
            assert command not in message  # 命令行绝不回显

    with pytest.raises(StdioCommandRejected):
        check_stdio_commands({"local": {"enabled": True, "type": "stdio"}})  # 缺 command


def test_disabled_and_remote_servers_skip_the_check():
    check_stdio_commands({"local": {"enabled": False, "type": "stdio", "command": "/usr/bin/python"}})
    check_stdio_commands({"remote": {"enabled": True, "type": "http", "url": "https://x"}})
    check_stdio_commands({"remote": {"enabled": True, "transport": "http", "url": "https://x"}})


def test_enabling_an_illegal_stdio_command_is_422_without_echoing_it(config_path, monkeypatch):
    _seed(config_path, {"enabled": False, "type": "stdio", "command": "/usr/bin/python"})
    client = _client(monkeypatch)

    response = client.patch("/api/mcp/config", json={"name": "local", "enabled": True})

    assert response.status_code == 422
    assert response.json()["code"] == "stdio_command_rejected"
    assert "npx" in response.json()["detail"] and "uvx" in response.json()["detail"]
    assert "/usr/bin/python" not in response.text
    assert _disk(config_path)["mcpServers"]["local"]["enabled"] is False


def test_a_broken_server_can_still_be_disabled(config_path, monkeypatch):
    _seed(config_path, {"enabled": True, "type": "stdio", "command": "/usr/bin/python"})
    client = _client(monkeypatch)

    response = client.patch("/api/mcp/config", json={"name": "local", "enabled": False})

    assert response.status_code == 200
    assert response.json()["mcpServers"]["local"]["enabled"] is False
    assert _disk(config_path)["mcpServers"]["local"]["command"] == "/usr/bin/python"


def test_an_allowed_bare_name_can_be_enabled(config_path, monkeypatch):
    monkeypatch.setenv("MYFLOW_MCP_STDIO_COMMAND_ALLOWLIST", "python3")
    _seed(config_path, {"enabled": False, "type": "stdio", "command": "python3"})
    client = _client(monkeypatch)

    response = client.patch("/api/mcp/config", json={"name": "local", "enabled": True})

    assert response.status_code == 200
    assert response.json()["mcpServers"]["local"]["enabled"] is True


def test_the_ops_file_path_still_loads_anything(config_path, monkeypatch):
    """直接改文件的运维路径不跑允许列表。"""
    _seed(config_path, {"enabled": True, "type": "stdio", "command": "/usr/bin/python"})

    config = ExtensionsConfig.from_file(config_path)

    assert config.mcp_servers["local"].command == "/usr/bin/python"
