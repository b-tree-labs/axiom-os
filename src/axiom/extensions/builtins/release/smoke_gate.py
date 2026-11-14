# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The release cut's smoke gate (LOOP-S6, ADR-257 D7).

A release is cut only from a main commit whose smoke journeys passed. No CI job runs the
journeys per commit: they already run against the idle slot at every deploy, before traffic
moves (LOOP-S5), and that run is recorded in the journey store by commit. The gate reads the
newest recorded result for the candidate commit, or for the nearest ancestor whose diff to
the candidate touches no shipped code (a documentation or test change does not need its own
smoke run). It refuses only when that result is missing or failed. When nothing is recorded,
``release cut --run-smoke URL`` runs the journeys once, on demand, and records them.

The release lock is the cut's own (``release_lock.take``, before the bump): another
release's lock refuses the cut there.

It is behind the ``release_smoke_gate`` flag (default off) until the owner turns it on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FLAG = "release_smoke_gate"
BRANCH = "main"
#: Paths whose change ships: a commit that touches one needs its own smoke result.
SHIPPED_PREFIXES: tuple[str, ...] = ("src/", "packages/", "infra/charts/", "infra/images/")
SHIPPED_FILES: frozenset[str] = frozenset({"pyproject.toml"})
#: How far back the gate looks for an ancestor's result.
MAX_ANCESTORS = 200


@dataclass
class Evidence:
    sha: str
    passed: bool
    detail: str = ""
    smoked: str = ""  # the commit the result was recorded for


@dataclass
class GateResult:
    ok: bool
    sha: str
    reasons: list[str] = field(default_factory=list)
    evidence: Evidence | None = None

    def lines(self) -> list[str]:
        if self.ok and self.evidence is not None:
            return [f"smoke journeys passed for {self.sha[:10]} ({self.evidence.detail})"]
        return self.reasons


def flag_on() -> bool:
    """Whether the gate is on (the ``release_smoke_gate`` flag, ADR-210). Off when unreadable."""
    from axiom.extensions.builtins.flags.service import env_override

    pinned = env_override(FLAG)
    if pinned is not None:
        return pinned == "on"
    try:
        from axiom.extensions.builtins.flags.service import FlagService

        return FlagService().is_enabled(FLAG, node=os.environ.get("AXIOM_NODE") or None)
    except Exception:  # noqa: BLE001 - an unreadable flag store leaves the default (off)
        return False


def ships(path: str) -> bool:
    return path in SHIPPED_FILES or path.startswith(SHIPPED_PREFIXES)


def evidence(mgr: Any, sha: str, *, state_dir: Path) -> Evidence:
    """The newest smoke result for ``sha`` or the nearest ancestor with no shipped change since."""
    from axiom.extensions.builtins.synthetic import store

    for candidate in mgr._git("rev-list", f"--max-count={MAX_ANCESTORS}", sha).split():
        rec = store.verdict(state_dir, candidate)
        if rec is None:
            continue
        since = ""
        if candidate != sha:
            shipped = [p for p in mgr._git("diff", "--name-only", candidate, sha).split() if ships(p)]
            if shipped:
                more = f" and {len(shipped) - 3} more" if len(shipped) > 3 else ""
                return Evidence(sha, False, f"the newest smoke result is for {candidate[:10]}, and shipped code "
                                            f"changed since ({', '.join(shipped[:3])}{more})", candidate)
            since = f", for {candidate[:10]}; nothing shipped changed since"
        where = f"{rec.get('source') or 'recorded'} run of {rec.get('role') or 'a role'}{since}"
        if rec.get("status") == "pass":
            return Evidence(sha, True, where, candidate)
        failing = "; ".join(rec.get("failing") or []) or str(rec.get("status"))
        return Evidence(sha, False, f"the newest smoke result failed ({where}): {failing}", candidate)
    return Evidence(sha, False, "no smoke result is recorded for it or an ancestor with no shipped change since")


def check(mgr: Any, *, state_dir: Path) -> GateResult:
    """Every reason the commit at HEAD must not be released; ``ok`` when there are none."""
    sha = mgr._git("rev-parse", "HEAD")
    reasons: list[str] = []
    try:
        mgr._git("merge-base", "--is-ancestor", sha, f"origin/{BRANCH}")
    except RuntimeError:
        reasons.append(f"{sha[:10]} is not on origin/{BRANCH}: a release is cut only from a {BRANCH} commit")
    ev = evidence(mgr, sha, state_dir=state_dir)
    if not ev.passed:
        reasons.append(f"smoke journeys have not passed for {sha[:10]}: {ev.detail}")
    return GateResult(ok=not reasons, sha=sha, reasons=reasons, evidence=ev)


__all__ = ["BRANCH", "FLAG", "MAX_ANCESTORS", "SHIPPED_FILES", "SHIPPED_PREFIXES", "Evidence", "GateResult",
           "check", "evidence", "flag_on", "ships"]
