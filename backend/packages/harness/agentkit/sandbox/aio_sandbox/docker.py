"""Docker control-plane helpers for the AIO sandbox provider.

Two things live here that must be right before any container exists:

1. Topology (this module's first half): the Gateway process may run on the host
   or inside a container with the docker socket mounted. That decides which
   address the sandbox port must be published on, and which address the Gateway
   must dial. Both are wrong in a way that is invisible until the first command
   times out, so resolution happens at startup and fails loudly.
2. The container lifecycle (added with the DockerClient below).

Nothing here imports docker SDKs: the provider drives the docker CLI, which the
deployment already needs.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agentkit.config.sandbox_config import AioSandboxConfig
from agentkit.sandbox.exceptions import SandboxRuntimeError

logger = logging.getLogger(__name__)

AUTO = "auto"
_LOOPBACK = "127.0.0.1"
_CONTAINER_MARKERS = ("docker", "kubepods", "containerd", "lxc")
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True, slots=True)
class Topology:
    """Where the sandbox port is published, and how the Gateway reaches it."""

    in_container: bool
    publish_host: str
    connect_host: str


def detect_in_container() -> bool:
    """Whether this process runs inside a container.

    MYFLOW_IN_CONTAINER overrides the probe, because a deployment can run in a
    container whose cgroup file says nothing useful (systemd scopes, gVisor,
    rootless runtimes).
    """

    override = os.environ.get("MYFLOW_IN_CONTAINER", "").strip().lower()
    if override in _TRUE_VALUES:
        return True
    if override in _FALSE_VALUES:
        return False
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(marker in cgroup for marker in _CONTAINER_MARKERS)


def resolve_topology(
    config: AioSandboxConfig,
    *,
    in_container: bool | None = None,
    gateway_lookup: Callable[[], str | None] | None = None,
) -> Topology:
    """Resolve publish_host / connect_host from the configured values.

    Rules, in order:

    1. Both ends configured: use them verbatim.
    2. One end configured: the auto end mirrors it. The pair describes one
       reachable route, so a split would point the daemon and the client at
       different addresses.
    3. Both auto, host Gateway: loopback.
    4. Both auto, containerized Gateway: the Docker bridge gateway address. It
       is the host as seen from the sandbox network, so publishing there and
       dialing there is the same route in both directions.

    Case 4 with no discoverable gateway raises: guessing loopback would make the
    container unreachable, and falling back to 0.0.0.0 would put a root shell on
    every host interface.
    """

    inside = detect_in_container() if in_container is None else in_container
    publish = config.publish_host
    connect = config.connect_host

    if publish != AUTO and connect != AUTO:
        return Topology(inside, publish, connect)

    if publish != AUTO:
        return Topology(inside, publish, publish)
    if connect != AUTO:
        return Topology(inside, connect, connect)

    if not inside:
        return Topology(False, _LOOPBACK, _LOOPBACK)

    gateway = gateway_lookup() if gateway_lookup is not None else None
    if not gateway:
        raise SandboxRuntimeError(
            "cannot resolve the Docker bridge gateway for the AIO sandbox: this "
            "Gateway runs in a container, so sandbox.aio.publish_host and "
            "sandbox.aio.connect_host cannot both stay 'auto'. Set both explicitly "
            "(for example the gateway address this container can reach, usually "
            "the docker0 address such as 172.17.0.1) and restart."
        )
    return Topology(True, gateway, gateway)


__all__ = [
    "AUTO",
    "Topology",
    "detect_in_container",
    "resolve_topology",
]
