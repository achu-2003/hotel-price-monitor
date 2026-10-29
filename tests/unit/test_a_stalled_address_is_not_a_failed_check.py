"""A request goes on to the host's next address when one will not connect.

From the Linux server a third of the handshakes to api.hotelzify.com hung for
the full timeout while the rest took 0.2 s, and Sterling failed about 130
checks a day for it. These pin what ``every_address`` does about that: each
attempt goes to the next address with a short connect budget, the name stays
what the certificate is checked against, and only a request that has not
reached ANY address fails.

A fake inner transport stands in for the network.
"""
from __future__ import annotations

import httpx
import pytest

from app.adapters.every_address import EveryAddressTransport

URL = "https://api.hotelzify.com/hotel/v2/hotel/availability?id=5171"


class Network(httpx.BaseTransport):
    """Addresses that stall, and a record of every attempt."""

    def __init__(self, stalls: set[str], *, refuse: bool = False):
        self.stalls = stalls
        self.refuse = refuse
        self.sent: list[httpx.Request] = []

    def handle_request(self, request):
        self.sent.append(request)
        if request.url.host in self.stalls:
            if self.refuse:
                raise httpx.ConnectError("refused", request=request)
            raise httpx.ConnectTimeout("handshake never finished", request=request)
        return httpx.Response(200, json={"ok": True}, request=request)


def client(network, addresses=("15.252.110.200", "65.1.31.207")):
    transport = EveryAddressTransport(network, resolver=lambda host, port: list(addresses))
    return httpx.Client(transport=transport, timeout=20.0)


def test_a_stalled_address_is_passed_over():
    network = Network({"15.252.110.200"})
    with client(network) as c:
        response = c.get(URL)
    assert response.json() == {"ok": True}
    assert [r.url.host for r in network.sent] == ["15.252.110.200", "65.1.31.207"]


def test_a_refused_address_is_passed_over_too():
    network = Network({"15.252.110.200"}, refuse=True)
    with client(network) as c:
        assert c.get(URL).status_code == 200


def test_the_name_is_what_the_certificate_and_the_server_are_given():
    network = Network(set())
    with client(network) as c:
        c.get(URL)
    [sent] = network.sent
    assert sent.extensions["sni_hostname"] == "api.hotelzify.com"
    assert sent.headers["Host"] == "api.hotelzify.com"
    assert (sent.url.path, sent.url.query) == ("/hotel/v2/hotel/availability", b"id=5171")


def test_each_address_gets_seconds_not_the_whole_timeout():
    network = Network(set())
    with client(network) as c:
        c.get(URL)
    timeout = network.sent[0].extensions["timeout"]
    assert timeout["connect"] == 5.0
    assert timeout["read"] == 20.0


def test_a_lone_address_is_asked_again():
    """A stall is not always the same address; one address is not one chance."""
    calls = []

    class Flaky(httpx.BaseTransport):
        def handle_request(self, request):
            calls.append(request.url.host)
            if len(calls) < 3:
                raise httpx.ConnectTimeout("stall", request=request)
            return httpx.Response(200, request=request)

    with client(Flaky(), addresses=("13.205.65.50",)) as c:
        assert c.get(URL).status_code == 200
    assert calls == ["13.205.65.50"] * 3


def test_every_address_stalling_is_still_a_timeout():
    """So the adapters' own handling -- TimeoutError_, retried by itself -- is unchanged."""
    network = Network({"15.252.110.200", "65.1.31.207"})
    with client(network) as c, pytest.raises(httpx.ConnectTimeout):
        c.get(URL)
    assert len(network.sent) == 3


def test_a_slow_answer_is_not_retried_elsewhere():
    """Connected and sent: going on would send the request twice."""
    network = Network(set())

    def slow(request):
        network.sent.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    network.handle_request = slow
    with client(network) as c, pytest.raises(httpx.ReadTimeout):
        c.get(URL)
    assert len(network.sent) == 1


