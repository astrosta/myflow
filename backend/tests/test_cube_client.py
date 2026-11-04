from __future__ import annotations

import sys
import types
from pathlib import Path

import httpx
import pytest

from agentkit.sandbox.cube.client import (
    CubesandboxFactory,
    build_metadata_query,
    error_status,
)
from fakes.cube_sdk import (
    FakeCommandResult,
    FakeCommands,
    FakeFiles,
    FakeSdkFactory,
    FakeSdkSandbox,
    FilesystemNotFoundError,
)


def _factory(http_client: httpx.Client | None = None) -> CubesandboxFactory:
    return CubesandboxFactory(
        api_url="http://cube:3000",
        api_key="",
        template_id="tpl",
        sandbox_domain="cube.app",
        proxy_node_ip=None,
        proxy_port=80,
        request_timeout=5,
        http_client=http_client,
    )


def test_build_metadata_query_encodes_pairs() -> None:
    assert (
        build_metadata_query({"myflow": "1", "myflow.thread": "t 1"})
        == "myflow=1&myflow.thread=t+1"
    )


def test_list_by_metadata_follows_next_token() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        if "nextToken" not in request.url.params:
            return httpx.Response(
                200,
                json=[{"sandboxID": "s1"}],
                headers={"x-next-token": "n1"},
            )
        return httpx.Response(200, json=[{"sandboxID": "s2"}])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    factory = _factory(client)
    assert [
        item["sandboxID"] for item in factory.list_by_metadata({"myflow": "1"})
    ] == ["s1", "s2"]
    assert len(calls) == 2


def test_list_by_metadata_sends_filters_and_api_key() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = request.url
        seen["api_key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    factory = CubesandboxFactory(
        api_url="http://cube:3000/",
        api_key="secret",
        template_id="tpl",
        sandbox_domain="cube.app",
        proxy_node_ip=None,
        proxy_port=80,
        request_timeout=5,
        http_client=client,
    )

    assert factory.list_by_metadata({"myflow": "1", "myflow.thread": "t 1"}) == []
    url = seen["url"]
    assert isinstance(url, httpx.URL)
    assert url.path == "/v2/sandboxes"
    assert url.params["myflow"] == "1"
    assert url.params["myflow.thread"] == "t 1"
    assert url.params["state"] == "running,paused"
    assert seen["api_key"] == "secret"


def test_error_status_reads_status_code() -> None:
    class Boom(Exception):
        status_code = 404

    assert error_status(Boom()) == 404


def test_error_status_without_status_code_is_none() -> None:
    assert error_status(RuntimeError("boom")) is None


def test_create_uses_configured_sdk_and_lazy_import(monkeypatch) -> None:
    module, calls = _install_fake_cubesandbox(monkeypatch)
    assert module is sys.modules["cubesandbox"]

    created = _factory().create(
        template="tpl",
        timeout=60,
        metadata={"myflow": "1"},
        lifecycle={"on_timeout": "pause"},
        network={"allow_public_traffic": False},
        allow_internet_access=True,
    )

    assert created == "created-sandbox"
    assert calls["config"] == {
        "api_url": "http://cube:3000",
        "api_key": None,
        "template_id": "tpl",
        "sandbox_domain": "cube.app",
        "proxy_node_ip": None,
        "proxy_port": 80,
        "request_timeout": 5,
    }
    assert calls["create"] == {
        "template": "tpl",
        "timeout": 60,
        "metadata": {"myflow": "1"},
        "lifecycle": {"on_timeout": "pause"},
        "network": {"allow_public_traffic": False},
        "allow_internet_access": True,
        "config": calls["config_objs"][0],
    }


def test_connect_and_health_use_configured_sdk(monkeypatch) -> None:
    _, calls = _install_fake_cubesandbox(monkeypatch)
    factory = _factory()

    assert factory.connect("remote-1") == "connected-sandbox"
    assert factory.health() is None
    assert calls["connect"] == {
        "sandbox_id": "remote-1",
        "config": calls["config_objs"][0],
    }
    assert calls["health"] == {"config": calls["config_objs"][1]}


def test_client_module_does_not_import_cubesandbox_at_import_time() -> None:
    import agentkit.sandbox.cube.client as client_module

    assert "cubesandbox" not in vars(client_module)


