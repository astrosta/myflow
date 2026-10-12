"""客户端 IP 与受信代理（M9 §2.5）。

登录失败按 **TCP peer IP** 计数。只有在 peer 属于 `AUTH_TRUSTED_PROXIES` 时才信
`X-Real-IP`；**永远不看 `X-Forwarded-For`**——它是客户端可以整段伪造的列表，
用它做限速等于把"锁定谁"的决定权交给攻击者。
"""

from __future__ import annotations

import ipaddress
import os

from fastapi import Request

TRUSTED_PROXIES_ENV = "AUTH_TRUSTED_PROXIES"


def trusted_proxies() -> tuple[str, ...]:
    raw = os.getenv(TRUSTED_PROXIES_ENV, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def peer_ip(request: Request) -> str:
    """ASGI 层的 TCP peer。没有（测试里的裸 scope）就是空串。"""
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return host if isinstance(host, str) else ""


def is_trusted_proxy(ip: str, *, trusted: tuple[str, ...] | None = None) -> bool:
    if not ip:
        return False
    entries = trusted_proxies() if trusted is None else trusted
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip in entries
    for entry in entries:
        if entry == ip:
            return True
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request: Request) -> str:
    """限速用的键：peer IP；peer 受信时才用 `X-Real-IP`。"""
    peer = peer_ip(request)
    if is_trusted_proxy(peer):
        real = request.headers.get("x-real-ip")
        if real and real.strip():
            return real.strip()
    return peer or "unknown"


def forwarded_proto(request: Request) -> str | None:
    """受信代理转发的协议。只用于判断 cookie 能不能持久化 / 加 Secure。"""
    peer = peer_ip(request)
    if not is_trusted_proxy(peer):
        return None
    proto = request.headers.get("x-forwarded-proto")
    if not proto:
        return None
    return proto.split(",")[0].strip().lower() or None
