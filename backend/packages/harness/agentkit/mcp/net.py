"""Host-resolution guard for MCP endpoints (SSRF, best effort).

`ServerUrlPolicy` rejects *literal* private targets, but a public-looking hostname
can resolve to 127.0.0.1 or 10.x, and a stored URL is replayed on every later
connection.  This module is the one resolver the management service, the sync and
the runtime share: it asks the platform resolver for every address of a host
(getaddrinfo returns all of them, so a round-robin name that includes one private
address is caught) and reports the addresses that must not be reached.

Two deliberate limits, both documented rather than hidden:

* **an unresolvable name is not rejected here.**  The connection then fails on its
  own, and an offline deployment must still be able to store a server it will
  reach later.  Only addresses that *do* resolve are judged.
* **DNS rebinding remains possible.**  The check runs before the connection, not
  inside the transport, so a name can resolve publicly here and privately a
  moment later.  Closing that needs a transport that pins the resolved address;
  this build documents the residual risk instead of pretending it is airtight.

Secrets: only host names and IP addresses are ever echoed, never the URL query
string, the headers or the credentials.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from functools import lru_cache
from urllib.parse import urlsplit

from agentkit.mcp.errors import McpServerValidationError


def host_of(url: str) -> str:
    """The lower-case hostname of a URL, or "" when it has none."""
    try:
        return (urlsplit(str(url or "")).hostname or "").strip().rstrip(".").lower()
    except ValueError:
        return ""


def _port_of(url: str) -> int | None:
    try:
        return urlsplit(str(url or "")).port
    except ValueError:
        return None


class _UnresolvedHost(LookupError):
    """A cached lookup that produced nothing.

    lru_cache stores return values, not raised exceptions, so raising is what keeps
    a failed or empty resolution out of the cache.
    """


def _lookup_host_addresses(host: str, port: int | None) -> tuple[str, ...]:
    """Every address the platform resolver reports, or () when it reports none."""
    try:
        infos = socket.getaddrinfo(host, port or 0, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return ()
    addresses: list[str] = []
    for info in infos:
        address = ""
        if len(info) > 4 and isinstance(info[4], tuple) and info[4]:
            address = str(info[4][0]).split("%", 1)[0]
        if address and address not in addresses:
            addresses.append(address)
    return tuple(addresses)


@lru_cache(maxsize=256)
def _cached_host_addresses(host: str, port: int | None) -> tuple[str, ...]:
    """The expensive, stable case: a successful non-empty resolution.

    A failed lookup is deliberately *not* cached.  A server row can be written while
    its hostname is temporarily unresolvable, and a cached () would then mask the
    address it resolves to during every later sync/call - including 127.0.0.1.
    """
    addresses = _lookup_host_addresses(host, port)
    if not addresses:
        raise _UnresolvedHost(host)
    return addresses


def resolve_host_addresses(host: str, port: int | None = None) -> tuple[str, ...]:
    """Every address the platform resolver reports for one host.

    Only successful, non-empty resolutions are cached: they are the expensive and
    stable case, while a failed or empty lookup must be retried so the SSRF guard
    can see a name that starts resolving.  An empty tuple means "not resolvable
    now" - the caller must not read it as "public".  Tests replace this function
    (the cache lives in the private helper, so a patch is always honoured).
    """
    text = str(host or "").strip().strip("[]").rstrip(".").lower()
    if not text:
        return ()
    try:
        return _cached_host_addresses(text, port)
    except _UnresolvedHost:
        return ()


def is_private_address(address: str) -> bool:
    """True for any address that is not globally routable.

    This is the resolver-side classifier; ServerUrlPolicy._is_private_target makes
    the same decision for a literal when the URL is written.  The two exist because
    one check must run without DNS (a fast 422) and the other only after resolution
    (a stored hostname can rebind), and they must agree: a non-global address is
    never reachable through either path.
    """
    text = str(address or "").split("%", 1)[0].strip()
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return False
    return not parsed.is_global


def private_addresses(host: str, port: int | None = None) -> tuple[str, ...]:
    """The resolved addresses of one host that must not be reached."""
    return tuple(
        address
        for address in resolve_host_addresses(host, port)
        if is_private_address(address)
    )


def private_addresses_of_url(url: str) -> tuple[str, ...]:
    """The private addresses a URL's host resolves to (empty when it is fine)."""
    host = host_of(url)
    if not host:
        return ()
    return private_addresses(host, _port_of(url))


def private_target_message(offending: tuple[str, ...]) -> str:
    """One fixed sentence plus the addresses; never the URL or a credential."""
    return (
        "MCP server url resolves to a private, loopback, link-local or otherwise "
        f"non-global address ({', '.join(offending)}); "
        "set mcp_servers.allow_private_networks=true "
        "to allow it"
    )


async def ensure_public_endpoint(
    url: str,
    *,
    allow_private_networks: bool,
    error: type[Exception] = McpServerValidationError,
) -> None:
    """Raise `error` when `url` resolves to an address this process may not reach.

    Runs the blocking resolver off the event loop.  `allow_private_networks` is
    the same switch the literal check uses: one opt-in for both.

    `error` is the caller's vocabulary, because the same fact means two things: a
    URL being *written* is a validation failure (422, the user can fix it), while a
    stored URL being *connected* is a configuration failure (degrade the row, say
    so in `last_error`).
    """
    if allow_private_networks:
        return
    if not host_of(url):
        return
    offending = await asyncio.to_thread(private_addresses_of_url, url)
    if offending:
        raise error(private_target_message(offending))


__all__ = [
    "ensure_public_endpoint",
    "host_of",
    "is_private_address",
    "private_target_message",
    "private_addresses",
    "private_addresses_of_url",
    "resolve_host_addresses",
]
