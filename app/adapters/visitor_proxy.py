"""The connection a visitor site is read through, when ours is refused.

WHY THIS EXISTS
===============
From 16 Nov 2026 15:42 Goibibo served the production server no room list at
all: every load came back as its "Sold Out" page, for three hotels, for
nineteen hours, at one attempt every two hours. The same adapter on a home
connection read all of Sterling's rooms at 10:55 the next morning, in the
same minute the server was refused. So it is the ADDRESS that is scored -- a
data-centre range -- and no amount of pacing gets a refused address served.

``BROWSER_VISITOR_PROXY`` names another way out: a proxy on a connection the
site treats as a guest's. Only the visitor Chrome uses it (see
BrowserPool._ensure_visitor_browser), so every other site is still read
directly and nothing is paid for that does not need it.

WHY A RELAY, AND NOT PLAYWRIGHT'S ``proxy=``
============================================
Chrome takes a proxy as ``--proxy-server`` and has no flag for its username
and password; it asks, as a dialog. Playwright answers that dialog only for a
context created with ``proxy=``, by intercepting requests over DevTools -- and
the visitor context is plain on purpose: on 5 Nov 2026 Goibibo refused the
attached Chrome both times its context carried overrides. So the credentials
are added OUTSIDE the browser. Chrome is pointed at a relay on 127.0.0.1 that
needs none, and the relay passes each connection to the real proxy with
``Proxy-Authorization`` on it. Chrome is left an ordinary Chrome behind an
ordinary proxy.

A proxy that needs no password (one that allows this server's address
instead) is given to Chrome directly, and no relay runs.

WHAT THE RELAY DOES NOT DO
==========================
It adds the header to the FIRST request on a connection and then copies
bytes. That is the whole of an HTTPS load -- one CONNECT, then the tunnel --
and every page read this way is HTTPS. A second plain-http request reusing
the same connection would go out without the header and be answered 407.
"""
from __future__ import annotations

import base64
import socket
import socketserver
import ssl
import threading
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from app.core.errors import AdapterConfigError
from app.core.logging import get_logger

log = get_logger("visitor_proxy")

#: What Chrome's --proxy-server understands.
_SCHEMES = {"http": 80, "https": 443, "socks5": 1080, "socks4": 1080}

#: Schemes the relay can speak to. Chrome cannot send a SOCKS password at all,
#: and the relay only knows how to put a header on an HTTP proxy request.
_RELAYED_SCHEMES = ("http", "https")

_CONNECT_TIMEOUT_SECONDS = 20
_MAX_HEAD_BYTES = 64 * 1024

#: Chromium's net errors for "the proxy, not the site, failed". Lower-cased,
#: as open_page compares them.
PROXY_NET_ERRORS = (
    "err_proxy_connection_failed",
    "err_tunnel_connection_failed",
    "err_proxy_auth_unsupported",
    "err_proxy_auth_requested",
    # What Chrome reports for a 407 through the relay: the provider refused
    # the username or password. Seen on 17 Nov 2026 with a wrong password.
    "err_invalid_auth_credentials",
    "err_proxy_certificate_invalid",
    "err_no_supported_proxies",
    "err_socks_connection_failed",
    "err_socks_connection_host_unreachable",
)


@dataclass(frozen=True)
class VisitorProxy:
    """Where the visitor Chrome's traffic goes instead of straight out."""

    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @property
    def needs_relay(self) -> bool:
        return self.username is not None

    @property
    def address(self) -> str:
        """The proxy without its credentials: what may be logged or shown."""
        return f"{self.scheme}://{self.host}:{self.port}"


