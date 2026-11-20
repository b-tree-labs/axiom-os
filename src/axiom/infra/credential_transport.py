# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where a sender may take its key.

A sender that holds a bearer key for a remote intake has two ways to hand it to
someone it did not mean to: send it in the clear, and follow a redirect to a
host it never chose. Both are one mistyped scheme or one misconfigured proxy
away, and neither shows in a sender's log.

- :func:`check_url` allows ``https`` anywhere, and ``http`` only to this machine
  or a private network (a node talking to its own edge on a compose network),
  deciding by what the host *resolves to*, not by how it is spelled.
- :func:`opener` builds a urllib opener that does not follow redirects, so a
  3xx comes back to the sender as the answer it is.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class PlaintextRefused(OSError):
    """The key would have gone over a network in the clear. An ``OSError`` so
    every sender already treats it as "hold the data and say why"."""


def _addresses(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return []  # unresolvable: the connection will fail by itself, loudly
    found = []
    for info in infos:
        try:
            found.append(ipaddress.ip_address(info[4][0].split("%")[0]))
        except ValueError:
            continue
    return found


def check_url(url: str) -> None:
    """Raise :class:`PlaintextRefused` unless a key may be sent to ``url``."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme != "http" or not parts.hostname:
        raise PlaintextRefused(
            f"{url!r} is not an http(s) address; set the intake address to https://<host>"
        )
    public = [a for a in _addresses(parts.hostname) if a.is_global]
    if public:
        raise PlaintextRefused(
            f"refusing to send the key to {parts.hostname} ({public[0]}) over plain http, "
            f"where anyone on the path can read it; set the intake address to "
            f"https://{parts.hostname}"
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None  # urllib then raises the 3xx as an HTTPError: the answer, not a detour


def opener(*, context: ssl.SSLContext | None = None) -> urllib.request.OpenerDirector:
    """An opener that never follows a redirect, verifying TLS (against
    ``context`` when given)."""
    handlers: list = [_NoRedirect()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


_DEFAULT: urllib.request.OpenerDirector | None = None


def default_opener() -> urllib.request.OpenerDirector:
    """One shared no-redirect opener for senders that verify TLS the usual way.

    Building an opener costs a few milliseconds (it reads the proxy settings), and
    a sender posts many times a minute from several threads; ``urlopen`` shared one
    too. Senders that trust an extra CA build their own with :func:`opener`.
    """
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = opener()
    return _DEFAULT


__all__ = ["PlaintextRefused", "check_url", "default_opener", "opener"]
