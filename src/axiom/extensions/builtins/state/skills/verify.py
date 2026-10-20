# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``state.verify`` -- re-hash blobs, recompute the chain, cross-check the ledger."""

from __future__ import annotations

from typing import Any

from axiom.infra import state_snapshots as ss
from axiom.infra.skills import SkillContext, SkillResult

from ._common import cli, resolve


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    _sd, _root, _policy, store = resolve(params, ctx)
    deep = not params.get("quick")
    v = ss.verify_store(store, deep=deep)
    if params.get("id"):
        try:
            sid = ss.resolve_id(store, params["id"])
        except ss.SnapshotError as exc:
            return SkillResult(ok=False, errors=[str(exc)])
        results = [r for r in v.results if r.id == sid]
    else:
        results = v.results
    c = cli()
    bad = [r for r in results if not r.ok]
    errors = [f"{r.id}: {p}" for r in bad for p in r.problems]
    if not params.get("id"):
        errors += [f"ledger: {p}" for p in v.ledger.problems] + [f"store: {p}" for p in v.store_problems]
    good = [r.id for r in results if r.ok]
    nxt = ([f"{c} state restore {sorted(good)[-1]}   (newest intact snapshot)"]
           if good else [f"{c} state snapshot --force   (take a fresh one)"])
    if bad:
        nxt.append(f"{c} state prune   (corrupt snapshots are removed once an intact one is protected)")
    return SkillResult(
        ok=not errors,
        errors=errors,
        value={"deep": deep, "checked": len(results), "ok_ids": good, "bad_ids": [r.id for r in bad],
               "ledger_ok": v.ledger.ok, "ledger_head": v.ledger.head, "ledger_entries": v.ledger.entries,
               "next_steps": nxt},
        actions_taken=[f"verified {len(results)} snapshot(s) ({'deep' if deep else 'quick'}); "
                       f"ledger head {v.ledger.head[:16] or '(empty)'}  <- record this off-box to detect a full rewrite"],
    )
