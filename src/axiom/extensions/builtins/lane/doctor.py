# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What the registry says, against what the machine is doing.

A registry records intent. `doctor` is the compensating check for everything
intent cannot see: the session that never claimed, the lane that died, the
checkout somebody deleted while a server was importing from it.

The last of those is why this module exists in the shape it does. A worktree
was landed, clean, merged, and removed — correct by every git and pull-request
measure — and two long-running servers had been importing a package from it
since the day before. Nothing noticed until a stylesheet returned 500 two days
later, and the traceback was in a log nobody was reading. Git could not have
warned: the binding lived in a virtualenv. So the check that matters most here
is the dullest one — **does every editable install still point at a directory
that exists** — and it is the check a tool written from the registry outwards
would never have thought to include.

Findings are graded, and the grading is the point: `broken` is something that
is wrong now, `drift` is something that will be wrong later, and `note` is a
fact worth surfacing that is nobody's fault. A checker that reports everything
at one severity gets ignored at every severity.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .registry import RESERVED, UNMANAGED, Lane

BROKEN = "broken"
DRIFT = "drift"
NOTE = "note"


@dataclass(frozen=True)
class Finding:
    level: str
    subject: str
    detail: str
    fix: str = ""

    def render(self) -> str:
        tail = f"  → {self.fix}" if self.fix else ""
        return f"[{self.level}] {self.subject}: {self.detail}{tail}"


# --- what the machine is actually doing ------------------------------------


