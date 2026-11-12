# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi node <verb>``: KEEL's command, a thin wrapper over the skills (ADR-056).

Every verb builds the parameters, invokes ``node.<verb>`` through the
registry, and renders the result. Nothing here reads the machine; the
skills do, so chat, MCP and this command cannot disagree.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext

VERBS = ("inventory", "vitals", "doctor", "drift", "report", "declare", "clean", "reap", "window")


def skill_for(verb: str) -> str:
    """The registered skill behind a verb; ``reap`` is the one whose names differ."""
    return {"reap": "node.reap_worktrees"}.get(verb, f"node.{verb}")


def _cli_name() -> str:
    try:
        from axiom.infra.brand import cli_name

        return cli_name()
    except Exception:  # noqa: BLE001 - the brand is cosmetic here
        return "axi"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"{_cli_name()} node", description="KEEL: the machine this node runs on"
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="verb", metavar="verb")

    inv = sub.add_parser(
        "inventory", help="what runs here: engines, models, units, ports, GPU, disks"
    )
    inv.add_argument(
        "--only", help="comma-separated sections: engines,models,units,ports,gpu,disks"
    )

    vit = sub.add_parser("vitals", help="how the machine is doing right now")
    vit.add_argument(
        "--thresholds", action="store_true", help="show the floors the verdict is judged against"
    )
    vit.add_argument(
        "--alert", action="store_true", help="hand any breach to HERALD (what the heartbeat does)"
    )
    vit.add_argument(
        "--reap", action="store_true",
        help="on an even keel, also reap finished worktrees (the heartbeat's upkeep)",
    )

    doc = sub.add_parser("doctor", help="what is wrong and the exact fix")
    doc.add_argument("--check", help="run one check by name")

    dr = sub.add_parser("drift", help="where the node disagrees with itself or its tracked source")
    dr.add_argument("--only", help="comma-separated rules: pins,files,units")

    rep = sub.add_parser("report", help="the node in one document")
    rep.add_argument("--since", default="7d", help="window for recent events (default 7d)")
    rep.add_argument("--md", action="store_true", help="render Markdown")

    dec = sub.add_parser("declare", help="tell KEEL about what Axiom did not install")
    kind = dec.add_subparsers(dest="kind", metavar="kind", required=True)
    eng = kind.add_parser("engine", help="a model engine run outside Axiom")
    eng.add_argument("--name", required=True)
    eng.add_argument("--unit", default="")
    eng.add_argument("--port", type=int)
    eng.add_argument("--weights", default="", help="directory the weights are read from")
    eng.add_argument("--owner", default="", help="the account or role that owns it")
    eng.add_argument("--kind", default="", help="vllm, ollama, llama.cpp, ...")
    eng.add_argument("--note", default="")
    tier = kind.add_parser("tier", help="a storage tier for a mount")
    tier.add_argument("--mount", required=True)
    tier.add_argument("--tier", choices=("hot", "bulk"), default="hot")
    tier.add_argument(
        "--floor", type=int, default=0, help="GB free below which vitals report pressure"
    )
    trk = kind.add_parser("tracked", help="a live file and the source it should match")
    trk.add_argument("--live", required=True)
    trk.add_argument("--source", default="")

    cl = sub.add_parser("clean", help="what could be reclaimed, as a plan")
    cl.add_argument("--plan", action="store_true", default=True, help="show the plan (default)")
    cl.add_argument("--apply", metavar="PLAN", help="apply a plan by id (approval required)")
    rp = sub.add_parser("reap", help="remove finished git worktrees safely (plan unless --apply)")
    rp.add_argument("--root", default="", help="workspace to sweep")
    rp.add_argument("--apply", action="store_true", help="remove, up to the batch size")
    rp.add_argument("--min-age-hours", type=float, default=None)
    rp.add_argument("--batch", type=int, default=None)
    rp.add_argument("--retention-days", type=float, default=None)
    win = sub.add_parser(
        "window", help="maintenance windows: who is working on this node, on what, until when"
    )
    win.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=("status", "open", "extend", "close", "check"),
    )
    win.add_argument(
        "--scope", default="", help="node,database,serving,storage,models,network,deploy"
    )
    win.add_argument(
        "--for", dest="expect", default="", help="how long, like 45m or 2h (open, extend)"
    )
    win.add_argument("--owner", default="", help="who to ask (open; defaults to you)")
    win.add_argument("--note", default="", help="what is being done (open)")
    win.add_argument("--id", default="", help="the window (extend, close)")
    win.add_argument("--outcome", default="", help="how it went (close)")
    return p