def test_a_redirect_is_followed_from_the_name_not_the_address():
    hops = []

    class Redirecting(httpx.BaseTransport):
        def handle_request(self, request):
            hops.append((request.url.host, request.headers["Host"]))
            if len(hops) == 1:
                return httpx.Response(301, headers={"Location": "/v3/availability"}, request=request)
            return httpx.Response(200, request=request)

    transport = EveryAddressTransport(Redirecting(), resolver=lambda host, port: ["15.252.110.200"])
    with httpx.Client(transport=transport, follow_redirects=True) as c:
        response = c.get(URL)
    assert response.url.host == "api.hotelzify.com"
    assert hops == [("15.252.110.200", "api.hotelzify.com")] * 2


def test_every_connection_asks_for_segments_that_get_through():
    """The cause, not the symptom: full-size packets from AWS never arrived.

    Measured on 29 Oct from the worker: 6/6 handshakes timed out at the
    default segment size and 6/6 finished in 0.06 s at 1,200.
    """
    import socket

    from app.adapters import every_address

    if not every_address.segment_cap():
        pytest.skip("this platform cannot cap a socket's segment size (the server's can)")
    assert every_address.segment_cap() == [(socket.IPPROTO_TCP, socket.TCP_MAXSEG, 1200)]
    pool = EveryAddressTransport()._inner._pool
    assert isinstance(pool._network_backend, every_address.CappedSegmentsBackend)


def test_the_cap_is_on_the_socket_before_it_connects(monkeypatch):
    """The MSS is announced in the SYN. httpx's socket_options land after
    connect() and so change nothing the server sees -- which is why the
    backend exists at all."""
    import socket

    from app.adapters import every_address

    calls = []
    CAP = (socket.IPPROTO_TCP, 2, 1200)

    class Recording:
        def __init__(self, *args):
            pass

        def settimeout(self, value):
            calls.append(("timeout", value))

        def setsockopt(self, *option):
            calls.append(("opt", option))

        def connect(self, address):
            calls.append(("connect", address))

        def close(self):
            pass

    monkeypatch.setattr(every_address, "segment_cap", lambda: [CAP])
    monkeypatch.setattr(socket, "socket", Recording)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("65.1.31.207", 443))])
    every_address.CappedSegmentsBackend().connect_tcp("65.1.31.207", 443, timeout=5.0)
    assert calls.index(("opt", CAP)) < calls.index(("connect", ("65.1.31.207", 443)))


def test_a_socket_that_will_not_connect_is_a_connect_timeout(monkeypatch):
    """So EveryAddressTransport moves on, exactly as it does for httpcore's own."""
    import socket

    import httpcore

    from app.adapters import every_address

    class Stalling:
        def __init__(self, *args):
            pass

        def settimeout(self, value):
            pass

        def setsockopt(self, *option):
            pass

        def connect(self, address):
            raise socket.timeout("timed out")

        def close(self):
            pass

    monkeypatch.setattr(every_address, "segment_cap", lambda: [])
    monkeypatch.setattr(socket, "socket", Stalling)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("65.1.31.207", 443))])
    with pytest.raises(httpcore.ConnectTimeout):
        every_address.CappedSegmentsBackend().connect_tcp("65.1.31.207", 443, timeout=5.0)


def test_a_platform_that_refuses_the_cap_still_connects(monkeypatch):
    """Windows defines TCP_MAXSEG and refuses it; that must not fail every request."""
    import socket

    from app.adapters import every_address

    class Refusing:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def setsockopt(self, *args):
            raise OSError(10042, "unsupported option")

    every_address.segment_cap.cache_clear()
    monkeypatch.setattr(socket, "TCP_MAXSEG", 2, raising=False)
    monkeypatch.setattr(socket, "socket", lambda *a, **k: Refusing())
    try:
        assert every_address.segment_cap() == []
    finally:
        every_address.segment_cap.cache_clear()
