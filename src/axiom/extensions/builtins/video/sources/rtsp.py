# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Source kind ``rtsp``: the live stream of an IP camera (ephemeral; spec-video §rtsp).

Params: ``url`` (required) and ``transport`` (``tcp``, the default, or ``udp``). A
URL that carries a login is refused with the vault instruction rather than
scrubbed: by the time it would be scrubbed the password has already been typed
into a file or a shell. The login comes from the connector's ``credential_ref``,
as ``username:password`` (or a JSON object with ``username`` and ``password``),
and reaches the ``ffmpeg`` input arguments only, percent-encoded.

``detect`` is an RTSP ``DESCRIBE`` with that login; ``verify`` is one frame through
the same ``ffmpeg`` pipe capture uses.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit, urlunsplit

from . import InvalidSourceConfig, SourceKind, SourceParam, VerifyReport

if TYPE_CHECKING:
    from axiom.extensions.builtins.connector.detect import DetectResult

    from ..ffmpeg import FFmpegTool

KIND_NAME = "rtsp"
DEFAULT_TRANSPORT = "tcp"
TRANSPORTS = ("tcp", "udp")
SCHEMES = ("rtsp", "rtsps")
DEFAULT_TIMEOUT_S = 20.0
DESCRIBE_TIMEOUT_S = 8.0

#: What ffmpeg may open for an rtsp source read through a concat script on a pipe.
PROTOCOLS = "pipe,rtsp,rtsps,tcp,udp,rtp,tls,crypto"

#: The rungs detect reports, in order; diagnose climbs them.
DETECT_STEPS = ("url-resolves", "describe-answers", "auth-accepted", "codec-decodable")

#: SDP encoding name -> the ffmpeg decoder that reads it.
_DECODERS = {
    "H264": "h264",
    "H265": "hevc",
    "HEVC": "hevc",
    "JPEG": "mjpeg",
    "MP4V-ES": "mpeg4",
    "MPV": "mpeg2video",
    "H263": "h263",
    "H263-1998": "h263",
    "H263-2000": "h263",
    "VP8": "vp8",
    "VP9": "vp9",
    "AV1": "av1",
}


def _cli() -> str:
    from axiom.extensions.builtins.video.naming import cli_name

    return cli_name()


def vault_instruction() -> str:
    """Where a camera login goes instead of a URL. ``<source name>`` is the caller's to fill."""
    cli = _cli()
    return (
        f"store the login in the vault with `{cli} vault set video-<source name>` (it reads "
        "username:password from stdin, never the command line) and pass "
        "--credential-ref foreign://video-<source name>; a URL never carries it"
    )


def parse_credential(secret: Any) -> tuple[str, str]:
    """``(username, password)`` from a resolved secret: ``user:password`` or JSON.

    Refused without echoing the value when it is neither.
    """
    raw = getattr(secret, "value", secret)
    text = (
        raw.decode("utf-8", "replace") if isinstance(raw, bytes | bytearray) else str(raw or "")
    ).strip()
    fix = vault_instruction()
    if not text:
        raise InvalidSourceConfig("the stored camera login is empty", fix=fix)
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            raise InvalidSourceConfig(
                "the stored camera login looks like JSON but does not parse", fix=fix
            ) from None
        username, password = data.get("username"), data.get("password")
        if not (isinstance(username, str) and username and isinstance(password, str)):
            raise InvalidSourceConfig(
                "the stored camera login needs both a username and a password", fix=fix
            )
        return username, password
    username, sep, password = text.partition(":")
    if not sep or not username:
        raise InvalidSourceConfig(
            "the stored camera login is not in the form username:password", fix=fix
        )
    return username, password


def check_url(url: str) -> None:
    """Refuse anything that is not an ``rtsp://`` or ``rtsps://`` address with a host and no login."""
    hint = "pass --url rtsp://host[:port]/path"
    if not isinstance(url, str) or not url.strip():
        raise InvalidSourceConfig("an rtsp source needs a url", fix=hint)
    parts = urlsplit(url.strip())
    if "@" in parts.netloc:
        raise InvalidSourceConfig(
            "the url carries a login, and a login is never kept in a connector file",
            fix=vault_instruction(),
        )
    if parts.scheme.lower() not in SCHEMES:
        raise InvalidSourceConfig(
            f"an rtsp source's url starts rtsp:// or rtsps://, not {parts.scheme or 'nothing'}",
            fix=hint,
        )
    if not parts.hostname:
        raise InvalidSourceConfig("the url names no host", fix=hint)
    try:
        parts.port  # noqa: B018 - raises on a port that is not a number
    except ValueError:
        raise InvalidSourceConfig("the url's port is not a number", fix=hint) from None