def _params(args: argparse.Namespace) -> dict[str, Any]:
    v = args.verb
    if v in ("inventory", "drift"):
        return {"only": [s.strip() for s in args.only.split(",") if s.strip()]} if args.only else {}
    if v == "vitals":
        return {
            "thresholds": bool(args.thresholds),
            "alert": bool(args.alert),
            "reap": bool(args.reap),
        }
    if v == "reap":
        return {
            k: x
            for k, x in {
                "root": args.root, "apply": args.apply, "min_age_hours": args.min_age_hours,
                "batch": args.batch, "retention_days": args.retention_days,
            }.items()
            if x not in ("", None, False)
        }
    if v == "doctor":
        return {"check": args.check} if args.check else {}
    if v == "report":
        return {"since": args.since, "markdown": bool(args.md)}
    if v == "declare":
        if args.kind == "engine":
            return {
                "kind": "engine",
                "name": args.name,
                "unit": args.unit,
                "port": args.port,
                "weights": args.weights,
                "owner": args.owner,
                "engine_kind": args.kind,
                "note": args.note,
            }
        if args.kind == "tier":
            return {"kind": "tier", "mount": args.mount, "tier": args.tier, "floor_gb": args.floor}
        return {"kind": "tracked", "live": args.live, "source": args.source}
    if v == "clean":
        return {"apply": args.apply} if args.apply else {"plan": True}
    if v == "window":
        return {
            k: getattr(args, k)
            for k in ("action", "scope", "expect", "owner", "note", "id", "outcome")
            if getattr(args, k)
        }
    return {}


# -- rendering -----------------------------------------------------------------
# Columns are sized from their content (axiom.infra.cli_format.table), never fixed.


def _gb(n: int | float | None) -> str:
    return "?" if n is None else f"{n / 2**30:.0f} GB"


def _table(rows: list[list[object]], headers: list[str], wrap_last: bool = True) -> list[str]:
    from axiom.infra.cli_format import Column, table

    cols = [
        Column(header=h, wrap=wrap_last and i == len(headers) - 1) for i, h in enumerate(headers)
    ]
    return table(rows, cols, headers=True) if rows else []


def render_inventory(v: dict[str, Any]) -> str:
    lines: list[str] = []
    if "engines" in v:
        lines.append("engines")
        rows = []
        for e in v["engines"] or []:
            port = f":{e['port']}" if e.get("port") else ""
            models = ", ".join(
                e.get("served_as") or e.get("models") or ([e["model"]] if e.get("model") else [])
            )
            state = "running" if e.get("running") else "declared, not running"
            rows.append(
                [
                    e.get("name") or e.get("kind"),
                    e.get("kind", ""),
                    (e.get("host") or "") + port,
                    e.get("source", ""),
                    state,
                    e.get("owner") or "",
                    models,
                ]
            )
        lines += _table(
            rows, ["name", "kind", "listens", "source", "state", "owner", "models"]
        ) or ["  none detected or declared"]
    if v.get("models"):
        lines.append("models served")
        lines += _table(
            [[m["name"], m["engine"], m.get("port") or ""] for m in v["models"]],
            ["model", "engine", "port"],
            wrap_last=False,
        )
    if "gpu" in v:
        lines.append("gpu")
        lines += _table(
            [
                [g["index"], g["name"], f"{g['memory_used_mib']} of {g['memory_total_mib']} MiB"]
                for g in v["gpu"] or []
            ],
            ["gpu", "name", "memory used"],
        ) or ["  no GPU visible"]
        lines += _table(
            [[a["pid"], a["process"], f"{a['memory_mib']} MiB"] for a in v.get("gpu_apps") or []],
            ["pid", "process", "memory"],
        )
    if "disks" in v:
        lines.append("disks")
        lines += _table(
            [
                [
                    d["mount"],
                    d.get("tier") or "",
                    _gb(d["free_bytes"]),
                    _gb(d["size_bytes"]),
                    f"{d['used_pct']}%",
                ]
                for d in v["disks"] or []
            ],
            ["mount", "tier", "free", "size", "used"],
            wrap_last=False,
        )
    if "units" in v:
        active = [u for u in v["units"] or [] if u.get("active") == "active"]
        failed = [u for u in v["units"] or [] if u.get("active") == "failed"]
        lines.append(
            f"units: {len(active)} active, {len(failed)} failed"
            + (": " + ", ".join(u["unit"] for u in failed) if failed else "")
        )
    if "ports" in v:
        lines.append("listening")
        lines += _table(
            [
                [f"{sock['address']}:{sock['port']}", sock.get("process", "")]
                for sock in v["ports"] or []
            ],
            ["address", "process"],
        )
    for x in v.get("exposed_credentials") or []:
        lines.append(f"exposed: {x['engine']}'s credential is on its {x['where']}")
    return "\n".join(lines) or "nothing to show"


