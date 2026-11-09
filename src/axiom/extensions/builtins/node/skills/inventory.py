# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``node.inventory``: what runs on this machine."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import collect, declarations
from ._handoff import KEEP_EXPOSED, next_step

SECTIONS = ("engines", "models", "units", "ports", "gpu", "disks")


def _state_dir(ctx: SkillContext | None) -> Path:
    if ctx is not None and getattr(ctx, "state_dir", None):
        return Path(ctx.state_dir)
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir()


def _collectors(ctx: SkillContext | None) -> collect.Collectors:
    return collect.active()


def gather(ctx: SkillContext | None, only: list[str] | None = None) -> dict[str, Any]:
    """The inventory as data. Pure given the collectors and the declarations."""
    want = set(only or SECTIONS)
    c = _collectors(ctx)
    decl = declarations.load(_state_dir(ctx))
    out: dict[str, Any] = {}
    exposed: list[dict[str, Any]] = []

    engines: list[dict[str, Any]] = []
    if want & {"engines", "models"}:
        detected = collect.detect_engines(collect.parse_ps(c.ps()))
        by_port = {e.get("port"): e for e in detected if e.get("port")}
        for d in decl.engines:
            hit = (
                by_port.get(d.port)
                if d.port
                else next((e for e in detected if e.get("kind") == d.kind and d.kind), None)
            )
            row = {
                "name": d.name,
                "kind": d.kind or (hit or {}).get("kind", ""),
                "unit": d.unit,
                "port": d.port,
                "weights": d.weights,
                "owner": d.owner,
                "source": "declared",
                "running": hit is not None,
            }
            if hit:
                row.update(
                    {
                        k: hit[k]
                        for k in (
                            "pid",
                            "user",
                            "uptime_s",
                            "host",
                            "model",
                            "served_as",
                            "tls",
                            "key_required",
                        )
                        if k in hit
                    }
                )
                detected.remove(hit)
            engines.append(row)
        for e in detected:
            row = {"name": e["kind"], **e, "running": True, "declared": False}
            engines.append(row)
        for e in engines:
            if e.get("key_required") and e.get("running"):
                exposed.append(
                    {"engine": e["name"], "where": "command line (--api-key)", "next": KEEP_EXPOSED}
                )
                e["credential"] = "on the command line: visible in any process listing"
        if "engines" in want:
            out["engines"] = engines

    if "models" in want:
        models: list[dict[str, Any]] = []
        for e in engines:
            if not e.get("running") or not e.get("port"):
                continue
            host = e.get("host") or "127.0.0.1"
            host = "127.0.0.1" if host in ("0.0.0.0", "::", "[::]") else host
            probe = c.models(int(e["port"]), host, bool(e.get("tls")))
            e["models"] = probe.get("models", [])
            e["probe"] = probe.get("status")
            for m in probe.get("models", []):
                models.append({"name": m, "engine": e["name"], "port": e["port"]})
            if probe.get("status") == "key required":
                for m in e.get("served_as") or []:
                    models.append(
                        {
                            "name": m,
                            "engine": e["name"],
                            "port": e["port"],
                            "from": "command line (the endpoint needs a key)",
                        }
                    )
        out["models"] = models

    if "units" in want:
        units = collect.parse_systemctl_units(c.user_units())
        for u in units:
            u["scope"] = "user"
        sys_units = collect.parse_systemctl_units(c.system_units())
        declared_units = {d.unit for d in decl.engines if d.unit}
        for u in sys_units:
            if u["unit"] in declared_units or u["unit"].startswith(
                ("ollama", "vllm", "docker", "k3s", "nginx", "postgres")
            ):
                u["scope"] = "system"
                units.append(u)
        out["units"] = units

    if "ports" in want:
        out["ports"] = collect.parse_ss_listeners(c.listeners())

    if "gpu" in want:
        out["gpu"] = collect.parse_nvidia_smi_gpus(c.gpus())
        out["gpu_apps"] = collect.parse_nvidia_smi_apps(c.gpu_apps()) if out["gpu"] else []

    if "disks" in want:
        tiers = {t.mount: t for t in decl.tiers}
        disks = collect.parse_df(c.df())
        for d in disks:
            t = tiers.get(d["mount"])
            d["tier"] = t.tier if t else ""
            d["floor_gb"] = t.floor_gb if t else 0
        out["disks"] = disks

    out["declared"] = decl.as_dict()
    out["exposed_credentials"] = exposed
    return out


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    only = params.get("only")
    only = [s for s in only if s in SECTIONS] if isinstance(only, list) else None
    value = gather(ctx, only)
    steps = [next_step("node.drift", "see whether what runs matches what is pinned and tracked")]
    undeclared = [
        e["name"]
        for e in value.get("engines", [])
        if not e.get("declared", True) and e.get("source") != "declared"
    ]
    if undeclared:
        steps.append(
            next_step(
                "node.declare",
                f"declare {', '.join(undeclared)} so the inventory names the owner and weights (an engine is detected but not declared)",
            )
        )
    for x in value.get("exposed_credentials", []):
        steps.append(
            next_step(
                KEEP_EXPOSED,
                f"{x['engine']}'s credential is on its command line; KEEP records the exposure and rotates it (a credential is exposed)",
            )
        )
    return SkillResult(
        ok=True,
        value=value,
        actions_taken=["read the machine's processes, units, ports, GPU and disks"],
        next=steps,
    )


__all__ = ["SECTIONS", "gather", "run"]
