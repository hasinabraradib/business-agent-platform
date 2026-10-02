"""SSRF protection and limits for URL ingestion. No real DNS or network: all injected."""

import asyncio

import httpx
import pytest

from app.ingestion.fetch import (
    FetchedPage,
    FetchError,
    UnsafeURLError,
    fetch_url,
    parse_fetched,
    validate_url,
)

PUBLIC_IP = "93.184.216.34"
DNS = {
    "shop.example": [PUBLIC_IP],
    "cdn.example": ["151.101.1.1"],
    "internal.example": ["10.0.0.5"],
    "metadata.example": ["169.254.169.254"],
    "localhost.example": ["127.0.0.1"],
    "mixed.example": [PUBLIC_IP, "192.168.1.10"],
    "v6-loopback.example": ["::1"],
}


async def fake_resolver(host: str, port: int) -> list[str]:
    if host not in DNS:
        raise OSError("no such host")
    return DNS[host]


async def fetch(url: str, handler, **kwargs) -> FetchedPage:
    kwargs.setdefault("max_bytes", 1_000_000)
    kwargs.setdefault("timeout_seconds", 5)
    return await fetch_url(
        url, resolver=fake_resolver, transport=httpx.MockTransport(handler), **kwargs
    )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://shop.example/file",
        "file:///etc/passwd",
        "gopher://shop.example/",
        "javascript:alert(1)",
        "//shop.example/no-scheme",
        "http://user:pass@shop.example/",
        "http:///no-host",
    ],
)
async def test_disallowed_urls_are_refused(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        await validate_url(url, fake_resolver)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.1.2.3:8080/admin",
        "http://10.0.0.1/",
        "http://172.16.5.4/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
        "http://100.64.0.1/",  # carrier-grade NAT
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fe80::1]/",  # IPv6 link-local
        "http://[fd00::1]/",  # IPv6 unique local
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
        "http://[::ffff:10.0.0.1]/",
        "http://224.0.0.1/",  # multicast
    ],
)
async def test_private_loopback_and_link_local_ip_literals_are_refused(url: str) -> None:
    with pytest.raises(UnsafeURLError, match="non-public address"):
        await validate_url(url, fake_resolver)


@pytest.mark.parametrize(
    "host",
    [
        "internal.example",
        "metadata.example",
        "localhost.example",
        "mixed.example",
        "v6-loopback.example",
    ],
)
async def test_hostnames_resolving_to_non_public_addresses_are_refused(host: str) -> None:
    with pytest.raises(UnsafeURLError, match="non-public address"):
        await validate_url(f"https://{host}/page", fake_resolver)


async def test_unresolvable_host_is_refused() -> None:
    with pytest.raises(UnsafeURLError, match="could not resolve"):
        await validate_url("https://nope.example/", fake_resolver)


async def test_connection_is_pinned_to_the_checked_ip() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, html="<p>Hello.</p>", headers={"content-type": "text/html"})

    page = await fetch("https://shop.example/about#team", handler)

    request = seen[0]
    assert request.url.host == PUBLIC_IP
    assert request.headers["host"] == "shop.example"
    assert request.extensions["sni_hostname"] == "shop.example"
    assert page.url == "https://shop.example/about"
    assert page.body == b"<p>Hello.</p>"


@pytest.mark.parametrize(
    "location",
    [
        "http://internal.example/admin",
        "http://127.0.0.1:6379/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "file:///etc/passwd",
    ],
)
async def test_redirects_to_unsafe_targets_are_refused(location: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"location": location})

    with pytest.raises(UnsafeURLError):
        await fetch("https://shop.example/go", handler)
    assert len(seen) == 1  # the unsafe target was never requested


async def test_relative_and_cross_host_redirects_are_followed_and_rechecked() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.headers['host']}{request.url.path}")
        if request.url.path == "/start":
            return httpx.Response(301, headers={"location": "/moved"})
        if request.url.path == "/moved":
            return httpx.Response(302, headers={"location": "https://cdn.example/final"})
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    page = await fetch("https://shop.example/start", handler)
    assert seen == ["shop.example/start", "shop.example/moved", "cdn.example/final"]
    assert page.url == "https://cdn.example/final"


async def test_redirect_loops_are_capped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/again"})

    with pytest.raises(FetchError, match="too many redirects"):
        await fetch("https://shop.example/", handler, max_redirects=3)


async def test_response_size_is_capped_while_streaming() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000, headers={"content-type": "text/plain"})

    with pytest.raises(FetchError, match="larger than"):
        await fetch("https://shop.example/", handler, max_bytes=1000)


async def test_declared_content_length_over_cap_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"tiny", headers={"content-length": "999999999"})

    with pytest.raises(FetchError, match="larger than"):
        await fetch("https://shop.example/", handler, max_bytes=1000)


async def test_slow_responses_time_out() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, text="late")

    with pytest.raises(FetchError, match="timed out"):
        await fetch("https://shop.example/", handler, timeout_seconds=0.2)


async def test_http_errors_are_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(FetchError, match="HTTP 404"):
        await fetch("https://shop.example/missing", handler)


def test_fetched_content_is_parsed_by_content_type() -> None:
    html = FetchedPage(
        "https://x.example/",
        "text/html; charset=utf-8",
        "<title>T</title><h1>হ্যালো</h1><p>Body text.</p>".encode(),
    )
    parsed = parse_fetched(html)
    assert parsed.title == "T"
    assert [b.text for b in parsed.blocks] == ["হ্যালো", "Body text."]

    text = parse_fetched(FetchedPage("https://x.example/a.txt", "text/plain", b"Plain."))
    assert [b.text for b in text.blocks] == ["Plain."]

    from app.ingestion.parsers import ParseError

    with pytest.raises(ParseError, match="unsupported content type"):
        parse_fetched(FetchedPage("https://x.example/i.png", "image/png", b"\x89PNG"))
