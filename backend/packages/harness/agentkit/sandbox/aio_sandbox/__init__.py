"""AIO sandbox provider: Docker container isolation for the tool sandbox.

The provider path is agentkit.sandbox.aio_sandbox:AioSandboxProviderDocker. The
data plane talks to the all-in-one-sandbox HTTP API through agent-sandbox; the
control plane drives the docker CLI. Both stay behind a narrow boundary
(docker.py / client.py) so tests never need a real daemon.
"""

from __future__ import annotations

__all__: list[str] = []
