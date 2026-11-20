# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Run one (artifact x damage) cell: populate, damage, run the real reader, classify."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from .artifacts import ARTIFACTS, Artifact, populate, sha
from .harness import (
    CRASH,
    DIR_DAMAGE,
    FILE_DAMAGE,
    ChaosRoot,
    ProbeResult,
    classify,
    damage,
    restore_permissions,
    run_probe,
)


def damage_kinds(art: Artifact) -> tuple[str, ...]:
    return FILE_DAMAGE if art.kind == "file" else DIR_DAMAGE


def cells() -> list[tuple[str, str]]:
    return [(a.id, k) for a in ARTIFACTS for k in damage_kinds(a)]


@dataclass
class Cell:
    artifact: str
    damage: str
    klass: str
    why: str
    violation: str | None
    result: ProbeResult

    @property
    def final(self) -> str:
        """``crash`` also when the reader 'worked' but the answer was wrong."""
        return CRASH if self.violation else self.klass

    @property
    def silent_loss(self) -> bool:
        return self.klass == "silent-default" and self.damage not in ("partial_write",)


def run_cell(art: Artifact, kind: str, root: Path) -> Cell:
    chaos = ChaosRoot(root)
    try:
        pop = populate(art, chaos)
        target = chaos.path(art.rel)
        before = sha(target) if target.is_file() else ""
        damage(kind, target, chaos.root, wrong_schema=art.wrong_schema)
        # What the damaged file held, so a check can tell "the reader left it alone"
        # from "the reader wrote something over it".
        pop = {**(pop if isinstance(pop, dict) else {}), "original_sha": before,
               "damaged_sha": sha(target) if target.is_file() else ""}
        res = run_probe(chaos, art.probe)
        klass, why = classify(res, art.hint_for(chaos) if art.hint is None else art.hint)
        if res.outcome == "error" and art.hint:
            klass, why = classify(res, art.hint)
        violation = art.check(res, chaos, pop, kind)
        return Cell(art.id, kind, klass, why, violation, res)
    finally:
        restore_permissions(chaos.root)


def run_cell_tmp(art_id: str, kind: str) -> Cell:
    from .artifacts import BY_ID

    with tempfile.TemporaryDirectory(prefix="axiom-chaos-") as td:
        return run_cell(BY_ID[art_id], kind, Path(td).resolve())


ABSENT = frozenset({"delete_file", "delete_dir", "rename_away", "dangling_symlink"})
#: Kinds after which the original bytes are intact or the file is simply gone:
#: a silent default is the honest answer (it is indistinguishable from first run).
BENIGN_SILENT = ABSENT | {"partial_write", "future_version"}


def verdict(art: Artifact, cell: Cell) -> str | None:
    """None when the outcome is acceptable, else the reason it is not."""
    if cell.violation:
        return cell.violation
    if cell.klass == CRASH:
        return cell.why
    if art.empty_is_valid and cell.damage == "zero_length":
        return None
    if cell.klass == "silent-default" and art.loss_matters and cell.damage not in BENIGN_SILENT:
        return "silent default over damaged user data (nothing said)"
    return None
