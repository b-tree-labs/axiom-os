# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``ffmpeg`` and ``ffprobe`` as subprocesses: locate them, build their commands, read them.

ADR-240 D2: decoding happens in a subprocess, never in the Axiom process, so a
decoder bug or a codec hang is killed and restarted rather than taking the
service down. Everything after a kind's input arguments is the same for every
kind (spec-video §Design).

The input arguments of a source that needs a login carry it, because ``ffmpeg``
takes it nowhere else. So a command is never logged as is (:func:`redact_command`),
and anything ``ffmpeg`` prints, which echoes its input URL login included, is
passed through :func:`scrub` before it is shown, stored or raised.

``AXIOM_VIDEO_FFMPEG`` and ``AXIOM_VIDEO_FFPROBE`` name the binaries when they are
not on ``PATH``; set to a path where nothing is, they mean "not installed" and are
never silently replaced by whatever is on ``PATH``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote, unquote

from .naming import command as _command
from .sources import VerifyReport, VideoSourceError

FFMPEG_ENV = "AXIOM_VIDEO_FFMPEG"
FFPROBE_ENV = "AXIOM_VIDEO_FFPROBE"
DEFAULT_PROBE_TIMEOUT_S = 20.0

#: Where a kind's input arguments name a secret input: replaced, at spawn, by
#: ``pipe:<fd>``, a pipe the child inherits and the kind's ``ffmpeg_secret_input()``
#: is written to. What is in it (a URL with its login) is never on a command line.
SECRET_INPUT = "pipe:{secret_fd}"

#: The output options every capture shares, after the kind's input arguments.
RAW_OUTPUT = ("-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1")

#: ``scheme://user:password@`` in any text: the login a URL carries.
_LOGIN = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/@\s]*)@")
_MASK = "***"


def _cli() -> str:
    from axiom.extensions.builtins.video.naming import cli_name

    return cli_name()


class FFmpegMissing(VideoSourceError):
    """``ffmpeg`` or ``ffprobe`` is not installed here; ``fix`` is the install line for this platform."""

    def __init__(self, message: str = "", *, platform: str | None = None) -> None:
        super().__init__(
            message
            or "ffmpeg and ffprobe are not installed on this machine, and decoding is an ffmpeg subprocess",
            code="ffmpeg-missing",
            fix=install_hint(platform),
        )


def install_hint(platform: str | None = None) -> str:
    """How to install ``ffmpeg`` on this platform (or ``platform``), and how to point at one elsewhere."""
    plat = platform or sys.platform
    if plat.startswith("darwin"):
        how = "`brew install ffmpeg`"
    elif plat.startswith("win"):
        how = "`winget install --id Gyan.FFmpeg`"
    else:
        how = "`sudo apt install ffmpeg` on Debian or Ubuntu, or your distribution's ffmpeg package"
    return f"install ffmpeg ({how}), or set {FFMPEG_ENV} and {FFPROBE_ENV} to where ffmpeg and ffprobe are"


def secrets_in(args: Iterable[str]) -> list[str]:
    """The passwords carried by any URL login in ``args``, raw and decoded."""
    found: list[str] = []
    for arg in args:
        for match in _LOGIN.finditer(str(arg)):
            _user, sep, password = match.group(2).partition(":")
            if sep and password:
                found += [password, unquote(password)]
    return found


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """``text`` with every URL login removed and every known secret (raw or percent-encoded) masked."""
    known = [s for s in secrets if s]
    out = _LOGIN.sub(lambda m: f"{m.group(1)}{_MASK}@", text or "")
    for secret in sorted({*known, *(quote(s, safe="") for s in known)}, key=len, reverse=True):
        out = out.replace(secret, _MASK)
    return out


def redact_command(cmd: Sequence[str], secrets: Iterable[str] = ()) -> list[str]:
    """A command safe to record: :func:`scrub` applied to every argument."""
    known = [*secrets, *secrets_in(cmd)]
    return [scrub(str(arg), known) for arg in cmd]


