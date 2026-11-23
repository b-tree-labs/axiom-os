# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``node.clean``: what could be reclaimed, as a plan.

Plans only in this release. A plan lists what KEEL can see: models installed in
Ollama that no role is pinned to, container images nothing uses, and hot-tier
directories that the declarations say belong on the bulk tier. Nothing here is
destructive, and nothing measured is ever deleted: AEOS 1.0 §6.2 wants a
reviewer for destructive steward operations, and KEEL's answer is to archive.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import collect, declarations
from ._handoff import next_step
from .inventory import _collectors, _state_dir

GB = 2**30


def plan_items(ctx: SkillContext | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    c = _collectors(ctx)
    decl = declarations.load(_state_dir(ctx))
    # Ollama models no role is pinned to.
    try:
        from axiom.llm import model_alignment

        a = model_alignment.check()
        if a.installed is not None:
            for m in sorted(a.installed - set(a.pins.values())):
                items.append(
                    {
                        "action": "unpin",
                        "what": f"ollama model {m}",
                        "reclaims": "",
                        "why": "installed but no role resolves to it",
                        "how": f"ollama rm {m}",
                        "safe": "the model can be pulled again",
                    }
                )
    except Exception:  # noqa: BLE001 - no Ollama, no items
        pass
    # Container images nothing uses (reported, never pruned by KEEL: another session may own them).
    text = c.extra.get("docker_df") if isinstance(c.extra, dict) else None
    if text is None:
        text = collect.run(["docker", "system", "df", "--format", "{{json .}}"], timeout=15)
    for line in (text or "").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("Type") == "Images" and row.get("Reclaimable", "0B") not in ("0B", "0 B"):
            items.append(
                {
                    "action": "report",
                    "what": "container images nothing uses",
                    "reclaims": str(row.get("Reclaimable")).split(" ")[0],
                    "why": "images are owned by whoever created them; KEEL reports, the owner prunes",
                    "how": "docker image prune (by the owner)",
                    "safe": "pullable again",
                }
            )
    # Hot tier under pressure with a bulk tier declared: archive candidates are the owner's call; KEEL names the destination.
    hot = [t for t in decl.tiers if t.tier == "hot" and t.floor_gb]
    bulk = [t for t in decl.tiers if t.tier == "bulk"]
    if hot and bulk:
        disks = {d["mount"]: d for d in collect.parse_df(c.df())}
        for t in hot:
            d = disks.get(t.mount)
            if d and d["free_bytes"] / GB < t.floor_gb:
                items.append(
                    {
                        "action": "archive",
                        "what": f"idle data on {t.mount}",
                        "reclaims": f"to reach {t.floor_gb} GB free",
                        "why": f"{t.mount} is under its floor and {bulk[0].mount} is the declared bulk tier",
                        "how": f"move idle directories to {bulk[0].mount}, leave symlinks; owners decide which",
                        "safe": "a move, not a deletion",
                    }
                )
    return items


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    if params.get("apply"):
        return SkillResult(
            ok=False,
            errors=[
                "node clean --apply is not available in this release: plans are proposals, and applying one is a separate approved step that archives, never deletes"
            ],
        )
    items = plan_items(ctx)
    plan_id = (
        hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()[:10] if items else ""
    )
    value = {
        "plan_id": plan_id,
        "items": items,
        "note": "plans only in this release; nothing measured is ever deleted" if items else "",
    }
    return SkillResult(
        ok=True,
        value=value,
        actions_taken=[f"planned {len(items)} reclaim item(s)"],
        next=[next_step("node.vitals", "confirm the headroom after the owner acts")]
        if items
        else [],
    )


__all__ = ["plan_items", "run"]
