# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A stand-in for ``ffmpeg`` and ``ffprobe``, so the video tests need neither binary.

Run as ``fake_ffmpeg.py ffmpeg <args>`` or ``fake_ffmpeg.py ffprobe <args>`` (the
fixtures write two wrapper scripts that do exactly that). It speaks only the
handful of invocations the extension makes:

* ``-version`` and ``-decoders``;
* ``ffprobe ... -of json <input>``: one video stream;
* ``ffmpeg ... -frames:v 1 ... -f rawvideo -pix_fmt rgb24 pipe:1``: one frame;
* ``ffmpeg ... -vf fps=N -f rawvideo -pix_fmt rgb24 pipe:1``: frames at N per
  second until it is killed (or ``FAKE_VIDEO_FRAMES`` have been written).

Behaviour is chosen by the environment and by the input URL's last path segment:

``FAKE_VIDEO_SIZE`` ``WxH`` (default ``64x48``), ``FAKE_VIDEO_CODEC`` (``h264``),
``FAKE_VIDEO_FPS`` (``10``), ``FAKE_VIDEO_DECODERS`` (``h264,hevc,mjpeg``),
``FAKE_VIDEO_FRAMES`` (capture stops after this many), ``FAKE_VIDEO_STALL_AFTER``
(capture goes silent, still running, after this many), ``FAKE_VIDEO_ARGV_LOG``
(append each argv as one JSON line, so a test can see what reached the child),
``FAKE_VIDEO_SCENE`` (what the frames show; by default every byte of frame ``n`` is
``n % 256``): ``flat`` is a still gray room, and ``crossing:<first>:<last>`` is that
room with a bright square crossing it from frame ``first`` to frame ``last``, so a
trigger has something to see.

An input of ``-f concat -i pipe:N`` is read the way ``ffmpeg`` reads it: an ffconcat
script on the inherited descriptor N, whose ``file '...'`` line is the URL (login
included) and whose ``option rtsp_transport`` line is the transport. What was read
there is appended to ``FAKE_VIDEO_INPUT_LOG``, apart from the argv log.

A path ending ``/unauthorized``, ``/missing``, ``/refused`` or ``/garbled`` fails
the way the real tools do, echoing the input URL, login included, on stderr. That
echo is the point: the extension has to scrub it.
"""

from __future__ import annotations

import json
import os
import sys
import time

FAILURES = {
    "unauthorized": "{url}: Server returned 401 Unauthorized (authorization failed)",
    "missing": "{url}: Server returned 404 Not Found",
    "refused": "[tcp @ 0x1] Connection to tcp://127.0.0.1:9 failed: Connection refused\n{url}: Connection refused",
    "garbled": "{url}: Invalid data found when processing input",
}


def _size() -> tuple[int, int]:
    w, _, h = os.environ.get("FAKE_VIDEO_SIZE", "64x48").partition("x")
    return int(w), int(h)


_GRAY, _BRIGHT = 100, 255


def _frame(width: int, height: int, n: int) -> bytes:
    scene = os.environ.get("FAKE_VIDEO_SCENE", "")
    if not scene:
        return bytes([n % 256]) * (width * height * 3)
    room = bytes([_GRAY]) * (width * 3)
    kind, _, span = scene.partition(":")
    first, _, last = span.partition(":")
    if kind != "crossing" or not int(first) <= n < int(last):
        return room * height
    side = max(4, min(width, height) // 3)
    x = ((n - int(first)) * 4) % max(1, width - side)
    y = (height - side) // 2
    row = (
        bytes([_GRAY]) * (3 * x)
        + bytes([_BRIGHT]) * (3 * side)
        + bytes([_GRAY]) * (3 * (width - x - side))
    )
    return b"".join(row if y <= r < y + side else room for r in range(height))


def _arg_after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else ""


def _read_concat(spec: str) -> tuple[str, dict[str, str]]:
    """``(url, options)`` from an ffconcat script on ``pipe:N``."""
    fd = int(spec.partition(":")[2] or 0)
    with os.fdopen(fd, "rb", closefd=True) as fh:
        text = fh.read().decode("utf-8", "replace")
    url, options = "", {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("file "):
            body = line[5:].strip()
            if body.startswith("'") and body.endswith("'"):
                body = body[1:-1].replace("'\\''", "'")
            url = body
        elif line.startswith("option "):
            _, key, value = line.split(" ", 2)
            options[key] = value
    return url, options


def main(argv: list[str]) -> int:
    tool, args = argv[0], argv[1:]
    log = os.environ.get("FAKE_VIDEO_ARGV_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps([tool, *args]) + "\n")
    if "-version" in args:
        print(f"{tool} version 0.0-fake Copyright (c) the tests")
        return 0
    if "-decoders" in args:
        print("Decoders:\n V..... = Video\n ------")
        for name in os.environ.get("FAKE_VIDEO_DECODERS", "h264,hevc,mjpeg").split(","):
            if name:
                print(f" V....D {name:<20} fake {name} decoder")
        return 0

    url = _arg_after(args, "-i")
    if _arg_after(args, "-f") == "concat" and url.startswith("pipe:"):
        url, options = _read_concat(url)
        read_log = os.environ.get("FAKE_VIDEO_INPUT_LOG")
        if read_log:
            with open(read_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"tool": tool, "url": url, "options": options}) + "\n")
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    if tail in FAILURES:
        print(FAILURES[tail].format(url=url), file=sys.stderr)
        return 1

    width, height = _size()
    if tool == "ffprobe":
        fps = os.environ.get("FAKE_VIDEO_FPS", "10")
        stream = {
            "codec_name": os.environ.get("FAKE_VIDEO_CODEC", "h264"),
            "width": width,
            "height": height,
            "r_frame_rate": "20/1",
            "avg_frame_rate": f"{fps}/1",
        }
        print(json.dumps({"programs": [], "streams": [stream]}))
        return 0

    out = sys.stdout.buffer
    if _arg_after(args, "-frames:v") == "1":
        out.write(_frame(width, height, 0))
        out.flush()
        return 0

    vf = _arg_after(args, "-vf")
    rate = float(vf.partition("fps=")[2] or 10) if "fps=" in vf else 10.0
    limit = int(os.environ.get("FAKE_VIDEO_FRAMES", "0") or 0)
    stall_after = int(os.environ.get("FAKE_VIDEO_STALL_AFTER", "0") or 0)
    n = 0
    try:
        while not limit or n < limit:
            if stall_after and n >= stall_after:
                time.sleep(3600)  # alive and silent: a camera that froze
            out.write(_frame(width, height, n))
            out.flush()
            n += 1
            time.sleep(1.0 / rate)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
