# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What the video extension keeps on disk, under ``<state>/video/``, and nothing else.

* ``claims/<name>.claim``: who holds a source (``axiom.infra.pending_claim``). Two
  copies of the capture service never both hold one (VID-007).
* ``started/<name>``: a source is to be captured. ``instrument video start`` sets it and
  ``instrument video stop`` clears it, so a copy brought up during a switch knows what to
  take over, and a copy waiting for a claim knows when to give up.
* ``verify/<name>.json``: the last verify, which decides whether a source is
  active (VID-003).
* ``live/<name>.json``: the holder's session and counters, which ``status`` reads
  from another process; ``standby/<name>.<copy>.json``: a copy waiting its turn.
* ``logs/<name>.log``: what a capture process printed, already scrubbed.
* ``reload/<name>``: ``connector reload`` asking the holder to apply the source's
  trigger attachments; the holder answers in its live snapshot.
* ``episodes/<name>.json``: the episodes a trigger has open on the source, so the
  next holder can close what a crashed one left open.
* ``events.jsonl``: the event ledger (``ledger.py``).

No credential is ever written here; the tests read every file to prove it.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from axiom.infra import pending_claim, process_registry
from axiom.infra.state import atomic_write

#: A claim not touched for this long is stale even if its owner is alive (wedged).
CLAIM_STALE_S = 30.0


def video_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "video"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


# ---- claims ------------------------------------------------------------------


def claim_item(name: str, state_dir: Path) -> Path:
    """The item a source's claim is taken on (the claim file sits beside it)."""
    return video_dir(state_dir) / "claims" / name


def claim_path(name: str, state_dir: Path) -> Path:
    return pending_claim.claim_path(claim_item(name, state_dir))


def claim_holder(name: str, state_dir: Path) -> dict[str, Any] | None:
    """``{pid, started_at, token, alive}`` for whoever holds the claim, or ``None``."""
    path = claim_path(name, state_dir)
    if not path.exists():
        return None
    data = _read_json(path) or {}
    alive = not pending_claim.is_stale(claim_item(name, state_dir), stale_after_s=CLAIM_STALE_S)
    return {
        "pid": data.get("pid"),
        "started_at": data.get("started_at"),
        "token": data.get("token"),
        "alive": alive,
    }


def release_claim(name: str, state_dir: Path) -> bool:
    """Remove a claim whose owner has gone. A live owner's claim is left alone."""
    holder = claim_holder(name, state_dir)
    if holder is None or holder["alive"]:
        return False
    return _unlink(claim_path(name, state_dir))


# ---- started marks -------------------------------------------------------------


def started_marker(name: str, state_dir: Path) -> Path:
    return video_dir(state_dir) / "started" / name