class SecretInput:
    """The secret part of an input, delivered on a pipe the child inherits.

    ``with SecretInput(cmd, secret) as (argv, pass_fds): Popen(argv, pass_fds=pass_fds)``:
    the ``SECRET_INPUT`` element of ``cmd`` becomes ``pipe:<fd>``, the secret is
    written to that pipe and its write end closed, and the parent's copy of the read
    end is closed once the child holds it. Nothing secret is in ``argv``.
    """

    def __init__(self, cmd: Sequence[str], secret: bytes | None) -> None:
        self.cmd = list(cmd)
        self.secret = secret
        self._read: int | None = None
        if SECRET_INPUT in self.cmd and secret is None:
            raise ValueError("the input names a secret input, and no secret was given for it")

    def described(self, argv: Sequence[str]) -> list[str]:
        """``argv`` for a record a person reads: the input as the URL it is, scrubbed.

        ``-f concat -safe 0 -protocol_whitelist ... -i pipe:11`` says nothing about which
        camera a decoder reads (and fills the record's length); ``-i rtsp://***@host/path
        (on pipe:11)`` does.
        """
        if not self.secret:
            return list(argv)
        text = self.secret.decode("utf-8", "replace")
        urls = [scrub(u, self.secrets) for u in re.findall(r"[a-z][a-z0-9+.\-]*://[^\s']+", text)]
        out = list(argv)
        for i, arg in enumerate(out):
            if arg.startswith("pipe:") and arg != "pipe:1" and i > 0 and out[i - 1] == "-i":
                out[i] = f"{', '.join(urls) or 'a secret input'} (on {arg})"
                head = out[: i - 1]
                for flag, takes_value in (
                    ("-protocol_whitelist", True),
                    ("-safe", True),
                    ("-f", True),
                ):
                    if len(head) >= 2 and head[-2] == flag and takes_value:
                        head = head[:-2]
                out = [*head, *out[i - 1 :]]
                break
        return out

    @property
    def secrets(self) -> list[str]:
        """What to scrub from anything the child prints: the logins in the secret."""
        return secrets_in([self.secret.decode("utf-8", "replace")]) if self.secret else []

    def __enter__(self) -> tuple[list[str], tuple[int, ...]]:
        if SECRET_INPUT not in self.cmd:
            return self.cmd, ()
        read, write = os.pipe()
        try:
            # A script is a few hundred bytes, far under a pipe's buffer: this cannot block.
            os.write(write, self.secret or b"")
        finally:
            os.close(write)
        self._read = read
        return [f"pipe:{read}" if a == SECRET_INPUT else a for a in self.cmd], (read,)

    def __exit__(self, *exc: object) -> None:
        if self._read is not None:
            with contextlib.suppress(OSError):
                os.close(self._read)
            self._read = None


def classify_failure(stderr: str) -> tuple[str, str]:
    """``(code, fix)`` for what ``ffmpeg`` or ``ffprobe`` said when it failed."""
    low = (stderr or "").lower()
    cli = _cli()
    if "401" in low or "unauthorized" in low or "authorization failed" in low:
        return "auth-failed", (
            f"the source refused the login: store the right one with `{cli} vault set <credential name>` "
            "(it reads username:password from stdin) and check the source's credential_ref names it"
        )
    if any(
        m in low for m in ("invalid data found", "unsupported codec", "could not find codec")
    ) or ("decoder" in low and "not found" in low):
        return "codec-unsupported", (
            "this ffmpeg cannot decode the stream's codec: set the source to H.264, or install an ffmpeg "
            "build that has the decoder"
        )
    if "461" in low or "unsupported transport" in low:
        return "transport-unsupported", (
            "the source does not accept that transport: set transport to tcp (or udp) in the source, "
            "under [connector.params.source]"
        )
    if "404" in low or "not found" in low:
        return "not-found", "the source has no stream at that path: check the path in the URL"
    if any(
        m in low
        for m in ("failed to resolve", "name or service not known", "name resolution", "nodename")
    ):
        return (
            "unreachable",
            "the host name in the URL does not resolve: check it, or use the source's IP address",
        )
    if any(
        m in low
        for m in (
            "connection refused",
            "timed out",
            "no route to host",
            "network is unreachable",
            "connection reset",
        )
    ):
        return "unreachable", (
            "nothing answered at that address: check the source is powered and on this network, and the "
            "host and port in the URL"
        )
    return (
        "error",
        f"run `{_command()} diagnose <name>`: it names the first thing that is wrong and its fix",
    )


