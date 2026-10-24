"""M7 §2.4：路由清单、能力门、打码、两层开关。

M7 **没有创建类路由**：PUT /api/mcp/config 与 POST /api/skills 是 404/405（不是 403）——
创建能力确实还没实现，而不是"你没权限"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.gateway import deps
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.errors import InvalidExtensionsConfig
from backend.app.gateway.mcp_wire import restore_masked_secrets
from backend.app.gateway.routers import mcp, skills
from backend.app.gateway.run_models import CallerKind, RunCaller
from fakes.skill_tree import write_app_config

ADMIN = frozenset({"admin"})
SKILLS_READ = frozenset({"skills:read"})

SECRET_TOKEN = "ghp_super_secret_value"
SKILL_BODY = """---
name: foo
description: 处理 PDF 文档
license: MIT
allowed-tools: [read_file, bash]
---

# Skill Instructions
"""


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("MYFLOW_MCP_STDIO_COMMAND_ALLOWLIST", raising=False)
    monkeypatch.delenv("MYFLOW_SKILLS_PREFS_PATH", raising=False)


@pytest.fixture
def config_path(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "extensions_config.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {
                        "enabled": True,
                        "type": "stdio",
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-github"],
                        "env": {"GITHUB_TOKEN": SECRET_TOKEN},
                        "headers_from_context": {"Authorization": "GITHUB_TOKEN"},
                        "user_auth": {"users": {"u1": {"token": "user-scoped"}}},
                        "tool_name_prefix": True,
                        "session_init_timeout": 60,
                        "tool_call_timeout": 60,
                    }
                },
                "skills": {"foo": {"enabled": True}},
                "mcpInterceptors": ["ops.Interceptor"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(path))
    monkeypatch.setenv("MYFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("MYFLOW_CONFIG_PATH", str(write_app_config(tmp_path)))
    return path


class _Identity(dict):
    """一个 client 一个身份槽：同一个测试里可以中途换成另一个用户/另一组能力。"""

    def __init__(self, scopes: frozenset[str] | None, user_id: str | None) -> None:
        super().__init__(scopes=scopes, user_id=user_id)

    def as_(self, scopes: frozenset[str] | None, *, user_id: str | None = None) -> "_Identity":
        self["scopes"] = scopes
        self["user_id"] = user_id
        return self


def _client(monkeypatch, scopes: frozenset[str] | None = ADMIN, *, user_id: str | None = None) -> TestClient:
    application = FastAPI(redirect_slashes=False)
    # M9 §2.11：这些用例测的是业务路由，不是认证门；显式钉住"认证关闭"，
    # 免得结果取决于仓库里有没有 config.yaml。
    install_middleware(application, auth_settings=AuthSettings.disabled())
    install_error_handlers(application)
    application.include_router(mcp.router, prefix="/api")
    application.include_router(skills.router, prefix="/api")
    identity = _Identity(scopes if scopes is not None else None, user_id)
    if scopes is not None:
        monkeypatch.setattr(
            deps,
            "caller_from_request",
            lambda request: RunCaller(
                kind=CallerKind.HTTP,
                user_id=identity["user_id"],
                scopes=identity["scopes"],
            ),
        )
    client = TestClient(application, raise_server_exceptions=False)
    client.identity = identity  # type: ignore[attr-defined]
    return client


def _write_skill(root: Path, name: str, *, category: str = "public", body: str = SKILL_BODY) -> None:
    skill_dir = root / "skills" / category / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(body.replace("name: foo", f"name: {name}"), encoding="utf-8")


def _disk(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_skills_list_needs_skills_read_not_admin(config_path, monkeypatch):
    _write_skill(Path(config_path).parent, "foo")

    assert _client(monkeypatch, SKILLS_READ).get("/api/skills").status_code == 200
    denied = _client(monkeypatch, ADMIN).get("/api/skills")

    assert denied.status_code == 403
    assert denied.json()["code"] == "permission_denied"


def test_creation_lives_in_the_author_router_not_this_one(config_path, monkeypatch):
    """M7 的路由对不声明创建；Task 8 起 POST /api/skills 由作者路由提供。

    这个测试只装配 mcp + skills 两个 router，所以仍然是 404；应用级路由表里
    POST /api/skills 必须存在（否则前端"新建技能"全 404）。
    """
    client = _client(monkeypatch)

    put_mcp = client.put("/api/mcp/config", json={"mcpServers": {}})
    post_skills = client.post("/api/skills", json={"name": "foo"})

    assert put_mcp.status_code in (404, 405)
    assert post_skills.status_code in (404, 405)

    from backend.app.gateway.routers import skill_drafts

    methods = {
        method
        for route in skill_drafts.router.routes
        if route.path == "/skills"
        for method in getattr(route, "methods", ()) or ()
    }
    assert "POST" in methods


def test_admin_only_routes(config_path, monkeypatch):
    client = _client(monkeypatch, SKILLS_READ)
    calls = [
        ("get", "/api/mcp/config", None),
        ("patch", "/api/mcp/config", {"name": "github", "enabled": False}),
        ("post", "/api/mcp/cache/reset", None),
        ("post", "/api/skills/reload", None),
        ("patch", "/api/skills/foo", {"enabled": False}),
        ("get", "/api/skills?scope=global", None),
    ]

    for method, url, body in calls:
        response = getattr(client, method)(url, json=body) if body is not None else getattr(client, method)(url)
        assert response.status_code == 403, (method, url)
        assert response.json()["code"] == "permission_denied"


def test_reload_is_declared_before_the_skill_id_route(config_path, monkeypatch):
    paths = [route.path for route in skills.router.routes]

    assert paths.index("/skills/reload") < paths.index("/skills/{skill_id}")
    response = _client(monkeypatch).post("/api/skills/reload")

    assert response.status_code == 200
    assert response.json() == {"reloaded": True}


def test_a_plain_user_can_flip_only_their_own_preference(config_path, monkeypatch):
    _write_skill(Path(config_path).parent, "foo")
    alice = _client(monkeypatch, SKILLS_READ, user_id="alice")

    response = alice.put("/api/skills/foo/preference", json={"enabled": False})

    assert response.status_code == 200
    body = response.json()
    # Task 15 追加了 Git 目录字段（本来源下都是 null），M7 的五个字段形状不变。
    assert {
        key: body[key]
        for key in ("name", "description", "category", "enabled", "global_enabled")
    } == {
        "name": "foo",
        "description": "处理 PDF 文档",
        "category": "public",
        "enabled": False,
        "global_enabled": None,
    }
    assert body["skill_id"] is None and body["scope"] is None
    assert alice.get("/api/skills").json()["skills"][0]["enabled"] is False
    alice.identity.as_(SKILLS_READ, user_id="bob")
    assert alice.get("/api/skills").json()["skills"][0]["enabled"] is True
    # 个人开关不碰全局配置
    assert _disk(config_path)["skills"] == {"foo": {"enabled": True}}


def test_personal_preference_needs_the_enabled_field(config_path, monkeypatch):
    _write_skill(Path(config_path).parent, "foo")

    response = _client(monkeypatch, SKILLS_READ, user_id="alice").put(
        "/api/skills/foo/preference", json={}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


def test_an_unknown_skill_is_404_on_both_switches(config_path, monkeypatch):
    client = _client(monkeypatch, ADMIN, user_id="alice")

    global_patch = client.patch("/api/skills/ghost", json={"enabled": False})
    client.identity.as_(SKILLS_READ, user_id="alice")
    preference = client.put("/api/skills/ghost/preference", json={"enabled": False})

    assert global_patch.status_code == 404
    assert global_patch.json()["code"] == "skill_not_found"
    assert preference.status_code == 404
    assert preference.json()["code"] == "skill_not_found"


def test_scope_global_shows_the_global_value_to_admins(config_path, monkeypatch):
    _write_skill(Path(config_path).parent, "foo")
    admin = _client(monkeypatch, ADMIN | SKILLS_READ, user_id="alice")

    admin.patch("/api/skills/foo", json={"enabled": False})

    effective = admin.get("/api/skills").json()["skills"][0]
    global_view = admin.get("/api/skills?scope=global").json()["skills"][0]
    assert effective["enabled"] is False and effective["global_enabled"] is None
    assert global_view["global_enabled"] is False
    assert _disk(config_path)["skills"] == {"foo": {"enabled": False}}


def test_the_global_switch_does_not_touch_mcp_servers(config_path, monkeypatch):
    _write_skill(Path(config_path).parent, "foo")

    response = _client(monkeypatch).patch("/api/skills/foo", json={"enabled": False})

    assert response.status_code == 200
    disk = _disk(config_path)
    assert set(disk["mcpServers"]) == {"github"}
    assert disk["mcpInterceptors"] == ["ops.Interceptor"]


def test_get_masks_every_secret(config_path, monkeypatch):
    response = _client(monkeypatch).get("/api/mcp/config")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"mcpServers"}  # by_alias：不是 mcp_servers
    server = body["mcpServers"]["github"]
    assert server["env"] == {"GITHUB_TOKEN": "****"}
    assert server["user_auth"]["users"] == {"u1": {"token": "****"}}
    assert server["headers_from_context"] == {"Authorization": "GITHUB_TOKEN"}
    assert SECRET_TOKEN not in response.text
    assert "user-scoped" not in response.text
    assert str(config_path) not in response.text


def test_masked_values_are_restored_only_when_something_is_stored():
    """PUT 路由留给后续阶段（能力位 mcp:create）；回写逻辑现在就钉住。"""
    disk = {"github": {"env": {"GITHUB_TOKEN": SECRET_TOKEN}}}

    restored = restore_masked_secrets({"github": {"env": {"GITHUB_TOKEN": "****"}}}, disk)

    assert restored["github"]["env"]["GITHUB_TOKEN"] == SECRET_TOKEN
    with pytest.raises(InvalidExtensionsConfig) as raised:
        restore_masked_secrets({"fresh": {"env": {"TOKEN": "****"}}}, disk)
    assert SECRET_TOKEN not in str(raised.value)
    assert "TOKEN" in str(raised.value)


def test_patch_flips_only_one_server(config_path, monkeypatch):
    client = _client(monkeypatch)

    response = client.patch("/api/mcp/config", json={"name": "github", "enabled": False})

    assert response.status_code == 200
    disk = _disk(config_path)
    assert disk["mcpServers"]["github"]["enabled"] is False
    assert disk["skills"] == {"foo": {"enabled": True}}
    assert disk["mcpInterceptors"] == ["ops.Interceptor"]


def test_patch_of_an_unknown_server_is_404(config_path, monkeypatch):
    response = _client(monkeypatch).patch("/api/mcp/config", json={"name": "ghost", "enabled": False})

    assert response.status_code == 404
    assert response.json()["code"] == "mcp_server_not_found"


def test_cache_reset_reports_this_worker_only(config_path, monkeypatch):
    response = _client(monkeypatch).post("/api/mcp/cache/reset")

    assert response.status_code == 200
    assert response.json() == {"reset": True}


def test_an_illegal_skill_name_is_422(config_path, monkeypatch):
    response = _client(monkeypatch).patch("/api/skills/bad%20name", json={"enabled": False})

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


def test_a_vanished_explicit_config_is_500_without_the_path(config_path, monkeypatch):
    client = _client(monkeypatch)
    config_path.unlink()

    response = client.get("/api/mcp/config")

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert str(config_path) not in response.text


def test_the_skill_list_shares_one_scan_with_the_model_index(config_path, monkeypatch):
    """列表里有、索引里没有（或反过来）就是 bug：两处都走 load_skills。"""
    root = Path(config_path).parent
    _write_skill(root, "foo")
    _write_skill(root, "bar")
    monkeypatch.setenv("MYFLOW_SKILLS_PREFS_PATH", str(root / "skills_prefs.json"))
    from agentkit.config import get_app_config
    from agentkit.skills.prompt import build_skills_section

    listed = [item["name"] for item in _client(monkeypatch, SKILLS_READ).get("/api/skills").json()["skills"]]
    section = build_skills_section(config=get_app_config().skills)
    # 索引前面有一行说明（"这是索引，不是正文"），只取 <available_skills> 块里的条目
    block = section[section.index("<available_skills>") : section.index("</available_skills>")]
    indexed = [
        line[2:].split(" [")[0].split(":")[0]
        for line in block.splitlines()
        if line.startswith("- ")
    ]

    assert listed == sorted(indexed) == ["bar", "foo"]

def test_replace_mcp_servers_keeps_the_running_path_alive(config_path, monkeypatch):
    """PUT 路由留给后续阶段，但整份替换的实现（打码还原 + 允许列表 + 原子写）现在就钉住。"""
    from backend.app.gateway.mcp_wire import replace_mcp_servers
    from backend.app.gateway.models.request import McpConfigUpdateRequest

    body = McpConfigUpdateRequest.model_validate(
        {
            "mcpServers": {
                "github": {"enabled": True, "command": "npx", "env": {"GITHUB_TOKEN": "****"}},
                "other": {"transport": "http", "url": "https://mcp.example.test/mcp"},
            }
        }
    )

    saved = replace_mcp_servers(body)

    disk = _disk(config_path)
    assert set(disk["mcpServers"]) == {"github", "other"}  # 整份替换，不是 merge
    assert disk["mcpServers"]["github"]["env"] == {"GITHUB_TOKEN": SECRET_TOKEN}
    assert disk["mcpServers"]["other"]["type"] == "http"
    assert disk["skills"] == {"foo": {"enabled": True}}
    assert disk["mcpInterceptors"] == ["ops.Interceptor"]
    # 保存结果还没打码：PUT 路由（后续阶段）必须自己过一次 mask_mcp_config 再回给客户端
    from backend.app.gateway.mcp_wire import mask_mcp_config

    assert saved["mcpServers"]["github"]["env"] == {"GITHUB_TOKEN": SECRET_TOKEN}
    assert mask_mcp_config(saved)["mcpServers"]["github"]["env"] == {"GITHUB_TOKEN": "****"}
