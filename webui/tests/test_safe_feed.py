from __future__ import annotations

import ipaddress

import pytest

from safe_feed import PublicOnlyResolver, validate_feed_url


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/feed.xml",
    "http://localhost/feed.xml",
    "http://service.local/feed.xml",
    "http://127.0.0.1/feed.xml",
    "http://10.0.0.8/feed.xml",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/feed.xml",
    "http://example.com:8080/feed.xml",
    "http://user:password@example.com/feed.xml",
    "https://example.com:444/feed.xml",
])
def test_rejects_unsafe_feed_urls(url):
    with pytest.raises(ValueError):
        validate_feed_url(url)


@pytest.mark.parametrize("url", [
    "https://example.com/feed.xml",
    "http://feeds.example.org/rss?category=tech",
    "https://[2606:4700:4700::1111]/feed.xml",
])
def test_accepts_public_http_feed_urls(url):
    assert validate_feed_url(url) == url


@pytest.mark.asyncio
async def test_resolver_rejects_non_global_literal():
    resolver = PublicOnlyResolver()
    with pytest.raises(OSError):
        await resolver.resolve("192.168.1.1", 80)


@pytest.mark.asyncio
async def test_resolver_accepts_global_literal():
    resolver = PublicOnlyResolver()
    result = await resolver.resolve("1.1.1.1", 80)
    assert result[0]["host"] == "1.1.1.1"
    assert ipaddress.ip_address(result[0]["host"]).is_global

@pytest.mark.parametrize("url", [
    "http://example.com:443/feed.xml",
    "https://example.com:80/feed.xml",
])
def test_rejects_scheme_mismatched_standard_ports(url):
    with pytest.raises(ValueError):
        validate_feed_url(url)
