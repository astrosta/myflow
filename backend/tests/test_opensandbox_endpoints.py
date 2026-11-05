"""Endpoint rewriting for OpenSandbox's server-authored data-plane addresses.

The server returns a host it can reach (for example host.docker.internal), not
necessarily the host the Gateway can reach. These tests drive the transport
wrapper that rewrites the host while preserving the server-published port and
path.
"""

from __future__ import annotations

import sys
import types

import httpx

from agentkit.sandbox.opensandbox.client import (
    SdkOsbClient,
    _EndpointRewriteTransport,
    _domain_netloc,
)


class _Recorder(httpx.BaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.closed = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, content=b"ok", request=request)

    def close(self) -> None:
        self.closed = True


def _call(transport: _EndpointRewriteTransport, url: str) -> httpx.Request:
    request = httpx.Request("GET", url)
    response = transport.handle_request(request)
    assert response.status_code == 200
    return request


def test_maps_the_server_host_and_preserves_port_and_path() -> None:
    inner = _Recorder()
    transport = _EndpointRewriteTransport(
        inner, host_map={"host.docker.internal": "127.0.0.1"}
    )

    request = _call(
        transport,
        "http://host.docker.internal:44772/proxy/44772/command",
    )

    assert str(request.url) == "http://127.0.0.1:44772/proxy/44772/command"
    assert request.headers["host"] == "127.0.0.1:44772"


def test_fallback_rewrites_data_plane_but_not_the_control_plane() -> None:
    inner = _Recorder()
    transport = _EndpointRewriteTransport(
        inner,
        fallback_host="127.0.0.1",
        control_netloc="opensandbox-server:8080",
    )

    control = _call(transport, "http://opensandbox-server:8080/v1/sandboxes")
    data = _call(transport, "http://host.docker.internal:18080/health")

    assert str(control.url) == "http://opensandbox-server:8080/v1/sandboxes"
    assert str(data.url) == "http://127.0.0.1:18080/health"
    assert data.headers["host"] == "127.0.0.1:18080"


def test_fallback_can_rewrite_a_data_port_on_the_control_host() -> None:
    inner = _Recorder()
    transport = _EndpointRewriteTransport(
        inner,
        fallback_host="127.0.0.1",
        control_netloc="opensandbox-server:8080",
    )

    data = _call(transport, "http://opensandbox-server:44772/command")

    assert str(data.url) == "http://127.0.0.1:44772/command"


def test_a_host_port_key_only_matches_that_endpoint() -> None:
    inner = _Recorder()
    transport = _EndpointRewriteTransport(
        inner,
        host_map={"host.docker.internal:44772": "127.0.0.1"},
    )

    mapped = _call(transport, "http://host.docker.internal:44772/command")
    untouched = _call(transport, "http://host.docker.internal:18080/health")

    assert str(mapped.url) == "http://127.0.0.1:44772/command"
    assert str(untouched.url) == "http://host.docker.internal:18080/health"


def test_close_closes_the_inner_transport() -> None:
    inner = _Recorder()
    transport = _EndpointRewriteTransport(inner, host_map={})
    transport.close()
    assert inner.closed is True


def test_domain_netloc_accepts_host_port_and_scheme_shapes() -> None:
    assert _domain_netloc("opensandbox-server:8080") == "opensandbox-server:8080"
    assert _domain_netloc("http://opensandbox-server:8080/v1") == "opensandbox-server:8080"
    assert _domain_netloc("[::1]:8080") == "::1:8080"


def test_client_keeps_the_rewrite_options() -> None:
    client = SdkOsbClient(
        "opensandbox-server:8080",
        endpoint_host="127.0.0.1",
        endpoint_host_map={"host.docker.internal": "127.0.0.1"},
        use_server_proxy=False,
    )

    assert client.endpoint_host == "127.0.0.1"
    assert client.endpoint_host_map == {"host.docker.internal": "127.0.0.1"}
    assert client.use_server_proxy is False

def test_connection_wraps_the_sdk_transport(monkeypatch) -> None:
    created: list[object] = []
    inner = _Recorder()

    class FakeConnectionConfig:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            created.append(self)

        def new_owned_transport(self) -> _Recorder:
            return inner

    package = types.ModuleType("opensandbox")
    config_module = types.ModuleType("opensandbox.config")
    config_module.ConnectionConfigSync = FakeConnectionConfig  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opensandbox", package)
    monkeypatch.setitem(sys.modules, "opensandbox.config", config_module)

    client = SdkOsbClient(
        "opensandbox-server:8080",
        endpoint_host="127.0.0.1",
        use_server_proxy=False,
    )
    config = client._connection()

    assert config.kwargs["domain"] == "opensandbox-server:8080"
    assert config.kwargs["use_server_proxy"] is False
    assert isinstance(config.kwargs["transport"], _EndpointRewriteTransport)
    client.close()
    assert inner.closed is True
