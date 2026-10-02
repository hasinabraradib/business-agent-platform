"""Fetch a web page for ingestion without enabling SSRF.

- Only http/https; no credentials in the URL.
- The host is resolved and every address must be public (not private, loopback, link-local,
  reserved, CGNAT, multicast...). IPv4-mapped IPv6 addresses are unwrapped first.
- The connection goes to the checked IP (with the original Host header and TLS server name),
  so a second DNS lookup cannot swap in an internal address (DNS rebinding).
- Redirects are followed manually and every hop is checked again.
- Response size and total time are capped; environment proxies are ignored.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from app.ingestion.parsers import ParsedDocument, ParseError, parse_html, parse_pdf, parse_text
from app.ingestion.parsers import parse_markdown as _parse_markdown

Resolver = Callable[[str, int], Awaitable[list[str]]]

USER_AGENT = "BusinessAgentPlatform-Ingest/0.1"
ACCEPT = "text/html,application/xhtml+xml,text/plain,text/markdown,application/pdf;q=0.9"


class UnsafeURLError(ValueError):
    """The URL is not allowed (scheme, credentials, or a non-public address)."""


class FetchError(Exception):
    """The page could not be fetched (HTTP error, too large, too slow, ...)."""


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def check_public_ip(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        raise UnsafeURLError(f"refusing to fetch a non-public address ({ip})")
    return ip


@dataclass(frozen=True)
class ValidatedTarget:
    url: httpx.URL
    host: str
    ip: str


async def validate_url(url: str, resolver: Resolver = system_resolver) -> ValidatedTarget:
    try:
        parsed = httpx.URL(url.strip())
    except (httpx.InvalidURL, TypeError) as exc:
        raise UnsafeURLError("invalid URL") from exc
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError("only http and https URLs are allowed")
    if not parsed.host:
        raise UnsafeURLError("URL has no host")
    if parsed.userinfo:
        raise UnsafeURLError("URLs with credentials are not allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        addresses = [str(ipaddress.ip_address(parsed.host))]
    except ValueError:
        try:
            addresses = await resolver(parsed.host, port)
        except OSError as exc:
            raise UnsafeURLError(f"could not resolve host {parsed.host!r}") from exc
    if not addresses:
        raise UnsafeURLError(f"could not resolve host {parsed.host!r}")
    # Every address must be public: a host with one public and one private record is refused.
    checked = [check_public_ip(a) for a in addresses]
    return ValidatedTarget(
        url=parsed.copy_with(fragment=None), host=parsed.host, ip=str(checked[0])
    )


@dataclass(frozen=True)
class FetchedPage:
    url: str  # final URL after redirects
    content_type: str
    body: bytes


async def fetch_url(
    url: str,
    *,
    max_bytes: int,
    timeout_seconds: float,
    max_redirects: int = 5,
    resolver: Resolver = system_resolver,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FetchedPage:
    try:
        async with asyncio.timeout(timeout_seconds):
            return await _fetch(url, max_bytes, timeout_seconds, max_redirects, resolver, transport)
    except TimeoutError:
        raise FetchError(f"timed out after {timeout_seconds:g} seconds") from None
    except httpx.HTTPError as exc:
        raise FetchError(f"could not fetch the page ({type(exc).__name__})") from exc


async def _fetch(
    url: str,
    max_bytes: int,
    timeout_seconds: float,
    max_redirects: int,
    resolver: Resolver,
    transport: httpx.AsyncBaseTransport | None,
) -> FetchedPage:
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False, timeout=timeout_seconds, trust_env=False
    ) as client:
        current = url
        for _ in range(max_redirects + 1):
            target = await validate_url(current, resolver)
            request = client.build_request(
                "GET",
                target.url.copy_with(host=target.ip),
                headers={
                    "Host": target.url.netloc.decode("ascii"),
                    "User-Agent": USER_AGENT,
                    "Accept": ACCEPT,
                },
                # TLS: send SNI and verify the certificate for the real hostname, not the IP.
                extensions={"sni_hostname": target.host} if target.url.scheme == "https" else {},
            )
            response = await client.send(request, stream=True)
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("redirect without a Location header")
                    current = str(target.url.join(location))
                    continue
                if response.status_code >= 400:
                    raise FetchError(f"the page returned HTTP {response.status_code}")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise FetchError(_too_large(max_bytes))
                body = bytearray()
                async for piece in response.aiter_bytes():
                    body += piece
                    if len(body) > max_bytes:
                        raise FetchError(_too_large(max_bytes))
                return FetchedPage(
                    url=str(target.url),
                    content_type=response.headers.get("content-type", ""),
                    body=bytes(body),
                )
            finally:
                await response.aclose()
        raise FetchError(f"too many redirects (more than {max_redirects})")


def _too_large(max_bytes: int) -> str:
    return f"the page is larger than {max_bytes // (1024 * 1024)} MB"


def parse_fetched(page: FetchedPage) -> ParsedDocument:
    media_type, _, params = page.content_type.partition(";")
    media_type = media_type.strip().lower()
    charset = None
    for param in params.split(";"):
        key, _, value = param.partition("=")
        if key.strip().lower() == "charset":
            charset = value.strip().strip('"') or None

    if media_type in ("text/html", "application/xhtml+xml", ""):
        # BeautifulSoup detects the encoding (HTTP charset, <meta charset>, BOM) from bytes.
        return parse_html(page.body, encoding=charset)
    if media_type == "application/pdf":
        return parse_pdf(page.body)
    text = page.body.decode(charset or "utf-8", errors="replace")
    if media_type == "text/markdown":
        return _parse_markdown(text)
    if media_type == "text/plain":
        return parse_text(text)
    raise ParseError(f"unsupported content type: {media_type}")
