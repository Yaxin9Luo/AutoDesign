from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class RemoteUrlPolicyError(ValueError):
    pass


def validate_remote_http_url(
    url: str,
    *,
    allow_private_network: bool,
    require_https: bool = False,
) -> str:
    cleaned = (url or "").strip()
    parsed = urlparse(cleaned)
    allowed_schemes = {"https"} if require_https else {"http", "https"}
    if parsed.scheme.lower() not in allowed_schemes or not parsed.hostname:
        expected = "https" if require_https else "http or https"
        raise RemoteUrlPolicyError(f"remote URL must use {expected}")
    if parsed.username is not None or parsed.password is not None:
        raise RemoteUrlPolicyError("remote URL must not contain credentials")
    if allow_private_network:
        return cleaned

    try:
        addresses = {
            entry[4][0]
            for entry in socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise RemoteUrlPolicyError(f"remote URL host could not be resolved: {exc}") from exc
    if not addresses:
        raise RemoteUrlPolicyError("remote URL host did not resolve")
    for raw in addresses:
        address = ipaddress.ip_address(raw.split("%", 1)[0])
        if not address.is_global:
            raise RemoteUrlPolicyError(
                "remote URL resolves to a private or non-public network address"
            )
    return cleaned
