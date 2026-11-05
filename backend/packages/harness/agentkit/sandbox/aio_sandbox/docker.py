"""Docker control-plane helpers for the AIO sandbox provider.

Two things live here that must be right before any container exists:

1. Topology (this module's first half): the Gateway process may run on the host
   or inside a container with the docker socket mounted. That decides which
   address the sandbox port must be published on, and which address the Gateway
   must dial. Both are wrong in a way that is invisible until the first command
   times out, so resolution happens at startup and fails loudly.
2. The container lifecycle (DockerClient): create, inspect, start, stop, remove,
   and label-based discovery.

Nothing here imports docker SDKs: the provider drives the docker CLI, which the
deployment already needs.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agentkit.config.sandbox_config import AioSandboxConfig
from agentkit.sandbox.exceptions import SandboxRuntimeError
from agentkit.sandbox.host_mounts import MountEntry

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


# --------------------------------------------------------------------------
# Container control plane
# --------------------------------------------------------------------------

#: Docker's answer when a container name is taken by another container.
_NAME_IN_USE_MARKERS = (
    "is already in use by container",
    "conflict. the container name",
)
#: Docker's answer when a container id/name does not exist (any casing).
_NOT_FOUND_MARKERS = ("no such object", "no such container")


@dataclass(frozen=True, slots=True)
class CommandResult:
    """One finished docker CLI invocation."""

    stdout: str
    stderr: str
    returncode: int


DockerRunner = Callable[[Sequence[str], "float | None"], CommandResult]


class DockerContainerError(SandboxRuntimeError):
    """A docker CLI call failed.

    not_found means the container is gone (the provider must drop its handle);
    name_in_use means another container already owns the name (the provider
    must adopt or replace it rather than treating it as a transport failure).
    """

    def __init__(
        self,
        message: str,
        *,
        not_found: bool = False,
        name_in_use: bool = False,
    ) -> None:
        super().__init__(message)
        self.not_found = not_found
        self.name_in_use = name_in_use


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    """Everything one docker run needs."""

    name: str
    image: str
    labels: Mapping[str, str]
    mounts: Sequence["MountEntry"]
    publish_host: str
    container_port: int
    env: Mapping[str, str] = field(default_factory=dict)
    network_mode: str = "bridge"
    network_name: str | None = None
    cpus: float | None = None
    memory: str | None = None
    pids_limit: int | None = None

    def run_argv(self) -> list[str]:
        """Build the docker run argv.

        --mount is used instead of -v because a bind source may contain a colon
        or a comma. Loopback-only publication is the caller's decision
        (publish_host); this method never widens it.
        """

        argv = [
            "run",
            "-d",
            "--name",
            self.name,
            "--cap-drop=ALL",
            "--security-opt",
            "no-new-privileges",
        ]
        for key in sorted(self.labels):
            argv.extend(["--label", f"{key}={self.labels[key]}"])
        for entry in self.mounts:
            spec = f"type=bind,source={entry.host_path},target={entry.container_path}"
            if entry.read_only:
                spec += ",readonly"
            argv.extend(["--mount", spec])
        argv.extend(["-p", f"{self.publish_host}::{self.container_port}"])
        if self.memory:
            argv.extend(["--memory", self.memory])
        if self.cpus:
            argv.extend(["--cpus", f"{self.cpus:g}"])
        if self.pids_limit:
            argv.extend(["--pids-limit", str(self.pids_limit)])
        if self.network_name:
            argv.extend(["--network", self.network_name])
        elif self.network_mode == "none":
            argv.extend(["--network", "none"])
        for key in sorted(self.env):
            argv.extend(["-e", f"{key}={self.env[key]}"])
        argv.append(self.image)
        return argv


@dataclass(frozen=True, slots=True)
class ContainerState:
    """The slice of docker inspect this provider reads."""

    container_id: str
    name: str
    labels: Mapping[str, str]
    running: bool
    host_port: int | None


def subprocess_runner(argv: Sequence[str], timeout: float | None = None) -> CommandResult:
    """Run the docker CLI without a shell."""

    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SandboxRuntimeError(
            "the docker CLI is not installed or not on PATH; the AIO sandbox "
            "provider needs it to manage sandbox containers"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxRuntimeError(
            f"docker {argv[0] if argv else ''} did not finish within {timeout}s"
        ) from exc
    return CommandResult(
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        returncode=int(completed.returncode),
    )


class DockerClient:
    """The docker CLI surface the provider depends on."""

    def __init__(
        self,
        *,
        docker_bin: str = "docker",
        runner: DockerRunner | None = None,
        run_timeout: float = 300.0,
        timeout: float = 60.0,
    ) -> None:
        self._bin = docker_bin
        self._runner: DockerRunner = runner or subprocess_runner
        self._run_timeout = run_timeout
        self._timeout = timeout

    # -- daemon and images ---------------------------------------------

    def version(self) -> str:
        result = self._call(["version", "--format", "{{.Server.Version}}"])
        return result.stdout.strip()

    def image_exists(self, image: str) -> bool:
        result = self._raw(["image", "inspect", image])
        return result.returncode == 0

    def bridge_gateway(self) -> str | None:
        """The docker0 gateway address, or None when it cannot be read."""

        result = self._raw(
            ["network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"]
        )
        if result.returncode != 0:
            logger.debug("docker network inspect bridge failed: %s", result.stderr.strip())
            return None
        gateway = result.stdout.strip()
        return gateway or None

    # -- lifecycle ------------------------------------------------------

    def run(self, spec: ContainerSpec) -> str:
        """Create and start one container; returns the new container id."""

        result = self._call(spec.run_argv(), timeout=self._run_timeout)
        return result.stdout.strip()

    def inspect(self, name: str, *, container_port: int) -> ContainerState:
        """Inspect one container, translating docker's errors into flags."""

        result = self._raw(["inspect", name])
        if result.returncode != 0:
            raise _container_error(result, f"docker inspect {name} failed")
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            raise DockerContainerError(
                f"docker inspect {name} returned unparseable JSON: {exc}"
            ) from exc
        if not isinstance(payload, list) or not payload:
            raise DockerContainerError(f"docker inspect {name} returned no container")
        entry = payload[0]
        if not isinstance(entry, dict):
            raise DockerContainerError(f"docker inspect {name} returned a malformed entry")
        return ContainerState(
            container_id=str(entry.get("Id") or ""),
            name=str(entry.get("Name") or "").lstrip("/"),
            labels=dict((entry.get("Config") or {}).get("Labels") or {}),
            running=bool((entry.get("State") or {}).get("Running")),
            host_port=_host_port(entry, container_port),
        )

    def start(self, name: str) -> None:
        self._call(["start", name])

    def stop(self, name: str, *, timeout: float = 10.0) -> None:
        self._tolerate_absent(["stop", "--time", f"{timeout:g}", name], name)

    def remove(self, name: str) -> None:
        self._tolerate_absent(["rm", "-f", name], name)

    def list_by_labels(self, labels: Mapping[str, str]) -> list[ContainerState]:
        """Container ids matching every label, in creation order."""

        argv = ["ps", "-a", "--format", "{{.ID}}"]
        for key in sorted(labels):
            argv.extend(["--filter", f"label={key}={labels[key]}"])
        result = self._call(argv)
        ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        states: list[ContainerState] = []
        for container_id in ids:
            try:
                states.append(self.inspect(container_id, container_port=0))
            except DockerContainerError as exc:
                if not exc.not_found:
                    raise
        return states

    # -- internals ------------------------------------------------------

    def _call(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        result = self._raw(argv, timeout=timeout)
        if result.returncode != 0:
            raise _container_error(result, f"docker {argv[0] if argv else ''} failed")
        return result

    def _raw(
        self, argv: Sequence[str], *, timeout: float | None = None
    ) -> CommandResult:
        return self._runner([self._bin, *argv], timeout or self._timeout)

    def _tolerate_absent(self, argv: Sequence[str], name: str) -> None:
        result = self._raw(argv)
        if result.returncode == 0:
            return
        if _is_not_found(result.stderr, name):
            return
        raise _container_error(result, f"docker {argv[0]} {name} failed")


def _host_port(entry: Mapping[str, object], container_port: int) -> int | None:
    if container_port <= 0:
        return None
    settings = entry.get("NetworkSettings")
    if not isinstance(settings, Mapping):
        return None
    ports = settings.get("Ports")
    if not isinstance(ports, Mapping):
        return None
    bindings = ports.get(f"{container_port}/tcp")
    if not isinstance(bindings, list):
        return None
    for binding in bindings:
        if not isinstance(binding, Mapping):
            continue
        raw = binding.get("HostPort")
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            continue
    return None


def _is_not_found(stderr: str, name: str) -> bool:
    text = stderr.lower()
    if not any(marker in text for marker in _NOT_FOUND_MARKERS):
        return False
    # Compare the resource name byte-exact but case-insensitively, because CLI
    # v29 lowercases the scaffolding ("error: no such object: <name>").
    return name.lower() in text


def _container_error(result: CommandResult, prefix: str) -> DockerContainerError:
    detail = (result.stderr or result.stdout).strip() or f"exit code {result.returncode}"
    lowered = detail.lower()
    return DockerContainerError(
        f"{prefix}: {detail}",
        not_found=any(marker in lowered for marker in _NOT_FOUND_MARKERS),
        name_in_use=any(marker in lowered for marker in _NAME_IN_USE_MARKERS),
    )


__all__ = [
    "AUTO",
    "CommandResult",
    "ContainerSpec",
    "ContainerState",
    "DockerClient",
    "DockerContainerError",
    "DockerRunner",
    "Topology",
    "detect_in_container",
    "resolve_topology",
    "subprocess_runner",
]
