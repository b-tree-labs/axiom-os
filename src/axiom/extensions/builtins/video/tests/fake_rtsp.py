# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A camera that only answers ``DESCRIBE``: enough for detect and diagnose, no binaries.

It speaks RTSP/1.0 over TCP on 127.0.0.1, answers ``OPTIONS`` and ``DESCRIBE``,
and asks for a login the way IP cameras do: ``401`` with a ``WWW-Authenticate``
challenge (Basic or Digest, RFC 2617 MD5), then ``200`` with an SDP body once the
``Authorization`` header is right. A path it does not serve is ``404``.

Every request is recorded (method, URL, the Authorization scheme) so a test can
see what the client sent without the test reading a password back out of it.
"""

from __future__ import annotations

import base64
import hashlib
import re
import socketserver
import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit

SDP = (
    "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=fake\r\nc=IN IP4 0.0.0.0\r\nt=0 0\r\n"
    "m=video 0 RTP/AVP 96\r\na=control:trackID=0\r\na=rtpmap:96 {codec}/90000\r\n"
)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()  # noqa: S324 - RFC 2617 is MD5


@dataclass
class Seen:
    method: str
    url: str
    auth_scheme: str | None


@dataclass
class FakeCamera:
    """``with FakeCamera(users={"cam": "pw"}) as cam: describe(cam.url("/t"), ...)``."""

    users: dict[str, str] = field(default_factory=dict)
    auth: str = "basic"  # basic | digest
    paths: tuple[str, ...] = ("/t",)
    codec: str = "H264"
    realm: str = "fakecam"
    nonce: str = "0123456789abcdef"
    seen: list[Seen] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        assert self._server is not None, "use FakeCamera as a context manager"
        return self._server.server_address[1]

    def url(self, path: str = "/t") -> str:
        return f"rtsp://127.0.0.1:{self.port}{path}"

    # ---- server ------------------------------------------------------------

    def __enter__(self) -> FakeCamera:
        camera = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:  # one connection, any number of requests
                while True:
                    head = b""
                    while not head.endswith(b"\r\n\r\n"):
                        chunk = self.rfile.readline()
                        if not chunk:
                            return
                        head += chunk
                    reply = camera._answer(head.decode("latin-1"))
                    self.wfile.write(reply.encode("latin-1"))
                    self.wfile.flush()

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # ---- protocol ----------------------------------------------------------

    def _answer(self, head: str) -> str:
        lines = head.split("\r\n")
        method, url, _version = (lines[0].split(" ") + ["", "", ""])[:3]
        headers = {}
        for line in lines[1:]:
            key, _, value = line.partition(":")
            if key:
                headers[key.strip().lower()] = value.strip()
        cseq = headers.get("cseq", "0")
        authz = headers.get("authorization")
        self.seen.append(Seen(method, url, authz.split(" ", 1)[0] if authz else None))

        if method == "OPTIONS":
            return f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nPublic: OPTIONS, DESCRIBE\r\n\r\n"
        if method != "DESCRIBE":
            return f"RTSP/1.0 405 Method Not Allowed\r\nCSeq: {cseq}\r\n\r\n"
        if self.users and not self._authorized(method, url, authz):
            if self.auth == "digest":
                challenge = f'Digest realm="{self.realm}", nonce="{self.nonce}", algorithm="MD5"'
            else:
                challenge = f'Basic realm="{self.realm}"'
            return f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\nWWW-Authenticate: {challenge}\r\n\r\n"
        if (urlsplit(url).path or "/") not in self.paths:
            return f"RTSP/1.0 404 Not Found\r\nCSeq: {cseq}\r\n\r\n"
        body = SDP.format(codec=self.codec)
        return (
            f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Type: application/sdp\r\n"
            f"Content-Length: {len(body)}\r\n\r\n{body}"
        )

    def _authorized(self, method: str, url: str, authz: str | None) -> bool:
        if not authz:
            return False
        scheme, _, rest = authz.partition(" ")
        if self.auth == "basic" and scheme.lower() == "basic":
            try:
                user, _, password = base64.b64decode(rest).decode().partition(":")
            except Exception:  # noqa: BLE001
                return False
            return self.users.get(user) == password
        if self.auth == "digest" and scheme.lower() == "digest":
            fields = dict(re.findall(r'(\w+)="?([^",]*)"?', rest))
            password = self.users.get(fields.get("username", ""))
            if password is None or fields.get("nonce") != self.nonce:
                return False
            ha1 = _md5(f"{fields['username']}:{self.realm}:{password}")
            ha2 = _md5(f"{method}:{fields.get('uri', url)}")
            if fields.get("qop"):
                expected = _md5(
                    f"{ha1}:{self.nonce}:{fields['nc']}:{fields['cnonce']}:{fields['qop']}:{ha2}"
                )
            else:
                expected = _md5(f"{ha1}:{self.nonce}:{ha2}")
            return fields.get("response") == expected
        return False


__all__ = ["FakeCamera", "Seen"]
