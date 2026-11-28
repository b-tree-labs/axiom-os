# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A node checks that the release it must follow is running (ADR-182 D4a).

A contracting migration names the release that made it safe::

    # contract: <what> unused since <released version>

CI checks that the version is released (``migration_guard``). This checks that
it is *running here*: a process older than that version, run against the
database after the revision, reads the old structure, and some create it again
empty.

Three parts:

- **The record.** ``<state>/releases/proven.json``: the release every
  long-running unit on this node was seen running, when, and which units.
  Installed is not proven; a unit that was never restarted still runs what it
  imported. :func:`prove` writes it after a deploy.
- **The check.** :func:`check` reads the revisions a migration is about to
  apply and returns a refusal when one carries a contract marker newer than the
  proven release. The one path that brings a schema to head asks it first.
- **The one way through.** A node with no record at all cannot show what it
  ran. The operator states it once (:func:`confirm`); the statement is recorded
  as a change with who made it. Once a record exists the record decides.

A fresh database is exempt: with no earlier revision applied there is no older
reader to protect.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from axiom.infra.deploy.migration_guard import _MARKER

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class Proof:
    """The release this node was last shown to be running."""

    version: str
    at: str
    units: tuple[str, ...] = ()
    #: Set when the record is an operator's statement, not a measurement.
    confirmed_by: str = ""


@dataclass(frozen=True)
class Prerequisite:
    """A pending revision that needs a release running before it is applied."""

    revision: str
    version: str
    what: str


class AlreadyProven(RuntimeError):
    """An operator's statement was offered on a node that already has a record."""


def proven_path() -> Path:
    """``AXI_PROVEN_RELEASE`` if set, else ``<state>/releases/proven.json``."""
    override = os.environ.get("AXI_PROVEN_RELEASE")
    if override:
        return Path(override)
    from axiom.infra.paths import get_platform_state_dir

    return get_platform_state_dir() / "releases" / "proven.json"


def _triple(version: str) -> tuple[int, int, int] | None:
    found = _VERSION.search(version or "")
    return tuple(int(g) for g in found.groups()) if found else None  # type: ignore[return-value]


