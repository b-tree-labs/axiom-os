# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Free disk space where a node writes, against two floors.

* the **refusal floor**: below it the ingest face answers 507 to data before
  writing anything, so a flood fills the sender's spool (built to hold it)
  rather than this disk;
* the **alarm floor**, higher: below it the node says so in its status and
  heartbeat while there is still room, so a person hears about a filling disk
  before anything is refused.

Read live each call; the point is to notice the disk filling during a flood.
Safe with nothing set: refuse below 1 GiB or 2 %, alarm below 5 GiB or 10 %.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

MIN_FREE_BYTES_ENV = "AXIOM_INGEST_MIN_FREE_BYTES"
MIN_FREE_PERCENT_ENV = "AXIOM_INGEST_MIN_FREE_PERCENT"
ALARM_FREE_BYTES_ENV = "AXIOM_DISK_ALARM_FREE_BYTES"
ALARM_FREE_PERCENT_ENV = "AXIOM_DISK_ALARM_FREE_PERCENT"
PATH_ENV = "AXIOM_INGEST_HEADROOM_PATH"
#: More volumes to watch, ``label=path[,label=path]``. A node's database often
#: sits on its own volume (and a database whose volume fills stops: its write-
#: ahead log has nowhere to go), so the node watches it beside its own.
WATCH_ENV = "AXIOM_DISK_WATCH"


def _path() -> str:
    raw = os.environ.get(PATH_ENV) or os.environ.get("AXI_STATE_DIR")
    if raw:
        return raw
    from axiom.infra.paths import get_user_state_dir

    return str(get_user_state_dir())


def _floor(total: int, bytes_env: str, bytes_default: int, pct_env: str, pct_default: float) -> int:
    return max(int(float(os.environ.get(bytes_env) or bytes_default)),
               int(total * float(os.environ.get(pct_env) or pct_default) / 100))


def headroom(path: str | os.PathLike | None = None) -> dict[str, Any]:
    """``ok`` is False below the refusal floor; ``alarm`` is True below the alarm floor."""
    where = str(path) if path is not None else _path()
    while not os.path.exists(where) and os.path.dirname(where) != where:
        where = os.path.dirname(where)  # a directory not made yet lives on its parent's disk
    usage = shutil.disk_usage(where)
    floor = _floor(usage.total, MIN_FREE_BYTES_ENV, 1 << 30, MIN_FREE_PERCENT_ENV, 2)
    alarm = max(floor, _floor(usage.total, ALARM_FREE_BYTES_ENV, 5 << 30, ALARM_FREE_PERCENT_ENV, 10))
    return {"path": where, "free_bytes": usage.free, "total_bytes": usage.total,
            "floor_bytes": floor, "alarm_bytes": alarm,
            "ok": usage.free >= floor, "alarm": usage.free < alarm}


def watched() -> list[dict[str, Any]]:
    """Each volume named in ``AXIOM_DISK_WATCH``, labelled, with its headroom.

    A volume that cannot be read is reported as such rather than left out: a
    watch that silently stops watching is the failure this exists to prevent.
    """
    out: list[dict[str, Any]] = []
    for item in (os.environ.get(WATCH_ENV) or "").split(","):
        label, sep, path = item.strip().partition("=")
        if not sep or not label.strip() or not path.strip():
            continue
        if not os.path.isdir(path.strip()):
            # headroom() would measure the nearest parent, i.e. some other disk.
            out.append({"label": label.strip(), "path": path.strip(), "error": "not mounted here",
                        "ok": False, "alarm": True})
            continue
        try:
            out.append({"label": label.strip(), **headroom(path.strip())})
        except OSError as exc:
            out.append({"label": label.strip(), "path": path.strip(), "error": exc.strerror or str(exc),
                        "ok": False, "alarm": True})
    return out


def used_fraction(disk: dict[str, Any]) -> float | None:
    total, free = disk.get("total_bytes"), disk.get("free_bytes")
    if not total or free is None:
        return None
    return (total - free) / total


__all__ = ["ALARM_FREE_BYTES_ENV", "ALARM_FREE_PERCENT_ENV", "MIN_FREE_BYTES_ENV", "MIN_FREE_PERCENT_ENV",
           "PATH_ENV", "WATCH_ENV", "headroom", "used_fraction", "watched"]
