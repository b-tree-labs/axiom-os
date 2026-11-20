# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``hygiene.stat disk`` and ``hygiene.disk_watch``: the disk-pressure ladder.

Two capabilities, one reading:

- ``hygiene.stat`` with ``resource=disk`` is the read. It measures every
  volume the node depends on against the kubelet's eviction line and returns
  the ladder rung; it writes nothing, so it is safe on MCP and in chat ("is
  the disk OK?").
- ``hygiene.disk_watch`` is the timer's verb (``axi hygiene stat disk
  --notify``). It takes the same reading, tells a person through the
  notifications inbox when the rung changes or a page stands, and saves the
  reading for the one-line ``axi status`` row. It writes, so it stays CLI-only.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import disk_ladder as dl
from .. import disk_probes as probes_mod


def _render(report: dict[str, Any]) -> list[str]:
    from axiom.infra.cli_format import Column, table

    lines = [f"disk: {report['summary']}"]
    k = report.get("kubelet", {})
    if k.get("source") == "configz":
        lines.append(f"  kubelet evictionHard {k.get('evictionHard')}, "
                     f"evictionMinimumReclaim {k.get('evictionMinimumReclaim') or '{}'}")
    rows = []
    for v in report.get("volumes", []):
        if v.get("margin_gb") is None:
            rows.append([v["rung"], v["name"], "-", "-", "-", "-"])
        else:
            rows.append([v["rung"], v["name"], f"{v['free_gb']:.0f} GB",
                         f"{v['floor_gb']:.0f} GB", f"{v['margin_gb']:.0f} GB",
                         f"{v['recovery_line_gb']:.0f} GB"])
    if rows:
        cols = [Column("rung"), Column("volume"), Column("free", align="right"),
                Column("floor", align="right"), Column("margin", align="right"),
                Column("clears at", align="right")]
        lines += table(rows, cols, headers=True)
    for v in report.get("volumes", []):
        lines.append(f"  {v['name']}: {v['path']}")
        for r in v.get("reasons", []):
            lines.append(f"    - {r}")
    for i in report.get("images", []):
        tail = f": {i['remedy']}" if i.get("remedy") else ""
        lines.append(f"  image {i['image']} {i['rung']}{tail}")
    for f in report.get("findings", []):
        lines.append(f"  note: {f}")
    return lines


def _config(params: dict[str, Any]) -> dl.LadderConfig:
    return probes_mod.resolve_config(params.get("config") or None)


def report(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """The disk-pressure ladder for this node: margin to eviction per volume, and never-pull images."""
    try:
        cfg = _config(params)
    except (OSError, ValueError) as exc:
        return SkillResult(ok=False, errors=[f"disk ladder configuration: {exc}"])
    rep = dl.assess(cfg, probes_mod.NodeProbes(), now=_now())
    rep["config"] = cfg.source
    return SkillResult(ok=True, value=rep, actions_taken=_render(rep))


def watch(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Take the ladder reading, notify a person on a change of rung, and save it for status."""
    try:
        cfg = _config(params)
    except (OSError, ValueError) as exc:
        return SkillResult(ok=False, errors=[f"disk ladder configuration: {exc}"])
    recipient = cfg.recipient or probes_mod.default_recipient()
    cfg.recipient = recipient
    rep = dl.watch(
        cfg,
        probes=probes_mod.NodeProbes(),
        notify=probes_mod.inbox_notifier(recipient),
        state_path=probes_mod.state_path(),
        now=_now(),
    )
    rep["config"] = cfg.source
    lines = _render(rep)
    if rep["notified"]:
        lines.append(f"  notified {recipient} ({dl.PRIORITY.get(rep['rung'], 'normal')})")
    # A notification that could not be written is an error: the timer's exit
    # status is how the node's own watchdog hears about a monitor nobody hears.
    return SkillResult(ok=not rep["notify_errors"], value=rep, actions_taken=lines,
                       errors=[f"could not notify: {e}" for e in rep["notify_errors"]])


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


__all__ = ["report", "watch"]
