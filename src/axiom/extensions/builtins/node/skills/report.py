# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``node.report``: the node in one document, for handover and onboarding."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from . import doctor, drift, inventory, vitals
from ._handoff import FLEET_REPORT, WHOAMI, handoff, next_step, reachable

GB = 2**30


def gather(ctx: SkillContext | None, *, since: str = "7d") -> dict[str, Any]:
    ident: dict[str, Any] = {}
    if reachable(ctx, WHOAMI):
        rec = handoff(ctx, WHOAMI, {})
        if rec["status"] == "ok" and isinstance(rec.get("value"), dict):
            ident = rec["value"]
    inv = inventory.gather(ctx)
    vit = vitals.gather(ctx)
    doc = doctor.gather()
    dr = drift.gather(ctx)
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "since": since,
        "identity": ident,
        "inventory": inv,
        "vitals": vit,
        "doctor": doc,
        "drift": dr,
    }


def markdown(r: dict[str, Any]) -> str:
    inv, vit, doc, dr = r["inventory"], r["vitals"], r["doctor"], r["drift"]
    lines = [f"# Node report ({r['generated_at']})", ""]
    if r.get("identity"):
        lines += (
            ["## Identity", ""]
            + [f"- {k}: {v}" for k, v in r["identity"].items() if isinstance(v, (str, int, float))][
                :12
            ]
            + [""]
        )
    lines += ["## Vitals", "", f"Verdict: **{vit['verdict']}**", ""]
    for d in vit["disks"]:
        tier = f" ({d['tier']})" if d.get("tier") else ""
        lines.append(
            f"- {d['mount']}{tier}: {d['free_bytes'] / GB:.0f} GB free of {d['size_bytes'] / GB:.0f} GB, {d['state']}"
        )
    for g in vit.get("gpu", []):
        lines.append(
            f"- GPU {g['index']} {g['name']}: {g['memory_used_mib']} of {g['memory_total_mib']} MiB used"
        )
    lines += ["", "## Engines and models", ""]
    for e in inv.get("engines", []):
        port = f":{e['port']}" if e.get("port") else ""
        models = (
            ", ".join(
                e.get("models") or e.get("served_as") or ([e["model"]] if e.get("model") else [])
            )
            or "?"
        )
        state = "running" if e.get("running") else "declared, not running"
        lines.append(
            f"- {e['name']} ({e.get('kind', '?')}{port}, {e.get('source', 'detected')}, {state}): {models}"
            + (f"; owner {e['owner']}" if e.get("owner") else "")
            + (f"; weights {e['weights']}" if e.get("weights") else "")
        )
    if not inv.get("engines"):
        lines.append("- none detected or declared")
    units = inv.get("units", [])
    failed = [u["unit"] for u in units if u.get("active") == "failed"]
    lines += [
        "",
        f"## Units: {sum(1 for u in units if u.get('active') == 'active')} active, {len(failed)} failed",
        "",
    ]
    lines += [f"- failed: {u}" for u in failed]
    lines += ["", f"## Doctor: {doc['summary']}", ""]
    for f in doc["findings"]:
        if f["severity"] in ("error", "warning"):
            lines.append(
                f"- [{f['severity']}] {f['name']}: {f['message']}"
                + (f" Fix: {f['fix']}" if f.get("fix") else "")
            )
    lines += ["", f"## Drift: {dr['summary']}", ""]
    for f in dr["findings"]:
        lines.append(
            f"- [{f['severity']}] {f['code']}: {f['message']}"
            + (f" Fix: {f['fix']}" if f.get("fix") else "")
        )
    if inv.get("exposed_credentials"):
        lines += ["", "## Exposed credentials", ""] + [
            f"- {x['engine']}: {x['where']}" for x in inv["exposed_credentials"]
        ]
    return "\n".join(lines).rstrip() + "\n"


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    value = gather(ctx, since=str(params.get("since") or "7d"))
    if params.get("markdown"):
        value["markdown"] = markdown(value)
    steps = [
        next_step(
            FLEET_REPORT,
            "push this node's report to the fleet console (the node reports to a console)",
        )
    ]
    return SkillResult(
        ok=True,
        value=value,
        actions_taken=["composed the node report from inventory, vitals, doctor and drift"],
        next=steps,
    )


__all__ = ["gather", "markdown", "run"]
