"""E2B protocol descriptors, explicit sync, and the SDK-backed sandbox.

The package stays import-light: nothing here imports the optional e2b/redis
extras at module scope, so configuration and a local-only deployment can import
it without the E2B SDK installed.
"""

from agentkit.sandbox.e2b.provider import E2BSandboxProvider
from agentkit.sandbox.e2b.sandbox import E2BSandbox
from agentkit.sandbox.e2b.sync import (
    LazySkillUploader,
    SkillSource,
    SkillUploadTooLarge,
    SyncLimits,
    SyncStats,
    download_tree,
    ensure_container_dirs,
    upload_tree,
)

__all__ = [
    "E2BSandbox",
    "E2BSandboxProvider",
    "LazySkillUploader",
    "SkillSource",
    "SkillUploadTooLarge",
    "SyncLimits",
    "SyncStats",
    "download_tree",
    "ensure_container_dirs",
    "upload_tree",
]
