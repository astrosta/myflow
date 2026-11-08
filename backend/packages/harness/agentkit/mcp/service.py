"""MCP server management service: URL policy, owner scope and CRUD orchestration.

The service is the seam between the HTTP API and the pieces Task 1/2 built: it
validates the user's configuration, encrypts headers before they reach the store,
and asks the sync service to discover tools after every write. It never returns
plaintext headers and every read is owner-scoped, because an MCP server is private
to the user that created it.

A failed sync is not a failed write: the configuration stays saved, the row is
marked status=error/reachable=false with a sanitized last_error, and the user can
correct the URL or headers and refresh.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.exc import IntegrityError

from agentkit.mcp.crypto import (
    MAX_HEADERS_TOTAL_BYTES,
    HeaderCipher,
    validate_headers,
)
from agentkit.mcp.errors import (
    McpServerConfigurationError,
    McpServerConflict,
    McpServerError,
    McpServerNotFound,
    McpServerValidationError,
)
from agentkit.mcp.models import (
    McpServerRecord,
    truncate_error,
    validate_server_name,
    validate_transport,
)

logger = logging.getLogger(__name__)

MASK = "****"
MAX_SERVER_URL_CHARS = 2048

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "x-access-token",
    }
)


class _UnsetType:
    """Marks a field the caller did not send, distinct from an explicit None."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "UNSET"


UNSET = _UnsetType()


@dataclass(frozen=True, slots=True)
class McpServerWrite:
    """The editable server configuration.

    name is accepted on create and checked against the stored slug on update;
    it is never written back. Optional fields use UNSET so an update can leave a
    column alone while an explicit headers=None clears it.
    """

    name: str | None = None
    display_name: str | None | _UnsetType = UNSET
    description: str | _UnsetType = UNSET
    url: str | _UnsetType = UNSET
    transport: str | _UnsetType = UNSET
    headers: Mapping[str, str] | None | _UnsetType = UNSET
    verify_tls: bool | _UnsetType = UNSET
    enabled: bool | _UnsetType = UNSET


@dataclass(frozen=True, slots=True)
class McpServerReindexStats:
    """The sync service's real counters for one owner-scoped reindex."""

    scanned: int = 0
    changed: int = 0
    upserted: int = 0
    deleted: int = 0
    failed: int = 0
    duration_ms: int = 0
    error: str | None = None


def url_without_query(url: str | None) -> str:
    """The endpoint without its query string: what the management API may show."""
    text = str(url or "")
    if not text:
        return ""
    parts = urlsplit(text)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def url_has_query(url: str | None) -> bool:
    """True when the stored endpoint carries a query string (never returned)."""
    text = str(url or "")
    return bool(text and urlsplit(text).query)


def merge_url_query(incoming: str, stored: str | None) -> str:
    """Keep the stored query when an edit submits a URL without one."""
    incoming_parts = urlsplit(incoming)
    if incoming_parts.query:
        return incoming
    stored_query = urlsplit(str(stored or "")).query
    if not stored_query:
        return incoming
    return urlunsplit(
        (
            incoming_parts.scheme,
            incoming_parts.netloc,
            incoming_parts.path,
            stored_query,
            "",
        )
    )