def test_fake_files_maps_container_paths_to_a_host_root(tmp_path: Path) -> None:
    files = FakeFiles({"/mnt/user-data": tmp_path / "user-data"})
    files.write("/mnt/user-data/workspace/sub/a.txt", "hello")

    host = tmp_path / "user-data" / "workspace" / "sub" / "a.txt"
    assert host.read_text() == "hello"
    assert files.writes == ["/mnt/user-data/workspace/sub/a.txt"]
    assert files.read("/mnt/user-data/workspace/sub/a.txt", format="text") == "hello"
    assert files.read("/mnt/user-data/workspace/sub/a.txt", format="bytes") == b"hello"
    assert files.list("/mnt/user-data/workspace") == [
        {
            "name": "sub",
            "type": "FILE_TYPE_DIRECTORY",
            "size": files.stat("/mnt/user-data/workspace/sub")["size"],
        }
    ]


def test_fake_files_stat_missing_raises_not_found() -> None:
    files = FakeFiles()

    with pytest.raises(FilesystemNotFoundError) as exc_info:
        files.stat("/mnt/user-data/missing")

    assert exc_info.value.status_code == 404


def test_fake_commands_records_calls_and_delegates() -> None:
    commands = FakeCommands()
    result = commands.run("echo hi", timeout=7, envs={"A": "1"}, user="root")
    assert result == FakeCommandResult("", "", 0)
    assert commands.calls[0]["cmd"] == "echo hi"
    assert commands.calls[0]["timeout"] == 7

    commands.handler = lambda cmd, **kwargs: FakeCommandResult("hi\n", "", 0)
    assert commands.run("echo hi").stdout == "hi\n"


def test_fake_sandbox_records_lifecycle() -> None:
    sandbox = FakeSdkSandbox("s1")

    sandbox.pause()
    sandbox.kill()
    sandbox.close()

    assert sandbox.paused is True
    assert sandbox.killed is True
    assert sandbox.closed is True


def test_fake_factory_records_create_connect_and_health(tmp_path: Path) -> None:
    factory = FakeSdkFactory(root_map={"/mnt/user-data": tmp_path / "user-data"})
    factory.existing = [
        {
            "sandboxID": "remote-1",
            "metadata": {"myflow": "1", "myflow.thread": "t1"},
        }
    ]

    assert factory.list_by_metadata({"myflow": "1", "myflow.thread": "t1"}) == (
        factory.existing
    )
    connected = factory.connect("remote-1")
    created = factory.create(
        template="tpl",
        timeout=60,
        metadata={"myflow": "1", "myflow.thread": "t2"},
        lifecycle={},
        network={},
        allow_internet_access=True,
    )
    factory.health()

    assert connected.sandbox_id == "remote-1"
    assert created.sandbox_id == "s1"
    assert factory.connected == ["remote-1"]
    assert factory.created == [created]
    assert factory.health_calls == 1


def test_fake_factory_kills_existing_metadata_row(tmp_path: Path) -> None:
    factory = FakeSdkFactory(root_map={"/mnt/user-data": tmp_path / "user-data"})
    row = {
        "sandboxID": "remote-1",
        "metadata": {"myflow": "1", "myflow.thread": "t1"},
    }
    factory.existing = [row]

    factory.connect("remote-1").kill()

    assert row["killed"] is True
    assert factory.list_by_metadata({"myflow": "1"}) == []


def test_fake_factory_fail_canary_corrupts_reads(tmp_path: Path) -> None:
    factory = FakeSdkFactory(root_map={"/mnt/user-data": tmp_path / "user-data"})
    factory.fail_canary = True
    sandbox = factory.create(
        template="tpl",
        timeout=60,
        metadata={"myflow": "1"},
        lifecycle={},
        network={},
        allow_internet_access=True,
    )
    (tmp_path / "user-data").mkdir(parents=True)
    (tmp_path / "user-data" / "probe").write_text("expected")

    assert sandbox.files.read("/mnt/user-data/probe", format="text") != "expected"


def _install_fake_cubesandbox(monkeypatch: pytest.MonkeyPatch) -> tuple[types.ModuleType, dict]:
    module = types.ModuleType("cubesandbox")
    calls: dict = {}

    class FakeConfig:
        def __init__(self, **kwargs: object) -> None:
            calls["config"] = kwargs
            calls.setdefault("config_objs", []).append(self)

    class FakeSandbox:
        @classmethod
        def create(cls, **kwargs: object) -> str:
            calls["create"] = kwargs
            return "created-sandbox"

        @classmethod
        def connect(cls, sandbox_id: str, **kwargs: object) -> str:
            calls["connect"] = {"sandbox_id": sandbox_id, **kwargs}
            return "connected-sandbox"

        @classmethod
        def health(cls, **kwargs: object) -> dict[str, str]:
            calls["health"] = kwargs
            return {"status": "ok"}

    module.Config = FakeConfig  # type: ignore[attr-defined]
    module.Sandbox = FakeSandbox  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cubesandbox", module)
    return module, calls
