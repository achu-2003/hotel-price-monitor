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