def mark_started(name: str, state_dir: Path) -> None:
    path = started_marker(name, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{time.time()}\n", encoding="utf-8")


def clear_started(name: str, state_dir: Path) -> bool:
    return _unlink(started_marker(name, state_dir))


def is_started(name: str, state_dir: Path) -> bool:
    return started_marker(name, state_dir).exists()


def started_sources(state_dir: Path) -> list[str]:
    folder = video_dir(state_dir) / "started"
    try:
        return sorted(
            p.name for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")
        )
    except OSError:
        return []


# ---- verify records --------------------------------------------------------------


@dataclass(frozen=True)
class VerifyRecord:
    """The last verify of a source. ``report`` holds codec, width, height, fps, latency_ms."""

    ok: bool
    at: str
    report: dict[str, Any] | None = None
    error: str | None = None
    code: str | None = None
    fix: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VerifyRecord:
        return cls(
            ok=bool(data.get("ok")),
            at=str(data.get("at") or ""),
            report=data.get("report"),
            error=data.get("error"),
            code=data.get("code"),
            fix=data.get("fix"),
        )


def verify_record_path(name: str, state_dir: Path) -> Path:
    return video_dir(state_dir) / "verify" / f"{name}.json"


def write_verify_record(name: str, record: VerifyRecord, state_dir: Path) -> None:
    atomic_write(verify_record_path(name, state_dir), record.as_dict())


def read_verify_record(name: str, state_dir: Path) -> VerifyRecord | None:
    data = _read_json(verify_record_path(name, state_dir))
    return VerifyRecord.from_dict(data) if data else None


def _unlink_with_lock(path: Path) -> None:
    """A record and the lock file ``atomic_write`` keeps beside it."""
    _unlink(path)
    _unlink(path.with_suffix(path.suffix + ".lock"))


def clear_verify_record(name: str, state_dir: Path) -> None:
    _unlink_with_lock(verify_record_path(name, state_dir))


# ---- live snapshots ---------------------------------------------------------------


def live_path(name: str, state_dir: Path) -> Path:
    return video_dir(state_dir) / "live" / f"{name}.json"


def write_live(name: str, snapshot: dict[str, Any], state_dir: Path) -> None:
    atomic_write(live_path(name, state_dir), snapshot)


def read_live(name: str, state_dir: Path) -> dict[str, Any] | None:
    return _read_json(live_path(name, state_dir))


def clear_live(name: str, state_dir: Path) -> None:
    _unlink_with_lock(live_path(name, state_dir))


def _standby_dir(state_dir: Path) -> Path:
    return video_dir(state_dir) / "standby"


def write_standby(name: str, copy_id: str, snapshot: dict[str, Any], state_dir: Path) -> None:
    atomic_write(_standby_dir(state_dir) / f"{name}.{copy_id}.json", snapshot)


def clear_standby(name: str, copy_id: str, state_dir: Path) -> None:
    path = _standby_dir(state_dir) / f"{name}.{copy_id}.json"
    _unlink(path)
    with contextlib.suppress(OSError):
        path.with_suffix(".json.lock").unlink()


def read_standby(name: str, state_dir: Path) -> list[dict[str, Any]]:
    """The copies waiting for ``name``'s claim, sorted by copy id. A copy whose process is gone is not one."""
    out = []
    for path in sorted(_standby_dir(state_dir).glob(f"{name}.*.json")):
        data = _read_json(path)
        if not data:
            continue
        pid = data.get("pid")
        if isinstance(pid, int) and process_registry.probe(pid) is None:
            continue
        out.append(data)
    return sorted(out, key=lambda d: str(d.get("copy_id")))


# ---- reload requests ------------------------------------------------------------


def reload_request_path(name: str, state_dir: Path) -> Path:
    return video_dir(state_dir) / "reload" / name


def request_reload(name: str, state_dir: Path) -> str:
    """Ask the holder to apply ``name``'s attachments again. Returns the request's id."""
    request = uuid.uuid4().hex[:12]
    path = reload_request_path(name, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{request}.tmp")
    tmp.write_text(f"{request}\n", encoding="utf-8")
    os.replace(tmp, path)
    return request


def read_reload_request(name: str, state_dir: Path) -> str | None:
    try:
        return reload_request_path(name, state_dir).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def clear_reload_request(name: str, state_dir: Path) -> None:
    _unlink(reload_request_path(name, state_dir))


# ---- open episodes -----------------------------------------------------------------


def episodes_path(name: str, state_dir: Path) -> Path:
    return video_dir(state_dir) / "episodes" / f"{name}.json"


def read_open_episodes(name: str, state_dir: Path) -> dict[str, dict[str, Any]]:
    """The ``started`` row of every episode open on ``name``, by incident key."""
    data = _read_json(episodes_path(name, state_dir)) or {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def write_open_episodes(name: str, episodes: dict[str, dict[str, Any]], state_dir: Path) -> None:
    """Replace them; none removes the file (and its lock)."""
    path = episodes_path(name, state_dir)
    if episodes:
        atomic_write(path, episodes)
    else:
        _unlink_with_lock(path)


def log_path(name: str, state_dir: Path) -> Path:
    return video_dir(state_dir) / "logs" / f"{name}.log"


def clear_log(name: str, state_dir: Path) -> None:
    _unlink(log_path(name, state_dir))


__all__ = [
    "CLAIM_STALE_S",
    "VerifyRecord",
    "claim_holder",
    "claim_item",
    "claim_path",
    "episodes_path",
    "clear_live",
    "clear_log",
    "clear_reload_request",
    "clear_standby",
    "clear_started",
    "clear_verify_record",
    "is_started",
    "live_path",
    "log_path",
    "mark_started",
    "read_live",
    "read_open_episodes",
    "read_reload_request",
    "read_standby",
    "read_verify_record",
    "release_claim",
    "reload_request_path",
    "request_reload",
    "started_marker",
    "started_sources",
    "verify_record_path",
    "video_dir",
    "write_live",
    "write_open_episodes",
    "write_standby",
    "write_verify_record",
]
