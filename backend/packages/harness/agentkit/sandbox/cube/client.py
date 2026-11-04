"""SDK boundary for the CubeSandbox control plane and per-sandbox APIs.

The CubeSandbox package is an optional dependency. It is imported only inside
the methods that need it, so a local-only deployment can import the sandbox
package without installing agentkit[cube].

The Protocols here are the narrow surface CubeSandboxProvider and CubeSandbox
depend on. The fake in tests/fakes/cube_sdk.py implements the same surface.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

_DEFAULT_STATE_FILTER = "running,paused"
# Only a forbidden metadata query degrades to client-side filtering. Missing or
# rejected credentials (401) and server faults (5xx) must still surface.
_FORBIDDEN_STATUS = 403

logger = logging.getLogger(__name__)


class SdkSandbox(Protocol):
    """The subset of cubesandbox.Sandbox used by this project."""

    sandbox_id: str
    commands: Any
    files: Any

    def kill(self) -> None: ...

    def pause(self) -> None: ...

    def close(self) -> None: ...


class SdkFactory(Protocol):
    """The CubeSandbox SDK operations the provider needs."""

    def create(
        self,
        *,
        template: str,
        timeout: int,
        metadata: dict[str, str],
        lifecycle: dict,
        network: dict,
        allow_internet_access: bool,
    ) -> SdkSandbox: ...

    def connect(self, sandbox_id: str) -> SdkSandbox: ...

    def list_by_metadata(self, metadata: Mapping[str, str]) -> list[dict]: ...

    def health(self) -> None: ...


def build_metadata_query(metadata: Mapping[str, str]) -> str:
    """Render metadata as un-nested query pairs (x-www-form-urlencoded)."""

    return urlencode(list(metadata.items()))


def _row_matches_metadata(row: object, metadata: Mapping[str, str]) -> bool:
    """Exact, string-compared metadata match for the fallback list."""

    if not isinstance(row, dict):
        return False
    found = row.get("metadata")
    if not isinstance(found, Mapping):
        return False
    return all(
        str(found.get(key)) == str(value) for key, value in metadata.items()
    )


def error_status(exc: BaseException) -> int | None:
    """Return an SDK/HTTP status code attached to an exception, if any."""

    status = getattr(exc, "status_code", None)
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


class CubesandboxFactory:
    """Thin adapter around cubesandbox.Config/Sandbox plus the v2 list API.

    One factory instance is cheap and reusable. The optional http_client is
    injected by tests and is never closed by this class; a client created by
    list_by_metadata is scoped to that call.
    """

    def __init__(
        self,
        api_url: str,
        api_key: str,
        template_id: str,
        sandbox_domain: str,
        proxy_node_ip: str | None,
        proxy_port: int,
        request_timeout: float,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.template_id = template_id
        self.sandbox_domain = sandbox_domain
        self.proxy_node_ip = proxy_node_ip
        self.proxy_port = proxy_port
        self.request_timeout = request_timeout
        self._http_client = http_client

    def create(
        self,
        *,
        template: str,
        timeout: int,
        metadata: dict[str, str],
        lifecycle: dict,
        network: dict,
        allow_internet_access: bool,
    ) -> SdkSandbox:
        sdk = self._import_sdk()
        return sdk.Sandbox.create(
            template=template,
            timeout=timeout,
            metadata=metadata,
            lifecycle=lifecycle,
            network=network,
            allow_internet_access=allow_internet_access,
            config=self._sdk_config(sdk),
        )

    def connect(self, sandbox_id: str) -> SdkSandbox:
        sdk = self._import_sdk()
        return sdk.Sandbox.connect(sandbox_id, config=self._sdk_config(sdk))

    def list_by_metadata(self, metadata: Mapping[str, str]) -> list[dict]:
        """List sandboxes matching every metadata pair, following pagination.

        CubeAPI expects one metadata query parameter whose value is the
        key=value pairs joined by ampersands:
        ?metadata=myflow%3D1%26myflow.thread%3Dt1&state=running,paused.
        The SDK's Sandbox.list_v2 does not expose that filter, so this method
        talks to the v2 endpoint directly. x-next-token carries the next page
        cursor back as the nextToken query parameter.

        When the deployment forbids the metadata query (HTTP 403), discovery
        degrades instead of blocking the run: one warning, one unfiltered
        Sandbox.list_v2 call, and the same exact-pair match applied here.
        """

        headers: dict[str, str] = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        params = {
            "metadata": build_metadata_query(metadata),
            "state": _DEFAULT_STATE_FILTER,
        }

        results: list[dict] = []
        next_token: str | None = None
        seen_tokens: set[str] = set()

        while True:
            request_params = dict(params)
            if next_token is not None:
                request_params["nextToken"] = next_token

            response = self._get("/v2/sandboxes", headers, request_params)
            if response.status_code == _FORBIDDEN_STATUS:
                return self._list_v2_by_metadata(metadata)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("CubeAPI /v2/sandboxes did not return a JSON list")
            results.extend(payload)

            next_token = response.headers.get("x-next-token")
            next_token = next_token.strip() if next_token else None
            if not next_token:
                return results
            if next_token in seen_tokens:
                raise RuntimeError(
                    f"CubeAPI repeated pagination token {next_token!r}"
                )
            seen_tokens.add(next_token)

    def _list_v2_by_metadata(self, metadata: Mapping[str, str]) -> list[dict]:
        """Degraded discovery: one unfiltered list_v2 call, filtered here."""

        logger.warning(
            "CubeAPI returned HTTP %s for /v2/sandboxes metadata discovery; "
            "falling back to Sandbox.list_v2() with client-side filtering. "
            "Grant the API key permission to list sandboxes by metadata to "
            "restore server-side filtering.",
            _FORBIDDEN_STATUS,
        )
        sdk = self._import_sdk()
        rows = sdk.Sandbox.list_v2(config=self._sdk_config(sdk))
        if not isinstance(rows, list):
            raise ValueError("CubeSandbox Sandbox.list_v2 did not return a JSON list")
        return [row for row in rows if _row_matches_metadata(row, metadata)]

    def health(self) -> None:
        sdk = self._import_sdk()
        sdk.Sandbox.health(config=self._sdk_config(sdk))

    def _get(
        self,
        path: str,
        headers: dict[str, str],
        params: dict[str, str],
    ) -> httpx.Response:
        if self._http_client is not None:
            return self._http_client.get(
                f"{self.api_url}{path}",
                headers=headers,
                params=params,
            )
        with httpx.Client(
            base_url=self.api_url,
            timeout=self.request_timeout,
            headers=headers,
        ) as client:
            return client.get(path, params=params)

    @staticmethod
    def _import_sdk() -> Any:
        import cubesandbox

        return cubesandbox

    def _sdk_config(self, sdk: Any) -> Any:
        return sdk.Config(
            api_url=self.api_url,
            api_key=self.api_key or None,
            template_id=self.template_id,
            sandbox_domain=self.sandbox_domain,
            proxy_node_ip=self.proxy_node_ip,
            proxy_port=self.proxy_port,
            request_timeout=self.request_timeout,
        )


__all__ = [
    "CubesandboxFactory",
    "SdkFactory",
    "SdkSandbox",
    "build_metadata_query",
    "error_status",
]
