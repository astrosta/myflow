"""AIO sandbox provider: Docker container isolation for the tool sandbox.

The provider path is agentkit.sandbox.aio_sandbox:AioSandboxProviderDocker. The
data plane talks to the all-in-one-sandbox HTTP API through agent-sandbox; the
control plane drives the docker CLI. Both stay behind narrow boundaries
(client.py / docker.py) so tests never need a real daemon.
"""

from __future__ import annotations

from agentkit.sandbox.aio_sandbox.provider import AioSandboxProviderDocker

__all__ = ["AioSandboxProviderDocker"]
