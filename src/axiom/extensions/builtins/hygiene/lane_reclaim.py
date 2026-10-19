# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TIDY's half of `lane`: what can be reclaimed, proposed not executed.

ADR-046 splits the two infra agents by what they do rather than what they
touch: *RIVET makes the green; TIDY removes the brown.* Destructive
working-state cleanup — worktrees included — is TIDY's, and RIVET is
forbidden it. `lane` adds a fourth kind of brown to the three TIDY already
knows: a lane whose checkout is gone, whose branch has landed, or whose
database no longer answers to anything.

**Everything here returns a proposal.** Nothing drops a database, removes a
worktree or edits the registry. That is not timidity: a lane released by
mistake is re-claimed in a second, and a database dropped by mistake is
somebody's afternoon. The asymmetry is the whole reason the proposal and the
act are separate calls.

The guard that matters most is the one TIDY already grew for worktrees and
`lane` extended — F2, the bound floor. A lane may be perfectly finished by
every git measure and still be the thing a running server imports from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Reclaim:
    """One lane, and what could be done about it."""

    lane: str
    why: list[str] = field(default_factory=list)
    #: Commands, in order. Printed for a human or an approval flow — never run.
    proposed: list[str] = field(default_factory=list)
    #: Non-empty means do not act. A reason the evidence is not enough.
    blocked_by: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return bool(self.proposed) and not self.blocked_by

    def render(self) -> str:
        head = f"{self.lane}: " + "; ".join(self.why)
        if self.blocked_by:
            return head + "\n    BLOCKED — " + "; ".join(self.blocked_by)
        return head + "".join(f"\n    {c}" for c in self.proposed)


def assess(lanes: dict, *, workspace: Path, venvs: list[Path] | None = None,
           branch_landed=None) -> list[Reclaim]:
    """Which lanes look reclaimable, and which must not be touched.

    `branch_landed(branch) -> bool | None` is injected rather than called
    here: deciding whether a branch has landed is RIVET's authority, not
    TIDY's (ADR-046). ``None`` means "cannot tell", which is treated as a
    block rather than a yes — this is the direction where being wrong costs
    something.
    """
    from .worktrees import editable_bindings, default_venvs

    checked = venvs if venvs is not None else default_venvs(workspace)
    out: list[Reclaim] = []

    for name, lane in sorted(lanes.items()):
        why: list[str] = []
        blocked: list[str] = []

        root = Path(lane.root) if getattr(lane, "root", "") else None
        if root is not None and not root.exists():
            why.append(f"its checkout {root.name} is gone")
        elif root is not None and branch_landed is not None and lane.branch:
            landed = branch_landed(lane.branch)
            if landed is True:
                why.append(f"branch {lane.branch} has landed")
            elif landed is None:
                blocked.append(f"cannot tell whether {lane.branch} landed")

        # "Cannot tell" must SURFACE, not vanish. An early return on an empty
        # `why` dropped exactly the lanes TIDY was unable to assess, which is
        # the set most worth showing a human: silence read as "fine".
        if not why and not blocked:
            continue
        if not why:
            why.append("could not be assessed")

        # F2 — the floor. A gone checkout cannot be bound, but a landed one can.
        if root is not None and root.exists():
            bound = editable_bindings(root, checked)
            for venv, package in bound:
                blocked.append(
                    f"{venv} imports '{package}' from this checkout — "
                    "removing it breaks a running process, and git cannot see that"
                )

        proposed = [f"axi lane release {name}"]
        if getattr(lane, "database", "") and lane.isolated:
            # Printed, never run. Dropping a database is the irreversible one.
            proposed.append(f"dropdb {lane.database}   # irreversible; run deliberately")

        out.append(Reclaim(lane=name, why=why, proposed=proposed, blocked_by=blocked))

    return out


def render(items: list[Reclaim]) -> str:
    if not items:
        return "nothing to reclaim"
    lines = [r.render() for r in items]
    blocked = sum(1 for r in items if r.blocked_by)
    if blocked:
        lines += ["", f"{blocked} blocked — a lane in use is not a lane to reclaim."]
    return "\n".join(lines)


__all__ = ["Reclaim", "assess", "render"]