def listening_ports() -> dict[int, str]:
    """``{port: "command pid"}`` for everything listening on loopback."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
    except Exception:
        return {}
    found: dict[int, str] = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        m = re.search(r":(\d+)$", parts[8])
        if m:
            found[int(m.group(1))] = f"{parts[0]} pid {parts[1]}"
    return found


def editable_targets(venv: Path) -> dict[str, Path]:
    """``{package: source directory}`` for every editable install in `venv`.

    Reads both spellings pip has used: a bare path in a ``.pth``, and the
    ``__editable___*_finder.py`` mapping modern pip writes. Cheap, and the
    only way to see a binding git cannot.
    """
    out: dict[str, Path] = {}
    for site in venv.glob("lib/python*/site-packages"):
        for pth in site.glob("*.pth"):
            try:
                for line in pth.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("/") and Path(line).name:
                        out.setdefault(pth.stem.replace("__editable__.", ""), Path(line))
            except OSError:
                continue
        for finder in site.glob("__editable___*_finder.py"):
            try:
                text = finder.read_text()
            except OSError:
                continue
            for pkg, path in re.findall(r"'([A-Za-z0-9_.]+)':\s*'([^']+)'", text):
                out.setdefault(pkg, Path(path))
    return out


# --- the checks -------------------------------------------------------------


def check_editable_installs(venvs: list[Path]) -> list[Finding]:
    """Every editable install must point at something that still exists.

    This is the check that would have caught a removed worktree BEFORE the
    removal, and caught it again at any point in the two days afterwards.
    """
    findings: list[Finding] = []
    for venv in venvs:
        for package, target in sorted(editable_targets(venv).items()):
            if target.exists():
                continue
            findings.append(
                Finding(
                    BROKEN,
                    f"{venv.name}:{package}",
                    f"editable install points at {target}, which does not exist",
                    "reinstall from a live checkout: pip install -e <path>",
                )
            )
    return findings


def check_ports(lanes: dict[str, Lane], listening: dict[int, str]) -> list[Finding]:
    claimed = {p: name for name, lane in lanes.items() for p in lane.ports}
    findings: list[Finding] = []
    for port, who in sorted(listening.items()):
        if port in RESERVED or port in claimed:
            continue
        if not (8000 <= port <= 9999):
            continue  # somebody's editor, not our business
        findings.append(
            Finding(
                DRIFT,
                f":{port}",
                f"{who} is listening and no lane claims it",
                "claim it, or stop it",
            )
        )
    for name, lane in sorted(lanes.items()):
        down = [p for p in lane.ports if p not in listening]
        if len(down) == len(lane.ports):
            findings.append(
                Finding(NOTE, name, "claimed but nothing is listening", "start it, or release")
            )
    return findings


def check_trees(lanes: dict[str, Lane], root: Path) -> list[Finding]:
    """Checkouts a lane says must move together, that are not there."""
    findings: list[Finding] = []
    for name, lane in sorted(lanes.items()):
        for tree in lane.trees:
            p = Path(tree) if Path(tree).is_absolute() else root / tree
            if not p.exists():
                findings.append(
                    Finding(
                        BROKEN,
                        f"{name}:{tree}",
                        "a tree this lane composes from is missing",
                        "restore the worktree, or drop it from the lane",
                    )
                )
    return findings


def pin_state(tree: Path) -> tuple[bool, int] | None:
    """``(is_pinned, commits_behind)`` for a checkout, or None if it is not one.

    Pinned means a DETACHED worktree — the arrangement `axiom-service`,
    `nos-service` and `appkit-service` use so a serving node does not move
    when somebody checks out a branch in the tree they develop in.
    """
    import subprocess

    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", "-C", str(tree), *args],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    if git("rev-parse", "--git-dir") is None:
        return None
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    pinned = branch == "HEAD"
    behind = git("rev-list", "--count", "HEAD..origin/main")
    return pinned, int(behind) if behind and behind.isdigit() else 0


def check_pins(
    lanes: dict[str, Lane],
    root: Path,
    *,
    state: Callable[[Path], tuple[bool, int] | None] = pin_state,
) -> list[Finding]:
    """A pinned serving tree that has fallen behind what it pins to.

    Pinning stops a serving node moving when somebody checks out a branch.
    It does not stop the tree going STALE, and a stale pin is quieter than
    a missing one: the node serves, every probe is green, and it is serving
    code from whenever somebody last remembered. `axiom-service` was nine
    commits behind when this was written, and nothing anywhere said so.

    Reported, not advanced. Moving a serving tree to whatever is on main is
    a deployment, and a deployment nobody asked for is how a local pair
    starts running code that has never been looked at.

    `git pull` is not the fix and is not offered: it fails on a detached
    HEAD, which is what a pin is.
    """
    findings: list[Finding] = []
    for name, lane in sorted(lanes.items()):
        for tree in lane.trees:
            path = Path(tree) if Path(tree).is_absolute() else root / tree
            if not path.exists():
                continue  # check_trees owns absence; this owns staleness
            got = state(path)
            if got is None:
                continue
            pinned, behind = got
            if pinned and behind:
                findings.append(
                    Finding(
                        DRIFT,
                        f"{name}:{tree}",
                        f"pinned and {behind} commit(s) behind origin/main",
                        f"git -C {tree} checkout origin/main   (pull fails on a detached HEAD)",
                    )
                )
    return findings


def check_holds(lanes: dict[str, Lane]) -> list[Finding]:
    """Files more than one lane is editing right now.

    The collision ports and databases do not touch. Reported at DRIFT, and
    deliberately not at BROKEN: two sessions genuinely do need the same file
    sometimes, and a check that treated it as a fault would be silenced, and
    then nobody would know anything. Knowing is the whole of it.
    """
    by_path: dict[str, list[str]] = {}
    for name, lane in sorted(lanes.items()):
        for path in lane.holds:
            by_path.setdefault(path, []).append(name)
    return [
        Finding(
            DRIFT,
            path,
            f"held by {', '.join(sorted(who))} at once",
            "agree who has it, and `axi lane drop` from the other",
        )
        for path, who in sorted(by_path.items())
        if len(who) > 1
    ]


def check_isolation(lanes: dict[str, Lane]) -> list[Finding]:
    """Say plainly which lanes are not database-isolated.

    Reported, never hidden and never an error. A lane that honestly declares
    itself unmanaged is safer than one that claims an isolation it lacks —
    the danger is a reader who believes the claim.
    """
    return [
        Finding(
            NOTE,
            name,
            f"not database-isolated ({UNMANAGED}); migrations here affect others",
            "set dsn_var when it gains its own database",
        )
        for name, lane in sorted(lanes.items())
        if not lane.isolated
    ]


def run(
    lanes: dict[str, Lane],
    *,
    venvs: list[Path],
    root: Path,
    listening: dict[int, str] | None = None,
) -> list[Finding]:
    heard = listening_ports() if listening is None else listening
    findings = [
        *check_editable_installs(venvs),
        *check_ports(lanes, heard),
        *check_trees(lanes, root),
        *check_pins(lanes, root),
        *check_holds(lanes),
        *check_isolation(lanes),
    ]
    order = {BROKEN: 0, DRIFT: 1, NOTE: 2}
    return sorted(findings, key=lambda f: (order.get(f.level, 9), f.subject))


__all__ = [
    "BROKEN",
    "DRIFT",
    "NOTE",
    "Finding",
    "check_editable_installs",
    "check_holds",
    "check_pins",
    "pin_state",
    "check_isolation",
    "check_ports",
    "check_trees",
    "editable_targets",
    "listening_ports",
    "run",
]
