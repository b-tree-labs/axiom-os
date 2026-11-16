# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``node.vitals``: how the machine is doing right now, and the soundings on the heartbeat."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import collect, declarations
from ._handoff import HERALD_ALERT, PLINTH_STORAGE, handoff, next_step
from .inventory import _collectors, _state_dir

#: A mount with no declared floor is judged on percent free.
DEFAULT_WARN_PCT = 10.0
DEFAULT_CRIT_PCT = 5.0
GB = 2**30


def _meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    try:
        text = path.read_text()
    except OSError:
        return {}
    out: dict[str, int] = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        parts = v.split()
        if parts and parts[0].isdigit():
            out[k.strip()] = int(parts[0]) * 1024
    return out


def kubelet_floors(decl: declarations.Declarations) -> dict[str, dict[str, Any]]:
    """Per mount, the kubelet's hard eviction floor and minimum reclaim when the mount is a
    kubelet nodefs. Declared on the tier today (``kubelet_floor_gb``, ``kubelet_reclaim_gb``);
    reading the kubelet's configz directly is the next step."""
    out: dict[str, dict[str, Any]] = {}
    for t in decl.tiers:
        if t.kubelet_floor_gb:
            out[t.mount] = {
                "eviction_hard_bytes": int(t.kubelet_floor_gb * GB),
                "minimum_reclaim_bytes": int(t.kubelet_reclaim_gb * GB),
                "source": "declared",
            }
    return out


