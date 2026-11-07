"""Fetch RSS/Atom feeds without allowing URLs to reach private network targets."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3


def validate_feed_url(raw_url: str) -> str:
    """Validate URL syntax and obvious unsafe hosts before making a request.

    DNS results are checked again by PublicOnlyResolver at connection time,
    which prevents a hostname from resolving to loopback, private, link-local,
    multicast, reserved, or otherwise non-global addresses.
    """
    value = (raw_url or "").strip()
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid feed URL") from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("Feed URL must use http:// or https://")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Feed URLs must not contain credentials")
    expected_port = 80 if parts.scheme.lower() == "http" else 443
    if port is not None and port != expected_port:
        raise ValueError("Feed URL must use the standard port for its scheme")
    host = parts.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal", ".lan")):
        raise ValueError("Feed host must be publicly reachable")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Feed host must be publicly reachable")
    return value


class PublicOnlyResolver(aiohttp.abc.AbstractResolver):
    """Resolve only globally routable addresses, checking the actual result."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, family=family)
            addresses = []
            seen = set()
            for fam, _type, proto, _canonname, sockaddr in infos:
                ip = ipaddress.ip_address(sockaddr[0])
                if not ip.is_global:
                    raise OSError("Feed hostname resolved to a non-public address")
                key = (fam, sockaddr[0])
                if key in seen:
                    continue
                seen.add(key)
                addresses.append({"hostname": host, "host": sockaddr[0], "port": port,
                                  "family": fam, "proto": proto, "flags": socket.AI_NUMERICHOST})
            if not addresses:
                raise OSError("Feed hostname did not resolve to a public address")
            return addresses
        if not literal.is_global:
            raise OSError("Feed host must be publicly reachable")
        fam = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
        return [{"hostname": host, "host": str(literal), "port": port,
                 "family": fam, "proto": 0, "flags": socket.AI_NUMERICHOST}]

    async def close(self):
        return None


async def fetch_feed_bytes(raw_url: str) -> bytes:
    """Fetch a bounded feed document with validated, manually followed redirects."""
    url = validate_feed_url(raw_url)
    resolver = PublicOnlyResolver()
    connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
    timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False) as session:
        for redirect_count in range(MAX_REDIRECTS + 1):
            url = validate_feed_url(url)
            async with session.get(url, allow_redirects=False, headers={"User-Agent": "ReedMuhnBot-FeedReader/1.0"}) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location or redirect_count >= MAX_REDIRECTS:
                        raise ValueError("Feed redirected too many times or without a location")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                length = response.headers.get("Content-Length")
                if length:
                    try:
                        if int(length) > MAX_FEED_BYTES:
                            raise ValueError("Feed response is too large")
                    except ValueError as exc:
                        if str(exc) == "Feed response is too large":
                            raise
                        raise ValueError("Invalid feed response length") from exc
                chunks = []
                size = 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > MAX_FEED_BYTES:
                        raise ValueError("Feed response is too large")
                    chunks.append(chunk)
                return b"".join(chunks)
    raise ValueError("Feed redirect limit exceeded")
