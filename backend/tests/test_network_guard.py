import socket

import httpx
import pytest


def test_dns_for_external_hosts_is_blocked() -> None:
    with pytest.raises(OSError, match="Network access is disabled"):
        socket.getaddrinfo("generativelanguage.googleapis.com", 443)


async def test_real_http_calls_fail_fast() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError, match="Network access is disabled"):
            await client.get("https://example.com/")