class ServerUrlPolicy:
    """The SSRF gate for MCP endpoints.

    Only configured schemes are accepted. Userinfo and fragments are always
    rejected (they hide a different destination or are never sent to the server),
    while path and query are kept because many MCP endpoints need them. Private,
    loopback and link-local literals are rejected unless the deployment explicitly
    opts in with allow_private_networks.
    """

    def __init__(
        self,
        allowed_schemes: tuple[str, ...] | list[str] | None = None,
        *,
        allow_private_networks: bool = False,
        max_servers_per_user: int = 20,
        enabled: bool = True,
    ) -> None:
        schemes = ("https",) if allowed_schemes is None else allowed_schemes
        self.allowed_schemes = tuple(
            str(scheme).strip().lower() for scheme in schemes
        )
        self.allow_private_networks = bool(allow_private_networks)
        self.max_servers_per_user = int(max_servers_per_user)
        self.enabled = bool(enabled)

    def validate_url(self, url: str | None) -> str:
        """Return the checked URL, or raise McpServerValidationError."""
        text = str(url or "").strip()
        if not text:
            raise McpServerValidationError("MCP server url is required")
        if len(text) > MAX_SERVER_URL_CHARS:
            raise McpServerValidationError(
                f"MCP server url must be at most {MAX_SERVER_URL_CHARS} characters"
            )
        if _CONTROL.search(text) or " " in text:
            raise McpServerValidationError(
                "MCP server url must not contain whitespace or control characters"
            )
        try:
            parts = urlsplit(text)
            parts.port
        except ValueError:
            raise McpServerValidationError("MCP server url has an invalid port") from None

        scheme = parts.scheme.lower()
        if scheme not in self.allowed_schemes:
            raise McpServerValidationError(
                "MCP server url scheme must be one of "
                f"{list(self.allowed_schemes)}, got {scheme or '<none>'!r}"
            )
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise McpServerValidationError(
                "MCP server url must not contain userinfo (user:password@host)"
            )
        if parts.fragment or "#" in text:
            raise McpServerValidationError("MCP server url must not contain a fragment")

        host = (parts.hostname or "").strip().rstrip(".").lower()
        if not host:
            raise McpServerValidationError("MCP server url must include a host")
        if not self.allow_private_networks and self._is_private_target(host):
            raise McpServerValidationError(
                "MCP server url points at localhost, a loopback/link-local address "
                "or a private network; set mcp_servers.allow_private_networks=true "
                "to allow it"
            )
        return text

    # Readable aliases: callers and tests use validate or check too.
    validate = validate_url
    check = validate_url
    validate_server_url = validate_url

    @staticmethod
    def _is_private_target(host: str) -> bool:
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
            return True
        if "%" in host:  # IPv6 zone id: link-local by definition
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Non-canonical IPv4 forms (127.1, 0177.0.0.1, 2130706433,
            # 0x7f000001) resolve to loopback in common HTTP stacks even though
            # ipaddress refuses them. Treat address-shaped labels as targets.
            labels = host.split(".")
            return bool(labels) and all(
                re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)", label) for label in labels
            )
        return bool(
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_unspecified
            or address.is_reserved
            or address.is_multicast
        )


class McpServerSync(Protocol):
    """The discovery side effect of a write; Task 5 supplies the real one."""

    async def sync_server(
        self, server_id: str, reason: str = "server_refresh"
    ) -> Any: ...

    async def remove_server_catalog(self, server_id: str) -> Any: ...

    async def reindex(self, owner_user_id: str) -> McpServerReindexStats: ...


class McpToolStore(Protocol):
    """The read-only tool catalog dependency; Task 4 supplies the real one."""

    async def list_for_server(
        self, server_id: str, owner_user_id: str
    ) -> list[Any]: ...


def is_sensitive_header(name: str) -> bool:
    return name.strip().lower() in _SENSITIVE_HEADER_NAMES


