"""A visitor site that refuses this machine's address is read through a proxy.

THE FAULT THIS PINS
===================
From 16 Nov 2026 15:42 Goibibo gave the production server its "Sold Out" page
on every load -- three hotels, nineteen hours, one attempt every two hours --
while the same adapter on a home connection read all of Sterling's rooms. The
address was what it scored, and the server had no other way out.

``BROWSER_VISITOR_PROXY`` is that other way. These tests fix four things about
it: only the visitor Chrome is sent through it; the context stays plain (a
context with overrides was refused on 5 Nov); a proxy's password is added by a
relay outside the browser and never reaches a log line or an error message;
and a dead proxy is reported as the proxy, not as the site.

The relay tests use real sockets on 127.0.0.1. No browser is started.
"""
from __future__ import annotations

import base64
import socket
import threading

import pytest
from pydantic import SecretStr

from app.adapters import playwright_base, visitor_proxy
from app.adapters.playwright_base import BrowserPool
from app.adapters.visitor_proxy import VisitorProxy
from app.core.errors import AdapterConfigError

# -- reading the setting ----------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   "])
def test_no_setting_means_straight_out(value):
    assert visitor_proxy.parse(value) is None


def test_the_usual_form_is_understood():
    proxy = visitor_proxy.parse("http://ags-user:s3cr%40t@gw.example.net:7000")

    assert proxy == VisitorProxy("http", "gw.example.net", 7000, "ags-user", "s3cr@t")
    assert proxy.needs_relay
    assert proxy.address == "http://gw.example.net:7000"


def test_a_bare_host_and_port_is_an_http_proxy():
    proxy = visitor_proxy.parse("gw.example.net:7000")

    assert proxy == VisitorProxy("http", "gw.example.net", 7000)
    assert not proxy.needs_relay


@pytest.mark.parametrize(
    "value",
    [
        "ftp://gw.example.net:7000",
        "http://:7000",
        "http://gw.example.net:notaport",
        "socks5://ags-user:hunter2@gw.example.net:1080",
    ],
)
def test_an_unusable_value_is_refused_without_repeating_it(value):
    with pytest.raises(AdapterConfigError) as raised:
        visitor_proxy.parse(value)

    # The value carries a password and this message is shown on Attention.
    assert "hunter2" not in str(raised.value)
    assert "gw.example.net" not in str(raised.value)


def test_a_proxy_without_a_password_goes_to_chrome_as_it_is():
    proxy = visitor_proxy.parse("socks5://gw.example.net:1080")

    assert visitor_proxy.chrome_argument(proxy) == "--proxy-server=socks5://gw.example.net:1080"


def test_a_proxy_failure_is_explained_without_the_password():
    proxy = visitor_proxy.parse("http://ags-user:hunter2@gw.example.net:7000")

    message = visitor_proxy.explain(
        "page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://www.goibibo.com/x", proxy
    )

    assert "gw.example.net:7000" in message
    assert "ERR_TUNNEL_CONNECTION_FAILED" in message
    assert "hunter2" not in message and "ags-user" not in message


# -- the relay ---------------------------------------------------------


class _Upstream:
    """A stand-in for the provider's proxy: records one request, answers it."""

    def __init__(self) -> None:
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self.received = b""
        self.done = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return  # closed by the fixture before anything connected
        with conn:
            while b"\r\n\r\n" not in self.received:
                self.received += conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            # Now a tunnel: echo, as the far site would answer.
            conn.sendall(b"echo:" + conn.recv(65536))
        self.done.set()

    def close(self) -> None:
        self._listener.close()


@pytest.fixture
def upstream():
    server = _Upstream()
    yield server
    server.close()
    visitor_proxy.stop_relays()


def _local_port(argument: str) -> int:
    prefix = "--proxy-server=http://127.0.0.1:"
    assert argument.startswith(prefix), argument
    return int(argument.removeprefix(prefix))


def test_the_relay_signs_in_and_then_only_carries_bytes(upstream):
    proxy = VisitorProxy("http", "127.0.0.1", upstream.port, "ags-user", "hunter2")
    port = _local_port(visitor_proxy.chrome_argument(proxy))

    with socket.create_connection(("127.0.0.1", port), timeout=5) as chrome:
        chrome.sendall(
            b"CONNECT www.goibibo.com:443 HTTP/1.1\r\nHost: www.goibibo.com:443\r\n\r\n"
        )
        assert chrome.recv(65536).startswith(b"HTTP/1.1 200")
        chrome.sendall(b"tls-bytes")
        assert chrome.recv(65536) == b"echo:tls-bytes"

    assert upstream.done.wait(5)
    lines = upstream.received.split(b"\r\n")
    assert lines[0] == b"CONNECT www.goibibo.com:443 HTTP/1.1"
    token = base64.b64encode(b"ags-user:hunter2")
    assert b"Proxy-Authorization: Basic " + token in lines
    assert b"Host: www.goibibo.com:443" in lines


