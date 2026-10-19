# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.probe_nodes``: check every node the platform can reach, from outside.

Run on the platform node, every minute (the orchestrator registers it). Each
node whose heartbeat advertises a ``probe_url`` gets a GET of its ``/readyz``;
the result is kept for the uptime report, where a node that keeps beating
while the check fails reads as a network outage rather than as ours
(ADR-182 D5a). Push-only nodes advertise no address and are not checked.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore
    from axiom.extensions.builtins.data_platform.uptime_probe import ProbeStore, probe_nodes
    from axiom.infra import site_scope

    store = HeartbeatStore.from_env()
    names = (
        sorted(p.name for p in store.root.iterdir() if p.is_dir()) if store.root.is_dir() else []
    )
    wanted = [s for s in str(params.get("sites") or "").split(",") if s]
    names = [n for n in site_scope.resolve().filter(names) if not wanted or n in wanted]
    checked = probe_nodes(
        store, ProbeStore.from_env(), sites=names, timeout_s=float(params.get("timeout_s") or 5.0)
    )
    lines = []
    for site, nodes in checked.items():
        if not nodes:
            lines.append(f"{site}: no reachable node (push-only nodes are not checked)")
        for node, ok in sorted(nodes.items()):
            lines.append(f"{site}/{node}: {'up' if ok else 'NOT reachable'}")
    return SkillResult(
        ok=True,
        value={"checked": checked},
        actions_taken=lines or ["no sites in scope"],
    )
