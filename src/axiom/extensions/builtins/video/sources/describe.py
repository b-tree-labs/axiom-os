# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An RTSP ``DESCRIBE``: the cheapest question that tells a camera apart from a network.

It answers what ``ffmpeg`` folds into one error: does anything listen, does it speak
RTSP, does it accept this login (Basic, or Digest per RFC 2617 with MD5), does it
serve this path, and which video codec does its SDP announce. Nothing is decoded
and no media is set up.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import ssl
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

DEFAULT_PORT = 554
DEFAULT_TLS_PORT = 322
_MAX_HEAD = 64 * 1024
_MAX_BODY = 1024 * 1024
#: Static RTP payload types for video (RFC 3551 table 5), for an SDP with no rtpmap.
_STATIC_VIDEO = {"26": "JPEG", "31": "H261", "32": "MPV", "34": "H263"}
_USER_AGENT = "axiom-video"
#: ``key="quoted value"`` or ``key=bare`` in a WWW-Authenticate challenge.
_PARAM = re.compile(r'(\w+)=(?:"([^"]*)"|([^,\s]*))')


class RtspProtocolError(OSError):
    """Something answered, but not in RTSP."""


@dataclass(frozen=True)
class DescribeResult:
    status: int
    reason: str
    headers: dict[str, list[str]] = field(default_factory=dict)
    sdp: str = ""
    challenge: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def auth_required(self) -> bool:
        return self.status == 401


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()  # noqa: S324 - RFC 2617 Digest is MD5


def _connect(host: str, port: int, *, tls: bool, timeout: float) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=timeout)
    if not tls:
        return sock
    return ssl.create_default_context().wrap_socket(sock, server_hostname=host)


def _read_response(sock: socket.socket) -> DescribeResult:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            if not buf:
                raise RtspProtocolError("the server closed the connection without answering")
            break
        buf += chunk
        if len(buf) > _MAX_HEAD:
            raise RtspProtocolError("the response head is too large to be RTSP")
    head, _, body = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status_line = lines[0].split(" ", 2)
    if (
        len(status_line) < 2
        or not status_line[0].startswith("RTSP/")
        or not status_line[1].isdigit()
    ):
        raise RtspProtocolError(f"not an RTSP response: {lines[0][:60]!r}")
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers.setdefault(key.strip().lower(), []).append(value.strip())
    length = int((headers.get("content-length") or ["0"])[0] or 0)
    if length > _MAX_BODY:
        raise RtspProtocolError("the SDP body is too large")
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    challenges = headers.get("www-authenticate") or []
    return DescribeResult(
        status=int(status_line[1]),
        reason=status_line[2] if len(status_line) > 2 else "",
        headers=headers,
        sdp=body[:length].decode("utf-8", errors="replace"),
        challenge=_pick_challenge(challenges),
    )


def _pick_challenge(challenges: list[str]) -> str | None:
    """Digest if offered (the login never crosses the wire), else Basic, else whatever came."""
    for wanted in ("digest", "basic"):
        for challenge in challenges:
            if challenge.lower().startswith(wanted):
                return challenge
    return challenges[0] if challenges else None


def _authorization(
    challenge: str, method: str, uri: str, username: str, password: str
) -> str | None:
    scheme = challenge.split(" ", 1)[0].lower()
    if scheme == "basic":
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        return f"Basic {token}"
    if scheme != "digest":
        return None
    fields = {key: quoted or bare for key, quoted, bare in _PARAM.findall(challenge)}
    realm, nonce = fields.get("realm", ""), fields.get("nonce", "")
    ha1 = _md5(f"{username}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']
    qops = [q.strip() for q in fields.get("qop", "").split(",") if q.strip()]
    if "auth" in qops:
        cnonce, nc = os.urandom(8).hex(), "00000001"
        response = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        parts += [f'response="{response}"', "qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
    else:
        parts.append(f'response="{_md5(f"{ha1}:{nonce}:{ha2}")}"')
    if fields.get("opaque"):
        parts.append(f'opaque="{fields["opaque"]}"')
    parts.append("algorithm=MD5")
    return "Digest " + ", ".join(parts)


def _send(sock: socket.socket, method: str, uri: str, cseq: int, authorization: str | None) -> None:
    lines = [
        f"{method} {uri} RTSP/1.0",
        f"CSeq: {cseq}",
        "Accept: application/sdp",
        f"User-Agent: {_USER_AGENT}",
    ]
    if authorization:
        lines.append(f"Authorization: {authorization}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))


def describe(
    url: str,
    *,
    username: str | None = None,
    password: str | None = None,
    timeout: float = 5.0,
) -> DescribeResult:
    """``DESCRIBE url`` and, if challenged and a login is given, once more with it.

    Raises ``socket.gaierror`` when the host does not resolve, ``OSError`` when
    nothing answers, and :class:`RtspProtocolError` when what answers is not RTSP.
    """
    parts = urlsplit(url)
    tls = parts.scheme.lower() == "rtsps"
    host = parts.hostname or ""
    port = parts.port or (DEFAULT_TLS_PORT if tls else DEFAULT_PORT)
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    if ":" in host:  # an IPv6 literal goes back in brackets
        netloc = f"[{host}]" + (f":{parts.port}" if parts.port else "")
    uri = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))

    with _connect(host, port, tls=tls, timeout=timeout) as sock:
        _send(sock, "DESCRIBE", uri, 1, None)
        first = _read_response(sock)
        if not first.auth_required or username is None or not first.challenge:
            return first
        authorization = _authorization(first.challenge, "DESCRIBE", uri, username, password or "")
        if authorization is None:
            return first
        try:
            _send(sock, "DESCRIBE", uri, 2, authorization)
            return _read_response(sock)
        except (RtspProtocolError, OSError):
            pass  # some cameras close after a 401; ask again on a fresh connection
    with _connect(host, port, tls=tls, timeout=timeout) as sock:
        _send(sock, "DESCRIBE", uri, 1, authorization)
        return _read_response(sock)


def sdp_video_codec(sdp: str) -> str | None:
    """The encoding name of the first video stream in an SDP body (``H264``, ``H265``, ``JPEG``...)."""
    lines = [line.strip() for line in sdp.replace("\r\n", "\n").split("\n")]
    for i, line in enumerate(lines):
        if not line.startswith("m=video"):
            continue
        fields = line.split()
        payloads = fields[3:] if len(fields) > 3 else []
        if not payloads:
            return None
        first = payloads[0]
        for attr in lines[i + 1 :]:
            if attr.startswith("m="):
                break
            match = re.match(rf"a=rtpmap:{re.escape(first)}\s+([^/\s]+)", attr)
            if match:
                return match.group(1).upper()
        return _STATIC_VIDEO.get(first)
    return None


__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_TLS_PORT",
    "DescribeResult",
    "RtspProtocolError",
    "describe",
    "sdp_video_codec",
]
