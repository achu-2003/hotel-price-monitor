"""An HTTP client that tries each of a site's addresses before giving up.

WHY
===
On 29 Oct 2026, from the Linux server, a third of the connections to
api.hotelzify.com opened in 20 ms and then hung in the TLS handshake until the
20-second timeout; the rest finished in 0.2 s. The same requests from the old
Windows server took about a second, every time. Sterling, the one hotel on
that engine, failed about 130 checks a day, and its "successes" took 20.5 s
because the promotions or tax call had hung and been skipped.

httpx connects to ONE address per request -- whichever the resolver returned
first -- and waits out the whole timeout on it. A load balancer has several,
and an unanswered handshake on one says nothing about the others.

WHAT THIS DOES
==============
Every request resolves the host, and each attempt goes to the next address,
with a short connect budget (TCP and TLS together). An attempt that never
connected never sent the request, so moving on is safe for any method. The
addresses come from DNS on every call, so a balancer that swaps its IPs needs
nothing from anybody.

The certificate is still checked against the host's NAME: the attempt is sent
to the IP with the name as its SNI and Host header.
"""
from __future__ import annotations

import socket
from typing import Any

import httpx

from app.core.logging import get_logger

log = get_logger("adapter.every_address")

#: Connect + TLS for one address. A healthy handshake to these APIs is well
#: under a second; one that has not finished in five is the stall, not a slow
#: network.
CONNECT_SECONDS = 5.0
#: Attempts per request when the host has fewer addresses than this: the same
#: addresses are tried again, because a stall is not always the same address.
MIN_ATTEMPTS = 3


def resolve(host: str, port: int) -> list[str]:
    """The host's addresses in the resolver's order, without repeats."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    seen: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.append(address)
    return seen


def _is_ip(host: str) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, host)
            return True
        except OSError:
            pass
    return False


class EveryAddressTransport(httpx.BaseTransport):
    """:class:`httpx.HTTPTransport`, one address at a time."""

    def __init__(self, inner: httpx.BaseTransport | None = None, *,
                 connect_seconds: float = CONNECT_SECONDS, min_attempts: int = MIN_ATTEMPTS,
                 resolver=resolve) -> None:
        self._inner = inner or httpx.HTTPTransport()
        self._connect = connect_seconds
        self._min_attempts = min_attempts
        self._resolve = resolver

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if not host or _is_ip(host):
            return self._inner.handle_request(request)
        addresses = self._resolve(host, request.url.port or (443 if request.url.scheme == "https" else 80))
        if not addresses:
            return self._inner.handle_request(request)  # let httpx report the DNS failure

        timeout = dict(request.extensions.get("timeout") or {})
        timeout["connect"] = min(timeout.get("connect") or self._connect, self._connect)
        attempts = max(len(addresses), self._min_attempts)
        last: httpx.TransportError | None = None
        for attempt in range(attempts):
            address = addresses[attempt % len(addresses)]
            try:
                return self._inner.handle_request(self._to(request, address, host, timeout))
            except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
                last = exc
                log.info("address_did_not_connect", host=host, address=address,
                         attempt=attempt + 1, error=type(exc).__name__)
        assert last is not None
        raise last

    @staticmethod
    def _to(request: httpx.Request, address: str, host: str, timeout: dict[str, Any]) -> httpx.Request:
        headers = request.headers.copy()
        headers["Host"] = request.headers.get("Host", host)
        return httpx.Request(
            request.method, request.url.copy_with(host=address), headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "timeout": timeout, "sni_hostname": host},
        )

    def close(self) -> None:
        self._inner.close()


def client(timeout: httpx.Timeout | float) -> httpx.Client:
    """What the API adapters use in place of ``httpx.Client(timeout=..., follow_redirects=True)``."""
    return httpx.Client(timeout=timeout, follow_redirects=True, transport=EveryAddressTransport())