def _last_lines(text: str, n: int = 3) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return " / ".join(lines[-n:])


def _rate(value: object) -> float | None:
    num, _, den = str(value or "").partition("/")
    try:
        rate = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


@dataclass(frozen=True)
class FFmpegTool:
    """Where ``ffmpeg`` and ``ffprobe`` are (``None`` for absent), and what to run on them."""

    ffmpeg: str | None
    ffprobe: str | None

    @classmethod
    def locate(cls, env: Mapping[str, str] | None = None) -> FFmpegTool:
        env = os.environ if env is None else env

        def find(var: str, exe: str) -> str | None:
            override = (env.get(var) or "").strip()
            if override:
                return (
                    override if os.path.isfile(override) and os.access(override, os.X_OK) else None
                )
            return shutil.which(exe, path=env.get("PATH"))

        return cls(ffmpeg=find(FFMPEG_ENV, "ffmpeg"), ffprobe=find(FFPROBE_ENV, "ffprobe"))

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)

    def require(self) -> None:
        """Raise :class:`FFmpegMissing` unless both binaries are here."""
        if not self.available:
            missing = [n for n, p in (("ffmpeg", self.ffmpeg), ("ffprobe", self.ffprobe)) if not p]
            raise FFmpegMissing(
                f"{' and '.join(missing)} not found on this machine; decoding is an ffmpeg subprocess"
            )

    def version(self) -> str:
        self.require()
        done = self._run([str(self.ffmpeg), "-version"], timeout=10)
        return (done[1].decode("utf-8", "replace").splitlines() or ["ffmpeg (version unknown)"])[
            0
        ].strip()

    def decoders(self) -> frozenset[str]:
        """The video decoders this ``ffmpeg`` was built with."""
        self.require()
        _rc, out, _err = self._run([str(self.ffmpeg), "-hide_banner", "-decoders"], timeout=10)
        names = set()
        for line in out.decode("utf-8", "replace").splitlines():
            match = re.match(r"^\s*V[.A-Z]{5}\s+(\S+)", line)
            if match and match.group(1) != "=":
                names.add(match.group(1))
        return frozenset(names)

    def probe_command(self, input_args: Sequence[str]) -> list[str]:
        return [
            str(self.ffprobe),
            "-v",
            "error",
            *input_args,
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,avg_frame_rate,r_frame_rate",
            "-of",
            "json",
        ]

    def grab_command(self, input_args: Sequence[str]) -> list[str]:
        """One frame, through exactly the pipe capture uses."""
        return [
            str(self.ffmpeg),
            "-nostdin",
            "-loglevel",
            "error",
            *input_args,
            "-frames:v",
            "1",
            *RAW_OUTPUT,
        ]

    def capture_command(self, input_args: Sequence[str], *, fps_cap: int) -> list[str]:
        return [
            str(self.ffmpeg),
            "-nostdin",
            "-loglevel",
            "error",
            *input_args,
            "-vf",
            f"fps={fps_cap}",
            *RAW_OUTPUT,
        ]

    def probe(
        self,
        input_args: Sequence[str],
        *,
        secret_input: bytes | None = None,
        secrets: Iterable[str] = (),
        timeout: float = DEFAULT_PROBE_TIMEOUT_S,
    ) -> VerifyReport:
        """Ask ``ffprobe`` what the stream is, then decode one frame of it.

        Raises :class:`VideoSourceError` (classified, scrubbed) or :class:`FFmpegMissing`.
        """
        self.require()
        known = [*secrets, *secrets_in(input_args), *SecretInput(input_args, secret_input).secrets]

        rc, out, err = self._run(
            self.probe_command(input_args),
            timeout=timeout,
            secrets=known,
            secret_input=secret_input,
        )
        if rc != 0:
            code, fix = classify_failure(err)
            raise VideoSourceError(
                f"ffprobe could not read the stream: {_last_lines(err) or f'exit {rc}'}",
                code=code,
                fix=fix,
            )
        try:
            streams = json.loads(out.decode("utf-8", "replace") or "{}").get("streams") or []
        except ValueError:
            streams = []
        if not streams:
            raise VideoSourceError(
                "the source has no video stream ffprobe can see",
                code="no-frame",
                fix="check the URL names a video stream (some sources serve audio or metadata on other paths)",
            )
        stream = streams[0]
        try:
            width, height = int(stream["width"]), int(stream["height"])
        except (KeyError, TypeError, ValueError):
            raise VideoSourceError(
                "ffprobe found a video stream but not its size",
                code="no-frame",
                fix="the source has not sent a decodable picture yet; try again, or check its encoder settings",
            ) from None
        codec = str(stream.get("codec_name") or "unknown")
        fps = _rate(stream.get("avg_frame_rate")) or _rate(stream.get("r_frame_rate"))

        started = time.monotonic()
        rc, out, err = self._run(
            self.grab_command(input_args), timeout=timeout, secrets=known, secret_input=secret_input
        )
        latency_ms = round((time.monotonic() - started) * 1000.0, 1)
        if len(out) < width * height * 3:
            if rc != 0 or err.strip():
                code, fix = classify_failure(err)
                raise VideoSourceError(
                    f"ffmpeg decoded no frame: {_last_lines(err) or f'exit {rc}'}",
                    code=code,
                    fix=fix,
                )
            raise VideoSourceError(
                f"the stream answered but no whole {width}x{height} frame came out of it",
                code="no-frame",
                fix="the source may not have sent a keyframe yet: try again, and check its keyframe interval",
            )
        return VerifyReport(codec=codec, width=width, height=height, fps=fps, latency_ms=latency_ms)

    # ---- running ---------------------------------------------------------

    @staticmethod
    def _run(
        cmd: list[str],
        *,
        timeout: float,
        secrets: Iterable[str] = (),
        secret_input: bytes | None = None,
    ) -> tuple[int, bytes, str]:
        """``(returncode, stdout, scrubbed stderr)``, registered under its redacted command."""
        from axiom.infra import process_registry

        hidden = SecretInput(cmd, secret_input)
        known = [*secrets, *secrets_in(cmd), *hidden.secrets]
        record_id, env = process_registry.prepare_spawn()
        with hidden as (argv, pass_fds):
            proc = subprocess.Popen(  # noqa: S603 - argv list, no shell, no secret in it
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                pass_fds=pass_fds,
            )
        with process_registry.registered_process(
            proc,
            record_id=record_id,
            kind="tool",
            purpose="video probe",
            extension="video",
            command=" ".join(redact_command(hidden.described(argv), known)),
            max_runtime_s=timeout + 30.0,
        ):
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise VideoSourceError(
                    f"the source did not answer within {timeout:.0f} s",
                    code="timeout",
                    fix="check the source is on and reachable from this machine, and the host and port in the URL",
                ) from None
        return proc.returncode, out or b"", scrub((err or b"").decode("utf-8", "replace"), known)


__all__ = [
    "DEFAULT_PROBE_TIMEOUT_S",
    "FFMPEG_ENV",
    "FFPROBE_ENV",
    "FFmpegMissing",
    "FFmpegTool",
    "RAW_OUTPUT",
    "SECRET_INPUT",
    "SecretInput",
    "classify_failure",
    "install_hint",
    "redact_command",
    "scrub",
    "secrets_in",
]