def mask_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Mask sensitive values while preserving non-secret headers verbatim."""
    masked: dict[str, str] = {}
    for key, value in (headers or {}).items():
        key_text = str(key)
        masked[key_text] = (
            MASK if is_sensitive_header(key_text) and value else str(value)
        )
    return masked


def restore_masked_headers(
    incoming: Mapping[str, str] | None,
    stored: Mapping[str, str] | None,
) -> dict[str, str]:
    """Replace **** with the stored value; an unknown mask is a 422.

    Header names are case-insensitive, so the lookup is too: a client that sends
    authorization: **** against a stored Authorization still gets the original
    credential, not a literal sentinel.
    """
    stored_map = {str(key): str(value) for key, value in (stored or {}).items()}
    stored_by_lower = {key.lower(): key for key in stored_map}
    restored: dict[str, str] = {}
    for key, value in (incoming or {}).items():
        key_text = str(key)
        if str(value) == MASK:
            stored_key = stored_by_lower.get(key_text.strip().lower())
            if stored_key is None:
                raise McpServerValidationError(
                    f"header {key_text} is masked but no stored value exists"
                )
            restored[stored_key] = stored_map[stored_key]
        else:
            restored[key_text] = str(value)
    return restored


class McpServerService:
    """Owner-scoped CRUD plus the sync side effects of each write."""

    def __init__(
        self,
        store: Any,
        cipher: HeaderCipher,
        client: Any,
        sync: McpServerSync,
        tool_store: McpToolStore | None,
        policy: ServerUrlPolicy | Any,
    ) -> None:
        self.store = store
        self.cipher = cipher
        self.client = client
        self.sync = sync
        self.tool_store = tool_store
        self.policy = _coerce_policy(policy)

    # -- reads ------------------------------------------------------------- #

    async def list_servers(self, owner_user_id: str) -> list[McpServerRecord]:
        owner = _require_owner(owner_user_id)
        return list(await self.store.list_for_owner(owner))

    async def get_server(
        self, server_id: str, owner_user_id: str
    ) -> McpServerRecord:
        owner = _require_owner(owner_user_id)
        record = await self.store.get(server_id, owner)
        if record is None:
            raise McpServerNotFound("MCP server not found")
        return record

    async def list_tools(self, server_id: str, owner_user_id: str) -> list[Any]:
        record = await self.get_server(server_id, owner_user_id)
        if self.tool_store is None:
            return []
        tools = await self.tool_store.list_for_server(record.id, record.owner_user_id)
        return list(tools or [])

    async def tool_count(self, server_id: str, owner_user_id: str) -> int:
        return len(await self.list_tools(server_id, owner_user_id))

    # -- writes ------------------------------------------------------------ #

    async def create_server(
        self, owner_user_id: str, data: McpServerWrite
    ) -> McpServerRecord:
        owner = _require_owner(owner_user_id)
        name = _validated_name(data.name)
        if await self.store.get_by_name(owner, name) is not None:
            raise McpServerConflict(
                f"MCP server name {name!r} is already used by this user"
            )
        existing = await self.store.list_for_owner(owner)
        limit = self.policy.max_servers_per_user
        if limit and len(existing) >= limit:
            raise McpServerConflict(
                f"MCP server limit reached ({limit} per user); delete one before "
                "creating another"
            )

        enabled = True if data.enabled is UNSET else bool(data.enabled)
        if data.verify_tls is None:
            raise McpServerValidationError(
                "mcp_servers.verify_tls must be true or false; omit it to keep the default"
            )
        headers = None if data.headers is UNSET else data.headers
        if headers is not None:
            # A create has no stored row to restore from: a literal **** is a
            # client bug and must never be stored as a credential.
            headers = restore_masked_headers(headers, {})
        encrypted = self._encrypt_headers(headers)
        record = McpServerRecord(
            owner_user_id=owner,
            name=name,
            display_name=_optional_text(
                None if data.display_name is UNSET else data.display_name
            ),
            description=_text(None if data.description is UNSET else data.description),
            transport=_validated_transport(
                "streamable_http" if data.transport is UNSET else data.transport
            ),
            url=self.policy.validate_url(
                "" if data.url is UNSET else data.url
            ),
            headers_encrypted=encrypted,
            verify_tls=(
                True if data.verify_tls is UNSET else bool(data.verify_tls)
            ),
            enabled=enabled,
            status="pending" if enabled else "disabled",
            reachable=False,
        )
        try:
            created = await self.store.create(record)
        except IntegrityError:
            raise McpServerConflict(
                f"MCP server name {name!r} is already used by this user"
            ) from None
        if not created.enabled:
            return created
        return await self._sync_and_refresh(created, owner)

    async def update_server(
        self, server_id: str, owner_user_id: str, data: McpServerWrite
    ) -> McpServerRecord:
        owner = _require_owner(owner_user_id)
        current = await self.get_server(server_id, owner)
        if data.name not in (None, "") and data.name != current.name:
            raise McpServerValidationError(
                "mcp_servers.name is immutable after creation"
            )

        fields: dict[str, Any] = {}
        if data.display_name is not UNSET:
            fields["display_name"] = _optional_text(data.display_name)
        if data.description is not UNSET:
            fields["description"] = _text(data.description)
        if data.transport is not UNSET:
            fields["transport"] = _validated_transport(data.transport)
        if data.url is not UNSET:
            incoming_url = self.policy.validate_url(data.url)
            # The API never exposes the query string, so an edit form sends the
            # endpoint back without it. Preserve the stored query unless the user
            # supplied a new one explicitly.
            fields["url"] = self.policy.validate_url(
                merge_url_query(incoming_url, current.url)
            )
        if data.verify_tls is not UNSET:
            if data.verify_tls is None:
                raise McpServerValidationError(
                    "mcp_servers.verify_tls must be true or false; omit it to keep "
                    "the current value"
                )
            fields["verify_tls"] = bool(data.verify_tls)
        if data.headers is not UNSET:
            if data.headers is None:
                fields["headers_encrypted"] = None
            else:
                stored = self._decrypt_headers(current)
                fields["headers_encrypted"] = self._encrypt_headers(
                    restore_masked_headers(data.headers, stored)
                )

        updated = await self.store.update(server_id, owner, fields)
        if updated is None:
            raise McpServerNotFound("MCP server not found")
        if not updated.enabled:
            return updated
        return await self._sync_and_refresh(updated, owner)

    async def delete_server(self, server_id: str, owner_user_id: str) -> bool:
        owner = _require_owner(owner_user_id)
        if not await self.store.delete(server_id, owner):
            raise McpServerNotFound("MCP server not found")
        remove = getattr(self.sync, "remove_server_catalog", None)
        if callable(remove):
            try:
                await remove(server_id)
            except Exception as exc:  # noqa: BLE001 - the row is already gone
                # Never log the exception text: a sync failure can quote an
                # upstream response (and therefore a credential) verbatim.
                logger.warning(
                    "MCP server %s was deleted but its catalog cleanup failed (%s)",
                    server_id,
                    type(exc).__name__,
                )
        return True

    async def set_enabled(
        self, server_id: str, owner_user_id: str, enabled: bool
    ) -> McpServerRecord:
        owner = _require_owner(owner_user_id)
        updated = await self.store.set_enabled(server_id, owner, bool(enabled))
        if updated is None:
            raise McpServerNotFound("MCP server not found")
        if not updated.enabled:
            return updated
        return await self._sync_and_refresh(updated, owner)

    async def refresh_server(
        self, server_id: str, owner_user_id: str
    ) -> McpServerRecord:
        record = await self.get_server(server_id, owner_user_id)
        return await self._sync_and_refresh(
            record, record.owner_user_id, reason="server_refresh"
        )

    # -- status / catalog -------------------------------------------------- #

    async def status(self, owner_user_id: str) -> dict[str, Any]:
        """The caller's server counts, derived from their own rows.

        This is the Task 3 projection. Task 5's runtime status() replaces it with
        sync/index details, but the owner scoping and tool counts are the contract
        the management page already relies on.
        """
        records = await self.list_servers(owner_user_id)
        tools_total = 0
        for record in records:
            tools_total += len(await self.list_tools(record.id, record.owner_user_id))
        last_sync = max(
            (record.last_tools_sync_at for record in records if record.last_tools_sync_at),
            default=None,
        )
        first_error = next(
            (record.last_error for record in records if record.last_error), None
        )
        return {
            "enabled": bool(self.policy.enabled),
            "servers_total": len(records),
            "servers_active": sum(1 for row in records if row.status == "active"),
            "servers_pending": sum(1 for row in records if row.status == "pending"),
            "servers_error": sum(1 for row in records if row.status == "error"),
            "servers_disabled": sum(1 for row in records if row.status == "disabled"),
            "servers_reachable": sum(1 for row in records if row.reachable),
            "tools_total": tools_total,
            "indexed_tools": tools_total,
            "last_sync_at": last_sync,
            "last_error": first_error,
        }

    async def reindex(self, owner_user_id: str) -> McpServerReindexStats:
        """Ask the sync service for real counters; never invent them here."""
        owner = _require_owner(owner_user_id)
        result = await self.sync.reindex(owner)
        if isinstance(result, McpServerReindexStats):
            return result
        if isinstance(result, Mapping):
            fields = {
                key: result[key]
                for key in (
                    "scanned",
                    "changed",
                    "upserted",
                    "deleted",
                    "failed",
                    "duration_ms",
                    "error",
                )
                if key in result
            }
            return McpServerReindexStats(**fields)
        raise TypeError(
            "McpServerSync.reindex() must return McpServerReindexStats or a mapping"
        )

    # -- headers ----------------------------------------------------------- #

    def masked_headers(self, record: McpServerRecord) -> dict[str, str]:
        return mask_headers(self._decrypt_headers(record))

    def _decrypt_headers(self, record: McpServerRecord) -> dict[str, str]:
        try:
            return self.cipher.decrypt(record.headers_encrypted)
        except ValueError:
            raise McpServerConfigurationError(
                "stored MCP server headers could not be decrypted with the configured "
                "encryption key"
            ) from None

    def _encrypt_headers(self, headers: Mapping[str, str] | None) -> str | None:
        if headers is None:
            return None
        try:
            checked = validate_headers(headers)
        except ValueError as exc:
            raise McpServerValidationError(str(exc)) from None
        if not checked:
            return None
        try:
            return self.cipher.encrypt(checked)
        except ValueError as exc:
            raise McpServerValidationError(str(exc)) from None

    # -- sync -------------------------------------------------------------- #

    async def _sync_and_refresh(
        self,
        record: McpServerRecord,
        owner_user_id: str,
        *,
        reason: str | None = None,
    ) -> McpServerRecord:
        try:
            if reason is None:
                await self.sync.sync_server(record.id)
            else:
                await self.sync.sync_server(record.id, reason=reason)
        except Exception as exc:  # noqa: BLE001 - a failed sync must keep the row
            failed = await self.store.set_status(
                record.id,
                owner_user_id,
                status="error",
                reachable=False,
                last_error=_sanitize_sync_error(exc),
            )
            if failed is None:
                raise McpServerNotFound("MCP server not found") from None
            return failed
        refreshed = await self.store.get(record.id, owner_user_id)
        if refreshed is None:
            raise McpServerNotFound("MCP server not found")
        return refreshed


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


def _coerce_policy(policy: ServerUrlPolicy | Any | None) -> ServerUrlPolicy:
    if isinstance(policy, ServerUrlPolicy):
        return policy
    if policy is None:
        return ServerUrlPolicy()
    return ServerUrlPolicy(
        getattr(policy, "allowed_schemes", ("https",)),
        allow_private_networks=bool(
            getattr(policy, "allow_private_networks", False)
        ),
        max_servers_per_user=int(getattr(policy, "max_servers_per_user", 20)),
        enabled=bool(getattr(policy, "enabled", True)),
    )


def _require_owner(owner_user_id: str | None) -> str:
    owner = str(owner_user_id).strip() if owner_user_id else ""
    if not owner:
        raise McpServerValidationError(
            "owner_user_id is required: MCP servers are user-private"
        )
    return owner


def _validated_name(name: str | None) -> str:
    try:
        return validate_server_name(name)
    except ValueError as exc:
        raise McpServerValidationError(str(exc)) from None


def _validated_transport(transport: str | None) -> str:
    try:
        return validate_transport(transport)
    except ValueError as exc:
        raise McpServerValidationError(str(exc)) from None


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _optional_text(value: Any) -> str | None:
    text = "" if value is None else str(value).strip()
    return text or None


def _sanitize_sync_error(exc: BaseException) -> str:
    """A stored failure message that cannot contain a credential.

    Client errors are already built from fixed text; a fake or unexpected
    exception is reduced to its class. Header-shaped and bearer text is redacted
    as a second line of defense, because this string is shown in the UI.
    """
    if isinstance(exc, McpServerError):
        text = str(exc)
    else:
        text = f"MCP server sync failed ({type(exc).__name__})"
    text = re.sub(
        r"(?im)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key|"
        r"api-key|x-auth-token|x-access-token)\b\s*[:=][^\r\n]*",
        r"\1: ****",
        text,
    )
    text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "Bearer ****", text)
    text = re.sub(r"(?i)\b(token|secret|password|api[_-]?key)\s*[:=]\s*\S+", r"\1=****", text)
    text = _CONTROL.sub(" ", text)
    return truncate_error(text) or "MCP server sync failed"


__all__ = [
    "MASK",
    "MAX_HEADERS_TOTAL_BYTES",
    "McpServerReindexStats",
    "McpServerService",
    "McpServerSync",
    "McpServerWrite",
    "McpToolStore",
    "ServerUrlPolicy",
    "is_sensitive_header",
    "mask_headers",
    "merge_url_query",
    "restore_masked_headers",
    "url_has_query",
    "url_without_query",
]
