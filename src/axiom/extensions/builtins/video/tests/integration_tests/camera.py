# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A simulated IP camera that can be unplugged and plugged back in.

``mediamtx`` is the RTSP server (digest login, TCP and optionally UDP); ``ffmpeg``
publishes ``testsrc`` (H.264, a keyframe a second) to it. ``ffmpeg`` alone cannot
serve RTSP: ``-rtsp_flags listen`` is an input flag. ``power_off()`` kills both,
which is what a camera losing power looks like from the network; ``power_on()``
brings them back on the same address.

``scene`` is what the camera sees: ``testsrc`` (the default, always changing), a
``crossing`` (a still, noisy gray room that a bright square crosses for 3 s of
every 15 s) or ``still-noise`` (the same room with nothing in it, sensor noise only).
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
MEDIAMTX = shutil.which("mediamtx")
MISSING = [
    n for n, p in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE), ("mediamtx", MEDIAMTX)) if not p
]

CONFIG = """\
logLevel: warn
rtspAddress: 127.0.0.1:{port}
rtspTransports: [{transports}]
rtspAuthMethods: [digest]
rtpAddress: 127.0.0.1:{rtp}
rtcpAddress: 127.0.0.1:{rtcp}
rtmp: no
hls: no
webrtc: no
srt: no
moq: no
api: no
metrics: no
pprof: no
playback: no
authInternalUsers:
  - user: {user}
    pass: {password}
    ips: []
    permissions:
      - action: publish
      - action: read
paths:
  all_others:
"""

#: What a scene is, as an ffmpeg lavfi graph at 640x360, 10 fps.
SCENES = {
    "testsrc": "testsrc=size=640x360:rate=10",
    "crossing": (
        "color=c=0x606060:s=640x360:r=10[room];color=c=white:s=120x120:r=10[square];"
        "[room][square]overlay=x='mod(t*150,520)':y=120:enable='lt(mod(t,15),3)',"
        "noise=alls=8:allf=t[out0]"
    ),
    "still-noise": "color=c=0x606060:s=640x360:r=10,noise=alls=10:allf=t",
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def free_udp_pair() -> int:
    """An even UDP port whose successor is free too (RTP and RTCP)."""
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1] & ~1
        try:
            with (
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as a,
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as b,
            ):
                a.bind(("127.0.0.1", port))
                b.bind(("127.0.0.1", port + 1))
            return port
        except OSError:
            continue
    raise RuntimeError("no free UDP port pair")


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


class SimulatedCamera:
    """``with SimulatedCamera(tmp_path) as cam: cam.url, cam.user, cam.password``."""

    def __init__(
        self, workdir: Path, *, udp: bool = False, path: str = "t", scene: str = "testsrc"
    ) -> None:
        if scene not in SCENES:
            raise ValueError(f"no scene {scene!r}; scenes are {', '.join(SCENES)}")
        self.scene = scene
        self.workdir = Path(workdir)
        self.port = free_port()
        self.udp = udp
        self.rtp = free_udp_pair()
        self.path = path
        self.user = "viewer"
        self.password = f"pw-{uuid.uuid4().hex[:12]}"
        self._server: subprocess.Popen | None = None
        self._publisher: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"rtsp://127.0.0.1:{self.port}/{self.path}"

    def login(self) -> str:
        return f"{self.user}:{self.password}"

    def power_on(self, *, settle_s: float = 2.0) -> None:
        config = self.workdir / f"mediamtx-{self.port}.yml"
        config.write_text(
            CONFIG.format(
                port=self.port,
                transports="udp, tcp" if self.udp else "tcp",
                rtp=self.rtp,
                rtcp=self.rtp + 1,
                user=self.user,
                password=self.password,
            )
        )
        self._server = subprocess.Popen(
            [MEDIAMTX, str(config)],
            cwd=self.workdir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while not listening(self.port):
            if time.monotonic() > deadline:
                raise RuntimeError("mediamtx did not start")
            time.sleep(0.1)
        self._publisher = subprocess.Popen(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
                "-i", SCENES[self.scene], "-c:v", "libx264", "-tune", "zerolatency",
                "-g", "10", "-pix_fmt", "yuv420p", "-f", "rtsp", "-rtsp_transport", "tcp",
                f"rtsp://{self.user}:{self.password}@127.0.0.1:{self.port}/{self.path}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )  # fmt: skip
        time.sleep(settle_s)  # announce, then the first keyframe

    def power_off(self) -> None:
        for proc in (self._publisher, self._server):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        self._publisher = self._server = None

    def __enter__(self) -> SimulatedCamera:
        self.power_on()
        return self

    def __exit__(self, *exc: object) -> None:
        self.power_off()


__all__ = ["FFMPEG", "FFPROBE", "MEDIAMTX", "MISSING", "SimulatedCamera", "free_port", "listening"]
