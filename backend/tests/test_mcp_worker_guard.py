"""MCP servers enabled: the boot guard, the composed service, and the mounted routes.

An MCP deployment is process-level and key-dependent, so two facts must be true
before the gateway serves its first request:

* **it has a usable Fernet key.**  mcp_servers.headers_encrypted is encrypted with
  it, so an enabled block without a valid key would store headers this deployment
  can never decrypt.  validate_for_startup() fails the boot instead of the first
  server write;
* **it runs one worker.**  One startup pass, one midnight task and one sync lock
  per process is the whole concurrency story, so a second worker would run its own
  pass over the same catalog.

A disabled block constrains nothing and publishes nothing: no runtime in
app.state, no process-global handle, and no tool_search service for the two
builtins to call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentkit.config import AppConfig
from agentkit.config.mcp_servers_config import McpServersConfig
from agentkit.mcp.crypto import HeaderCipher
from agentkit.mcp.runtime_protocol import get_mcp_runtime, reset_mcp_runtime
from agentkit.mcp.service import McpServerService, ServerUrlPolicy
from agentkit.persistence.base import Base
from agentkit.tools.search.service import (
    get_tool_search_runtime,
    reset_tool_search_runtime,
)
from backend.app.gateway.app import (
    create_app,
    preflight_mcp_servers,
    start_mcp_runtime,
    stop_mcp_runtime,
)


@pytest.fixture(autouse=True)
def _clean_process_slots() -> Any:
    reset_mcp_runtime()
    reset_tool_search_runtime()
    yield
    reset_mcp_runtime()
    reset_tool_search_runtime()


def _enabled(**overrides: Any) -> McpServersConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "encryption_key": HeaderCipher.generate_key(),
    }
    base.update(overrides)
    return McpServersConfig(**base)


def _app_config(mcp: McpServersConfig, **sections: Any) -> AppConfig:
    """A full app config around one mcp_servers block (search off by default)."""
    data: dict[str, Any] = {
        "mcp_servers": mcp,
        "tool_search": {"enabled": False},
    }
    data.update(sections)
    return AppConfig(**data)


def _routes(app: FastAPI) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for route in app.routes:
        if type(route).__name__ == "_IncludedRouter":
            prefix = getattr(route.include_context, "prefix", "") or ""
            children = route.original_router.routes
        else:
            prefix = ""
            children = [route]
        for child in children:
            path = child.path
            if prefix and not path.startswith(prefix):
                path = prefix.rstrip("/") + path
            if not path.startswith("/api"):
                continue
            for method in getattr(child, "methods", set()):
                if method not in {"HEAD", "OPTIONS"}:
                    found.add((method, path))
    return found


# --------------------------------------------------------------------------- #
# Boot guard: the key and the worker count
# --------------------------------------------------------------------------- #


def test_an_enabled_block_without_a_key_refuses_to_start(monkeypatch: Any) -> None:
    monkeypatch.delenv("MYFLOW_MCP_SERVER_KEY", raising=False)
    config = AppConfig(mcp_servers={"enabled": True}, tool_search={"enabled": False})

    with pytest.raises(ValueError, match="encryption_key"):
        preflight_mcp_servers(config)


def test_an_enabled_block_with_an_invalid_key_refuses_to_start(monkeypatch: Any) -> None:
    monkeypatch.delenv("MYFLOW_MCP_SERVER_KEY", raising=False)
    config = AppConfig(
        mcp_servers={"enabled": True, "encryption_key": "not-a-fernet-key"},
        tool_search={"enabled": False},
    )

    with pytest.raises(ValueError, match="Fernet"):
        preflight_mcp_servers(config)


def test_the_key_may_come_from_the_environment(monkeypatch: Any) -> None:
    monkeypatch.setenv("MYFLOW_MCP_SERVER_KEY", HeaderCipher.generate_key())
    monkeypatch.setenv("GATEWAY_WORKERS", "1")

    preflight_mcp_servers(
        AppConfig(mcp_servers={"enabled": True}, tool_search={"enabled": False})
    )


def test_enabled_mcp_servers_with_multiple_workers_refuses_to_start(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("GATEWAY_WORKERS", "2")

    with pytest.raises(RuntimeError, match="GATEWAY_WORKERS"):
        preflight_mcp_servers(_app_config(_enabled()))


def test_enabled_mcp_servers_with_one_worker_passes(monkeypatch: Any) -> None:
    monkeypatch.setenv("GATEWAY_WORKERS", "1")

    preflight_mcp_servers(_app_config(_enabled()))


def test_disabled_mcp_servers_do_not_constrain_the_worker_count(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("GATEWAY_WORKERS", "4")
    monkeypatch.delenv("MYFLOW_MCP_SERVER_KEY", raising=False)

    preflight_mcp_servers(AppConfig(mcp_servers={"enabled": False}))


def test_a_disabled_block_still_refuses_an_empty_scheme_list() -> None:
    """Every server URL is checked against allowed_schemes, so it cannot be empty.

    The boot guard is only reached for an enabled block (an unused feature must
    not be able to block the gateway), so the block itself stays authoritative.
    """
    config = AppConfig(
        mcp_servers={"enabled": False, "allowed_schemes": []},
        tool_search={"enabled": False},
    )

    preflight_mcp_servers(config)
    with pytest.raises(ValueError, match="allowed_schemes"):
        config.mcp_servers.validate_for_startup()


# --------------------------------------------------------------------------- #
# Startup wiring
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_start_registers_the_runtime_and_the_management_service(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "app.db").as_posix()
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    application = FastAPI()
    try:
        await start_mcp_runtime(application, _app_config(_enabled()), factory)

        runtime = application.state.mcp_runtime
        assert runtime is not None
        assert get_mcp_runtime() is runtime
        service = application.state.mcp_server_service
        assert isinstance(service, McpServerService)
        # The composed service reads the same stores and cipher the runtime owns:
        # two ciphers would mean two keys.
        assert service.store is runtime.server_store
        assert service.cipher is runtime.cipher
        assert service.tool_store is runtime.tool_store
        assert service.sync is runtime
        assert service.policy.allowed_schemes == ("https",)
        # A deployment with tool_search off has nothing to retrieve with, so the
        # tool reports that instead of publishing a service with no channels.
        assert get_tool_search_runtime() is None
    finally:
        await stop_mcp_runtime(application)
        await engine.dispose()

    assert application.state.mcp_runtime is None
    assert application.state.mcp_server_service is None
    assert get_mcp_runtime() is None


@pytest.mark.asyncio
async def test_start_publishes_the_tool_search_service(tmp_path: Path) -> None:
    from agentkit.tools.search.service import McpToolSearchService

    engine = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / "app_search.db").as_posix()
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    application = FastAPI()
    app_config = AppConfig(
        mcp_servers=_enabled(),
        tool_search={
            "vector_store": {"uri": "http://milvus.test:19530"},
            "embedding": {"base_url": "http://embed.test", "model": "bge-m3"},
        },
    )
    try:
        await start_mcp_runtime(application, app_config, factory)

        assert isinstance(get_tool_search_runtime(), McpToolSearchService)
    finally:
        await stop_mcp_runtime(application)
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_disabled_block_starts_nothing() -> None:
    application = FastAPI()

    await start_mcp_runtime(
        application, AppConfig(mcp_servers={"enabled": False}), object()
    )

    assert application.state.mcp_runtime is None
    assert application.state.mcp_server_service is None
    assert get_mcp_runtime() is None
    assert get_tool_search_runtime() is None


@pytest.mark.asyncio
async def test_stop_is_safe_when_nothing_started() -> None:
    application = FastAPI()

    await stop_mcp_runtime(application)

    assert get_mcp_runtime() is None


def test_the_mcp_servers_router_is_mounted() -> None:
    routes = _routes(create_app(use_openviking_memory=False))

    assert ("GET", "/api/mcp-servers") in routes
    assert ("POST", "/api/mcp-servers") in routes
    assert ("GET", "/api/mcp-servers/status") in routes
    assert ("POST", "/api/mcp-servers/reindex") in routes
    assert ("GET", "/api/mcp-servers/{server_id}") in routes
    assert ("DELETE", "/api/mcp-servers/{server_id}") in routes
    assert ("POST", "/api/mcp-servers/{server_id}/refresh") in routes
    assert ("POST", "/api/mcp-servers/{server_id}/state") in routes
    assert ("GET", "/api/mcp-servers/{server_id}/tools") in routes


def test_the_mounted_route_reports_a_disabled_block_as_a_coded_503(
    monkeypatch: Any,
) -> None:
    """The route exists even when the block is off, and says so in one code.

    A 404 would read as "this deployment has no such feature"; the dependency
    layer answers a 503 with mcp_servers_disabled instead (pinned end to end in
    test_mcp_servers_router, on a bare app without the auth middleware).
    """
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.app.gateway import mcp_servers_deps
    from backend.app.gateway.errors import install_error_handlers
    from backend.app.gateway.routers import mcp_servers as router_module
    from backend.app.gateway.run_models import CallerKind, RunCaller

    monkeypatch.setattr(
        mcp_servers_deps,
        "get_app_config",
        lambda: SimpleNamespace(mcp_servers=SimpleNamespace(enabled=False)),
    )
    application = FastAPI()
    install_error_handlers(application)
    application.include_router(router_module.router, prefix="/api")
    # The auth gate is a middleware on the real app; this bare app answers the
    # caller dependency itself so the test sees the service dependency's answer.
    for dependency in (
        mcp_servers_deps.require_mcp_reader,
        mcp_servers_deps.require_mcp_writer,
    ):
        application.dependency_overrides[dependency] = lambda: RunCaller(
            kind=CallerKind.HTTP, user_id="u1", user_role="user"
        )

    response = TestClient(application).get("/api/mcp-servers")

    assert response.status_code == 503
    assert response.json()["code"] == "mcp_servers_disabled"


def test_the_url_policy_is_built_from_the_config_block() -> None:
    config = _enabled(allowed_schemes=["https", "http"], allow_private_networks=True)

    policy = ServerUrlPolicy(
        allowed_schemes=tuple(config.allowed_schemes),
        allow_private_networks=config.allow_private_networks,
        max_servers_per_user=config.max_servers_per_user,
        enabled=config.enabled,
    )

    assert policy.allowed_schemes == ("https", "http")
    assert policy.allow_private_networks is True
    assert policy.max_servers_per_user == config.max_servers_per_user


def test_a_disabled_build_publishes_no_runtime() -> None:
    from agentkit.mcp.runtime import build_mcp_runtime

    assert build_mcp_runtime(McpServersConfig()) is None