class RtspSource:
    """One camera stream."""

    kind = KIND_NAME
    replayable = False

    url: str
    transport: str

    def __init__(
        self,
        url: str,
        *,
        transport: str = DEFAULT_TRANSPORT,
        username: str | None = None,
        password: str | None = None,
        tool: FFmpegTool | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        check_url(url)
        transport = (transport or DEFAULT_TRANSPORT).strip().lower()
        if transport not in TRANSPORTS:
            raise InvalidSourceConfig(
                f"transport must be tcp or udp, got {transport!r}",
                fix='set transport = "tcp" (or "udp")',
            )
        self.url = url.strip()
        self.transport = transport
        self._username = username or None
        self._password = password if username else None
        self._tool = tool
        self._timeout_s = timeout_s

    def __repr__(self) -> str:
        return f"RtspSource(url={self.url!r}, transport={self.transport!r}, authenticated={self.authenticated})"

    __str__ = __repr__

    @property
    def authenticated(self) -> bool:
        return self._username is not None

    def _secrets(self) -> list[str]:
        return [self._password] if self._password else []

    def _tool_or_default(self) -> FFmpegTool:
        from ..ffmpeg import FFmpegTool

        return self._tool or FFmpegTool.locate()

    def _url_with_login(self) -> str:
        if not self._username:
            return self.url
        parts = urlsplit(self.url)
        login = f"{quote(self._username, safe='')}:{quote(self._password or '', safe='')}"
        return urlunsplit(
            (parts.scheme, f"{login}@{parts.netloc}", parts.path, parts.query, parts.fragment)
        )

    def ffmpeg_secret_input(self) -> bytes | None:
        """The ffconcat script naming the URL with its login, or ``None`` with no login.

        ffmpeg takes a login only in the URL, and argv is readable by every user of the
        machine, so a URL with a login reaches ffmpeg as this script on an inherited
        pipe (``-f concat -i pipe:N``), never as an argument.
        """
        if not self.authenticated:
            return None
        url = self._url_with_login().replace(
            "'", "'\\''"
        )  # ffconcat quoting; the login is %-encoded
        return (
            f"ffconcat version 1.0\nfile '{url}'\noption rtsp_transport {self.transport}\n".encode()
        )

    def ffmpeg_input_args(self) -> list[str]:
        if not self.authenticated:
            return ["-rtsp_transport", self.transport, "-i", self.url]
        from ..ffmpeg import SECRET_INPUT

        return ["-f", "concat", "-safe", "0", "-protocol_whitelist", PROTOCOLS, "-i", SECRET_INPUT]

    def redacted(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "transport": self.transport,
            "authenticated": self.authenticated,
        }

    def verify(self) -> VerifyReport:
        return self._tool_or_default().probe(
            self.ffmpeg_input_args(),
            secret_input=self.ffmpeg_secret_input(),
            secrets=self._secrets(),
            timeout=self._timeout_s,
        )

    def detect(self) -> DetectResult:
        from axiom.extensions.builtins.connector.detect import ConnectorState, DetectResult

        from ..ffmpeg import install_hint
        from .describe import (
            DEFAULT_PORT,
            DEFAULT_TLS_PORT,
            RtspProtocolError,
            describe,
            sdp_video_codec,
        )

        parts = urlsplit(self.url)
        host = parts.hostname or ""
        port = parts.port or (DEFAULT_TLS_PORT if parts.scheme.lower() == "rtsps" else DEFAULT_PORT)
        steps: list[dict[str, Any]] = []
        state = ConnectorState.CONFIGURED

        def rung(step: str, ok: bool, detail: str, fix: str = "") -> bool:
            steps.append({"step": step, "ok": ok, "detail": detail, "fix": "" if ok else fix})
            return ok

        def finish() -> DetectResult:
            for name in DETECT_STEPS[len(steps) :]:
                steps.append(
                    {
                        "step": name,
                        "ok": None,
                        "detail": "skipped: an earlier rung failed",
                        "fix": "",
                    }
                )
            failed = next((s for s in steps if s["ok"] is False), None)
            if failed is None:
                return DetectResult(
                    ConnectorState.CONFIGURED,
                    summary=f"{host}:{port} answered DESCRIBE, accepted the login and sends a codec ffmpeg decodes",
                    details={"steps": steps},
                )
            return DetectResult(
                state, summary=failed["detail"], next_action=failed["fix"], details={"steps": steps}
            )

        try:
            address = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0]
        except (socket.gaierror, UnicodeError) as exc:
            state = ConnectorState.BROKEN
            rung(
                "url-resolves",
                False,
                f"{host} does not resolve ({getattr(exc, 'strerror', None) or exc})",
                "check the host name in the source URL, or use the camera's IP address",
            )
            return finish()
        rung("url-resolves", True, f"{host} is {address}")

        try:
            answer = describe(
                self.url,
                username=self._username,
                password=self._password,
                timeout=min(self._timeout_s, DESCRIBE_TIMEOUT_S),
            )
        except RtspProtocolError as exc:
            state = ConnectorState.BROKEN
            rung(
                "describe-answers",
                False,
                f"something at {host}:{port} answered, but not in RTSP ({exc})",
                f"check that {port} is the camera's RTSP port (554 unless it was changed)",
            )
            return finish()
        except OSError as exc:
            state = ConnectorState.BROKEN
            rung(
                "describe-answers",
                False,
                f"nothing answered RTSP at {host}:{port} ({getattr(exc, 'strerror', None) or type(exc).__name__})",
                f"check the camera is powered and on this network, and that it serves RTSP on port {port} "
                "(554 unless it was changed)",
            )
            return finish()
        rung(
            "describe-answers",
            True,
            f"{host}:{port} answered DESCRIBE with {answer.status} {answer.reason}".strip(),
        )

        if answer.status == 401 and not self.authenticated:
            state = ConnectorState.PARTIAL
            rung(
                "auth-accepted",
                False,
                "the camera asks for a login and none is set",
                vault_instruction(),
            )
            return finish()
        if answer.status == 401:
            state = ConnectorState.BROKEN
            rung(
                "auth-accepted",
                False,
                "the camera refused the stored login",
                f"store the right login with `{_cli()} vault set <credential name>` (username:password) "
                "under the name the source's credential_ref points at",
            )
            return finish()
        if answer.status == 404:
            state = ConnectorState.BROKEN
            rung(
                "auth-accepted",
                False,
                f"the camera serves no stream at {parts.path or '/'}",
                "check the stream path in the URL against the camera's manual or its web page",
            )
            return finish()
        if not answer.ok:
            state = ConnectorState.BROKEN
            rung(
                "auth-accepted",
                False,
                f"the camera answered {answer.status} {answer.reason}".strip(),
                "check the URL; the camera's manual names its RTSP paths",
            )
            return finish()
        rung(
            "auth-accepted",
            True,
            "the camera accepted the login" if self.authenticated else "the camera needs no login",
        )

        codec = sdp_video_codec(answer.sdp)
        state = ConnectorState.BROKEN
        if codec is None:
            rung(
                "codec-decodable",
                False,
                "the camera's SDP announces no video stream",
                "check the URL names the camera's video stream",
            )
            return finish()
        tool = self._tool_or_default()
        if not tool.available:
            rung(
                "codec-decodable",
                False,
                f"the camera sends {codec}, and ffmpeg is not installed to decode it",
                install_hint(),
            )
            return finish()
        decoder = _DECODERS.get(codec, codec.lower())
        if decoder not in tool.decoders():
            rung(
                "codec-decodable",
                False,
                f"the camera sends {codec} and this ffmpeg has no {decoder} decoder",
                f"set the camera's stream to H.264, or install an ffmpeg build with the {decoder} decoder",
            )
            return finish()
        rung(
            "codec-decodable",
            True,
            f"the camera sends {codec}, which ffmpeg decodes with {decoder}",
        )
        return finish()


def build(source_params: Mapping[str, Any], credential: Any = None) -> RtspSource:
    """The kind's factory: ``(source_params, credential)`` to an :class:`RtspSource`."""
    params = dict(source_params)
    username = password = None
    if credential is not None:
        username, password = parse_credential(credential)
    return RtspSource(
        str(params.get("url") or ""),
        transport=str(params.get("transport") or DEFAULT_TRANSPORT),
        username=username,
        password=password,
    )


KIND = SourceKind(
    kind=KIND_NAME,
    replayable=False,
    factory=build,
    params=(
        SourceParam(
            "url", "rtsp://host[:port]/path, with no username or password in it", required=True
        ),
        SourceParam(
            "transport", "how RTP is carried", default=DEFAULT_TRANSPORT, choices=TRANSPORTS
        ),
    ),
    description="the live RTSP stream of an IP camera",
)


__all__ = [
    "DEFAULT_TRANSPORT",
    "DETECT_STEPS",
    "KIND",
    "KIND_NAME",
    "PROTOCOLS",
    "RtspSource",
    "TRANSPORTS",
    "build",
    "check_url",
    "parse_credential",
    "vault_instruction",
]
