"""In-memory docker CLI for the AIO sandbox provider tests.

The fake replays the small slice of docker this provider uses: version, image
inspect, network inspect, run, inspect, start, stop, rm -f and ps. It records
every argv so a test can assert on the exact command the provider would run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agentkit.sandbox.aio_sandbox.docker import CommandResult


@dataclass
class FakeContainer:
    container_id: str
    name: str
    image: str
    labels: dict[str, str] = field(default_factory=dict)
    mounts: list[str] = field(default_factory=list)
    publish_host: str = ""
    container_port: int = 8080
    host_port: int = 0
    running: bool = True
    env: dict[str, str] = field(default_factory=dict)
    cpus: str | None = None
    memory: str | None = None
    pids_limit: str | None = None
    network: str | None = None


class FakeDocker:
    """A docker CLI stand-in. Call it to get a DockerRunner."""

    def __init__(
        self,
        *,
        gateway: str | None = "172.17.0.1",
        image_present: bool = True,
        first_port: int = 40000,
        version: str = "29.0.0",
    ) -> None:
        self.calls: list[list[str]] = []
        self.containers: dict[str, FakeContainer] = {}
        self.gateway = gateway
        self.image_present = image_present
        self.version = version
        self.fail_run: str | None = None
        self._next_port = first_port
        self._next_id = 1

    # -- runner ---------------------------------------------------------

    def __call__(self, argv: list[str], timeout: float | None = None) -> CommandResult:
        del timeout
        self.calls.append(list(argv))
        args = list(argv)[1:]
        if not args:
            return _ok("")

        command = args[0]
        if command == "version":
            return _ok(self.version + "\n")
        if command == "image":
            return _ok("[]") if self.image_present else _err("Error: No such image")
        if command == "network":
            if self.gateway is None:
                return _err("Error: no such network: bridge")
            return _ok(self.gateway + "\n")
        if command == "run":
            return self._run(args[1:])
        if command == "inspect":
            return self._inspect(args[1])
        if command == "start":
            return self._start(args[1])
        if command == "stop":
            return self._mutate(args[-1], running=False)
        if command == "rm":
            return self._remove(args[-1])
        if command == "ps":
            return self._ps(args)
        return _err(f"Error: unsupported fake command: {command}")

    # -- commands -------------------------------------------------------

    def _run(self, args: list[str]) -> CommandResult:
        if self.fail_run is not None:
            return _err(self.fail_run)

        name = ""
        labels: dict[str, str] = {}
        mounts: list[str] = []
        env: dict[str, str] = {}
        publish_host = ""
        container_port = 0
        container: dict[str, Any] = {}

        index = 0
        while index < len(args):
            arg = args[index]
            if arg == "--name":
                name = args[index + 1]
                index += 2
            elif arg == "--label":
                key, _, value = args[index + 1].partition("=")
                labels[key] = value
                index += 2
            elif arg == "--mount":
                mounts.append(args[index + 1])
                index += 2
            elif arg in {"-p", "--publish"}:
                publish_host, _, port = args[index + 1].rpartition("::")
                container_port = int(port)
                index += 2
            elif arg in {"-e", "--env"}:
                key, _, value = args[index + 1].partition("=")
                env[key] = value
                index += 2
            elif arg in {"--cap-drop", "--security-opt", "--memory", "--cpus", "--pids-limit"}:
                key = arg.lstrip("-").replace("-", "_")
                container[key] = args[index + 1]
                index += 2
            elif arg.startswith("--cap-drop="):
                container["cap_drop"] = arg.split("=", 1)[1]
                index += 1
            elif arg == "--network":
                container["network"] = args[index + 1]
                index += 2
            else:
                # Anything else is either a flag this fake ignores or the image.
                if index == len(args) - 1:
                    container["image"] = arg
                index += 1

        if name in self.containers:
            return _err(
                "docker: Error response from daemon: Conflict. The container name "
                f'"/{name}" is already in use by container "abc".'
            )

        container_id = f"cid{self._next_id:03d}"
        self._next_id += 1
        created = FakeContainer(
            container_id=container_id,
            name=name,
            image=str(container.get("image", "")),
            labels=labels,
            mounts=mounts,
            publish_host=publish_host,
            container_port=container_port,
            host_port=self._next_port,
            env=env,
            cpus=container.get("cpus"),
            memory=container.get("memory"),
            pids_limit=container.get("pids_limit"),
            network=container.get("network"),
        )
        self._next_port += 1
        self.containers[name] = created
        return _ok(container_id + "\n")

    def _inspect(self, target: str) -> CommandResult:
        container = self._find(target)
        if container is None:
            return _err(f"Error response from daemon: No such object: {target}")
        payload = [
            {
                "Id": container.container_id,
                "Name": "/" + container.name,
                "Config": {"Labels": dict(container.labels)},
                "State": {"Running": container.running},
                "NetworkSettings": {
                    "Ports": {
                        f"{container.container_port}/tcp": [
                            {"HostIp": container.publish_host,
                             "HostPort": str(container.host_port)}
                        ]
                    }
                },
            }
        ]
        return _ok(json.dumps(payload))

    def _start(self, target: str) -> CommandResult:
        container = self._find(target)
        if container is None:
            return _err(f"Error response from daemon: No such object: {target}")
        container.running = True
        return _ok(target + "\n")

    def _mutate(self, target: str, *, running: bool) -> CommandResult:
        container = self._find(target)
        if container is None:
            return _err(f"Error response from daemon: No such object: {target}")
        container.running = running
        return _ok(target + "\n")

    def _remove(self, target: str) -> CommandResult:
        container = self._find(target)
        if container is None:
            return _err(f"Error response from daemon: No such object: {target}")
        self.containers.pop(container.name, None)
        return _ok(target + "\n")

    def _ps(self, args: list[str]) -> CommandResult:
        wanted: dict[str, str] = {}
        index = 0
        while index < len(args):
            if args[index] == "--filter":
                kind, _, payload = args[index + 1].partition("=")
                if kind == "label":
                    key, _, value = payload.partition("=")
                    wanted[key] = value
                index += 2
            else:
                index += 1
        lines = [
            container.container_id
            for container in self.containers.values()
            if all(container.labels.get(key) == value for key, value in wanted.items())
        ]
        return _ok("".join(line + "\n" for line in lines))

    # -- helpers --------------------------------------------------------

    def _find(self, target: str) -> FakeContainer | None:
        if target in self.containers:
            return self.containers[target]
        for container in self.containers.values():
            if container.container_id == target:
                return container
        return None

    def find(self, name: str) -> FakeContainer | None:
        return self.containers.get(name)


def _ok(stdout: str) -> CommandResult:
    return CommandResult(stdout=stdout, stderr="", returncode=0)


def _err(stderr: str) -> CommandResult:
    return CommandResult(stdout="", stderr=stderr, returncode=1)


__all__ = ["FakeContainer", "FakeDocker"]