def test_chrome_is_never_given_the_password(upstream):
    proxy = VisitorProxy("http", "127.0.0.1", upstream.port, "ags-user", "hunter2")

    argument = visitor_proxy.chrome_argument(proxy)

    assert "hunter2" not in argument and "ags-user" not in argument


def test_one_relay_serves_every_chrome_of_a_process(upstream):
    proxy = VisitorProxy("http", "127.0.0.1", upstream.port, "ags-user", "hunter2")

    assert visitor_proxy.chrome_argument(proxy) == visitor_proxy.chrome_argument(proxy)


def test_a_proxy_that_is_down_is_answered_not_dropped():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    proxy = VisitorProxy("http", "127.0.0.1", dead_port, "ags-user", "hunter2")
    port = _local_port(visitor_proxy.chrome_argument(proxy))

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=30) as chrome:
            chrome.settimeout(30)
            chrome.sendall(b"CONNECT www.goibibo.com:443 HTTP/1.1\r\n\r\n")
            # A status line is what Chrome turns into a proxy error that
            # open_page can name; a bare close reads as the site saying nothing.
            assert chrome.recv(65536).startswith(b"HTTP/1.1 502")
    finally:
        visitor_proxy.stop_relays()


# -- the visitor Chrome ------------------------------------------------


class _FakeProcess:
    pid = 4242

    def __init__(self, args, **_kwargs) -> None:
        self.args = args

    def poll(self):
        return 0

    def terminate(self) -> None: ...

    def wait(self, timeout=None):
        return 0


class _FakeContext:
    def set_default_timeout(self, _ms) -> None: ...
    def set_default_navigation_timeout(self, _ms) -> None: ...
    def close(self) -> None: ...


class _FakeBrowser:
    def __init__(self) -> None:
        self.context_kwargs: list[dict] = []

    def is_connected(self) -> bool:
        return True

    def new_context(self, **kwargs):
        self.context_kwargs.append(kwargs)
        return _FakeContext()

    def close(self) -> None: ...


class _FakePlaywright:
    def __init__(self, browser) -> None:
        self.chromium = type(
            "Chromium", (), {"connect_over_cdp": lambda _self, _url: browser}
        )()

    def stop(self) -> None: ...


@pytest.fixture
def started(monkeypatch):
    processes: list[_FakeProcess] = []

    def _popen(args, **kwargs):
        processes.append(_FakeProcess(args, **kwargs))
        return processes[-1]

    monkeypatch.setattr(playwright_base.subprocess, "Popen", _popen)
    monkeypatch.setattr(playwright_base, "_wait_for_devtools", lambda port, process: None)
    monkeypatch.setattr(
        playwright_base, "_visitor_chrome_executable", lambda channel, pw: "/opt/google/chrome/chrome"
    )
    browser = _FakeBrowser()
    pool = BrowserPool()
    pool._local.playwright = _FakePlaywright(browser)
    yield pool, browser, processes
    pool.close()


def _with_proxy(monkeypatch, value: str | None) -> None:
    settings = playwright_base.get_settings().model_copy(
        update={"browser_visitor_proxy": SecretStr(value) if value else None}
    )
    monkeypatch.setattr(playwright_base, "get_settings", lambda: settings)


def test_the_visitor_chrome_goes_out_through_the_proxy(started, monkeypatch):
    pool, browser, processes = started
    _with_proxy(monkeypatch, "http://gw.example.net:7000")

    with pool.context(locale="en-IN", timezone="Asia/Kolkata", as_visitor=True):
        pass

    assert "--proxy-server=http://gw.example.net:7000" in processes[0].args
    # Still a plain context: the proxy is Chrome's, not the context's.
    assert set(browser.context_kwargs[0]) == {"storage_state"}


def test_without_the_setting_chrome_is_started_as_before(started, monkeypatch):
    pool, _, processes = started
    _with_proxy(monkeypatch, None)

    with pool.context(locale="en-IN", timezone="Asia/Kolkata", as_visitor=True):
        pass

    assert not [a for a in processes[0].args if a.startswith("--proxy-server")]
