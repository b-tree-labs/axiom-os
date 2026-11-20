# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``state.show`` -- one snapshot's contents, exclusions and coverage."""

from __future__ import annotations

from collections import Counter
from typing import Any

from axiom.infra import state_snapshots as ss
from axiom.infra.skills import SkillContext, SkillResult

from ._common import cli, coverage_lines, resolve


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    state_dir, root, _policy, store = resolve(params, ctx)
    try:
        sid = ss.resolve_id(store, params.get("id") or "latest")
        m = store.read_manifest(sid)
    except ss.SnapshotError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    b = m["body"]
    files = b["files"]
    tiers = Counter(f["tier"] for f in files)
    c = cli()
    return SkillResult(ok=True, value={
        "id": sid, "created_at": b["created_at"], "host": b["host"], "axiom_version": b["axiom_version"],
        "reason": b["reason"], "trigger": b["trigger"], "pinned": store.is_pinned(sid),
        "files": len(files), "bytes": sum(f["size"] for f in files), "tiers": dict(tiers),
        "critical": [f["path"] for f in files if f["tier"] == "critical"],
        "excluded": b["excluded"], "secret_metadata": b.get("secret_metadata", []),
        "db": b.get("db", {}), "not_covered": b.get("not_covered", []),
        "chain_hash": m["chain_hash"], "ledger_head": store.ledger_head(),
        "coverage": coverage_lines(root, state_dir),
        "next_steps": [f"{c} state verify {sid}", f"{c} state diff {sid}", f"{c} state restore {sid}"],
    })
