"""OpenSandbox provider: Docker/Kubernetes sandboxes through a sandbox server.

The provider path is agentkit.sandbox.opensandbox:OpenSandboxProvider. The data
plane is the official opensandbox SDK (sync), the control plane is
opensandbox-server, and both stay behind client.py so tests need neither.
"""

from __future__ import annotations

from agentkit.sandbox.opensandbox.provider import OpenSandboxProvider

__all__ = ["OpenSandboxProvider"]
