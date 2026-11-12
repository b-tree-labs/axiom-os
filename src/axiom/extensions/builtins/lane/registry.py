# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The record of who has which lane.

Derivation (see :mod:`naming`) decides what a checkout's lane WOULD be. The
registry records what it actually got, so a second checkout can avoid it.
Those are different jobs and both are needed: derivation cannot see its
siblings, and a registry cannot see the session that never asked.

Three things are recorded that a first version would leave out, each because
leaving it out records an isolation the lane does not have:

``dsn_var``
    Which environment variable carries the isolation. Not every consumer
    reads ``AXIOM_DB_URL``; one reads ``STUDIO_GOLD_DSN`` against a database
    it does not migrate. ``unmanaged`` is a legal value, reported rather than
    hidden, because a lane that is honestly not isolated is safer than one
    that claims to be.

``trees``
    Checkouts that must move together. A surface composed from three
    repositories is not isolated by isolating one of them; a stale renderer
    beside a fresh database draws a figure that is WRONG rather than one that
    fails, and wrong is the expensive kind.

``venvs``
    Editable installs the lane depends on. A worktree can be landed, clean,
    and referenced by a running server's virtualenv — git has no idea, and
    removing it breaks the server with a stack trace nobody reads until a
    stylesheet 500s two days later. That happened; this is the record that
    would have prevented it.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import naming

DEFAULT_DSN_VAR = "AXIOM_DB_URL"

#: A lane that is honestly not database-isolated. Legal, and reported.
UNMANAGED = "unmanaged"

#: Ports a lane is never given. Standing services, not lanes.
RESERVED: dict[int, str] = {
    8770: "axiom base (standing reference)",
    8771: "consumer (standing reference)",
    8788: "axiom.llm.anthropic_ingress — do not reap",
}


@dataclass
class Lane:
    name: str
    front: int
    api: int
    #: Empty for an honestly unmanaged lane, and empty values are not written
    #: to the file — so this MUST have a default or such a lane cannot be
    #: read back. Found by the test for the unmanaged path.
    database: str = ""
    owner: str = ""
    branch: str = ""
    root: str = ""
    head: str = "unknown"
    dsn_var: str = DEFAULT_DSN_VAR
    trees: list[str] = field(default_factory=list)
    venvs: list[str] = field(default_factory=list)
    #: Shared files this lane is editing right now, stored EXACTLY as given.
    #:
    #: Ports and databases are contended by servers; files are contended by
    #: people, and that is the collision the other two do not touch. It cost
    #: us one file edited twice, two ADRs numbered 137, and two components
    #: both called Derivation — each found in review, after both sides had
    #: been written.
    #:
    #: Never resolved to an absolute path. Two sessions typing the same
    #: repo-relative path have to produce the same string, and resolving
    #: against each caller's worktree would make one reservation look like
    #: two — which is precisely the state this is here to end.
    holds: list[str] = field(default_factory=list)
    claimed: str = ""
    note: str = ""

    @property
    def isolated(self) -> bool:
        """Whether this lane actually has a database of its own."""
        return self.dsn_var != UNMANAGED and bool(self.database)

    @property
    def ports(self) -> tuple[int, int]:
        return (self.front, self.api)


class LaneTaken(ValueError):
    """The name is already claimed by somebody else."""


