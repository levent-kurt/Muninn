"""Outbound-target validation (SSRF guard).

``/scrape`` accepts a URL from the caller and fetches it - from the server, and
optionally through a real browser. Without validation that makes the service an
open proxy into whatever network the host can reach: loopback services, RFC1918
LANs, and cloud instance-metadata endpoints such as ``169.254.169.254``.

This module decides whether a target is allowed *before* any request is made:

* only ``http`` / ``https``;
* the hostname must resolve entirely to globally-routable addresses;
* an optional host allowlist, so an operator can permit specific internal
  targets (useful for scraping your own intranet) without opening everything.

Known limitation, stated plainly: this is a *pre-flight* check, so a hostname
whose DNS answer changes between validation and connection (DNS rebinding) can
still slip through. Closing that hole properly requires pinning the validated IP
into the transport, which is deliberately out of scope here. The guard raises
the bar substantially and stops the naive cases; it is not a WAF.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})


class TargetNotAllowedError(ValueError):
    """The requested URL must not be fetched (raised as HTTP 400 by default)."""


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return a rejection reason for a non-public address, else ``None``."""
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        # 169.254.0.0/16 also covers the cloud metadata endpoint.
        return "link-local address (includes cloud metadata services)"
    if ip.is_private:
        return "private network address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_reserved:
        return "reserved address"
    if ip.is_unspecified:
        return "unspecified address"
    if not ip.is_global:
        return "non-globally-routable address"
    return None


def _host_allowed(hostname: str, allowed_hosts: Iterable[str]) -> bool:
    """Allowlist match: exact host, or a dotted suffix (``*.example.com``)."""
    host = hostname.lower().rstrip(".")
    for pattern in allowed_hosts:
        candidate = pattern.lower().strip().lstrip("*.").rstrip(".")
        if not candidate:
            continue
        if host == candidate or host.endswith("." + candidate):
            return True
    return False


def _resolve(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve ``hostname`` to every address it maps to.

    Runs in a thread (``asyncio.to_thread``) because ``getaddrinfo`` blocks.
    """
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise TargetNotAllowedError(f"cannot resolve host {hostname!r}: {exc}") from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4]
        try:
            addresses.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:  # pragma: no cover - non-IP family entry
            continue
    if not addresses:
        raise TargetNotAllowedError(f"host {hostname!r} resolved to no usable address")
    return addresses


async def validate_target_url(
    url: str,
    *,
    allow_private: bool = False,
    allowed_hosts: Iterable[str] = (),
) -> str:
    """Validate ``url`` for outbound fetching and return it normalised.

    Raises :class:`TargetNotAllowedError` when the target is not permitted.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise TargetNotAllowedError(
            f"unsupported scheme {parts.scheme!r}; only http and https are allowed"
        )

    hostname = parts.hostname
    if not hostname:
        raise TargetNotAllowedError("URL has no hostname")

    allowlist = [h for h in allowed_hosts if h and h.strip()]
    if allowlist:
        # An explicit allowlist is the operator overriding the network defaults.
        if not _host_allowed(hostname, allowlist):
            raise TargetNotAllowedError(
                f"host {hostname!r} is not in SCRAPE_ALLOWED_HOSTS"
            )
        return urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))

    if allow_private:
        return urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))

    # IP literal: validate directly, no DNS involved.
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if reason := _ip_is_blocked(literal):
            raise TargetNotAllowedError(
                f"refusing to fetch {hostname}: it is a {reason}"
            )
        return urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))

    for address in await asyncio.to_thread(_resolve, hostname):
        if reason := _ip_is_blocked(address):
            raise TargetNotAllowedError(
                f"refusing to fetch {hostname!r}: it resolves to {address} ({reason})"
            )

    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))