def parse(value: str | None) -> VisitorProxy | None:
    """``BROWSER_VISITOR_PROXY`` as a proxy, or None when it is not set.

    Raises :class:`AdapterConfigError` for a value that cannot be used, and
    says why WITHOUT repeating the value: it has a password in it, and this
    message is stored and shown on Attention.
    """
    text = (value or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
        host, port = parts.hostname, parts.port
    except ValueError as exc:
        raise AdapterConfigError(
            "BROWSER_VISITOR_PROXY is not a proxy address. It should look like "
            "http://username:password@host:port."
        ) from exc
    scheme = parts.scheme.lower()
    if scheme not in _SCHEMES or not host:
        raise AdapterConfigError(
            "BROWSER_VISITOR_PROXY is not a proxy address. It should look like "
            "http://username:password@host:port (http, https, socks5 or socks4)."
        )
    username = unquote(parts.username) if parts.username else None
    password = unquote(parts.password) if parts.password else ""
    if username is not None and scheme not in _RELAYED_SCHEMES:
        raise AdapterConfigError(
            "BROWSER_VISITOR_PROXY has a username on a SOCKS proxy, which Chrome "
            "cannot sign in to. Use the provider's http:// address instead, or "
            "allow this server's address at the provider and leave the "
            "username out."
        )
    return VisitorProxy(
        scheme=scheme,
        host=host,
        port=port or _SCHEMES[scheme],
        username=username,
        password=password if username is not None else None,
    )


def chrome_argument(proxy: VisitorProxy) -> str:
    """The ``--proxy-server=`` flag that sends Chrome through ``proxy``."""
    if not proxy.needs_relay:
        return f"--proxy-server={proxy.address}"
    return f"--proxy-server=http://127.0.0.1:{_relay_port(proxy)}"


def explain(net_error_message: str, proxy: VisitorProxy) -> str:
    """A proxy failure in words somebody can act on, without the password."""
    code = next(
        (c for c in PROXY_NET_ERRORS if c in net_error_message.lower()), "proxy error"
    )
    return (
        f"The proxy this site is read through ({proxy.host}:{proxy.port}) did not "
        f"carry the page ({code.upper()}). Nothing reached the site itself. Check "
        f"that the proxy subscription is active and that BROWSER_VISITOR_PROXY "
        f"still has the right address, username and password."
    )


# -- the relay --------------------------------------------------------

_relays: dict[VisitorProxy, "_Relay"] = {}
_relays_lock = threading.Lock()


def _relay_port(proxy: VisitorProxy) -> int:
    """The local port relaying to ``proxy``; one relay per proxy per process."""
    with _relays_lock:
        relay = _relays.get(proxy)
        if relay is None:
            relay = _relays[proxy] = _Relay(proxy)
            log.info("visitor_proxy_relay_started", proxy=proxy.address, port=relay.port)
        return relay.port


class _Relay:
    """Accepts Chrome on 127.0.0.1 and forwards to the proxy, signed in."""

    def __init__(self, proxy: VisitorProxy) -> None:
        token = base64.b64encode(
            f"{proxy.username}:{proxy.password or ''}".encode()
        ).decode("ascii")
        header = f"Proxy-Authorization: Basic {token}\r\n".encode("ascii")

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:  # noqa: D401 - socketserver's name
                _serve(self.request, proxy, header)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

        self._server = Server(("127.0.0.1", 0), Handler)
        self.port: int = self._server.server_address[1]
        threading.Thread(
            target=self._server.serve_forever, name="visitor-proxy-relay", daemon=True
        ).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def stop_relays() -> None:
    """Stop every relay. For tests; a worker's relays end with its process."""
    with _relays_lock:
        for relay in _relays.values():
            relay.close()
        _relays.clear()


def _serve(client: socket.socket, proxy: VisitorProxy, header: bytes) -> None:
    upstream: socket.socket | None = None
    try:
        head, rest = _read_head(client)
        if head is None:
            return
        try:
            upstream = _connect(proxy)
        except OSError as exc:
            # Answered, not dropped: Chrome reports a refused tunnel as a
            # proxy failure, which open_page can name. A closed socket reads
            # as the site having sent nothing.
            log.warning("visitor_proxy_unreachable", proxy=proxy.address, error=str(exc))
            client.sendall(
                b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
            )
            return
        request_line, _, headers = head.partition(b"\r\n")
        upstream.sendall(request_line + b"\r\n" + header + headers + rest)
        back = threading.Thread(target=_copy, args=(upstream, client), daemon=True)
        back.start()
        _copy(client, upstream)
        back.join(timeout=5)
    except OSError:
        pass
    finally:
        for sock in (client, upstream):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def _read_head(client: socket.socket) -> tuple[bytes | None, bytes]:
    """The request line and headers Chrome sent, and anything after them."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = client.recv(65536)
        if not chunk or len(data) + len(chunk) > _MAX_HEAD_BYTES:
            return None, b""
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head + b"\r\n\r\n", rest


def _connect(proxy: VisitorProxy) -> socket.socket:
    sock = socket.create_connection(
        (proxy.host, proxy.port), timeout=_CONNECT_TIMEOUT_SECONDS
    )
    if proxy.scheme == "https":
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=proxy.host)
    # The timeout was for connecting. A tunnel sits idle between requests for
    # as long as the page keeps it open.
    sock.settimeout(None)
    return sock


def _copy(source: socket.socket, target: socket.socket) -> None:
    """Copy until ``source`` ends, then end both: a tunnel has no half-open use."""
    try:
        while data := source.recv(65536):
            target.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (source, target):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
