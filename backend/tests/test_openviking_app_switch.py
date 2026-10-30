"""The explicit long-term-memory switch chooses exactly one HTTP authority."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from agentkit.agents.memory.openviking.config import OpenVikingMemoryConfig
from backend.app.gateway.app import (
    create_app,
    start_openviking_memory,
    stop_openviking_memory,
)


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


def test_switch_off_mounts_profile_without_memory_admin() -> None:
    routes = _routes(create_app(use_openviking_memory=False))

    assert ("GET", "/api/memory/profile") in routes
    assert ("GET", "/api/memory/search") not in routes
    assert ("GET", "/api/admin/memory/status") not in routes


def test_switch_on_mounts_the_openviking_surface_without_canonical_reads() -> None:
    routes = _routes(create_app(use_openviking_memory=True))

    assert ("GET", "/api/memory/profile") in routes
    assert ("DELETE", "/api/memory/profile/{field_path:path}") in routes
    assert ("GET", "/api/memory/pending") in routes
    assert ("GET", "/api/admin/memory/status") in routes
    assert ("POST", "/api/admin/memory/flush") in routes
    assert ("GET", "/api/memory/search") not in routes


def test_profile_route_is_never_registered_twice() -> None:
    routes = _routes(create_app(use_openviking_memory=True))
    profile_gets = [route for route in routes if route == ("GET", "/api/memory/profile")]

    assert len(profile_gets) == 1


@pytest.mark.asyncio
async def test_disabled_runtime_start_is_a_noop() -> None:
    runtime, redis = await start_openviking_memory(
        FastAPI(),
        SimpleNamespace(memory=SimpleNamespace(openviking=OpenVikingMemoryConfig())),
        object(),
    )

    assert runtime is None
    assert redis is None


@pytest.mark.asyncio
async def test_enabled_runtime_requires_a_redis_url() -> None:
    config = OpenVikingMemoryConfig(
        enabled=True,
        user_id_salt="s" * 32,
        capture_stream={"redis_url": ""},
    )

    with pytest.raises(RuntimeError, match="redis_url"):
        await start_openviking_memory(
            FastAPI(),
            SimpleNamespace(memory=SimpleNamespace(openviking=config)),
            object(),
        )


@pytest.mark.asyncio
async def test_enabled_runtime_rejects_a_short_salt_before_opening_redis() -> None:
    config = OpenVikingMemoryConfig(
        enabled=True,
        user_id_salt="short",
        capture_stream={"redis_url": "redis://127.0.0.1:6379/0"},
    )

    with pytest.raises(RuntimeError, match="user_id_salt"):
        await start_openviking_memory(
            FastAPI(),
            SimpleNamespace(memory=SimpleNamespace(openviking=config)),
            object(),
        )


@pytest.mark.asyncio
async def test_stop_resets_the_runtime_and_closes_redis() -> None:
    class Redis:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    class Runtime:
        stopped = False

        async def stop(self) -> None:
            self.stopped = True

    application = FastAPI()
    runtime = Runtime()
    redis = Redis()

    await stop_openviking_memory(application, runtime, redis)

    assert runtime.stopped is True
    assert redis.closed is True
    assert application.state.openviking_memory is None
