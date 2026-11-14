# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""In-process doubles for the capture path: a source, a decoder and its sessions.

``FakeFrameSource`` is a ``VideoSource`` of kind ``fake`` (spec-video §Tests): it
verifies instantly, can change its frame size between sessions, and can be
offline for a number of verifies. ``FakeDecoder`` stands where the ``ffmpeg``
subprocess stands and plays one script per session, so a test decides exactly
when a session yields a frame, stalls, ends or ends on half a frame.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.connector.detect import ConnectorState, DetectResult
from axiom.extensions.builtins.video.capture import StallTimeout
from axiom.extensions.builtins.video.sources import (
    SourceKind,
    VerifyReport,
    VideoSourceError,
    register_source_kind,
    unregister_source_kind,
)

#: A session script item: frame bytes, or one of these words.
STALL = "stall"
EOF = "eof"
FOREVER = "forever"


def frame_bytes(width: int, height: int, value: int = 0) -> bytes:
    """One raw rgb24 frame, every byte ``value``."""
    return bytes([value % 256]) * (width * height * 3)


class FakeFrameSource:
    """A ``VideoSource`` that needs no network and no binaries."""

    kind = "fake"

    def __init__(
        self,
        *,
        replayable: bool = False,
        sizes: tuple[tuple[int, int], ...] = ((8, 6),),
        offline_verifies: int = 0,
        codec: str = "h264",
        fps: float = 10.0,
    ) -> None:
        self.replayable = replayable
        self.sizes = sizes
        self.offline_verifies = offline_verifies
        self.codec = codec
        self.fps = fps
        self.verify_calls = 0

    def detect(self) -> DetectResult:
        return DetectResult(ConnectorState.CONFIGURED, "the fake source is always there")

    def verify(self) -> VerifyReport:
        self.verify_calls += 1
        if self.offline_verifies > 0:
            self.offline_verifies -= 1
            raise VideoSourceError(
                "the fake source is offline", code="unreachable", fix="plug it back in"
            )
        width, height = self.sizes[min(self.verify_calls - 1, len(self.sizes) - 1)]
        return VerifyReport(
            codec=self.codec, width=width, height=height, fps=self.fps, latency_ms=1.0
        )

    def ffmpeg_input_args(self) -> list[str]:
        return ["-f", "lavfi", "-i", "testsrc"]

    def redacted(self) -> dict[str, Any]:
        return {"kind": self.kind}


@dataclass
class FakeSession:
    """One decode session: plays ``script``, then ends (or keeps going for ``FOREVER``)."""

    script: list[Any]
    width: int
    height: int
    log: list[tuple[Any, ...]]
    pid: int | None = None
    partial_frames: int = 0
    closed: bool = False
    timeouts: list[float] = field(default_factory=list)
    _n: int = 0
    _forever: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.script and self.script[-1] == FOREVER:
            self.script = self.script[:-1]
            self._forever = True

    def read(self, timeout: float) -> bytes | None:
        self.timeouts.append(timeout)
        if self.closed:
            return None
        if self.script:
            item = self.script.pop(0)
            if item == STALL:
                raise StallTimeout(f"no frame for {timeout:.1f} s")
            if item == EOF:
                return None
            if isinstance(item, tuple) and item[0] == "partial":
                self.partial_frames += 1
                return None
            return item
        if self._forever:
            time.sleep(0.002)
            self._n += 1
            return frame_bytes(self.width, self.height, self._n)
        return None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.log.append(("close", self.width, self.height))

    def error(self) -> str:
        return ""


class FakeDecoder:
    """Stands where ``ffmpeg`` stands. ``scripts[i]`` is what session ``i`` plays."""

    def __init__(
        self, scripts: list[list[Any]] | None = None, *, default: list[Any] | None = None
    ) -> None:
        self.scripts = [list(s) for s in (scripts or [])]
        self.default = list(default) if default is not None else []
        self.log: list[tuple[Any, ...]] = []
        self.sessions: list[FakeSession] = []
        self._lock = threading.Lock()

    def open(self, source: Any, *, width: int, height: int, fps_cap: int) -> FakeSession:
        with self._lock:
            script = self.scripts.pop(0) if self.scripts else list(self.default)
            self.log.append(("open", width, height, fps_cap))
            session = FakeSession(script=script, width=width, height=height, log=self.log)
            self.sessions.append(session)
            return session


@contextmanager
def registered_kind(source: Any, *, kind: str = "fake") -> Iterator[SourceKind]:
    """Register ``source`` as the one instance kind ``kind`` builds, for the block."""
    entry = SourceKind(
        kind=kind,
        replayable=bool(getattr(source, "replayable", False)),
        factory=lambda params, credential: source,
        description="a test double",
    )
    register_source_kind(entry)
    try:
        yield entry
    finally:
        unregister_source_kind(kind)


def dead_claim(item: Path) -> Path:
    """A claim on ``item`` whose owner has exited: what a crashed capture leaves behind."""
    from axiom.infra import pending_claim

    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True
    )
    path = pending_claim.claim_path(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    # started_at 1.0 never matches a live process, so even a reused pid reads as gone.
    path.write_text(json.dumps({"pid": int(done.stdout), "started_at": 1.0, "token": "dead"}))
    return path


class RecordingSleep:
    """Stands in for the supervisor's back-off wait. Stops it after ``stop_after`` waits."""

    def __init__(
        self, supervisor_ref: list[Any] | None = None, *, stop_after: int | None = None
    ) -> None:
        self.delays: list[float] = []
        self.stop_after = stop_after
        self.supervisor_ref = supervisor_ref if supervisor_ref is not None else []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        if (
            self.stop_after is not None
            and len(self.delays) >= self.stop_after
            and self.supervisor_ref
        ):
            self.supervisor_ref[0].request_stop()


__all__ = [
    "EOF",
    "FOREVER",
    "STALL",
    "FakeDecoder",
    "FakeFrameSource",
    "FakeSession",
    "RecordingSleep",
    "dead_claim",
    "frame_bytes",
    "registered_kind",
]