def render_vitals(v: dict[str, Any]) -> str:
    lines = [f"verdict: {v.get('verdict', '?')}"]
    lines += _table(
        [
            [
                d["mount"],
                d.get("tier") or "",
                _gb(d["free_bytes"]),
                f"{d['used_pct']}%",
                f"{d['floor_gb']} GB" if d.get("floor_gb") else "",
                d.get("state", ""),
            ]
            for d in v.get("disks", [])
        ],
        ["mount", "tier", "free", "used", "floor", "state"],
        wrap_last=False,
    )
    if v.get("memory"):
        m = v["memory"]
        lines.append(
            f"memory: {_gb(m.get('available_bytes'))} available of {_gb(m.get('total_bytes'))}"
        )
    if v.get("load") is not None:
        lines.append("load: " + " ".join(f"{x:.2f}" for x in v["load"]))
    for g in v.get("gpu", []):
        lines.append(
            f"gpu {g['index']}: {g['memory_free_mib']} MiB free of {g['memory_total_mib']}"
        )
    for f in v.get("findings", []):
        lines.append(f"[{f['severity']}] {f['message']}")
    if v.get("alert"):
        lines.append(f"alert: {v['alert']}")
    return "\n".join(lines)


def render_findings(v: dict[str, Any]) -> str:
    out = [v.get("summary", "")] if v.get("summary") else []
    for f in v.get("findings", []):
        if f.get("severity") in ("ok", "skipped"):
            continue
        out.append(
            f"[{f.get('severity', '?')}] {f.get('name') or f.get('code', '')}: {f.get('message', '')}"
        )
        if f.get("fix"):
            out.append(f"    fix: {f['fix']}")
    return "\n".join(out) or "nothing to report"


def render(verb: str, v: dict[str, Any]) -> str:
    if verb == "inventory":
        return render_inventory(v)
    if verb == "vitals":
        return render_vitals(v)
    if verb in ("doctor", "drift"):
        return render_findings(v)
    if verb == "report":
        return v.get("markdown") or json.dumps(v, indent=2, default=str)
    if verb == "declare":
        return v.get("what", "declared")
    if verb == "clean":
        head = [f"plan {v['plan_id']}"] if v.get("plan_id") else []
        rows = [
            [i["action"], i["what"], i.get("reclaims", ""), i.get("why", "")]
            for i in v.get("items", [])
        ]
        body = _table(rows, ["action", "what", "reclaims", "why"]) or [
            "nothing to reclaim that KEEL can see"
        ]
        return "\n".join(head + body + ([v["note"]] if v.get("note") else []))
    if verb == "reap":
        rows = [[w["path"], w.get("why", "")] for w in v.get("removed") or v.get("would_remove", [])]
        head = f"{v.get('mode', 'plan')}: {len(rows)} worktree(s)"
        skip = [[w["path"], w["reason"]] for w in v.get("skipped", [])]
        return "\n".join(
            [head, *_table(rows, ["worktree", "why"]), "skipped:", *_table(skip, ["worktree", "reason"])]
        )
    if verb == "window":
        ws = [v["window"]] if v.get("window") else v.get("windows") or v.get("blocking") or []
        head = ["clear: nothing open on that scope"] if v.get("clear") else []
        rows = [
            [
                w["window_id"],
                w["state"],
                ", ".join(w["scope"]),
                w["owner"],
                w["expected_end_at"],
                w.get("note", ""),
            ]
            for w in ws
        ]
        return "\n".join(
            head
            + (
                _table(rows, ["id", "state", "scope", "owner", "until", "note"])
                or ([] if head else ["no maintenance windows"])
            )
        )
    return json.dumps(v, indent=2, default=str)


def _registry(args: argparse.Namespace) -> Any:
    """KEEL's own skills, or the whole platform when this verb hands off to another agent.

    Binding everything takes seconds, so a plain reading does not pay for it. ``--alert`` and
    ``report`` do, because a hand-off goes through the registry: with only KEEL bound, the
    heartbeat's ``node vitals --alert`` found HERALD unreachable and told nobody.
    """
    if args.verb == "report" or getattr(args, "alert", False):
        from axiom.infra.skills_emit import bind_all_skills

        return bind_all_skills()
    from axiom.infra.skills import SkillRegistry

    from ..skills import bind

    registry = SkillRegistry()
    bind(registry)
    return registry


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.verb:
        build_parser().print_help()
        return 2
    from axiom.infra.paths import get_user_state_dir

    registry = _registry(args)
    ctx = SkillContext(
        registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.node")
    )
    result = invoke_capability(
        registry, skill_for(args.verb), _params(args), ctx, surface=CLI_SURFACE
    )
    if not result.ok:
        print("\n".join(result.errors), file=sys.stderr)
        return getattr(result, "exit_code", 1) or 1
    value = result.value if isinstance(result.value, dict) else {"value": result.value}
    print(json.dumps(value, indent=2, default=str) if args.json else render(args.verb, value))
    for step in getattr(result, "next", None) or []:
        if not args.json and isinstance(step, dict) and step.get("why"):
            print(f"next: {step.get('target', '')}: {step['why']}")
    return 0


__all__ = ["VERBS", "build_parser", "main", "render"]
