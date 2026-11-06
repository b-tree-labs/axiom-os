# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A claim on one pending item, so exactly one copy of a service finishes it.

Zero-gap services (ADR-182 D3) run an OLD and a NEW copy together during a
switch. Any "resume pending work" step that runs at import or start-up (finish
whatever is left in a spool directory) would otherwise be run by the new copy
against an item the old copy is still working on, and the item would be done
twice. The rule (ADR-182 D3a): **an item is finished only by the holder of its
claim.**

A claim is a small file beside the item, ``<item>.claim``, holding the owner's
pid and that process's start time (the start time makes a reused pid a
different process). It is created atomically (written to a temp file, then
hard-linked into place, which fails if a claim exists) so a reader never sees a
half-written claim and two racers never both win.

- An item whose claim is held by a LIVE owner is not claimable: not by another
  copy, and not by a second pass of the same process.
- A claim whose owner has exited is **stale**: it is taken over, atomically
  (renamed away, so only one taker wins).
- ``stale_after_s`` additionally treats a claim not :meth:`Claim.touch`-ed for
  that long as stale, for an owner that is alive but wedged.
- :meth:`Claim.release` removes the claim; an owner that exits without
  releasing is covered by the stale rule. Finishing an item and releasing its
  claim are separate steps: remove the item first, then release.

Domain-agnostic: it knows nothing about what an item is.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from axiom.infra import process_registry

CLAIM_SUFFIX = ".claim"


def claim_path(item: str | Path) -> Path:
    item = Path(item)
    return item.with_name(item.name + CLAIM_SUFFIX)


def _this_process() -> dict:
    pid = os.getpid()
    info = process_registry.probe(pid)
    return {"pid": pid, "started_at": info.start if info is not None else None}


def _owner_alive(pid: int | None, started_at: float | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    info = process_registry.probe(pid)
    if info is None:
        return False
    if started_at is not None and abs(info.start - started_at) > process_registry.START_TOLERANCE_S:
        return False
    return True


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_stale(item: str | Path, *, stale_after_s: float | None = None) -> bool:
    """Is the item's existing claim stale (owner gone, or silent too long)?

    ``False`` when there is no claim. An unreadable claim is stale only once it
    is older than a few seconds (a writer cannot have been interrupted
    mid-write, since claims are linked into place whole; unreadable means
    damaged).
    """
    path = claim_path(item)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return False
    data = _read(path)
    if data is None:
        return time.time() - mtime > 5.0
    if not _owner_alive(data.get("pid"), data.get("started_at")):
        return True
    return stale_after_s is not None and time.time() - mtime > stale_after_s


@dataclass
class Claim:
    """The holder's handle on one item. Context-manager friendly."""

    item: Path
    path: Path
    token: str

    def touch(self) -> None:
        """Heartbeat: keeps a long-running holder from looking wedged."""
        with contextlib.suppress(OSError):
            os.utime(self.path)

    def held(self) -> bool:
        data = _read(self.path)
        return bool(data and data.get("token") == self.token)

    def release(self) -> None:
        """Drop the claim, only if it is still ours (a taker may hold it now)."""
        if self.held():
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()

    def __enter__(self) -> Claim:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _link_claim(path: Path, token: str) -> bool:
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps({**_this_process(), "token": token}))
    try:
        os.link(tmp, path)  # atomic; FileExistsError if a claim is there
        return True
    except FileExistsError:
        return False
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def claim(item: str | Path, *, stale_after_s: float | None = None) -> Claim | None:
    """Claim ``item`` for this process, or ``None`` if another holder has it.

    Succeeds when there is no claim, or the existing one is stale (taken over).
    The item need not be checked for existence here; a caller that finds it
    gone after claiming simply releases.
    """
    item = Path(item)
    path = claim_path(item)
    token = uuid.uuid4().hex
    for _ in range(3):
        if _link_claim(path, token):
            return Claim(item, path, token)
        if not is_stale(item, stale_after_s=stale_after_s):
            return None
        # Take over: rename the stale claim away. Of several takers only one
        # rename succeeds; the rest see FileNotFoundError and retry the link.
        grave = path.with_name(f"{path.name}.stale-{uuid.uuid4().hex}")
        try:
            os.rename(path, grave)
        except FileNotFoundError:
            continue
        with contextlib.suppress(FileNotFoundError):
            grave.unlink()
    return None


def claimable(directory: str | Path, pattern: str = "*.json", *, stale_after_s: float | None = None) -> Iterator[Claim]:
    """Yield a held :class:`Claim` for each pending item this process may finish.

    Items held by another live owner are skipped silently. The caller finishes
    the item and releases the claim (use the claim as a context manager).
    """
    try:
        items = sorted(Path(directory).glob(pattern))
    except OSError:
        return
    for item in items:
        if item.name.endswith(CLAIM_SUFFIX):
            continue
        c = claim(item, stale_after_s=stale_after_s)
        if c is None:
            continue
        if not item.exists():  # finished between the listing and the claim
            c.release()
            continue
        yield c


__all__ = ["CLAIM_SUFFIX", "Claim", "claim", "claim_path", "claimable", "is_stale"]
