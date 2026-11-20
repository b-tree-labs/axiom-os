# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``dev.logs``: the logs of the node this checkout's lane runs, and its web server's.

The last ``n`` lines of each (at most ``MAX_LINES``), and the byte offset each ends at. With
``after`` (``{name: offset}`` from a previous call) only the whole lines written since, so
``dev logs --follow`` polls this and never reads a file twice. It reads the lane this checkout
already holds and never claims one or starts anything.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import node, web
from . import held_lane

#: At most this many lines of one log in one answer.
MAX_LINES = 2000


def _paths(home: Path) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    rec = node.read_record(home) or {}
    if rec.get("log"):
        out.append(("node", Path(rec["log"])))
    wrec = web.read_record(home) or {}
    web_log = Path(wrec["log"]) if wrec.get("log") else home / "web.log"
    out.append(("web", web_log))
    return [(name, p) for name, p in out if p.is_file()]


def _last(path: Path, n: int) -> tuple[list[str], int]:
    kept: deque[str] = deque(maxlen=n)
    with open(path, "rb") as f:
        for raw in f:
            kept.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        end = f.tell()
    return list(kept), end


def _after(path: Path, offset: int) -> tuple[list[str], int]:
    """Whole lines written after ``offset``; a line still being written waits for the next call."""
    lines: list[str] = []
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset
    if size < offset:
        offset = 0  # the log was truncated or replaced (a restart): read it from the start
    with open(path, "rb") as f:
        f.seek(offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break
            offset += len(raw)
            lines.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            if len(lines) >= MAX_LINES:
                break
    return lines, offset


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    lane = held_lane(params)
    if lane is None:
        return SkillResult(ok=False, errors=["this checkout holds no lane: start it with `dev up`"])
    home = node.home_for(ctx.state_dir, lane["name"])
    found = _paths(home)
    if not found:
        return SkillResult(
            ok=False, errors=[f"nothing has run in lane {lane['name']} yet: start it with `dev up`"]
        )
    n = max(1, min(int(params.get("n") or 40), MAX_LINES))
    after = params.get("after") or {}
    out = []
    for name, path in found:
        if name in after:
            lines, end = _after(path, int(after[name]))
        else:
            lines, end = _last(path, n)
        out.append({"name": name, "path": str(path), "lines": lines, "end": end})
    return SkillResult(value={"lane": lane["name"], "logs": out}, actions_taken=[x["path"] for x in out])


__all__ = ["MAX_LINES", "run"]