def judge_disks(
    disks: list[dict[str, Any]],
    tiers: dict[str, declarations.Tier],
    kubelet: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Each mount with its floor and state. A declared floor is absolute GB; otherwise percent free."""
    for d in disks:
        t = tiers.get(d["mount"])
        d["tier"] = t.tier if t else ""
        d["floor_gb"] = t.floor_gb if t else 0
        k = kubelet.get(d["mount"]) if kubelet else None
        d["kubelet"] = k
        d["effective_free_bytes"] = max(
            d["free_bytes"] - (k["eviction_hard_bytes"] + k["minimum_reclaim_bytes"] if k else 0), 0
        )
        free_gb = d["free_bytes"] / GB
        free_pct = 100.0 - d["used_pct"]
        if t and t.floor_gb:
            if free_gb < t.floor_gb * 0.5:
                d["state"] = "critical"
            elif free_gb < t.floor_gb:
                d["state"] = "pressure"
            else:
                d["state"] = "ok"
        else:
            d["state"] = (
                "critical"
                if free_pct < DEFAULT_CRIT_PCT
                else "pressure"
                if free_pct < DEFAULT_WARN_PCT
                else "ok"
            )
    return disks


def gather(
    ctx: SkillContext | None,
    *,
    meminfo: dict[str, int] | None = None,
    load: tuple[float, float, float] | None = None,
) -> dict[str, Any]:
    c = _collectors(ctx)
    decl = declarations.load(_state_dir(ctx))
    tiers = {t.mount: t for t in decl.tiers}
    disks = judge_disks(collect.read_disks(c), tiers, kubelet_floors(decl))
    mem = (
        meminfo
        if meminfo is not None
        else (_meminfo() or collect.parse_vm_stat(c.vm_stat(), c.memsize()))
    )
    memory = (
        {"total_bytes": mem.get("MemTotal", 0), "available_bytes": mem.get("MemAvailable", 0)}
        if mem.get("MemTotal")
        else {}
    )
    try:
        la = load if load is not None else os.getloadavg()
    except OSError:
        la = None
    gpus = collect.parse_nvidia_smi_gpus(c.gpus())
    findings: list[dict[str, str]] = []
    for d in disks:
        if d["state"] == "critical":
            findings.append(
                {
                    "severity": "critical",
                    "code": "disk-critical",
                    "message": f"{d['mount']} has {d['free_bytes'] / GB:.0f} GB free"
                    + (
                        f", under half its {d['floor_gb']} GB floor"
                        if d["floor_gb"]
                        else f" ({100 - d['used_pct']:.1f}%)"
                    ),
                }
            )
        elif d["state"] == "pressure":
            findings.append(
                {
                    "severity": "warning",
                    "code": "disk-pressure",
                    "message": f"{d['mount']} has {d['free_bytes'] / GB:.0f} GB free"
                    + (
                        f", under its {d['floor_gb']} GB floor"
                        if d["floor_gb"]
                        else f" ({100 - d['used_pct']:.1f}%)"
                    ),
                }
            )
    if (
        memory
        and memory["total_bytes"]
        and memory["available_bytes"] / memory["total_bytes"] < 0.05
    ):
        findings.append(
            {
                "severity": "warning",
                "code": "memory-pressure",
                "message": f"{memory['available_bytes'] / GB:.1f} GB of memory available",
            }
        )
    for g in gpus:
        if g["memory_total_mib"] and g["memory_free_mib"] / g["memory_total_mib"] < 0.03:
            findings.append(
                {
                    "severity": "info",
                    "code": "gpu-full",
                    "message": f"GPU {g['index']} has {g['memory_free_mib']} MiB free; a second model will not fit beside the first",
                }
            )
    # Blind findings go last: a measured problem leads, and outranks not having looked.
    blind = [name for name, reading in (("disk", disks), ("memory", memory)) if not reading]
    for sensor in blind:
        findings.append(
            {
                "severity": "blind",
                "code": f"{sensor}-blind",
                "message": f"KEEL could not read {sensor} on this machine, so it cannot say there is room",
            }
        )
    worst = (
        "critical"
        if any(f["severity"] == "critical" for f in findings)
        else "pressure"
        if any(f["severity"] == "warning" for f in findings)
        else "blind"
        if blind
        else "even keel"
    )
    return {
        "verdict": worst,
        "blind": blind,
        "disks": disks,
        "memory": memory,
        "load": list(la) if la else None,
        "gpu": gpus,
        "findings": findings,
        "thresholds": {
            "declared": [
                {"mount": t.mount, "tier": t.tier, "floor_gb": t.floor_gb} for t in decl.tiers
            ],
            "default_warn_pct": DEFAULT_WARN_PCT,
            "default_crit_pct": DEFAULT_CRIT_PCT,
        },
    }


def _publish(ctx: SkillContext | None, value: dict[str, Any]) -> None:
    """Leave the verdict where the chain ledger reads it (chain_ledger.VITALS_FILE):
    a critical node refuses new writes in any chain until there is room."""
    import json
    import time

    from axiom.infra.chain_ledger import VITALS_FILE
    from axiom.infra.state_integrity import atomic_write_text

    p = _state_dir(ctx) / VITALS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "verdict": value["verdict"],
        "at": time.time(),
        "blind": value["blind"],
        "critical_mounts": [d["mount"] for d in value["disks"] if d.get("state") == "critical"],
        "pressure_mounts": [d["mount"] for d in value["disks"] if d.get("state") == "pressure"],
    }
    atomic_write_text(p, json.dumps(doc, sort_keys=True))


def _reap_on_heartbeat(ctx: SkillContext | None, value: dict[str, Any], actions: list[str]) -> None:
    """Upkeep rides the heartbeat only on an even keel: never while pressured, critical or blind
    (no disk was read), because removing worktrees then would be acting on a reading we do not trust."""
    if value["verdict"] != "even keel" or not value["disks"]:
        value["reap"] = f"skipped: verdict is {value['verdict'] if value['disks'] else 'blind'}"
        return
    from .reap_worktrees import run as reap_run

    try:
        res = reap_run({"apply": True, "heartbeat": True}, ctx)
    except Exception as exc:  # noqa: BLE001 - upkeep must never break the soundings
        value["reap"] = f"failed: {exc}"
        return
    value["reap"] = res.value
    actions.extend(res.actions_taken)


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    value = gather(ctx)
    try:
        _publish(ctx, value)
    except OSError:
        pass
    if not params.get("thresholds"):
        value.pop("thresholds", None)
    actions = ["read free space, memory, load and GPU"]
    steps = []
    if value["verdict"] == "blind":
        steps.append(
            next_step("node.doctor", "find out why a reading came back empty on this machine")
        )
    elif value["verdict"] != "even keel":
        steps.append(
            next_step(
                "node.clean",
                "see what could be reclaimed before anything is evicted (a mount is under pressure)",
            )
        )
        steps.append(
            next_step(
                PLINTH_STORAGE,
                "the database's own storage view: which tables and volumes grew (the pressured mount holds the database)",
            )
        )
    if params.get("alert") and value["findings"]:
        worst = value["findings"][0]
        rec = handoff(
            ctx,
            HERALD_ALERT,
            {
                "recipient": params.get("recipient") or "@operator:local",
                "summary": f"node {value['verdict']}: "
                + "; ".join(f["message"] for f in value["findings"][:3]),
                "priority": "high" if value["verdict"] == "critical" else "normal",
                "actor": "@keel:local",
                "dedup_key": f"node.vitals:{worst['code']}:{value['disks'][0]['mount'] if value['disks'] else ''}",
            },
        )
        value["alert"] = f"{rec['target']}: {rec['status']}" + (
            f" ({rec['detail']})" if rec.get("detail") else ""
        )
        actions.append(
            "handed the breach to HERALD"
            if rec["status"] == "ok"
            else f"could not hand the breach to HERALD: {rec['status']}"
        )
    if params.get("reap"):
        _reap_on_heartbeat(ctx, value, actions)
    return SkillResult(ok=True, value=value, actions_taken=actions, next=steps)


__all__ = ["DEFAULT_CRIT_PCT", "DEFAULT_WARN_PCT", "gather", "judge_disks", "run"]