def read(path: Path | None = None) -> Proof | None:
    """The node's record, or ``None`` when there is none or it cannot be read.

    An unreadable record is no record: a refusal is the safe reading of a file
    nobody can interpret."""
    target = path or proven_path()
    try:
        raw = json.loads(target.read_text())
        if _triple(raw["version"]) is None:
            return None
        return Proof(
            version=str(raw["version"]),
            at=str(raw.get("at", "")),
            units=tuple(raw.get("units") or ()),
            confirmed_by=str(raw.get("confirmed_by", "")),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write(
    version: str, units: Iterable[str], *, confirmed_by: str = "", path: Path | None = None
) -> Proof:
    """Write the record, whole or not at all."""
    if _triple(version) is None:
        raise ValueError(f"{version!r} is not a release version")
    target = path or proven_path()
    proof = Proof(
        version=version,
        at=datetime.now(UTC).isoformat(),
        units=tuple(units),
        confirmed_by=confirmed_by,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_suffix(f".{os.getpid()}.tmp")
    scratch.write_text(json.dumps(asdict(proof), indent=2) + "\n")
    os.replace(scratch, target)
    return proof


def prerequisites(directory: Path, before: str, head: str) -> list[Prerequisite]:
    """The contract markers on the revisions between ``before`` and ``head``.

    Oldest first. A revision with several markers needs the newest version it
    names."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(directory))
    found: list[Prerequisite] = []
    for revision in script.iterate_revisions(head or "head", before or "base"):
        try:
            text = Path(revision.path).read_text()
        except OSError:
            continue
        markers = [m for m in _MARKER.finditer(text) if _triple(m["version"])]
        if not markers:
            continue
        newest = max(markers, key=lambda m: _triple(m["version"]))
        found.append(
            Prerequisite(revision=revision.revision, version=newest["version"], what=newest["what"])
        )
    found.reverse()
    return found


def refusal(pending: list[Prerequisite], *, fresh: bool, proof: Proof | None) -> str:
    """Why these revisions must not run yet, or ``""`` when they may."""
    if fresh or not pending:
        return ""
    need = max(pending, key=lambda p: _triple(p.version))
    because = f"revision {need.revision} removes {need.what}, unused since {need.version}"
    if proof is None:
        return (
            f"{because}, and this node has no record of the release it is running. "
            f"Confirm every long-running unit runs {need.version} or later, then state it once: "
            f"`axi db migrate upgrade head --prerequisite-confirmed {need.version}`"
        )
    if _triple(proof.version) < _triple(need.version):  # type: ignore[operator]
        return (
            f"{because}, and this node last proved {proof.version}. "
            f"Deploy {need.version}, confirm its units started on it, then take this release"
        )
    return ""


def check(directory: Path, before: str, head: str, *, path: Path | None = None) -> str:
    """The refusal for moving a schema from ``before`` to ``head``, or ``""``."""
    if not before or before == head:
        return ""
    return refusal(prerequisites(directory, before, head), fresh=False, proof=read(path))


def confirm(version: str, *, who: str | None = None, path: Path | None = None) -> Proof:
    """Record the operator's statement that this node runs ``version`` or later.

    Only for a node with no record at all: the statement is a person's word and
    nothing verifies it, so once a record exists the record decides."""
    existing = read(path)
    if existing is not None:
        raise AlreadyProven(
            f"this node already has a record (it proved {existing.version} at {existing.at}); "
            "the record decides, and a statement cannot replace it"
        )
    from axiom.infra.change_intent import record_change

    person = who or getpass.getuser()
    with record_change(
        "config",
        "release-prerequisite",
        detail=f"{person} confirmed this node runs {version} or later",
    ):
        return write(version, (), confirmed_by=person, path=path)


def stale_units(
    units: Iterable[str], *, installed_at: float, started_at: Callable[[str], float | None]
) -> list[str]:
    """Units that began before the installed code was written, or cannot be read.

    A unit whose start cannot be read is not proven, so it is listed too."""
    stale = []
    for unit in units:
        began = started_at(unit)
        if began is None or began < installed_at:
            stale.append(unit)
    return stale


def prove(
    version: str,
    units: Iterable[str],
    *,
    installed_at: float,
    started_at: Callable[[str], float | None],
    path: Path | None = None,
) -> tuple[Proof | None, list[str]]:
    """Write the record when every unit started on the installed code.

    Returns the proof and no stale units, or no proof and the units that are
    still running older code. Nothing is written in the second case."""
    names = list(units)
    stale = stale_units(names, installed_at=installed_at, started_at=started_at)
    if stale or not names:
        return None, stale
    return write(version, names, path=path), []


# -- the node's own readings (thin; the rules above are tested without them) ----


def _process_start(pid: int) -> float | None:
    try:
        age = subprocess.run(
            ["ps", "-o", "etimes=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
        return time.time() - int(age) if age else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def systemd_started_at(unit: str) -> float | None:
    """When a user unit's newest process began; ``None`` when it is not running.

    A reload swaps the worker and leaves the unit's own start time alone, so the
    main process and its children are asked, and the newest counts."""
    try:
        pid = int(
            subprocess.run(
                ["systemctl", "--user", "show", "-p", "MainPID", "--value", unit],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout.strip() or 0
        )  # fmt: skip
        if pid <= 0:
            return None
        children = subprocess.run(
            ["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=10, check=False
        ).stdout.split()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    starts = [s for s in (_process_start(p) for p in [pid, *map(int, children)]) if s is not None]
    return max(starts) if starts else None


def installed(distributions: Iterable[str] = ()) -> tuple[str, float]:
    """The installed platform version, and when the newest named distribution was written."""
    from importlib import metadata

    own = (metadata.packages_distributions().get("axiom") or ["axiom-os-lm"])[0]
    version = metadata.version(own)
    newest = 0.0
    for name in {own, *distributions}:
        try:
            located = metadata.distribution(name).locate_file("")
            for record in Path(str(located)).glob(f"{name.replace('-', '_')}-*.dist-info/METADATA"):
                newest = max(newest, record.stat().st_mtime)
        except (metadata.PackageNotFoundError, OSError):
            continue
    return version, newest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m axiom.infra.deploy.proven_release",
        description="Record the release this node runs, once every unit has started on the installed code.",
    )
    parser.add_argument("units", nargs="*", help="the long-running units to check")
    parser.add_argument(
        "--dist", action="append", default=[], help="another installed distribution the units load"
    )
    parser.add_argument("--show", action="store_true", help="print the record and change nothing")
    args = parser.parse_args(argv)

    if args.show or not args.units:
        proof = read()
        print(json.dumps(asdict(proof), indent=2) if proof else "no record")
        return 0 if proof else 1
    version, installed_at = installed(args.dist)
    if not installed_at:
        print(
            "could not read when the installed code was written; nothing recorded", file=sys.stderr
        )
        return 1
    proof, stale = prove(
        version, args.units, installed_at=installed_at, started_at=systemd_started_at
    )
    if proof is None:
        print(
            f"not proven: these units are not running the installed code: {', '.join(stale)}",
            file=sys.stderr,
        )
        return 1
    print(f"proven: {proof.version} on {', '.join(proof.units)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