class NoPortsFree(RuntimeError):
    """Every port in the lane range is reserved, claimed or listening."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class Registry:
    """The lanes file, read and written under an exclusive lock.

    The lock is not ceremony. Two sessions claiming at the same moment is the
    normal case on a machine with several agents on it, and a read-modify-write
    without one hands both the same port.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    # -- storage ----------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"lanes": {}}
        try:
            return json.loads(self.path.read_text()) or {"lanes": {}}
        except json.JSONDecodeError:
            # A corrupt registry must not silently become an empty one: that
            # would hand out every port again.
            raise RuntimeError(f"{self.path} is not valid JSON; fix or remove it") from None

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)

    @contextmanager
    def locked(self, timeout: float = 10.0):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        fd = None
        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"another process has held {self.lock_path} for {timeout}s; "
                        "if nothing is running, remove it"
                    ) from None
                time.sleep(0.05)
        try:
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            yield
        finally:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    # -- reads ------------------------------------------------------------

    def all(self) -> dict[str, Lane]:
        raw = self._read().get("lanes", {})
        out: dict[str, Lane] = {}
        for name, body in raw.items():
            fields = {k: v for k, v in body.items() if k in Lane.__dataclass_fields__}
            fields.setdefault("name", name)
            out[name] = Lane(**fields)
        return out

    def get(self, name: str) -> Lane | None:
        return self.all().get(name)

    def taken_ports(self) -> set[int]:
        used: set[int] = set(RESERVED)
        for lane in self.all().values():
            used.update(lane.ports)
        return used

    # -- writes -----------------------------------------------------------

    def claim(self, lane: Lane, *, replace: bool = False) -> Lane:
        with self.locked():
            data = self._read()
            lanes = data.setdefault("lanes", {})
            if lane.name in lanes and not replace:
                existing = lanes[lane.name]
                raise LaneTaken(
                    f"lane {lane.name!r} is held by {existing.get('owner') or 'someone'} "
                    f"on :{existing.get('front')}/{existing.get('api')}"
                )
            lane.claimed = lane.claimed or _now()
            # A retake keeps what the lane was holding. `--force` is for
            # moving a lane to a new port or branch; silently dropping its
            # files would hand them to another session mid-edit.
            kept = list(lanes.get(lane.name, {}).get("holds", []))
            lane.holds = kept + [h for h in lane.holds if h not in kept]
            # Keep keys this dataclass does not know about.
            #
            # Serialising the dataclass over the stored entry erases anything
            # another writer put there — a field a second tool owns, or one a
            # person set by hand. It is the failure nobody notices, because
            # the file still parses and still looks right. It happened: this
            # writer wiped a key the other lane tool owned, and that tool
            # raised KeyError for every caller until it was made defensive.
            #
            # A registry two tools share needs this. A registry one tool owns
            # still wants it the first time somebody edits the JSON.
            body = {k: v for k, v in lanes.get(lane.name, {}).items()
                    if k not in Lane.__dataclass_fields__}
            body.update({k: v for k, v in asdict(lane).items() if v not in ("", [], None)})
            lanes[lane.name] = body
            self._write(data)
        return lane

    def hold(self, name: str, paths: Sequence[str]) -> dict[str, list[str]]:
        """Take `paths` for this lane, and say who else already has them.

        The contention comes back from THIS call rather than waiting for
        doctor, because finding out at doctor time is finding out after
        both sides have been written.

        Reported, never refused. Two sessions genuinely do need the same
        file sometimes, and a registry that blocked it would be routed
        around within the hour — leaving nobody knowing anything. What
        neither side recovers from is not knowing.
        """
        with self.locked():
            data = self._read()
            lanes = data.setdefault("lanes", {})
            if name not in lanes:
                raise KeyError(f"no lane named {name!r} — claim it before holding files")
            mine = list(lanes[name].get("holds", []))
            for path in paths:
                if path not in mine:
                    mine.append(path)
            lanes[name]["holds"] = mine
            clash = {
                path: sorted(
                    other for other, body in lanes.items()
                    if other != name and path in body.get("holds", [])
                )
                for path in paths
            }
            self._write(data)
        return {path: who for path, who in clash.items() if who}

    def drop(self, name: str, paths: Sequence[str]) -> list[str]:
        """Let `paths` go. Dropping one never held is not an error —
        the point is the end state, not the bookkeeping."""
        with self.locked():
            data = self._read()
            lanes = data.setdefault("lanes", {})
            if name not in lanes:
                raise KeyError(f"no lane named {name!r}")
            held = list(lanes[name].get("holds", []))
            gone = [p for p in paths if p in held]
            lanes[name]["holds"] = [p for p in held if p not in set(paths)]
            self._write(data)
        return gone

    def contested(self) -> dict[str, list[str]]:
        """Paths more than one lane is holding, for doctor to report."""
        by_path: dict[str, list[str]] = {}
        for name, lane in sorted(self.all().items()):
            for path in lane.holds:
                by_path.setdefault(path, []).append(name)
        return {p: sorted(who) for p, who in sorted(by_path.items()) if len(who) > 1}

    def release(self, name: str) -> Lane | None:
        with self.locked():
            data = self._read()
            body = data.get("lanes", {}).pop(name, None)
            if body is None:
                return None
            self._write(data)
        fields = {k: v for k, v in body.items() if k in Lane.__dataclass_fields__}
        fields.setdefault("name", name)
        return Lane(**fields)


def allocate_ports(
    name: str, taken: set[int], *, listening: set[int] | None = None, stride: int = 2
) -> tuple[int, int]:
    """A free, stable-ish pair for `name`.

    Starts at the name's preferred slot so a checkout tends to return to the
    ports its owner already has open, then walks forward. A port that is
    LISTENING but unclaimed is skipped too — the registry records intent, and
    intent is not what owns a socket.
    """
    busy = set(taken) | set(listening or ())
    span = range(naming.FIRST_LANE_PORT, naming.LAST_LANE_PORT + 1, stride)
    start = naming.preferred_port(name, stride=stride)
    ordered = [p for p in span if p >= start] + [p for p in span if p < start]
    for front in ordered:
        api = front + 1
        if front not in busy and api not in busy:
            return front, api
    raise NoPortsFree(
        f"no free pair between {naming.FIRST_LANE_PORT} and {naming.LAST_LANE_PORT}; "
        "release a lane or widen the range"
    )


__all__ = [
    "DEFAULT_DSN_VAR",
    "RESERVED",
    "UNMANAGED",
    "Lane",
    "LaneTaken",
    "NoPortsFree",
    "Registry",
    "allocate_ports",
]
