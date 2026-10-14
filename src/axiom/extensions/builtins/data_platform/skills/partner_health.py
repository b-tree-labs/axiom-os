# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.partner_health``: every reporting site's health, and alerts for what needs a person.

Run on the platform node, on a schedule. Reads the heartbeat store, so it sees
only what the sites' nodes reported. With ``alert_to`` set, each condition
that needs a person (data stopped, a node out of date, an archive disk above
80%) goes to that principal's inbox once per condition per day; Herald
delivers from the inbox on the recipient's channels.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from axiom.extensions.builtins.data_platform import partner_health as ph
    from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore
    from axiom.infra import site_scope

    store = HeartbeatStore.from_env()
    names = (
        sorted(p.name for p in store.root.iterdir() if p.is_dir()) if store.root.is_dir() else []
    )
    wanted = [s for s in str(params.get("sites") or "").split(",") if s]
    names = [n for n in site_scope.resolve().filter(names) if not wanted or n in wanted]
    kw: dict[str, Any] = {"min_version": str(params.get("min_version") or "")}
    if params.get("stopped_after_h"):
        kw["stopped_after_s"] = float(params["stopped_after_h"]) * 3600
    sites = [ph.site_health(n, store=store, **kw) for n in names]
    found = [a for h in sites for a in ph.alerts(h)]
    sent, errors = 0, []
    recipient = str(params.get("alert_to") or "").strip()
    if recipient and found:
        try:
            sent = ph.deliver(found, send=ph.inbox_sender(recipient))
        except Exception as exc:  # noqa: BLE001 - a monitor nobody hears must say so
            errors.append(
                f"could not write the alerts to {recipient}'s inbox: {type(exc).__name__}: {exc}"
            )
    lines = []
    for h in sites:
        lines.append(f"{h['site']}: {h['state']} - {h['summary']}")
    for a in found:
        lines.append(f"  alert ({a['kind']}): {a['summary']}")
    if not sites:
        lines.append("no site has reported yet")
    return SkillResult(
        ok=not errors,
        value={"sites": sites, "alerts": found, "sent": sent, "text": "\n".join(lines)},
        errors=errors,
    )
