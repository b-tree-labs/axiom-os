# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``state.restore`` -- restore a snapshot; dry run unless ``apply``."""

from __future__ import annotations

from typing import Any

from axiom.infra import state_snapshots as ss
from axiom.infra.skills import SkillContext, SkillResult

from ._common import cli, resolve


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    state_dir, root, policy, _store = resolve(params, ctx)
    c = cli()
    try:
        r = ss.restore_snapshot(
            state_dir, root, params.get("id") or "latest",
            only=tuple(params.get("only") or ()), apply=bool(params.get("apply")),
            force=bool(params.get("force")), policy=policy,
        )
    except ss.SnapshotError as exc:
        return SkillResult(ok=False, errors=[str(exc)],
                           value={"next_steps": [f"{c} state ls", f"{c} state verify"]})
    changes = [d for d in r.diff if d.action not in ("unchanged", "kept")]
    kept = [d.path for d in r.diff if d.action == "kept"]
    nxt: list[str]
    if r.dry_run:
        nxt = ([f"{c} state restore {r.snapshot_id} --apply   (stop services first)"] if changes
               else [f"{c} state ls"])
        nxt.append(f"{c} state verify {r.snapshot_id}")
    elif r.ok:
        nxt = [
            "restart the services you stopped",
            f"{c} state diff {r.snapshot_id}   (should now show no changes)",
            f"undo: {c} state restore {r.pre_restore_snapshot} --apply" if r.pre_restore_snapshot
            else "undo: move the *.pre-restore-" + r.stamp + " files back",
            f"tidy up once satisfied: find {state_dir} -name '*.pre-restore-{r.stamp}*' -delete",
            f"databases are not in snapshots; see `{c} state snapshot --with-db` / the runbook",
        ]
    else:
        nxt = r.next_steps + ([f"{c} state restore {r.snapshot_id} --apply   (re-run: only what still differs is written)"]
                              if r.not_restored else []) or [f"{c} state verify", f"{c} state ls"]
    return SkillResult(
        ok=r.ok, errors=r.errors,
        value={**ss.snapshot_dict(r), "changes": [ss.snapshot_dict(d) for d in changes],
               "kept": kept,
               "next_steps": nxt},
        actions_taken=([f"restored {len(r.restored)} file(s) from {r.snapshot_id}"] if r.applied else
                       [f"dry run: {len(changes)} change(s) from {r.snapshot_id}; nothing written"] if r.snapshot_id
                       else []),
    )
